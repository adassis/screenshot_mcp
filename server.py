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
    Lit les credentials depuis la variable d'env GCP_CREDENTIALS_JSON
    (JSON du service account GCP, stringifié en une seule ligne).
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
        "Supporte les pages Dust authentifiées via email + mot de passe."
    )
)


# =============================================================================
# HELPER — Login Dust via WorkOS (email + mot de passe)
# =============================================================================

async def login_to_dust(page) -> None:
    """
    Automatise le login Dust via WorkOS AuthKit.

    Flow en 2 étapes :
      1. Saisir l'email → cliquer Continue
      2. Saisir le mot de passe → cliquer Continue

    Après le login, Dust redirige vers le workspace.
    La session est active dans tout le contexte Playwright.
    """

    # Étape 1 : Aller sur la page de login Dust (WorkOS AuthKit)
    await page.goto(
        "https://dust.tt/api/workos/login?returnTo=%2Fapi%2Flogin",
        wait_until="networkidle",
        timeout=30_000
    )

    # Étape 2 : Saisir l'email
    await page.wait_for_selector("input[type='email']", timeout=10_000)
    await page.fill("input[type='email']", DUST_EMAIL)

    # Cliquer sur "Continue" (1er submit → charge le champ password)
    await page.click("button[type='submit']")

    # Étape 3 : Attendre le champ mot de passe (2e étape WorkOS)
    await page.wait_for_selector("input[type='password']", timeout=10_000)
    await page.fill("input[type='password']", DUST_PASSWORD)

    # Cliquer sur "Continue" (2e submit → authentification)
    await page.click("button[type='submit']")

    # Étape 4 : Attendre la redirection vers dust.tt
    # WorkOS redirige vers dust.tt/api/workos/callback puis vers le workspace
    await page.wait_for_url("https://dust.tt/**", timeout=20_000)
    # ^ Session active dans le contexte Playwright à partir d'ici


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
    # ^ Timestamp UTC : "20260513_143022"
    uid       = str(uuid.uuid4())[:8]
    # ^ 8 caractères aléatoires : "a1b2c3d4"
    blob_name = f"screenshots/{ts}_{uid}.png"
    # ^ Chemin dans le bucket : "screenshots/20260513_143022_a1b2c3d4.png"

    # ── 2. Screenshot Playwright ──────────────────────────────
    async with async_playwright() as pw:

        browser = await pw.chromium.launch(
            args=[
                "--no-sandbox",
                # ^ Obligatoire dans les conteneurs Docker
                "--disable-setuid-sandbox",
                # ^ Idem
                "--disable-dev-shm-usage",
                # ^ Évite les crashs mémoire dans Docker
            ]
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800}
            # ^ On crée un contexte (= profil isolé) pour gérer la session
        )

        page = await context.new_page()

        if authenticated:
            # Se connecter à Dust avant de visiter l'URL cible
            # Après login, le cookie de session est actif dans tout le contexte
            await login_to_dust(page)

        # Naviguer vers l'URL cible (avec ou sans auth)
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        # ^ wait_until="networkidle" : attend que la page soit entièrement chargée
        # ^ timeout=30_000 : abandonne après 30 secondes

        # Capturer la page entière (défilement inclus)
        screenshot_bytes = await page.screenshot(full_page=True)

        await browser.close()
        # ^ Libère la mémoire — important sur Railway

    # ── 3. Upload GCS ─────────────────────────────────────────
    client = get_gcs_client()
    bucket = client.bucket(GCP_BUCKET_NAME)
    blob   = bucket.blob(blob_name)
    blob.upload_from_string(screenshot_bytes, content_type="image/png")
    # ^ Envoie les bytes PNG vers GCS avec le bon Content-Type

    # ── 4. URL publique ───────────────────────────────────────
    public_url = f"https://storage.googleapis.com/{GCP_BUCKET_NAME}/{blob_name}"
    # ^ Construction directe de l'URL publique GCS
    # ^ Fonctionne si le bucket a "allUsers → Storage Object Viewer" dans son IAM

    return public_url
    # ^ Cette URL est retournée à l'agent Dust comme output du tool


# =============================================================================
# MIDDLEWARE — Authentification Bearer Token
# =============================================================================

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """
    Intercepte chaque requête HTTP entrante.
    Vérifie la présence du bon token dans le header Authorization.
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
    print(f"🔑 Dust auth : {'Configurée' if DUST_EMAIL else 'Non configurée'}")

    app = mcp.streamable_http_app()
    # ^ App ASGI FastMCP en mode StreamableHTTP (supporté par Dust)

    app.add_middleware(BearerAuthMiddleware)
    # ^ Middleware d'auth injecté par-dessus l'app MCP

    uvicorn.run(app, host="0.0.0.0", port=PORT)
    # ^ host="0.0.0.0" obligatoire sur Railway
    # ^ PORT injecté automatiquement par Railway