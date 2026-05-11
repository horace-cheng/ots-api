#!/usr/bin/env bash
# =============================================================================
# OTS — Email Notification Infrastructure Setup
# =============================================================================
# 建立 Pub/Sub topic、push subscription 及所需的 IAM 權限，
# 讓 ots-api-backend 能夠發送 email 通知（透過 Brevo / SMTP）。
#
# 使用方式：./scripts/setup_notification_infra.sh [dev|staging|production]
#
# 相依性：需要已部署 ots-api-backend-{ENV} Cloud Run 服務。
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── 參數 ──────────────────────────────────────────────────────────────────────
ENV="${1:-}"
[[ "$ENV" =~ ^(dev|staging|production)$ ]] || \
  err "請指定環境：./scripts/setup_notification_infra.sh [dev|staging|production]"

PROJECT_ID="ots-translation"
REGION="asia-east1"

# ── 命名 ──────────────────────────────────────────────────────────────────────
TOPIC_NOTIFY="ots-notify-${ENV}"
SUB_PUSH_NAME="ots-notify-sub-${ENV}"           # push subscription 名稱
SA_PUSH_NAME="ots-notify-sub-${ENV}"            # push subscription 用的 SA
SA_API_NAME="ots-api-backend-${ENV}"            # API 服務的 SA
SERVICE_NAME="ots-api-backend-${ENV}"           # Cloud Run 服務名稱

# ── 全名 ──────────────────────────────────────────────────────────────────────
SA_API_EMAIL="${SA_API_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SA_PUSH_EMAIL="${SA_PUSH_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo ""
echo -e "${CYAN}=====================================================${NC}"
echo -e "${CYAN}  OTS Notification Infra — ENV: ${YELLOW}${ENV}${NC}"
echo -e "${CYAN}=====================================================${NC}"
echo ""

# =============================================================================
# 1. PUBLISHER IAM：API backend → notify topic
# =============================================================================
log "Step 1/5 — 授予 API backend SA publish 權限至 ${TOPIC_NOTIFY}..."

if gcloud pubsub topics get-iam-policy "$TOPIC_NOTIFY" --project="$PROJECT_ID" --format=json 2>/dev/null \
  | jq -e ".bindings[] | select(.role == \"roles/pubsub.publisher\") | .members[] | select(. == \"serviceAccount:${SA_API_EMAIL}\")" &>/dev/null; then
  warn "Publisher 權限已存在，跳過"
else
  gcloud pubsub topics add-iam-policy-binding "$TOPIC_NOTIFY" \
    --member="serviceAccount:${SA_API_EMAIL}" \
    --role="roles/pubsub.publisher" \
    --project="$PROJECT_ID" --quiet
  ok "Publisher 權限設定完成"
fi

# =============================================================================
# 2. PUSH SUBSCRIPTION SERVICE ACCOUNT
# =============================================================================
log "Step 2/5 — 建立 push subscription 專用 SA..."

if gcloud iam service-accounts describe "$SA_PUSH_EMAIL" --quiet &>/dev/null; then
  warn "SA 已存在：$SA_PUSH_NAME"
else
  gcloud iam service-accounts create "$SA_PUSH_NAME" \
    --display-name="OTS Notify Push Sub [${ENV}]" \
    --description="SA for Pub/Sub push subscription → ots-api-backend-${ENV}" \
    --quiet
  ok "SA 建立完成：$SA_PUSH_NAME"
fi

# =============================================================================
# 3. GRANT run.invoker TO PUSH SA
# =============================================================================
log "Step 3/5 — 授予 push SA 呼叫 Cloud Run 權限..."

API_URL=$(gcloud run services describe "$SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)' 2>/dev/null) || \
  err "找不到 Cloud Run 服務：$SERVICE_NAME（請先部署 API）"

if gcloud run services get-iam-policy "$SERVICE_NAME" --region="$REGION" --project="$PROJECT_ID" --format=json 2>/dev/null \
  | jq -e ".bindings[] | select(.role == \"roles/run.invoker\") | .members[] | select(. == \"serviceAccount:${SA_PUSH_EMAIL}\")" &>/dev/null; then
  warn "run.invoker 權限已存在，跳過"
else
  gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
    --region="$REGION" \
    --member="serviceAccount:${SA_PUSH_EMAIL}" \
    --role="roles/run.invoker" \
    --project="$PROJECT_ID" --quiet
  ok "run.invoker 權限設定完成"
fi

# =============================================================================
# 4. CREATE PUSH SUBSCRIPTION
# =============================================================================
PUSH_ENDPOINT="${API_URL}/internal/pubsub-notify"
log "Step 4/5 — 建立 push subscription：${SUB_PUSH_NAME}"
log "          └─ endpoint：${PUSH_ENDPOINT}"

if gcloud pubsub subscriptions describe "$SUB_PUSH_NAME" --project="$PROJECT_ID" --quiet &>/dev/null; then
  warn "Subscription 已存在，跳過：$SUB_PUSH_NAME"
  warn "  如需更新 endpoint，請先手動刪除："
  warn "    gcloud pubsub subscriptions delete $SUB_PUSH_NAME --project=$PROJECT_ID"
else
  gcloud pubsub subscriptions create "$SUB_PUSH_NAME" \
    --topic="$TOPIC_NOTIFY" \
    --push-endpoint="${PUSH_ENDPOINT}" \
    --push-auth-service-account="${SA_PUSH_EMAIL}" \
    --ack-deadline=30 \
    --message-retention-duration=7d \
    --expiration-period=never \
    --project="$PROJECT_ID" --quiet
  ok "Push subscription 建立完成：$SUB_PUSH_NAME"
fi

# =============================================================================
# 5. BREVO API KEY (提示)
# =============================================================================
echo ""
log "Step 5/5 — 檢查 BREVO_API_KEY secret..."
if gcloud secrets describe "ots-brevo-apikey-${ENV}" --project="$PROJECT_ID" --quiet &>/dev/null; then
  ok "Secret 已存在：ots-brevo-apikey-${ENV}"
else
  warn "Secret 尚未建立，請執行："
  echo "    echo -n 'your_brevo_api_key' | gcloud secrets create ots-brevo-apikey-${ENV} \\"
  echo "      --data-file=- --project=$PROJECT_ID --quiet"
fi

# =============================================================================
# 完成
# =============================================================================
echo ""
echo -e "${GREEN}=====================================================${NC}"
echo -e "${GREEN}  Notification Infra 設定完成 — ENV: ${YELLOW}${ENV}${NC}"
echo -e "${GREEN}=====================================================${NC}"
echo ""
echo "  Notify topic       : $TOPIC_NOTIFY"
echo "  Push subscription  : $SUB_PUSH_NAME"
echo "  Push SA            : $SA_PUSH_EMAIL"
echo "  Push endpoint      : $PUSH_ENDPOINT"
echo "  API service account: $SA_API_EMAIL"
echo ""
echo -e "${YELLOW}  EMAIL_PROVIDER 環境變數需設為 brevo（已在 cloudbuild.yaml 設定）${NC}"
echo -e "${YELLOW}  部署後 API 會自動透過 Pub/Sub → push subscription 發送 email${NC}"
echo ""
