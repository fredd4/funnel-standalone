# Funnel (standalone)

A self-contained sales-funnel visualizer. It reads a project's events from
**Firestore**, computes a funnel + source-performance breakdown, and renders it
as HTML/JSON. Runs as a **Python container on Cloud Run**, fronted by **Firebase
Hosting**.

It is **multi-project** and **pluggable**:

- **Config backend** (`CONFIG_BACKEND`): projects come from a YAML file
  (`config/projects.yaml`, default) **or** from Firestore (one doc per project),
  so you can add/edit projects without a redeploy. The schema is identical.
- **Data-source plugins**: each project picks a backend via `source.type`
  (default `firestore`); register new backends in `app/data_sources/registry.py`.
- Config is **data, never secrets** — credentials come from ADC/IAM or a
  `credentials_path`, never from the YAML/Firestore config itself.

> The real `config/projects.yaml` is **git-ignored** (it may name private
> projects). Copy `config/projects.example.yaml` to start.

## Architecture

```
Firebase Hosting  ──rewrite──►  Cloud Run (this FastAPI app)  ──►  Firestore (per project)
   public/                         app/web/main.py                    logs collection
                                   app/funnel/core.py  (engine)       _funnel_cache / _funnel_meta
                                   app/data_sources/   (Firestore)
```

| Component | Where |
|---|---|
| Per-project config | YAML or Firestore + `app/projects.py` |
| Data source | plugin registry + `app/data_sources/firestore_source.py` |
| Funnel engine | `app/funnel/core.py` |
| Cache | `app/cache.py` (memory / redis / firestore) |
| A/B dates, filter options | `app/funnel/metadata.py` (Firestore) |
| Currency → EUR | `app/currency.py` (hardcoded rate table) |
| Web / templates | FastAPI + Jinja2 (`app/web/`) |

## Quick text test

```bash
.venv/bin/python scripts/run_funnel.py myapp --from 2026-06-24 --to 2026-06-27
.venv/bin/python scripts/run_funnel.py myapp --days 7
```

Prints the funnel as text straight from Firestore — the fastest way to check the
engine without the web server.

> **Gotcha:** `get_funnel(...)`'s default `platform='iOS'` filters out web-only
> projects like myapp. The CLI and the web layer pass an empty platform by
> default; only set `--platform iOS` for mobile projects.

## Metadata (A/B dates, filter options)

Off by default (`METADATA_ENABLED=false`) so runs are pure-read and never write to
the target project's Firestore. Set `METADATA_ENABLED=true` in production if you
want the "Seen: …" A/B-date annotations and dynamic filter dropdowns to persist.

## Auth

Set `API_TOKENS` (comma-separated) to require a token. Two ways in:

- **API clients:** `Authorization: Bearer <token>`.
- **Browser:** visit `/login`, paste the token once — it's stored in a secure
  `__session` cookie (HTML pages redirect there when unauthenticated; `/logout`
  clears it). The cookie **must** be named `__session`: Firebase Hosting strips
  every other cookie before proxying to Cloud Run.

Empty `API_TOKENS` = open (only acceptable behind IAP or for local dev). The
health endpoint `/` is always public; data endpoints are not.

## Local dev

```bash
cd funnel-standalone
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Create your local config from the template, then provide the SA key:
cp config/projects.example.yaml config/projects.yaml      # then edit it
mkdir -p secrets                                           # drop the service-account JSON here
export GOOGLE_APPLICATION_CREDENTIALS=secrets/your-data-project.json

uvicorn app.web.main:app --reload
# open http://localhost:8000/myapp/funnel
```

## Deploy (Cloud Run + Firebase Hosting)

Full step-by-step in **[DEPLOY.md](DEPLOY.md)**. The short version:

```bash
# 1. Build & deploy the container
gcloud run deploy funnel-standalone \
  --source . --region europe-west1 \
  --set-env-vars CACHE_BACKEND=memory,METADATA_ENABLED=false \
  --timeout 600 --min-instances 1 --max-instances 1 --allow-unauthenticated

# 2. Wire Firebase Hosting -> Cloud Run (firebase.json already set up)
firebase deploy --only hosting
```

Put the Firestore service-account JSON in **Secret Manager** and mount it, or run
Cloud Run with a service account that has Firestore read on the target project
and leave `credentials_path` null (Application Default Credentials).

For production set `CACHE_BACKEND=redis` + `REDIS_URL` (Memorystore/Upstash): the
day-log cache values can exceed Firestore's 1 MB document limit.

To load project config from Firestore instead of the bundled YAML, set
`CONFIG_BACKEND=firestore` (optionally `CONFIG_FIRESTORE_PROJECT_ID` and
`CONFIG_COLLECTION`) and store one document per project in that collection — the
doc id is the project name and its fields match `config/projects.example.yaml`.
