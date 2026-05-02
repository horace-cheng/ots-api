"""
Root conftest.py — sets required environment variables BEFORE any project
module is imported (core/database.py calls create_async_engine at import time
and needs a non-empty DB_URL, even though it is always mocked in tests).
"""
import os

# Must be set before `core.database` is imported during test collection.
os.environ.setdefault("DB_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("GCS_TEMP_BUCKET", "test-bucket")
os.environ.setdefault("PUBSUB_TOPIC", "test-topic")
