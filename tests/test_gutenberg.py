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
        mock_insert_row = MagicMock()
        mock_insert_row.__getitem__.side_effect = lambda i: order_id if i == 0 else None

        mock_db.execute.side_effect = [
            MagicMock(fetchone=lambda: mock_insert_row),  # INSERT ... RETURNING id
            MagicMock(),                                   # UPDATE notes
        ]

        preview_payload = {"book_id": 1342, "word_count": 122189}

        with patch("routers.admin.gutenberg_svc.preview_book", new_callable=AsyncMock) as mock_preview, \
             patch("routers.admin.trigger_pipeline", new_callable=AsyncMock) as mock_trigger:
            mock_preview.return_value = preview_payload
            resp = admin_client.post("/admin/gutenberg/1342")

        assert resp.status_code == 200
        assert "Gutenberg book 1342 import triggered" in resp.json()["message"]
        assert order_id in resp.json()["message"]
        mock_preview.assert_awaited_once_with(1342)
        mock_trigger.assert_awaited_once_with(order_id)

    def test_import_db_error_returns_500(self, admin_client, mock_db):
        # INSERT...RETURNING returns no row (admin has no matching users row)
        mock_db.execute.side_effect = [
            MagicMock(fetchone=lambda: None),  # INSERT ... RETURNING id
        ]

        preview_payload = {"book_id": 1342, "word_count": 122189}

        with patch("routers.admin.gutenberg_svc.preview_book", new_callable=AsyncMock) as mock_preview:
            mock_preview.return_value = preview_payload
            resp = admin_client.post("/admin/gutenberg/1342")

        assert resp.status_code == 500
        assert "no matching users row" in resp.json()["detail"]

    def test_import_preview_failure_returns_502(self, admin_client, mock_db):
        with patch("routers.admin.gutenberg_svc.preview_book", new_callable=AsyncMock) as mock_preview:
            mock_preview.side_effect = ValueError("Gutenberg book 9999 not found")
            resp = admin_client.post("/admin/gutenberg/9999")

        assert resp.status_code == 502
        assert "Failed to fetch Gutenberg book" in resp.json()["detail"]


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


# ── Plain-text fallback utilities ───────────────────────────────────────────

class TestTextFallback:
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

    def test_split_drops_empty(self):
        from services.gutenberg import split_text_structured
        chunks = split_text_structured("\n\n\n   \n\n")
        assert chunks == []

    def test_split_handles_indented_chapters(self):
        from services.gutenberg import split_text_structured
        text = "                          CHAPTER I\n\nbody\n\n                          CHAPTER II\n\nbody"
        chunks = split_text_structured(text)
        assert len(chunks) == 2

    def test_split_handles_crlf_line_endings(self):
        from services.gutenberg import split_text_structured
        text = "CHAPTER I\r\n\r\nbody\r\n\r\nCHAPTER II\r\n\r\nbody\r\n"
        chunks = split_text_structured(text)
        assert len(chunks) == 2

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


# ── Chapter label classification (EPUB NCX) ───────────────────────────────

class TestIsChapterLabel:
    def _check(self, label: str) -> bool:
        from services.gutenberg import _is_chapter_label
        return _is_chapter_label(label)

    def test_chapter_with_period_and_title(self):
        assert self._check("CHAPTER I. The Beginning") is True
        assert self._check("Chapter 1. The Start") is True

    def test_chapter_bare_roman(self):
        assert self._check("CHAPTER I") is True
        assert self._check("Chapter V") is True

    def test_chapter_with_space_no_period(self):
        assert self._check("CHAPTER I The Beginning") is True
        assert self._check("CHAPTER I JONATHAN HARKER'S JOURNAL") is True

    def test_chapter_digit(self):
        assert self._check("Chapter 1") is True
        assert self._check("Chapter 12") is True

    def test_chapter_lowercase(self):
        assert self._check("chapter i") is True
        assert self._check("chapter one two") is False  # no Roman/digit

    def test_letter_prefix(self):
        assert self._check("Letter 1") is True
        assert self._check("Letter 4") is True

    def test_bare_roman_with_title(self):
        # Treasure Island style
        assert self._check("I The Old Sea-dog") is True
        assert self._check("V The Last of the Blind Man") is True

    def test_chapter_in_middle_of_corrupt_label(self):
        # Malformed NCX: previous chapter's content + new heading
        assert self._check("I hope Mr. Bingley will like it. CHAPTER II.") is True
        assert self._check("He rode a black horse. CHAPTER III.") is True

    def test_part_is_not_chapter(self):
        assert self._check("PART ONE—The Old Buccaneer") is False
        assert self._check("Part Two") is False
        assert self._check("PART 1") is False

    def test_frontmatter_is_not_chapter(self):
        assert self._check("Contents") is False
        assert self._check("CONTENTS") is False
        assert self._check("Title page") is False
        assert self._check("PREFACE") is False
        assert self._check("Preface") is False
        assert self._check("ILLUSTRATIONS") is False
        assert self._check("Dedication") is False
        assert self._check("Colophon") is False
        assert self._check("Transcriber's Note") is False

    def test_empty_is_not_chapter(self):
        assert self._check("") is False
        assert self._check("   ") is False

    def test_random_text_is_not_chapter(self):
        assert self._check("Some random prose.") is False
        assert self._check("It was the best of times.") is False


