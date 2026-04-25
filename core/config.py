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

    # ── GCP ───────────────────────────────────────────────────────────────
    project_id: str          = os.environ.get("PROJECT_ID", "ots-translation")
    env: str                 = os.environ.get("ENV", "dev")
    gcs_uploads_bucket: str  = os.environ.get("GCS_UPLOADS_BUCKET", "")
    gcs_outputs_bucket: str  = os.environ.get("GCS_OUTPUTS_BUCKET", "")
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

    class Config:
        # 允許從 .env 檔載入（本機開發用）
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
