"""
Tests for POST /admin/orders/{order_id}/redeliver

Covers the bug where `trigger_deliver_job` raised
`Unknown track type: gutenberg` because ``DELIVER_JOB_NAMES`` was
missing the ``gutenberg`` key. See change log
``2026-06-07_redeliver_gutenberg.md``.
"""
import os
os.environ.setdefault("DB_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("ENV", "test")
os.environ.setdefault("PROJECT_ID", "ots-translation")
os.environ.setdefault("REGION", "asia-east1")

import inspect
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


# ── Unit test: DELIVER_JOB_NAMES has all three track types ────────────────

def test_deliver_job_names_includes_gutenberg():
    """Regression: 'gutenberg' was missing, causing 500 on redeliver."""
    from services.pipeline import DELIVER_JOB_NAMES
    assert "gutenberg" in DELIVER_JOB_NAMES
    assert DELIVER_JOB_NAMES["gutenberg"] == "ots-gt-deliver-{env}"
    assert "fast" in DELIVER_JOB_NAMES
    assert "literary" in DELIVER_JOB_NAMES


# ── Unit test: trigger_deliver_job routes to the right job for each track ─

@pytest.mark.asyncio
async def test_trigger_deliver_job_fast_uses_ft_deliver():
    from services.pipeline import trigger_deliver_job
    with patch("google.cloud.run_v2.JobsClient") as MockClient:
        MockClient.return_value.run_job.return_value = MagicMock()
        await trigger_deliver_job("order-fast-1", "fast")
        # Verify the request was made with the fast deliver job name
        call = MockClient.return_value.run_job.call_args
        request = call.kwargs["request"]
        assert "ots-ft-deliver-test" in request.name


@pytest.mark.asyncio
async def test_trigger_deliver_job_literary_uses_lt_deliver():
    from services.pipeline import trigger_deliver_job
    with patch("google.cloud.run_v2.JobsClient") as MockClient:
        MockClient.return_value.run_job.return_value = MagicMock()
        await trigger_deliver_job("order-lt-1", "literary")
        call = MockClient.return_value.run_job.call_args
        request = call.kwargs["request"]
        assert "ots-lt-deliver-test" in request.name


@pytest.mark.asyncio
async def test_trigger_deliver_job_gutenberg_uses_gt_deliver():
    """Regression: this raised 'Unknown track type: gutenberg'."""
    from services.pipeline import trigger_deliver_job
    with patch("google.cloud.run_v2.JobsClient") as MockClient:
        MockClient.return_value.run_job.return_value = MagicMock()
        await trigger_deliver_job("order-gt-1", "gutenberg")
        call = MockClient.return_value.run_job.call_args
        request = call.kwargs["request"]
        assert "ots-gt-deliver-test" in request.name


@pytest.mark.asyncio
async def test_trigger_deliver_job_unknown_track_raises():
    from services.pipeline import trigger_deliver_job
    with pytest.raises(ValueError, match="Unknown track type: nonsense"):
        await trigger_deliver_job("order-x", "nonsense")


# ── Endpoint test: POST /admin/orders/{id}/redeliver ──────────────────────

def _set_db_row(mock_db, *, track_type: str = "gutenberg", order_id: str = "order-gt-uuid"):
    """Re-point the conftest mock_db to return the row we want from fetchone()."""
    row = MagicMock()
    row.id = order_id
    row.track_type = track_type
    result = MagicMock()
    result.fetchone.return_value = row
    mock_db.execute.return_value = result
    return row


def test_redeliver_gutenberg_calls_gt_deliver(admin_client, mock_db, monkeypatch):
    """Regression: a Gutenberg order should not 500 with 'Unknown track type'."""
    _set_db_row(mock_db, track_type="gutenberg", order_id="order-gt-uuid")

    from routers import admin as admin_mod
    calls = []
    async def fake_trigger(order_id, track_type):
        calls.append((order_id, track_type))
        return order_id
    monkeypatch.setattr(admin_mod, "trigger_deliver_job", fake_trigger)

    res = admin_client.post("/admin/orders/order-gt-uuid/redeliver")
    assert res.status_code == 200, res.text
    assert res.json() == {"message": "Deliver job triggered"}
    assert calls == [("order-gt-uuid", "gutenberg")]


def test_redeliver_404_for_missing_order(admin_client, mock_db):
    mock_db.execute.return_value.fetchone.return_value = None
    res = admin_client.post("/admin/orders/no-such-id/redeliver")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()
