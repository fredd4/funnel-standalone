"""
Central configuration for the standalone funnel app.

Everything is read from environment variables so the same container image can be
reused per project / per environment (Cloud Run revisions, Firebase Hosting
rewrites, local dev).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Project registry --------------------------------------------------------
# Where project configs come from:
#   yaml      -> read PROJECTS_CONFIG_PATH (default; great for 1-2 projects / dev)
#   firestore -> read one doc per project from CONFIG_COLLECTION in the hosting
#                project's Firestore (add/edit projects without a redeploy).
CONFIG_BACKEND = os.environ.get("CONFIG_BACKEND", "yaml")

# Path to the YAML file describing every project the funnel can render.
PROJECTS_CONFIG_PATH = os.environ.get(
    "PROJECTS_CONFIG_PATH", str(BASE_DIR / "config" / "projects.yaml")
)

# Firestore-config backend: which project's Firestore holds the config, and the
# collection name. Empty project id -> Application Default Credentials' project
# (the hosting project on Cloud Run). Config docs hold NO secrets.
CONFIG_FIRESTORE_PROJECT_ID = os.environ.get("CONFIG_FIRESTORE_PROJECT_ID", "")
CONFIG_COLLECTION = os.environ.get("CONFIG_COLLECTION", "_funnel_projects")

# --- Cache backend -----------------------------------------------------------
# memory   -> per-instance dict (fine for a single Cloud Run instance / dev)
# redis    -> recommended for production (Memorystore / Upstash); shared, fast,
#             no 1 MB document limit like Firestore has.
# firestore-> serverless, but only safe for SMALL values (AB-test dates, filter
#             options, rendered fragments). Day-log blobs may exceed 1 MB.
CACHE_BACKEND = os.environ.get("CACHE_BACKEND", "memory")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Firestore collection used by the firestore cache backend and for the small
# metadata collections (AB-test dates, filter options).
CACHE_COLLECTION = os.environ.get("CACHE_COLLECTION", "_funnel_cache")
METADATA_COLLECTION = os.environ.get("METADATA_COLLECTION", "_funnel_meta")

# When false, A/B-test dates and dynamic filter options are NOT read from or
# written to Firestore (reads return None/[], writes are skipped). Keeps test
# runs read-only and avoids touching the target project's Firestore. Turn on in
# production if you want the AB-date "Seen: …" annotations to persist.
METADATA_ENABLED = os.environ.get("METADATA_ENABLED", "false").lower() in ("1", "true", "yes")

# --- Currency conversion (source-performance revenue in EUR) -----------------
# Rates are hardcoded in app/currency.py (CURRENCY_TO_EUR). No API key needed.

# --- Auth (very small) -------------------------------------------------------
# Comma separated list of bearer tokens allowed to call the API. Empty = open
# (only acceptable behind Firebase Hosting + IAP or for local dev).
API_TOKENS = [t.strip() for t in os.environ.get("API_TOKENS", "").split(",") if t.strip()]
