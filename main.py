from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging

from core.config import settings
from core.database import check_db_connection
from core.firebase import init_firebase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan：啟動與關閉時執行 ───────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 啟動
    logger.info(f"Starting OTS API Backend [env={settings.env}]")

    init_firebase()

    db_ok = await check_db_connection()
    if not db_ok:
        logger.error("DB connection failed on startup — check Cloud SQL Auth Proxy")
    else:
        logger.info("DB connection OK")

    yield

    # 關閉
    logger.info("OTS API Backend shutting down")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="OTS Translation API",
    version="0.1.0",
    description="Original Tale Studio — Translation Service Backend",
    lifespan=lifespan,
    # production 環境關閉 docs
    docs_url="/docs" if settings.env != "production" else None,
    redoc_url="/redoc" if settings.env != "production" else None,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# 上線後改成實際域名
ALLOWED_ORIGINS = {
    "dev":        ["http://localhost:3000", "http://localhost:5173"],
    "staging":    ["https://staging.ots.tw"],
    "production": ["https://ots.tw", "https://www.ots.tw"],
}.get(settings.env, ["http://localhost:3000"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routers（逐步掛載）───────────────────────────────────────────────────────
# from routers import orders, files, payments, admin
# app.include_router(orders.router)
# app.include_router(files.router)
# app.include_router(payments.router)
# app.include_router(admin.router)


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health", tags=["system"])
async def health():
    """Cloud Run 健康檢查端點"""
    db_ok = await check_db_connection()
    return {
        "status": "ok" if db_ok else "degraded",
        "env": settings.env,
        "db": "connected" if db_ok else "error",
    }


@app.get("/", tags=["system"])
async def root():
    return {"service": "OTS Translation API", "version": "0.1.0"}
