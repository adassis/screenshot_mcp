# =============================================================
# config.py — Configuration centralisée du serveur MCP
# Toutes les variables d'environnement sont lues ici
# et importées depuis server.py
# =============================================================

import os

# ── Serveur MCP ───────────────────────────────────────────────

PORT = int(os.environ.get("PORT", 8000))
# ^ Railway injecte $PORT automatiquement

MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
# ^ Token secret partagé avec Dust pour sécuriser l'accès

# ── Google Cloud Storage ──────────────────────────────────────

GCP_BUCKET_NAME = os.environ.get("GCP_BUCKET_NAME", "")
# ^ Nom du bucket GCS — ex: "yago-screenshots"

GCP_CREDENTIALS_JSON = os.environ.get("GCP_CREDENTIALS_JSON", "")
# ^ Contenu JSON du service account GCP, en une seule ligne