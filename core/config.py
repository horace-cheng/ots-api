import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Cloud SQL ──────────────────────────────────────────────────────────
    # 由 Cloud Run --set-secrets 注入，格式：
    # postgresql+asyncpg://ots_app:PASSWORD@/ots?host=/cloudsql/PROJECT:REGION:INSTANCE
    db_url: str = os.environ.get("DB_URL", "")

    # ── ECPay 綠界金流 ─────────────────────────────────────────────────────
    ecpay_merchant_id: str = os.environ.get("ECPAY_MERCHANT_ID", "")
    ecpay_hash_key: str    = os.environ.get("ECPAY_HASH_KEY", "")
    ecpay_hash_iv: str     = os.environ.get("ECPAY_HASH_IV", "")
    # sandbox = True 時走測試環境
    ecpay_sandbox: bool    = os.environ.get("ECPAY_SANDBOX", "true").lower() == "true"

    # ── Stripe 金流 ────────────────────────────────────────────────────────
    stripe_secret_key: str      = os.environ.get("STRIPE_SECRET_KEY", "")
    stripe_webhook_secret: str  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    # ── GCP ───────────────────────────────────────────────────────────────
    project_id: str          = os.environ.get("PROJECT_ID", "ots-translation")
    env: str                 = os.environ.get("ENV", "dev")
    region: str              = os.environ.get("REGION", "asia-east1")
    gcs_uploads_bucket: str  = os.environ.get("GCS_UPLOADS_BUCKET", "")
    gcs_outputs_bucket: str  = os.environ.get("GCS_OUTPUTS_BUCKET", "")
    gcs_temp_bucket:    str  = os.environ.get("GCS_TEMP_BUCKET", "ots-translation-pipeline-temp-dev")
    pubsub_topic: str        = os.environ.get("PUBSUB_TOPIC", "ots-pipeline-trigger-dev")

    # 金流廠商
    payment_gateway: str     = os.environ.get("PAYMENT_GATEWAY", "manual")

    # Service Account email（Signed URL 簽名用）
    # Cloud Run 環境下自動從 metadata server 取得，也可明確指定
    service_account_email: str = os.environ.get(
        "SERVICE_ACCOUNT_EMAIL",
        f"ots-api-backend-dev@ots-translation.iam.gserviceaccount.com"
    )    
    web_portal_url: str      = os.environ.get("WEB_PORTAL_URL", "http://localhost:3000")

    # ── Gemini (試譯包 synopsis 生成) ──────────────────────────────────────
    gemini_api_key: str = os.environ.get("GEMINI_API_KEY", "")

    # ── Email 通知 ─────────────────────────────────────────────────────────
    email_provider: str       = os.environ.get("EMAIL_PROVIDER", "smtp")
    brevo_api_key: str        = os.environ.get("BREVO_API_KEY", "")
    email_from_address: str   = os.environ.get("EMAIL_FROM_ADDRESS", "noreply@ots.tw")
    email_from_name: str      = os.environ.get("EMAIL_FROM_NAME", "OTS 翻譯服務")
    smtp_host: str            = os.environ.get("SMTP_HOST", "localhost")
    smtp_port: int            = int(os.environ.get("SMTP_PORT", "1025"))
    smtp_username: str        = os.environ.get("SMTP_USERNAME", "")
    smtp_password: str        = os.environ.get("SMTP_PASSWORD", "")
    smtp_use_tls: bool        = os.environ.get("SMTP_USE_TLS", "false").lower() == "true"

    # ── Notification Pub/Sub ───────────────────────────────────────────────
    notify_topic: str = os.environ.get("NOTIFY_TOPIC", f"ots-notify-dev")

    # ── BRONCI TTS ─────────────────────────────────────────────────────────
    bronci_username: str  = os.environ.get("BRONCI_API_USERNAME", "")
    bronci_password: str  = os.environ.get("BRONCI_API_PASSWORD", "")
    bronci_base_url: str  = os.environ.get("BRONCI_API_BASE_URL", "https://rbtttsapi.bronci.com.tw")

    # ── Hugging Face Inference API ─────────────────────────────────────────
    hf_api_token: str      = os.environ.get("HF_API_TOKEN", "")
    hf_image_model: str    = os.environ.get("HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")

    # ── Replicate (fallback image gen) ─────────────────────────────────────
    replicate_api_token: str = os.environ.get("REPLICATE_API_TOKEN", "")

    class Config:
        # 允許從 .env 檔載入（本機開發用）
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
