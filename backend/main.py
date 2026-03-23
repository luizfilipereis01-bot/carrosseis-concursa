import os
import re

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth import router as auth_router
from services.canva_service import CanvaService
from services.claude_service import generate_carousel

load_dotenv()

app = FastAPI(title="Gerador de Carrosséis — Concursa AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)

canva = CanvaService()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class GenerateRequest(BaseModel):
    command: str
    canva_link: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extract_design_id(link: str) -> str:
    """Extract the design ID from a Canva edit URL.

    Accepts formats such as:
      https://www.canva.com/design/DAF.../edit
      https://www.canva.com/design/DAF.../view
      https://www.canva.com/brand/template/DAF...
    """
    m = re.search(r"/design/([^/?#\s]+)", link)
    if not m:
        m = re.search(r"/template/([^/?#\s]+)", link)
    if not m:
        raise HTTPException(
            status_code=400,
            detail=(
                "Link do Canva inválido. Use o link de edição do design "
                "(ex: https://www.canva.com/design/DAF.../edit)."
            ),
        )
    return m.group(1)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/generate")
async def generate(req: GenerateRequest):
    if not req.command.strip():
        raise HTTPException(status_code=400, detail="O comando não pode estar vazio.")
    if not req.canva_link.strip():
        raise HTTPException(status_code=400, detail="O link do Canva não pode estar vazio.")

    design_id = extract_design_id(req.canva_link)

    # 1. Generate carousel content with Claude (Criador de Carrosséis)
    try:
        carousel = await generate_carousel(req.command)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao gerar conteúdo: {exc}")

    # 2. Apply content to Canva design
    edit_url: str | None = None
    canva_error: str | None = None
    try:
        edit_url = await canva.apply_carousel(design_id, carousel)
    except Exception as exc:
        canva_error = str(exc)

    return {
        "success": True,
        "edit_url": edit_url or req.canva_link,
        "canva_applied": edit_url is not None,
        "canva_error": canva_error,
        "slides_count": len(carousel["slides"]),
        "theme": carousel.get("theme"),
        "platform": carousel.get("platform"),
        "post_time": carousel.get("post_time"),
        "hashtags": carousel.get("hashtags", []),
        "caption": carousel.get("caption"),
        "slides": carousel.get("slides", []),
        "raw": carousel.get("raw"),
    }


# ---------------------------------------------------------------------------
# Static frontend (served last so API routes take priority)
# ---------------------------------------------------------------------------

_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="static")
