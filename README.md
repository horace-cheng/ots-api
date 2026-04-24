# ots-api

FastAPI backend for the Original Tale Studio (OTS) translation service platform, deployed on Google Cloud Run.

## Stack

- **Runtime**: Python 3.12, FastAPI, Uvicorn
- **Database**: PostgreSQL via Cloud SQL (async SQLAlchemy + asyncpg)
- **Auth**: Firebase Authentication (ID token verification)
- **Storage**: Google Cloud Storage (signed URLs for client-side upload/download)
- **Payments**: ECPay / PAYUNi / manual wire transfer (switchable via env var)
- **Pipeline trigger**: Cloud Pub/Sub

## Local development

**Prerequisites**: Python 3.12+, `gcloud` CLI, Cloud SQL Auth Proxy

```bash
pip install -r requirements.txt
cp .env.example .env      # fill in credentials

# Authenticate with GCP (for Firebase + GCS ADC)
gcloud auth application-default login

# Start Cloud SQL Auth Proxy in a separate terminal
cloud-sql-proxy --port=5433 ots-translation:asia-east1:ots-db-dev

# Run the dev server
uvicorn main:app --reload --port 8080
```

API docs available at `http://localhost:8080/docs` (dev environment only).

## Environment variables

| Variable | Description |
|---|---|
| `DB_URL` | asyncpg connection string — use `localhost:5433` locally via proxy |
| `ENV` | `dev` / `staging` / `production` |
| `PAYMENT_GATEWAY` | `manual` / `ecpay` / `payuni` |
| `PROJECT_ID` | GCP project ID |
| `GCS_UPLOADS_BUCKET` | GCS bucket for source file uploads |
| `GCS_OUTPUTS_BUCKET` | GCS bucket for translated file downloads |
| `PUBSUB_TOPIC` | Pub/Sub topic that triggers the translation pipeline |
| `ECPAY_MERCHANT_ID` / `ECPAY_HASH_KEY` / `ECPAY_HASH_IV` | ECPay credentials (sandbox values in `.env.example`) |
| `ECPAY_SANDBOX` | `true` to use ECPay/PAYUNi staging endpoints |

On Cloud Run, secrets are injected via Secret Manager (`--set-secrets`).

## Deployment

Deployed via Cloud Build (`deploy/cloudbuild.yaml`) to Cloud Run in `asia-east1`. To deploy to a specific environment, pass the `_ENV` substitution:

```bash
gcloud builds submit --substitutions=_ENV=staging
```

The default target is `dev`.
