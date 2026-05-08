# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

OTS (Original Tale Studio) Translation API — a FastAPI backend deployed on Google Cloud Run, serving a translation service platform.

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in .env
cp .env.example .env

# Start Cloud SQL Auth Proxy (required for DB) on a separate terminal
cloud-sql-proxy --port=5433 ots-translation:asia-east1:ots-db-dev

# Start dev server
uvicorn main:app --reload --port 8080

# API docs (dev only)
open http://localhost:8080/docs
```

GCP auth for local development:
```bash
gcloud auth application-default login
```

## Key environment variables

| Variable | Purpose |
|---|---|
| `DB_URL` | PostgreSQL asyncpg URL — localhost:5433 via proxy locally, Unix socket on Cloud Run |
| `ENV` | `dev` / `staging` / `production` — controls CORS, SQL logging, docs visibility |
| `PAYMENT_GATEWAY` | `manual` / `ecpay` / `payuni` — selects payment implementation |
| `PROJECT_ID` | GCP project (`ots-translation`) |
| `GCS_UPLOADS_BUCKET` / `GCS_OUTPUTS_BUCKET` | GCS buckets for file I/O |
| `PUBSUB_TOPIC` | Pub/Sub topic to trigger the translation pipeline |

Secrets (`ECPAY_*`, `DB_URL`) are injected via Secret Manager on Cloud Run. Locally, put them in `.env`.

## Architecture

### Core modules (`core/`)

- **`config.py`** — single `Settings` instance (pydantic-settings) loaded from env + `.env` file. Imported as `from core.config import settings` everywhere.
- **`database.py`** — async SQLAlchemy engine + `AsyncSessionLocal`. `get_db()` is the FastAPI dependency. Pool is intentionally small (2+1) for Cloud SQL micro tier.
- **`firebase.py`** — initializes Firebase Admin SDK with ADC. `verify_firebase_token()` validates Firebase ID tokens for auth.
- **`storage.py`** — lazy singleton GCS client. Generates v4 signed URLs for client-side upload (PUT) and download (GET).

### Payment gateway pattern (`services/payment/`)

The payment layer is a strategy pattern designed so the router never imports any vendor SDK directly:

- **`base.py`** — defines `PaymentGateway` ABC with four abstract methods: `create_payment`, `parse_webhook`, `issue_invoice`, `refund`. Also defines all shared dataclasses (`PaymentRequest`, `WebhookPayload`, etc.) and exceptions (`PaymentError`, `InvoiceError`).
- **`factory.py`** — `get_payment_gateway()` (cached with `@lru_cache`) reads `PAYMENT_GATEWAY` env var and returns the matching implementation.
- **`manual.py`** — Year 1 fallback: no real gateway integration; generates a wire transfer instruction page URL. Invoice and refund raise errors (manual admin action required).
- **`ecpay.py`** — ECPay (綠界) integration: SHA256 CheckMacValue signing, AIO checkout, e-invoice API.
- **`payuni.py`** — PAYUNi (統一金流) integration: AES-256-CBC encrypted payload. Invoice and refund not yet implemented.
- **`stripe.py`** — Stripe Checkout Sessions: creates payment links via Checkout API, parses `checkout.session.completed` webhook events. Invoice not supported (external system). Refund uses `stripe.Refund.create`.

To add a new gateway: subclass `PaymentGateway` in a new file, implement all four methods, add a branch in `factory.py`, add env vars to `core/config.py`.

### Routers (`routers/`)

- **`payments.py`** — currently the only wired-up router (others commented out in `main.py`). The webhook endpoint (`POST /payments/webhook`) parses the callback, updates `payments` and `orders` tables via raw SQL, triggers the translation pipeline via Pub/Sub (`trigger_pipeline`), then auto-issues a B2C invoice (failure is non-fatal — logged, not re-raised).

**Stripe webhook note**: Unlike ECPay/PAYUNi (where the signature is in the form body), Stripe's signature verification requires the raw request body bytes and the `Stripe-Signature` header. The Stripe gateway's `parse_webhook()` expects an already-verified Stripe event dict — the router should call `stripe.Webhook.construct_event()` first.

### App lifecycle (`main.py`)

On startup: Firebase initialized → DB connection verified. CORS origins vary by `ENV`. `/docs` and `/redoc` are disabled in production.

## Deployment

Cloud Build pipeline (`deploy/cloudbuild.yaml`):
1. Build Docker image → tag with `$SHORT_SHA` + `latest`
2. Push to Artifact Registry (`asia-east1-docker.pkg.dev/ots-translation/ots/api-backend`)
3. Deploy to Cloud Run (`ots-api-backend`, region `asia-east1`) — secrets from Secret Manager, Cloud SQL via Unix socket, no public auth (`--no-allow-unauthenticated`)

Trigger with `_ENV=staging` or `_ENV=production` substitution to override the default `dev` target.
