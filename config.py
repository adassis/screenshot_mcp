import os

PORT                 = int(os.environ.get("PORT", 8000))
MCP_BEARER_TOKEN     = os.environ.get("MCP_BEARER_TOKEN", "")
GCP_BUCKET_NAME      = os.environ.get("GCP_BUCKET_NAME", "")
GCP_CREDENTIALS_JSON = os.environ.get("GCP_CREDENTIALS_JSON", "")

DUST_EMAIL    = os.environ.get("DUST_EMAIL", "")
# ^ Ton adresse email Dust

DUST_PASSWORD = os.environ.get("DUST_PASSWORD", "")
# ^ Ton mot de passe Dust