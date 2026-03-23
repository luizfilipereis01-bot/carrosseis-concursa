"""
Canva API integration.

Supports two editing strategies (tried in order):
  1. Editing Session API  — edits the original design in-place.
  2. Autofill API         — creates a new design from a brand template
                            (requires Canva Enterprise + template with named fields).

The field naming convention expected in brand templates:
  slide_1_headline, slide_1_body,
  slide_2_headline, slide_2_body, ...
"""

import asyncio
import os

import httpx

CANVA_API_BASE = "https://api.canva.com/rest/v1"
POLL_INTERVAL = 1.5   # seconds between autofill job status checks
POLL_TIMEOUT = 45     # seconds before giving up


class CanvaService:
    def __init__(self) -> None:
        self.access_token = os.getenv("CANVA_ACCESS_TOKEN", "")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def apply_carousel(self, design_id: str, carousel: dict) -> str | None:
        """
        Apply generated carousel content to a Canva design.

        Returns the edit URL of the updated (or newly created) design,
        or None if all strategies fail (the caller should still return
        the original link along with the raw text content).
        """
        # Strategy 1 — editing session (works on any design the user owns)
        try:
            url = await self._apply_via_editing_session(design_id, carousel)
            if url:
                return url
        except Exception as exc:
            print(f"[Canva] Editing session failed: {exc}")

        # Strategy 2 — autofill (requires Enterprise + brand template fields)
        try:
            url = await self._apply_via_autofill(design_id, carousel)
            if url:
                return url
        except Exception as exc:
            print(f"[Canva] Autofill failed: {exc}")

        return None

    # ------------------------------------------------------------------
    # Strategy 1 — Editing Session
    # ------------------------------------------------------------------

    async def _apply_via_editing_session(
        self, design_id: str, carousel: dict
    ) -> str | None:
        pages = await self._get_pages(design_id)
        if not pages:
            raise ValueError("No pages found in design.")

        session_id = await self._start_session(design_id)
        try:
            operations = self._build_operations(pages, carousel["slides"])
            if operations:
                await self._perform_operations(design_id, session_id, operations)
            result = await self._commit_session(design_id, session_id)
            return result.get("urls", {}).get("edit_url")
        except Exception:
            await self._cancel_session(design_id, session_id)
            raise

    async def _get_pages(self, design_id: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{CANVA_API_BASE}/designs/{design_id}/pages",
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json().get("items", [])

    async def _start_session(self, design_id: str) -> str:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{CANVA_API_BASE}/designs/{design_id}/editing-sessions",
                headers=self._headers(),
                json={},
            )
            r.raise_for_status()
            return r.json()["id"]

    async def _perform_operations(
        self, design_id: str, session_id: str, operations: list[dict]
    ) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{CANVA_API_BASE}/designs/{design_id}/editing-sessions/{session_id}/operations",
                headers=self._headers(),
                json={"operations": operations},
            )
            r.raise_for_status()

    async def _commit_session(self, design_id: str, session_id: str) -> dict:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{CANVA_API_BASE}/designs/{design_id}/editing-sessions/{session_id}/commit",
                headers=self._headers(),
                json={},
            )
            r.raise_for_status()
            return r.json()

    async def _cancel_session(self, design_id: str, session_id: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.delete(
                    f"{CANVA_API_BASE}/designs/{design_id}/editing-sessions/{session_id}",
                    headers=self._headers(),
                )
        except Exception:
            pass  # best-effort cleanup

    @staticmethod
    def _build_operations(pages: list[dict], slides: list[dict]) -> list[dict]:
        """
        Map carousel slides to page text elements.
        Heuristic: on each page, sort text elements top-to-bottom;
        the first is the headline, the second is the body.
        """
        operations: list[dict] = []
        for i, slide in enumerate(slides):
            if i >= len(pages):
                break
            page = pages[i]
            text_els = sorted(
                [e for e in page.get("elements", []) if e.get("type") == "text"],
                key=lambda e: e.get("position", {}).get("y", 0),
            )
            if not text_els:
                continue
            operations.append(
                {
                    "type": "update_text",
                    "element_id": text_els[0]["id"],
                    "text": slide["headline"],
                }
            )
            if len(text_els) > 1 and slide.get("body"):
                operations.append(
                    {
                        "type": "update_text",
                        "element_id": text_els[1]["id"],
                        "text": slide["body"],
                    }
                )
        return operations

    # ------------------------------------------------------------------
    # Strategy 2 — Autofill (Canva Enterprise / brand templates)
    # ------------------------------------------------------------------

    async def _apply_via_autofill(
        self, design_id: str, carousel: dict
    ) -> str | None:
        data: dict = {}
        for slide in carousel["slides"]:
            n = slide["number"]
            data[f"slide_{n}_headline"] = {
                "type": "text",
                "text": slide["headline"],
            }
            if slide.get("body"):
                data[f"slide_{n}_body"] = {
                    "type": "text",
                    "text": slide["body"],
                }

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{CANVA_API_BASE}/autofills",
                headers=self._headers(),
                json={
                    "brand_template_id": design_id,
                    "title": f"Carrossel — {carousel.get('theme', '')}",
                    "data": data,
                },
            )
            r.raise_for_status()
            job_id = r.json()["job"]["id"]

        return await self._poll_autofill(job_id)

    async def _poll_autofill(self, job_id: str) -> str | None:
        deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT
        async with httpx.AsyncClient(timeout=15) as client:
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(POLL_INTERVAL)
                r = await client.get(
                    f"{CANVA_API_BASE}/autofills/{job_id}",
                    headers=self._headers(),
                )
                r.raise_for_status()
                job = r.json()["job"]
                status = job["status"]
                if status == "success":
                    return (
                        job["result"]["design"]["urls"]["edit_url"]
                    )
                if status == "failed":
                    raise RuntimeError(
                        f"Autofill job failed: {job.get('error', {})}"
                    )
        raise TimeoutError("Autofill job timed out.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
