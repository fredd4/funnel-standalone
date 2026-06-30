# Deploying to production (Cloud Run + Firebase Hosting)

This app is a Python container. It runs on **Cloud Run**; **Firebase Hosting**
sits in front and rewrites traffic to it (`firebase.json` is already configured).

Below is the full path from a clean machine to a live URL. A fresh agent started
in this directory can follow it top to bottom.

---

## 0. Prerequisites (one-time)

- **gcloud CLI** — https://cloud.google.com/sdk/docs/install , then `gcloud auth login`
- **firebase CLI** — `npm i -g firebase-tools`, then `firebase login`
- A **Google Cloud / Firebase project** to host the service (can be the same
  `your-data-project` project, or a separate "ops" project — see note in step 3).
- Billing enabled on that project (Cloud Run + Cloud Build require it).
- **Read access to the data**: the service must read the `logs` collection in the
  `your-data-project` Firestore. Two options, pick one in step 2.

```bash
gcloud config set project <YOUR_HOSTING_PROJECT_ID>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com firestore.googleapis.com
```

---

## 1. Decisions to make before deploying

| Decision | Options | Recommendation |
|---|---|---|
| **Cache backend** | `memory` (per-instance) / `redis` (shared) / `firestore` | `memory` with **max-instances=1** for a low-traffic internal tool (free, simplest). Switch to `redis` only if you need >1 instance. |
| **Auth** | open / `API_TOKENS` / Cloud Run IAM + IAP | Set `API_TOKENS` to a secret string, OR deploy **without** `--allow-unauthenticated` and use IAP. Don't leave it fully open on a public URL. |
| **Firestore credentials** | IAM grant / mounted key file | **IAM grant** (no key files to manage) — see step 2A. |
| **Region** | any | Keep it consistent with `firebase.json` (currently `europe-west1`). Change both if you prefer another. |

---

## 2. Give the service read access to Firestore

### 2A. Recommended: IAM grant (no key file)

Cloud Run runs as a service account (default: `<PROJECT_NUMBER>-compute@developer.gserviceaccount.com`).
Grant it Firestore read on the data project:

```bash
# The project that OWNS the data (your-data-project):
gcloud projects add-iam-policy-binding your-data-project \
  --member="serviceAccount:<RUNTIME_SA_EMAIL>" \
  --role="roles/datastore.viewer"
```

Then in `config/projects.yaml` set `credentials_path: null` so the app uses the
runtime service account (Application Default Credentials). **This is cleaner — no
secret to rotate.**

### 2B. Alternative: mount the service-account key from Secret Manager

```bash
gcloud secrets create sample-sa-key --data-file=secrets/your-data-project.json
# grant the runtime SA access, then mount at deploy time:
#   --update-secrets=/secrets/sample.json=sample-sa-key:latest
# and set credentials_path: "/secrets/sample.json" in projects.yaml
```

> The local `secrets/` dir and `config/projects.yaml` are git-ignored. They exist
> on your machine but are NOT in the repo — you must provide them in production via
> one of the methods above.

---

## 3. Deploy the container to Cloud Run

`config/projects.yaml` is committed (it holds no secrets) and ships in the image
at build time — nothing to prepare here. With `credentials_path: null` the service
authenticates to Firestore via the runtime service account (step 2A).

```bash
gcloud run deploy funnel-standalone \
  --source . \
  --region europe-west1 \
  --memory 512Mi \
  --timeout 600 \
  --min-instances 1 \
  --max-instances 1 \
  --set-env-vars CACHE_BACKEND=memory,METADATA_ENABLED=false,API_TOKENS=<PICK_A_SECRET>
  # add --no-allow-unauthenticated if you'll use IAP instead of API_TOKENS
```

`--source .` triggers Cloud Build to build the `Dockerfile` and deploy. The first
build takes a few minutes. On success it prints a `*.run.app` URL — test it:

```bash
curl https://<service-url>/                       # {"status":"ok","projects":["myapp"]}
curl "https://<service-url>/myapp/funnel.json?date_from=2026-06-24&date_to=2026-06-27"
```

(If you set `API_TOKENS`, add `-H "Authorization: Bearer <token>"`.)

---

## 4. Put Firebase Hosting in front

`firebase.json` already rewrites all traffic to the Cloud Run service
`funnel-standalone` in `europe-west1`. Point it at your project and deploy:

```bash
firebase use --add            # pick your Firebase project, alias it "default"
firebase deploy --only hosting
```

Now `https://<your-project>.web.app/myapp/funnel` serves the funnel UI.

---

## 5. Post-deploy checklist

- [ ] `GET /` returns `{"status":"ok",...}`.
- [ ] `/myapp/funnel` renders the table (try a date range with data).
- [ ] Auth works the way you intended (token required, or IAP prompt).
- [ ] First request for a date range is slow, second is fast (cache works).
- [ ] If you set `METADATA_ENABLED=true`, the runtime SA also needs Firestore
      **write** on whichever project holds `_funnel_meta`.

## 6. Adding another project later

Add a block to `config/projects.yaml` (see `projects.example.yaml`), grant the
runtime SA read access to that project's Firestore, and redeploy. No code changes.

## 7. Updating after code changes

```bash
gcloud run deploy funnel-standalone --source . --region europe-west1   # redeploy container
firebase deploy --only hosting                                          # only if hosting config changed
```
