import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from routers.auth import get_admin_user
from routers.admin import router
from tests.factories import MOCK_ADMIN_USER
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.usefixtures("mock_notification_publisher")

@pytest.fixture
def admin_client(mock_db):
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_admin_user] = lambda: MOCK_ADMIN_USER

    return TestClient(app)

class TestImportGutenbergBook:
    def test_import_success(self, admin_client, mock_db):
        order_id = "order-gutenberg-1342"
        mock_order_row = MagicMock()
        mock_order_row.__getitem__.side_effect = lambda i: order_id if i == 0 else None

        mock_db.execute.side_effect = [
            MagicMock(), # INSERT
            MagicMock(fetchone=lambda: mock_order_row), # SELECT id
            MagicMock(), # UPDATE notes
        ]

        with patch("routers.admin.trigger_pipeline", new_callable=AsyncMock) as mock_trigger:
            resp = admin_client.post("/admin/gutenberg/1342")

        assert resp.status_code == 200
        assert "Gutenberg book 1342 import triggered" in resp.json()["message"]
        assert order_id in resp.json()["message"]
        mock_trigger.assert_awaited_once_with(order_id)

    def test_import_db_error_returns_500(self, admin_client, mock_db):
        mock_db.execute.side_effect = [
            MagicMock(), # INSERT
            MagicMock(fetchone=lambda: None), # SELECT id fails
        ]

        resp = admin_client.post("/admin/gutenberg/1342")
        assert resp.status_code == 500
        # The error message should be in "detail" for 500 errors, not "message"
        assert "Failed to create Gutenberg order" in resp.json()["detail"]


class TestPreviewGutenbergBook:
    def test_preview_success(self, admin_client):
        preview_payload = {
            "book_id":      1342,
            "title":        "Pride and Prejudice",
            "authors":      ["Austen, Jane"],
            "language":     "en",
            "word_count":   122189,
            "num_chapters": 61,
            "num_chunks":   61,
        }
        with patch("routers.admin.gutenberg_svc.preview_book", new_callable=AsyncMock) as mock_preview:
            mock_preview.return_value = preview_payload
            resp = admin_client.get("/admin/gutenberg/1342")

        assert resp.status_code == 200
        body = resp.json()
        assert body["book_id"] == 1342
        assert body["title"] == "Pride and Prejudice"
        assert body["authors"] == ["Austen, Jane"]
        assert body["word_count"] == 122189
        assert body["num_chunks"] == 61
        mock_preview.assert_awaited_once_with(1342)

    def test_preview_not_found_returns_404(self, admin_client):
        with patch("routers.admin.gutenberg_svc.preview_book", new_callable=AsyncMock) as mock_preview:
            mock_preview.side_effect = ValueError("Gutenberg book 9999 not found")
            resp = admin_client.get("/admin/gutenberg/9999")

        assert resp.status_code == 404
        assert "9999" in resp.json()["detail"]

    def test_preview_gutendex_failure_returns_502(self, admin_client):
        with patch("routers.admin.gutenberg_svc.preview_book", new_callable=AsyncMock) as mock_preview:
            mock_preview.side_effect = RuntimeError("connection timeout")
            resp = admin_client.get("/admin/gutenberg/1342")

        assert resp.status_code == 502
        assert "Gutendex" in resp.json()["detail"]


