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
# Create and activate a virtual environment (required — avoids system package conflicts)
python3.12 -m venv .venv
source .venv/bin/activate

# If python3.12-venv is missing on Ubuntu/Debian, install it first:
# sudo add-apt-repository ppa:deadsnakes/ppa
# sudo apt install python3.12-venv

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
| `NOTIFY_TOPIC` | Pub/Sub topic for email notification events (`ots-notify-{env}`) |
| `EMAIL_PROVIDER` | `brevo` (production) or `smtp` (local dev) |
| `EMAIL_FROM_ADDRESS` / `EMAIL_FROM_NAME` | Sender identity for outbound emails |
| `BREVO_API_KEY` | Brevo API key (Secret Manager in production) |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_USE_TLS` | SMTP provider settings |
| `WEB_PORTAL_URL` | Frontend URL used in email links (default: `http://localhost:3000`) |
| `ECPAY_MERCHANT_ID` / `ECPAY_HASH_KEY` / `ECPAY_HASH_IV` | ECPay credentials (sandbox values in `.env.example`) |
| `ECPAY_SANDBOX` | `true` to use ECPay/PAYUNi staging endpoints |

On Cloud Run, secrets are injected via Secret Manager (`--set-secrets`).

## Testing

```bash
# Install test dependencies (only needed once)
pip install -r requirements-dev.txt

# Run the full test suite
pytest

# Run a single test file
pytest tests/test_router_orders.py

# Run a single test by name
pytest tests/test_router_orders.py::TestCreateOrder::test_returns_201_with_payment_url

# Verbose output with print statements
pytest -v -s
```

Tests use `pytest-asyncio` (auto mode) and mock all external dependencies — no database, Firebase, or GCP connection required.

## Email Notifications

The API sends transactional email notifications for events like user registration, order creation, payment confirmation, account enable/disable, and editor/proofreader assignments.

### Architecture

```
Event Source (router) → publish_event_sync()
  → Pub/Sub ots-notify-{env}
  → Push subscription → POST /internal/pubsub-notify
  → handle_notify_event()
  → resolve recipients from DB
  → render_template(lang, event, context)
  → Brevo / SMTP send_email()
```

### Email templates

Templates live in `services/notification/templates/{lang}/`. Supported languages:
- `zh-tw` — all 10 event types
- `en` — all 10 event types
- `ja`, `ko` — delivery_complete, user_registered, order_created_ft, user_enabled, user_disabled

Adding a new event: add to `EventType` enum in `types.py`, create `{lang}/{event}.html`, add to `_SUBJECT_MAP` and `_HEADER_MAP` in `sender.py`.

### Infrastructure setup

Run once per environment after the first API deployment:

```bash
# Creates Pub/Sub push subscription, service account, and IAM bindings
./scripts/setup_notification_infra.sh dev     # or staging / production
```

This step is also included in `ots-workflow/bootstrap_orchestration.sh`.

### Provider configuration

| Provider | Env vars | Use case |
|---|---|---|
| **Brevo** | `EMAIL_PROVIDER=brevo`, `BREVO_API_KEY` | Production (300 emails/day free) |
| **SMTP** | `EMAIL_PROVIDER=smtp`, `SMTP_HOST`, `SMTP_PORT` | Local dev (Mailpit/Mailhog) |

The `EMAIL_FROM_ADDRESS` and `EMAIL_FROM_NAME` env vars control the sender identity.

### Troubleshooting

Check Cloud Run logs:
```bash
gcloud logging read 'resource.type=cloud_run_revision AND "Notify event"' --project=ots-translation
```

Common issues:
- **403 on publish** → API SA lacks `pubsub.publisher` on `ots-notify-{env}` → run the setup script
- **No email received** → push subscription missing → run the setup script
- **UUID errors** → non-UUID `order_id` passed to DB query → fixed by `_is_valid_uuid()` in `sender.py`
- **SMTP timeout** → verify `SMTP_HOST`/`SMTP_PORT` reachable from Cloud Run

## Deployment

Deployed via Cloud Build (`deploy/cloudbuild.yaml`) to Cloud Run in `asia-east1`. To deploy to a specific environment, pass the `_ENV` substitution:

```bash
gcloud builds submit --substitutions=_ENV=staging
```

The default target is `dev`.
