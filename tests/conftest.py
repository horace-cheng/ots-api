import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone


MOCK_FIREBASE_DECODED = {
    "uid": "firebase-uid-001",
    "email": "user@ots.tw",
    "email_verified": True,
    "name": "Test User",
}

MOCK_USER = {
    "uid": "firebase-uid-001",
    "email": "user@ots.tw",
    "user_id": "db-user-id-001",
    "client_type": "b2c",
}

MOCK_ADMIN_USER = {
    "uid": "admin-uid-001",
    "email": "admin@ots.tw",
    "user_id": "admin-db-id-001",
    "client_type": "b2c",
}


@pytest.fixture
def mock_db():
    """Async mock for SQLAlchemy AsyncSession with sensible defaults."""
    db = AsyncMock()

    user_row = MagicMock()
    user_row.id = "db-user-id-001"
    user_row.client_type = "b2c"

    result = MagicMock()
    result.fetchone.return_value = user_row
    result.fetchall.return_value = []
    result.scalar.return_value = 0
    db.execute.return_value = result
    return db


@pytest.fixture
def ecpay_settings(monkeypatch):
    """Inject ECPay sandbox credentials into the settings singleton."""
    from core.config import settings
    monkeypatch.setattr(settings, "ecpay_merchant_id", "2000132")
    monkeypatch.setattr(settings, "ecpay_hash_key", "5294y06JbISpM5x9")
    monkeypatch.setattr(settings, "ecpay_hash_iv", "v77hoKGq4kWxNNIS")
    monkeypatch.setattr(settings, "ecpay_sandbox", True)