# ── HTML stripping (EPUB chapter XHTML) ────────────────────────────────────

class TestStripHtml:
    def test_strips_simple_tags(self):
        from services.gutenberg import _strip_html
        result = _strip_html("<p>Hello <b>world</b>!</p>")
        assert result == "Hello world!"

    def test_preserves_paragraph_breaks(self):
        from services.gutenberg import _strip_html
        result = _strip_html("<p>Para 1</p><p>Para 2</p>")
        assert "Para 1" in result
        assert "Para 2" in result

    def test_handles_empty_input(self):
        from services.gutenberg import _strip_html
        assert _strip_html("") == ""


# ── fetch_epub_bytes (HTTP) ────────────────────────────────────────────────

class TestFetchEpubBytes:
    @pytest.mark.asyncio
    async def test_returns_bytes_on_success(self, monkeypatch):
        from services import gutenberg
        fake_bytes = b"PK\x03\x04" + b"\x00" * 1000  # ZIP header + padding

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def get(self, url):
                resp = MagicMock()
                resp.status_code = 200
                resp.content = fake_bytes
                return resp

        monkeypatch.setattr(gutenberg.httpx, "AsyncClient", _FakeAsyncClient)
        result = await gutenberg.fetch_epub_bytes(1342)
        assert result == fake_bytes

    @pytest.mark.asyncio
    async def test_returns_none_on_404(self, monkeypatch):
        from services import gutenberg

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def get(self, url):
                resp = MagicMock()
                resp.status_code = 404
                resp.content = b""
                return resp

        monkeypatch.setattr(gutenberg.httpx, "AsyncClient", _FakeAsyncClient)
        result = await gutenberg.fetch_epub_bytes(9999)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_network_error(self, monkeypatch):
        from services import gutenberg

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def get(self, url):
                raise RuntimeError("network down")

        monkeypatch.setattr(gutenberg.httpx, "AsyncClient", _FakeAsyncClient)
        result = await gutenberg.fetch_epub_bytes(1342)
        assert result is None


# ── parse_epub (in-memory) ─────────────────────────────────────────────────

def _make_minimal_epub(opf_xml: str, ncx_xml: str, chapter_files: dict) -> bytes:
    """Build a minimal EPUB ZIP in memory for testing."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("OEBPS/content.opf", opf_xml)
        zf.writestr("OEBPS/toc.ncx", ncx_xml)
        for name, content in chapter_files.items():
            zf.writestr(f"OEBPS/{name}", content)
    return buf.getvalue()


import io
import zipfile

OPF_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>{language}</dc:language>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx"/>
</package>"""

NCX_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="1"/></head>
  <docTitle><text>Test</text></docTitle>
  <navMap>
{nav_points}
  </navMap>
