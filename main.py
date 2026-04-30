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


# ── Lifespan ───────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting OTS API Backend [env={settings.env}]")
    logger.info(f"Payment gateway: {settings.payment_gateway}")

    init_firebase()

    db_ok = await check_db_connection()
    if not db_ok:
        logger.error("DB connection failed on startup — check Cloud SQL Auth Proxy")
    else:
        logger.info("DB connection OK")

    yield

    logger.info("OTS API Backend shutting down")


# ── App ───────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "OTS Translation API",
    version     = "0.1.0",
    description = "Original Tale Studio — Translation Service Backend",
    lifespan    = lifespan,
    docs_url    = "/docs"  if settings.env != "production" else None,
    redoc_url   = "/redoc" if settings.env != "production" else None,
)

# ── CORS ───────────────────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS = {
    "dev":        ["http://localhost:3000", "http://localhost:5173", settings.web_portal_url],
    "staging":    ["https://staging.ots.tw", settings.web_portal_url],
    "production": ["https://ots.tw", "https://www.ots.tw", settings.web_portal_url],
}.get(settings.env, ["http://localhost:3000", settings.web_portal_url])

app.add_middleware(
    CORSMiddleware,
    allow_origins     = ALLOWED_ORIGINS,
    allow_origin_regex = r"https://ots-frontend-.*\.run\.app",
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────────────────
from routers import orders, files, payments, admin, internal

app.include_router(orders.router)
app.include_router(files.router)
app.include_router(payments.router)
app.include_router(admin.router)
app.include_router(internal.router)

# ── Health / Root ──────────────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["system"])
async def health():
    db_ok = await check_db_connection()
    return {
        "status":          "ok" if db_ok else "degraded",
        "env":             settings.env,
        "db":              "connected" if db_ok else "error",
        "payment_gateway": settings.payment_gateway,
    }

@app.get("/", tags=["system"])
async def root():
    return {"service": "OTS Translation API", "version": "0.1.0"}