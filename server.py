# =============================================================================
# server.py — Serveur MCP Screenshot → GCP → Dust
# Stack : FastMCP + Playwright + Google Cloud Storage + Railway
# =============================================================================

import uuid
import json
from datetime import datetime

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from playwright.async_api import async_playwright
from playwright_stealth import stealth_async
from google.cloud import storage
from google.oauth2 import service_account

from config import (
    PORT,
    MCP_BEARER_TOKEN,
    GCP_BUCKET_NAME,
    GCP_CREDENTIALS_JSON,
    DUST_EMAIL,
    DUST_PASSWORD,
)


# =============================================================================
# HELPER — Client Google Cloud Storage
# =============================================================================

def get_gcs_client() -> storage.Client:
    """
    Retourne un client GCS authentifié.
    Lit les credentials depuis la variable d'env GCP_CREDENTIALS_JSON.
    """
    if GCP_CREDENTIALS_JSON:
        info  = json.loads(GCP_CREDENTIALS_JSON)
        creds = service_account.Credentials.from_service_account_info(info)
        return storage.Client(credentials=creds, project=info.get("project_id"))
    return storage.Client()


# =============================================================================
# SERVEUR MCP
# =============================================================================

mcp = FastMCP(
    name="screenshot-server",
    host="0.0.0.0",
    port=PORT,
    instructions=(
        "Serveur MCP Screenshot. "
        "Outil disponible : screenshot_url. "
        "Prend une URL en entrée, capture un screenshot pleine-page via Playwright, "
        "uploade le PNG sur GCP Cloud Storage et retourne l'URL publique de l'image. "
        "Supporte les pages Dust authentifiées via email + mot de passe (authenticated=True)."
    )
)


# =============================================================================
# HELPER — Login Dust via WorkOS (email + mot de passe)
# =============================================================================

async def login_to_dust(page) -> None:
    """
    Automatise le login Dust via WorkOS AuthKit.

    Flow :
      1. dust.tt/api/workos/login → redirige vers signin.dust.tt
      2. Saisir l'email → cliquer Continuer
      3. Saisir le mot de passe → cliquer Continuer (1er bouton submit)
      4. Attendre la redirection vers app.dust.tt

    Utilise playwright-stealth pour bypasser la détection bot de WorkOS.
    """

    # Étape 1 : Naviguer vers la page de login Dust
    # WorkOS redirige automatiquement vers signin.dust.tt
    await page.goto(
        "https://dust.tt/api/workos/login?returnTo=%2Fapi%2Flogin",
        wait_until="networkidle",
        timeout=30_000
    )
    await page.wait_for_url("https://signin.dust.tt/**", timeout=15_000)

    # Étape 2 : Saisir l'email
    await page.locator("input[name='email']").wait_for(state="visible", timeout=10_000)
    await page.locator("input[name='email']").fill(DUST_EMAIL)

    # Cliquer sur "Continuer" — .first car il peut y avoir plusieurs boutons submit
    await page.locator("button[type='submit']").first.click()
    await page.wait_for_load_state("networkidle")
    # ^ Après ce clic, WorkOS charge la page mot de passe (signin.dust.tt/password)

    # Étape 3 : Saisir le mot de passe
    await page.locator("input[name='password']").wait_for(state="visible", timeout=10_000)
    await page.locator("input[name='password']").fill(DUST_PASSWORD)

    # Cliquer sur "Continuer" — .first pour éviter le bouton passkey
    await page.locator("button[type='submit']").first.click()
    await page.wait_for_load_state("networkidle")

    # Étape 4 : Attendre la redirection finale vers app.dust.tt
    # Dust redirige : signin.dust.tt → dust.tt/api/workos/callback → app.dust.tt
    await page.wait_for_url("https://app.dust.tt/**", timeout=20_000)
    # ^ Session active dans tout le contexte Playwright à partir d'ici


# =============================================================================
# OUTIL MCP — screenshot_url
# =============================================================================

@mcp.tool()
async def screenshot_url(url: str, authenticated: bool = False) -> str:
    """
    Capture un screenshot pleine-page d'une URL,
    l'uploade dans un bucket GCP Cloud Storage public,
    et retourne son URL publique accessible.

    Args:
        url:           URL de la page à capturer.
        authenticated: Si True, se connecte à Dust avec DUST_EMAIL + DUST_PASSWORD
                       avant de visiter l'URL. À utiliser pour les pages Dust privées.

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
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        # Stealth appliqué avant toute navigation
        # Masque les indicateurs headless → bypass bot detection WorkOS
        await stealth_async(page)

        if authenticated:
            # Login Dust avant de visiter l'URL cible
            await login_to_dust(page)

        # Naviguer vers l'URL cible
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        screenshot_bytes = await page.screenshot(full_page=True)

        await browser.close()

    # ── 3. Upload GCS ─────────────────────────────────────────
    client = get_gcs_client()
    bucket = client.bucket(GCP_BUCKET_NAME)
    blob   = bucket.blob(blob_name)
    blob.upload_from_string(screenshot_bytes, content_type="image/png")

    # ── 4. URL publique ───────────────────────────────────────
    public_url = f"https://storage.googleapis.com/{GCP_BUCKET_NAME}/{blob_name}"
    return public_url


# =============================================================================
# MIDDLEWARE — Authentification Bearer Token
# =============================================================================

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Vérifie le header Authorization sur chaque requête entrante.
    Retourne 401 si le token est absent ou incorrect.
    """
    async def dispatch(self, request, call_next):
        if MCP_BEARER_TOKEN:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:].strip() != MCP_BEARER_TOKEN:
                return JSONResponse({"error": "Non autorisé"}, status_code=401)
        return await call_next(request)


# =============================================================================
# POINT D'ENTRÉE
# =============================================================================

if __name__ == "__main__":
    print(f"🚀 Serveur MCP Screenshot démarré sur le port {PORT}")
    print(f"🔐 Auth MCP  : {'Activée' if MCP_BEARER_TOKEN else 'DÉSACTIVÉE ⚠️'}")
    print(f"🔑 Dust auth : {'Configurée ({DUST_EMAIL})' if DUST_EMAIL else 'Non configurée'}")

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)

    uvicorn.run(app, host="0.0.0.0", port=PORT)