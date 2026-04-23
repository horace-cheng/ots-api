from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text
from core.config import settings
import logging

logger = logging.getLogger(__name__)

# ── Engine ────────────────────────────────────────────────────────────────────
# pool_size × max-instances <= Cloud SQL max_connections
# dev: 2 × 5 instances = 10 connections（db-f1-micro 上限 25）
engine = create_async_engine(
    settings.db_url,
    pool_size=2,
    max_overflow=1,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,   # 每次取出連線前先 ping，避免 stale connection
    echo=(settings.env == "dev"),  # dev 環境輸出 SQL log
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ── Dependency ────────────────────────────────────────────────────────────────
async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# ── Health check ──────────────────────────────────────────────────────────────
async def check_db_connection() -> bool:
    """啟動時確認 Cloud SQL 連線正常"""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"DB connection failed: {e}")
        return False
