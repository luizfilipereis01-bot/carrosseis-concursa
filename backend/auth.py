"""
Canva OAuth 2.0 + PKCE authentication flow.

Routes:
  GET /auth/login    → redirects to Canva's authorization page
  GET /auth/callback → exchanges the code for tokens, saves to .env
  GET /auth/status   → returns whether the app has a valid token
  GET /auth/logout   → clears stored tokens
"""

import base64
import hashlib
import os
import secrets
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CANVA_AUTH_URL = "https://www.canva.com/api/oauth/authorize"
CANVA_TOKEN_URL = "https://api.canva.com/rest/v1/oauth/token"
SCOPES = "design:content:read design:content:write"

ENV_FILE = Path(__file__).parent.parent / ".env"

router = APIRouter(prefix="/auth", tags=["auth"])

# In-memory store for pending PKCE verifiers  {state: (code_verifier, created_at)}
_pending: dict[str, tuple[str, float]] = {}
_PENDING_TTL = 300  # 5 minutes


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def _generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    verifier = secrets.token_urlsafe(64)  # ~86 chars, well within 43-128
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _clean_pending() -> None:
    now = time.time()
    stale = [s for s, (_, ts) in _pending.items() if now - ts > _PENDING_TTL]
    for s in stale:
        del _pending[s]


# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------


def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _write_env(updates: dict[str, str]) -> None:
    env = _read_env()
    env.update(updates)

    lines: list[str] = []
    if ENV_FILE.exists():
        # Preserve comments and ordering of existing file
        existing_keys: set[str] = set()
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                lines.append(line)
                continue
            if "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                existing_keys.add(k)
                if k in updates:
                    lines.append(f"{k}={updates[k]}")
                else:
                    lines.append(line)
        # Append new keys not already in file
        for k, v in updates.items():
            if k not in existing_keys:
                lines.append(f"{k}={v}")
    else:
        for k, v in env.items():
            lines.append(f"{k}={v}")

    try:
        ENV_FILE.write_text("\n".join(lines) + "\n")
    except (PermissionError, OSError):
        pass  # Read-only filesystem (Vercel) — tokens live in os.environ only


def _get_redirect_uri() -> str:
    host = os.getenv("APP_HOST", "http://localhost:8000")
    return f"{host}/auth/callback"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/login")
def login():
    """Redirect the user to Canva's OAuth authorization page."""
    client_id = os.getenv("CANVA_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(
            status_code=500,
            detail="CANVA_CLIENT_ID não configurado no .env.",
        )

    _clean_pending()
    state = secrets.token_urlsafe(16)
    verifier, challenge = _generate_pkce()
    _pending[state] = (verifier, time.time())

    params = (
        f"?response_type=code"
        f"&client_id={client_id}"
        f"&scope={SCOPES.replace(' ', '%20')}"
        f"&redirect_uri={_get_redirect_uri()}"
        f"&state={state}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
    )
    return RedirectResponse(CANVA_AUTH_URL + params)


@router.get("/callback")
async def callback(code: str = "", state: str = "", error: str = ""):
    """Handle Canva's OAuth redirect, exchange code for tokens."""
    if error:
        return HTMLResponse(_result_page(success=False, message=f"Canva recusou: {error}"))

    if state not in _pending:
        return HTMLResponse(
            _result_page(success=False, message="Estado inválido ou expirado. Tente novamente.")
        )

    verifier, _ = _pending.pop(state)
    client_id = os.getenv("CANVA_CLIENT_ID", "")
    client_secret = os.getenv("CANVA_CLIENT_SECRET", "")

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            CANVA_TOKEN_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": _get_redirect_uri(),
            },
        )

    if r.status_code != 200:
        msg = r.json().get("error_description") or r.text
        return HTMLResponse(_result_page(success=False, message=f"Erro ao trocar token: {msg}"))

    tokens = r.json()

    # Persist to os.environ immediately (works everywhere)
    os.environ["CANVA_ACCESS_TOKEN"] = tokens["access_token"]
    os.environ["CANVA_TOKEN_EXPIRES_AT"] = str(int(time.time()) + tokens.get("expires_in", 3600))
    if tokens.get("refresh_token"):
        os.environ["CANVA_REFRESH_TOKEN"] = tokens["refresh_token"]

    # Try to persist to .env file (works locally; silently skipped on Vercel)
    _write_env(
        {
            "CANVA_ACCESS_TOKEN": tokens["access_token"],
            "CANVA_REFRESH_TOKEN": tokens.get("refresh_token", ""),
            "CANVA_TOKEN_EXPIRES_AT": os.environ["CANVA_TOKEN_EXPIRES_AT"],
        }
    )

    # On Vercel (read-only FS), show the token so the user can save it to env vars
    on_vercel = bool(os.environ.get("VERCEL"))
    return HTMLResponse(
        _result_page(
            success=True,
            vercel_token=tokens["access_token"] if on_vercel else None,
        )
    )


