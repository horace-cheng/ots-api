"""
Unit tests for POST /admin/orders/{id}/rerun-stage endpoint.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_admin_user
from routers.admin import router
from tests.factories import MOCK_ADMIN_USER
from services.pipeline import RERUN_STAGE_JOBS, RERUN_STAGE_ORDER


@pytest.fixture
def admin_client(mock_db):
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_admin_user] = lambda: MOCK_ADMIN_USER
    return TestClient(app)


def _gutenberg_row(order_id: str = "order-gutenberg-001"):
    row = MagicMock()
    row.id = order_id
    row.track_type = "gutenberg"
    return row


class TestRerunStage:
    def test_invalid_stage_returns_400(self, admin_client, mock_db):
        resp = admin_client.post(
            "/admin/orders/order-001/rerun-stage",
            json={"stage": "bogus"},
        )
        assert resp.status_code == 400
        assert "Invalid stage" in resp.json()["detail"]
        assert "bogus" in resp.json()["detail"]

    def test_order_not_found_returns_404(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = None
        resp = admin_client.post(
            "/admin/orders/nonexistent/rerun-stage",
            json={"stage": "simplify"},
        )
        assert resp.status_code == 404

    def test_non_gutenberg_order_returns_400(self, admin_client, mock_db):
        row = MagicMock()
        row.id = "order-fast-001"
        row.track_type = "fast"
        mock_db.execute.return_value.fetchone.return_value = row
        resp = admin_client.post(
            "/admin/orders/order-fast-001/rerun-stage",
            json={"stage": "simplify"},
        )
        assert resp.status_code == 400
        assert "Gutenberg" in resp.json()["detail"]

    def test_simplify_triggers_correct_job(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _gutenberg_row()

        with patch("routers.admin.trigger_rerun_stage", new_callable=AsyncMock) as mock_trigger:
            mock_trigger.return_value = "ots-gt-simplify-dev"
            resp = admin_client.post(
                "/admin/orders/order-gutenberg-001/rerun-stage",
                json={"stage": "simplify"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "simplify" in body["message"]
        assert "ots-gt-simplify-dev" in body["message"]
        mock_trigger.assert_awaited_once_with("order-gutenberg-001", "simplify")

    def test_tailo_triggers_correct_job(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _gutenberg_row()

        with patch("routers.admin.trigger_rerun_stage", new_callable=AsyncMock) as mock_trigger:
            mock_trigger.return_value = "ots-gt-tailo-dev"
            resp = admin_client.post(
                "/admin/orders/order-gutenberg-001/rerun-stage",
                json={"stage": "tailo"},
            )

        assert resp.status_code == 200
        mock_trigger.assert_awaited_once_with("order-gutenberg-001", "tailo")

    def test_deliver_passes_redeliver_env(self, admin_client, mock_db):
        """deliver is the one stage that needs REDELIVER=true env var."""
        mock_db.execute.return_value.fetchone.return_value = _gutenberg_row()

        with patch("routers.admin.trigger_rerun_stage", new_callable=AsyncMock) as mock_trigger:
            mock_trigger.return_value = "ots-gt-deliver-dev"
            resp = admin_client.post(
                "/admin/orders/order-gutenberg-001/rerun-stage",
                json={"stage": "deliver"},
            )

        assert resp.status_code == 200
        mock_trigger.assert_awaited_once_with("order-gutenberg-001", "deliver")
        # The actual REDELIVER env var is set inside trigger_rerun_stage
        # via RERUN_STAGE_JOBS — covered by the integration logic, not this
        # endpoint test.

    def test_all_triggers_all_seven_stages(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _gutenberg_row()

        with patch("routers.admin.trigger_rerun_stage", new_callable=AsyncMock) as mock_trigger:
            mock_trigger.return_value = ",".join(
                f"ots-gt-{s}-dev" for s in RERUN_STAGE_ORDER
            )
            resp = admin_client.post(
                "/admin/orders/order-gutenberg-001/rerun-stage",
                json={"stage": "all"},
            )

        assert resp.status_code == 200
        mock_trigger.assert_awaited_once_with("order-gutenberg-001", "all")
        assert "all" in resp.json()["message"]
        assert "7 stages" in resp.json()["message"]

    def test_chapter_splitter_triggers_correct_job(self, admin_client, mock_db):
        """``chapter_splitter`` is the new LLM-driven stage between fetcher
        and extract_terms."""
        mock_db.execute.return_value.fetchone.return_value = _gutenberg_row()

        with patch("routers.admin.trigger_rerun_stage", new_callable=AsyncMock) as mock_trigger:
            mock_trigger.return_value = "ots-gt-chapter-splitter-dev"
            resp = admin_client.post(
                "/admin/orders/order-gutenberg-001/rerun-stage",
                json={"stage": "chapter_splitter"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "chapter_splitter" in body["message"]
        assert "ots-gt-chapter-splitter-dev" in body["message"]
        mock_trigger.assert_awaited_once_with("order-gutenberg-001", "chapter_splitter")

    def test_each_valid_stage_accepted(self, admin_client, mock_db):
        mock_db.execute.return_value.fetchone.return_value = _gutenberg_row()
        stages = ["fetcher", "chapter_splitter", "extract_terms", "translate",
                  "simplify", "tailo", "deliver", "all"]
        for stage in stages:
            mock_db.reset_mock()
            mock_db.execute.return_value.fetchone.return_value = _gutenberg_row()
            with patch("routers.admin.trigger_rerun_stage", new_callable=AsyncMock) as t:
                t.return_value = f"ots-gt-{stage}-dev"
                resp = admin_client.post(
                    "/admin/orders/order-gutenberg-001/rerun-stage",
                    json={"stage": stage},
                )
            assert resp.status_code == 200, f"stage={stage}: {resp.json()}"

    def test_each_stage_maps_to_correct_job_template(self):
        """Direct check of the RERUN_STAGE_JOBS table — guards against
        silent breakage if a new stage is added without updating the
        helper."""
        assert RERUN_STAGE_JOBS["fetcher"][0]            == "ots-gt-fetcher-{env}"
        assert RERUN_STAGE_JOBS["chapter_splitter"][0]   == "ots-gt-chapter-splitter-{env}"
        assert RERUN_STAGE_JOBS["extract_terms"][0]      == "ots-gt-extract-terms-{env}"
        assert RERUN_STAGE_JOBS["translate"][0]          == "ots-gt-translate-{env}"
        assert RERUN_STAGE_JOBS["simplify"][0]           == "ots-gt-simplify-{env}"
        assert RERUN_STAGE_JOBS["tailo"][0]              == "ots-gt-tailo-{env}"
        assert RERUN_STAGE_JOBS["deliver"][0]            == "ots-gt-deliver-{env}"
        # deliver stage sets REDELIVER=true so the job reuses existing data
        assert RERUN_STAGE_JOBS["deliver"][1] == {"REDELIVER": "true"}
        # other stages have no extra env
        for s in ("fetcher", "chapter_splitter", "extract_terms",
                  "translate", "simplify", "tailo"):
            assert RERUN_STAGE_JOBS[s][1] == {}

    def test_rerun_stage_order_full_pipeline(self):
        assert RERUN_STAGE_ORDER == [
            "fetcher", "chapter_splitter", "extract_terms", "translate",
            "simplify", "tailo", "deliver",
        ]