class TestGutenbergService:
    """Unit tests for the gutenberg service module (no HTTP mocking)."""

    def test_split_text_structured_chapters(self):
        from services.gutenberg import split_text_structured
        text = (
            "CHAPTER I.\n\nFirst chapter body.\n\n"
            "CHAPTER II.\n\nSecond chapter body.\n\n"
            "CHAPTER III.\n\nThird chapter body."
        )
        chunks = split_text_structured(text)
        assert len(chunks) >= 3
        assert any("First chapter" in c for c in chunks)
        assert any("Second chapter" in c for c in chunks)

    def test_split_text_structured_paragraphs(self):
        from services.gutenberg import split_text_structured
        text = "Para one.\n\nPara two.\n\nPara three."
        chunks = split_text_structured(text)
        assert len(chunks) == 3

    def test_split_text_structured_sentences(self):
        from services.gutenberg import split_text_structured
        text = "First sentence. Second sentence. Third sentence."
        chunks = split_text_structured(text)
        assert len(chunks) == 3

    def test_count_words(self):
        from services.gutenberg import count_words
        assert count_words("Hello, world! Foo bar.") == 4
        assert count_words("") == 0

    def test_count_chapters(self):
        from services.gutenberg import count_chapters
        text = (
            "CHAPTER I.\n\nBody.\n\n"
            "CHAPTER II.\n\nBody.\n\n"
            "CHAPTER III.\n\nBody."
        )
        assert count_chapters(text) == 3
        assert count_chapters("No chapters here, just text.") == 0

    def test_split_text_structured_drops_empty(self):
        from services.gutenberg import split_text_structured
        chunks = split_text_structured("\n\n\n   \n\n")
        assert chunks == []

    @pytest.mark.asyncio
    async def test_fetch_text_follows_redirects(self, monkeypatch):
        """Client must be configured with follow_redirects=True."""
        from services import gutenberg

        captured_kwargs: dict = {}

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                captured_kwargs.update(kwargs)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def get(self, url):
                resp = MagicMock()
                resp.status_code = 200
                resp.text = "body"
                return resp

        monkeypatch.setattr(gutenberg.httpx, "AsyncClient", _FakeAsyncClient)
        await gutenberg.fetch_text(139)
        assert captured_kwargs.get("follow_redirects") is True

    @pytest.mark.asyncio
    async def test_fetch_text_first_pattern_succeeds(self, monkeypatch):
        from services import gutenberg

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def get(self, url):
                resp = MagicMock()
                resp.status_code = 200
                resp.text = "first"
                return resp

        monkeypatch.setattr(gutenberg.httpx, "AsyncClient", _FakeAsyncClient)
        result = await gutenberg.fetch_text(1342)
        assert result == "first"

    @pytest.mark.asyncio
    async def test_fetch_text_falls_back_to_next_pattern(self, monkeypatch):
        from services import gutenberg

        urls_called: list = []

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def get(self, url):
                urls_called.append(url)
                resp = MagicMock()
                if len(urls_called) == 1:
                    resp.status_code = 404
                else:
                    resp.status_code = 200
                    resp.text = "second"
                return resp

        monkeypatch.setattr(gutenberg.httpx, "AsyncClient", _FakeAsyncClient)
        result = await gutenberg.fetch_text(1342)
        assert result == "second"
        assert len(urls_called) == 2
        assert urls_called[0] != urls_called[1]

    @pytest.mark.asyncio
    async def test_fetch_text_all_404_raises_value_error(self, monkeypatch):
        from services import gutenberg

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def get(self, url):
                resp = MagicMock()
                resp.status_code = 404
                return resp

        monkeypatch.setattr(gutenberg.httpx, "AsyncClient", _FakeAsyncClient)
        with pytest.raises(ValueError, match="No text file found"):
            await gutenberg.fetch_text(9999)

    def test_parse_header_metadata_extracts_title_author_language(self):
        from services.gutenberg import parse_header_metadata
        text = (
            "Title: Pride and Prejudice\n"
            "Author: Jane Austen\n"
            "Language: English\n"
            "*** START OF THE PROJECT GUTENBERG EBOOK PRIDE AND PREJUDICE ***\n"
            "CHAPTER I.\n"
        )
        meta = parse_header_metadata(text, fallback_book_id=1342)
        assert meta["title"] == "Pride and Prejudice"
        assert meta["authors"] == ["Jane Austen"]
        assert meta["language"] == "en"

    def test_parse_header_metadata_multiple_authors(self):
        from services.gutenberg import parse_header_metadata
        text = "Title: Test\nAuthor: A. Author, B. Author\nLanguage: French\n"
        meta = parse_header_metadata(text, fallback_book_id=1)
        assert meta["authors"] == ["A. Author", "B. Author"]
        assert meta["language"] == "fr"

    def test_parse_header_metadata_author_with_translator(self):
        """Parenthesized roles should not be split as new authors."""
        from services.gutenberg import parse_header_metadata
        text = "Title: Les Misérables\nAuthor: Victor Hugo, Isabel F. Hapgood (Translator)\nLanguage: French\n"
        meta = parse_header_metadata(text, fallback_book_id=1)
        assert meta["authors"] == ["Victor Hugo", "Isabel F. Hapgood (Translator)"]

    def test_parse_header_metadata_missing_uses_fallback(self):
        from services.gutenberg import parse_header_metadata
        text = "Just some body, no header."
        meta = parse_header_metadata(text, fallback_book_id=42)
        assert meta["title"] == "Gutenberg Book 42"
        assert meta["authors"] == []
        assert meta["language"] == "en"