@router.get("/refresh")
async def refresh_token():
    """Use the stored refresh token to obtain a new access token."""
    refresh = os.getenv("CANVA_REFRESH_TOKEN", "")
    if not refresh:
        raise HTTPException(status_code=400, detail="Nenhum refresh token armazenado. Faça login novamente.")

    client_id = os.getenv("CANVA_CLIENT_ID", "")
    client_secret = os.getenv("CANVA_CLIENT_SECRET", "")
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            CANVA_TOKEN_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            },
        )

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Falha ao renovar token: {r.text}")

    tokens = r.json()
    _write_env(
        {
            "CANVA_ACCESS_TOKEN": tokens["access_token"],
            "CANVA_TOKEN_EXPIRES_AT": str(int(time.time()) + tokens.get("expires_in", 3600)),
        }
    )
    os.environ["CANVA_ACCESS_TOKEN"] = tokens["access_token"]

    return {"refreshed": True}


@router.get("/status")
def status():
    token = os.getenv("CANVA_ACCESS_TOKEN", "")
    expires_at = int(os.getenv("CANVA_TOKEN_EXPIRES_AT", "0"))
    has_token = bool(token)
    expired = expires_at > 0 and time.time() > expires_at
    has_refresh = bool(os.getenv("CANVA_REFRESH_TOKEN", ""))

    return {
        "connected": has_token and not expired,
        "expired": expired,
        "has_refresh_token": has_refresh,
        "expires_at": expires_at or None,
    }


@router.get("/logout")
def logout():
    _write_env(
        {
            "CANVA_ACCESS_TOKEN": "",
            "CANVA_REFRESH_TOKEN": "",
            "CANVA_TOKEN_EXPIRES_AT": "",
        }
    )
    os.environ.pop("CANVA_ACCESS_TOKEN", None)
    os.environ.pop("CANVA_REFRESH_TOKEN", None)
    return {"logged_out": True}


# ---------------------------------------------------------------------------
# Result page (shown after OAuth callback)
# ---------------------------------------------------------------------------


def _result_page(
    success: bool, message: str = "", vercel_token: str | None = None
) -> str:
    if success:
        icon, title, color = "✅", "Canva conectado!", "#10b981"
        if vercel_token:
            body = (
                "Token gerado com sucesso! Como você está no Vercel, "
                "o token não pode ser salvo automaticamente. "
                "Copie o valor abaixo e adicione como variável de ambiente "
                "<strong>CANVA_ACCESS_TOKEN</strong> no painel do Vercel."
            )
        else:
            body = "Token salvo com sucesso. Pode fechar esta aba e voltar ao sistema."
    else:
        icon, title, body, color = (
            "❌",
            "Falha na autenticação",
            message or "Algo deu errado. Tente novamente.",
            "#ef4444",
        )
        vercel_token = None

    token_block = ""
    if vercel_token:
        token_block = f"""
  <div style="margin-top:20px;text-align:left">
    <p style="font-size:0.75rem;color:#6b7280;margin-bottom:6px">CANVA_ACCESS_TOKEN</p>
    <div style="position:relative">
      <code id="tok" style="display:block;background:#0f172a;border:1px solid #1e3a5f;
        border-radius:8px;padding:10px 12px;font-size:0.7rem;color:#67e8f9;
        word-break:break-all;line-height:1.5">{vercel_token}</code>
      <button onclick="navigator.clipboard.writeText(document.getElementById('tok').textContent);this.textContent='✓ Copiado!'"
        style="position:absolute;top:8px;right:8px;background:#1e3a5f;border:none;
          color:#67e8f9;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:0.7rem">
        Copiar
      </button>
    </div>
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8"/>
  <title>{title}</title>
  <style>
    body {{
      font-family: Inter, sans-serif;
      background: #030712;
      color: #f9fafb;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
    }}
    .card {{
      background: #111827;
      border: 1px solid #1f2937;
      border-radius: 16px;
      padding: 40px 48px;
      text-align: center;
      max-width: 480px;
      width: 90%;
    }}
    .icon {{ font-size: 48px; margin-bottom: 16px; }}
    h1 {{ font-size: 1.4rem; margin: 0 0 8px; color: {color}; }}
    p {{ color: #9ca3af; font-size: 0.9rem; line-height: 1.6; }}
    a {{
      display: inline-block;
      margin-top: 24px;
      padding: 10px 24px;
      background: linear-gradient(135deg,#6366f1,#a855f7);
      color: white;
      border-radius: 8px;
      text-decoration: none;
      font-weight: 600;
      font-size: 0.9rem;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>{title}</h1>
    <p>{body}</p>
    {token_block}
    <a href="/">Voltar ao sistema</a>
  </div>
</body>
</html>"""
