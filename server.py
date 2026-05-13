# =============================================================
# server.py — Serveur MCP Screenshot → GCP → Dust
# =============================================================

import uuid
import json
from datetime import datetime

import uvicorn
from mcp.server.fastmcp import FastMCP
# ^ Package officiel MCP (mcp[cli]) — PAS fastmcp standalone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from playwright.async_api import async_playwright
from google.cloud import storage
from google.oauth2 import service_account

from config import PORT, MCP_BEARER_TOKEN, GCP_BUCKET_NAME, GCP_CREDENTIALS_JSON
# ^ Toutes les variables viennent de config.py


# =============================================================
# HELPER — Client Google Cloud Storage
# =============================================================

def get_gcs_client() -> storage.Client:
    """Retourne un client GCS authentifié depuis le JSON de service account."""
    if GCP_CREDENTIALS_JSON:
        info  = json.loads(GCP_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(info)
        return storage.Client(credentials=creds, project=info.get("project_id"))
    return storage.Client()


# =============================================================
# SERVEUR MCP
# =============================================================

mcp = FastMCP(
    name="screenshot-server",
    host="0.0.0.0",
    # ^ Écoute sur toutes les interfaces réseau (requis sur Railway)
    port=PORT,
    # ^ Port injecté par Railway via $PORT
    instructions=(
        "Serveur MCP Screenshot. "
        "Outil disponible : screenshot_url. "
        "Prend une URL publique, capture un screenshot pleine-page via Playwright, "
        "uploade l'image PNG sur GCP Cloud Storage et retourne l'URL publique du PNG."
    )
)


# =============================================================
# OUTIL MCP — screenshot_url
# =============================================================

@mcp.tool()
async def screenshot_url(url: str) -> str:
    """
    Capture un screenshot pleine-page d'une URL publique,
    l'uploade dans un bucket GCP Cloud Storage public,
    et retourne son URL publique accessible par n'importe quel agent.

    Args:
        url: URL de la page web à capturer (ex: "https://www.dust.tt").

    Returns:
        URL publique du screenshot PNG hébergé sur GCS.
        Format : "https://storage.googleapis.com/{bucket}/{path}.png"
    """

    # ── 1. Nom de fichier unique ──────────────────────────────
    ts        = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    uid       = str(uuid.uuid4())[:8]
    blob_name = f"screenshots/{ts}_{uid}.png"

    # ── 2. Screenshot Playwright ──────────────────────────────
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        screenshot_bytes = await page.screenshot(full_page=True)
        await browser.close()

    # ── 3. Upload GCS ─────────────────────────────────────────
    client = get_gcs_client()
    bucket = client.bucket(GCP_BUCKET_NAME)
    blob   = bucket.blob(blob_name)
    blob.upload_from_string(screenshot_bytes, content_type="image/png")

    # ── 4. Rendre public + retourner URL ──────────────────────
    blob.make_public()
    return blob.public_url


# =============================================================
# MIDDLEWARE — Authentification Bearer Token
# =============================================================

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Vérifie la présence du bon token dans le header Authorization
    de chaque requête entrante.
    Retourne 401 si le token est absent ou incorrect.
    """
    async def dispatch(self, request, call_next):
        if MCP_BEARER_TOKEN:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:].strip() != MCP_BEARER_TOKEN:
                return JSONResponse({"error": "Non autorisé"}, status_code=401)
        return await call_next(request)


# =============================================================
# POINT D'ENTRÉE
# =============================================================

if __name__ == "__main__":
    print(f"🚀 Serveur MCP Screenshot démarré sur le port {PORT}")
    print(f"🔐 Auth : {'Activée' if MCP_BEARER_TOKEN else 'DÉSACTIVÉE ⚠️'}")

    app = mcp.streamable_http_app()
    # ^ Crée l'app ASGI FastMCP en mode StreamableHTTP
    #   (méthode confirmée par le serveur Pipedrive qui fonctionne)

    app.add_middleware(BearerAuthMiddleware)
    # ^ Injecte le middleware d'auth par-dessus l'app MCP

    uvicorn.run(app, host="0.0.0.0", port=PORT)