</ncx>"""

def _nav_point(label: str, src: str) -> str:
    return f"""    <navPoint id="{label}" playOrder="1">
      <navLabel><text>{label}</text></navLabel>
      <content src="{src}"/>
    </navPoint>"""


class TestParseEpub:
    def test_minimal_epub_with_chapters(self):
        from services.gutenberg import parse_epub
        opf = OPF_TEMPLATE.format(title="Test Book", author="Test Author", language="en")
        ncx = NCX_TEMPLATE.format(nav_points=(
            _nav_point("CONTENTS", "contents.html") +
            _nav_point("CHAPTER I. The Start", "ch1.html") +
            _nav_point("CHAPTER II. The End", "ch2.html")
        ))
        chapters = {
            "contents.html": "<html><body>Table of contents here</body></html>",
            "ch1.html":      "<html><body><h1>Start</h1><p>First chapter text.</p></body></html>",
            "ch2.html":      "<html><body><h1>End</h1><p>Second chapter text.</p></body></html>",
        }
        epub_bytes = _make_minimal_epub(opf, ncx, chapters)
        book = parse_epub(epub_bytes, fallback_book_id=1)

        assert book["book_id"] == 1
        assert book["title"] == "Test Book"
        assert book["authors"] == ["Test Author"]
        assert book["language"] == "en"
        assert len(book["chapters"]) == 2
        assert "First chapter" in book["chapters"][0]["text"]
        assert "Second chapter" in book["chapters"][1]["text"]

    def test_epub_filters_frontmatter(self):
        from services.gutenberg import parse_epub
        opf = OPF_TEMPLATE.format(title="X", author="Y", language="en")
        ncx = NCX_TEMPLATE.format(nav_points=(
            _nav_point("Title page",   "title.html") +
            _nav_point("Contents",     "contents.html") +
            _nav_point("Illustrations", "illust.html") +
            _nav_point("Preface",      "preface.html") +
            _nav_point("CHAPTER I.",   "ch1.html") +
            _nav_point("CHAPTER II.",  "ch2.html")
        ))
        chapters = {
            "title.html":   "<html><body>Title</body></html>",
            "contents.html": "<html><body>Contents</body></html>",
            "illust.html":  "<html><body>Illust</body></html>",
            "preface.html": "<html><body>Preface</body></html>",
            "ch1.html":     "<html><body>Chapter 1</body></html>",
            "ch2.html":     "<html><body>Chapter 2</body></html>",
        }
        epub_bytes = _make_minimal_epub(opf, ncx, chapters)
        book = parse_epub(epub_bytes, fallback_book_id=1)
        assert len(book["chapters"]) == 2
        assert book["chapters"][0]["title"] == "CHAPTER I."
        assert book["chapters"][1]["title"] == "CHAPTER II."

    def test_epub_skips_parts(self):
        from services.gutenberg import parse_epub
        opf = OPF_TEMPLATE.format(title="X", author="Y", language="en")
        ncx = NCX_TEMPLATE.format(nav_points=(
            _nav_point("PART ONE",         "part1.html") +
            _nav_point("PART TWO",         "part2.html") +
            _nav_point("CHAPTER I. The Start", "ch1.html") +
            _nav_point("CHAPTER II. The End",  "ch2.html")
        ))
        chapters = {
            "part1.html": "<html><body>Part 1</body></html>",
            "part2.html": "<html><body>Part 2</body></html>",
            "ch1.html":   "<html><body>First chapter</body></html>",
            "ch2.html":   "<html><body>Second chapter</body></html>",
        }
        epub_bytes = _make_minimal_epub(opf, ncx, chapters)
        book = parse_epub(epub_bytes, fallback_book_id=1)
        assert len(book["chapters"]) == 2

    def test_epub_no_chapters_raises(self):
        from services.gutenberg import parse_epub
        opf = OPF_TEMPLATE.format(title="Essay", author="X", language="en")
        ncx = NCX_TEMPLATE.format(nav_points=(
            _nav_point("Title page", "title.html") +
            _nav_point("Contents",   "contents.html")
        ))
        chapters = {
            "title.html":   "<html><body>Title</body></html>",
            "contents.html": "<html><body>Contents</body></html>",
        }
        epub_bytes = _make_minimal_epub(opf, ncx, chapters)
        with pytest.raises(ValueError, match="No chapters"):
            parse_epub(epub_bytes, fallback_book_id=1)

    def test_epub_uses_fallback_title(self):
        from services.gutenberg import parse_epub
        opf = OPF_TEMPLATE.format(title="", author="X", language="en")
        ncx = NCX_TEMPLATE.format(nav_points=_nav_point("CHAPTER I.", "ch1.html"))
        chapters = {"ch1.html": "<html><body>text</body></html>"}
        epub_bytes = _make_minimal_epub(opf, ncx, chapters)
        book = parse_epub(epub_bytes, fallback_book_id=42)
        assert book["title"] == "Gutenberg Book 42"

    def test_epub_uses_letter_prefix(self):
        from services.gutenberg import parse_epub
        opf = OPF_TEMPLATE.format(title="Frankenstein", author="Shelley", language="en")
        ncx = NCX_TEMPLATE.format(nav_points=(
            _nav_point("Letter 1", "ch1.html") +
            _nav_point("Letter 2", "ch2.html") +
            _nav_point("Chapter 1", "ch3.html")
        ))
        chapters = {
            "ch1.html": "<html><body>First letter</body></html>",
            "ch2.html": "<html><body>Second letter</body></html>",
            "ch3.html": "<html><body>First chapter</body></html>",
        }
        epub_bytes = _make_minimal_epub(opf, ncx, chapters)
        book = parse_epub(epub_bytes, fallback_book_id=84)
        assert len(book["chapters"]) == 3
        titles = [ch["title"] for ch in book["chapters"]]
        assert titles == ["Letter 1", "Letter 2", "Chapter 1"]


# ── fetch_book (integration: EPUB primary, text fallback) ─────────────────

class TestFetchBook:
    @pytest.mark.asyncio
    async def test_uses_epub_when_available(self, monkeypatch):
        from services import gutenberg

        opf = OPF_TEMPLATE.format(title="Ebook Title", author="Ebook Author", language="en")
        ncx = NCX_TEMPLATE.format(nav_points=(
            _nav_point("CHAPTER I. Start", "ch1.html") +
            _nav_point("CHAPTER II. End",  "ch2.html")
        ))
        chapters = {
            "ch1.html": "<html><body>First</body></html>",
            "ch2.html": "<html><body>Second</body></html>",
        }
        epub_bytes = _make_minimal_epub(opf, ncx, chapters)

        with patch.object(gutenberg, "fetch_epub_bytes", new_callable=AsyncMock) as mock_epub:
            mock_epub.return_value = epub_bytes
            book = await gutenberg.fetch_book(1)

        assert book["title"] == "Ebook Title"
        assert book["authors"] == ["Ebook Author"]
        assert len(book["chapters"]) == 2

    @pytest.mark.asyncio
    async def test_falls_back_to_text_when_epub_unavailable(self, monkeypatch):
        from services import gutenberg

        text = (
            "Title: Fallback Book\n"
            "Author: Some Author\n"
            "Language: English\n"
            "*** START OF THE PROJECT GUTENBERG EBOOK FALLBACK ***\n"
            "CHAPTER I.\n\nFirst body.\n\n"
            "CHAPTER II.\n\nSecond body.\n"
        )

        with patch.object(gutenberg, "fetch_epub_bytes", new_callable=AsyncMock) as mock_epub:
            mock_epub.return_value = None
            with patch.object(gutenberg, "fetch_text", new_callable=AsyncMock) as mock_text:
                mock_text.return_value = text
                book = await gutenberg.fetch_book(1)

        assert book["title"] == "Fallback Book"
        assert book["authors"] == ["Some Author"]
        # 3 chunks: [pre-chapter, chapter 1 body, chapter 2 body]
        assert len(book["chapters"]) == 3
        assert "First body" in book["chapters"][1]["text"]
        assert "Second body" in book["chapters"][2]["text"]

    @pytest.mark.asyncio
    async def test_falls_back_to_text_when_epub_parse_fails(self, monkeypatch):
        from services import gutenberg

        text = (
            "Title: Corrupted\n"
            "Author: X\n"
            "Language: English\n"
            "*** START OF THE PROJECT GUTENBERG EBOOK X ***\n"
            "CHAPTER I.\n\nOnly chapter.\n"
        )

        with patch.object(gutenberg, "fetch_epub_bytes", new_callable=AsyncMock) as mock_epub:
            mock_epub.return_value = b"not a real epub"  # parse will fail
            with patch.object(gutenberg, "fetch_text", new_callable=AsyncMock) as mock_text:
                mock_text.return_value = text
                book = await gutenberg.fetch_book(1)

        assert book["title"] == "Corrupted"
        # 2 chunks: [pre-chapter, chapter body]
        assert len(book["chapters"]) == 2
        assert "Only chapter" in book["chapters"][1]["text"]


# ── preview_book (top-level public API) ────────────────────────────────────

class TestPreviewBook:
    @pytest.mark.asyncio
    async def test_preview_uses_chapter_count(self, monkeypatch):
        from services import gutenberg
        fake_book = {
            "book_id":  1342,
            "title":    "Pride and Prejudice",
            "authors":  ["Jane Austen"],
            "language": "en",
            "chapters": [
                {"index": 0, "title": "Chapter I",   "text": "word " * 1000},
                {"index": 1, "title": "Chapter II",  "text": "word " * 2000},
                {"index": 2, "title": "Chapter III", "text": "word " * 500},
            ],
        }
        with patch.object(gutenberg, "fetch_book", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = fake_book
            preview = await gutenberg.preview_book(1342)

        assert preview["book_id"] == 1342
        assert preview["title"] == "Pride and Prejudice"
        assert preview["num_chapters"] == 3
        assert preview["num_chunks"] == 3
        assert preview["word_count"] == 3500
