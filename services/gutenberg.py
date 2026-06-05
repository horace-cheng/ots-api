"""
services/gutenberg.py

Project Gutenberg client.

Primary path: EPUB (gutenberg.org/ebooks/{id}.epub.noimages).
  - OPF metadata gives title/author/language directly (no header parsing)
  - NCX navPoints give structured chapter list (no regex guessing)
  - Each chapter XHTML becomes one chunk — clean boundaries

Fallback: plain text (gutenberg.org/cache/epub/{id}/pg{id}.txt etc.)
  - For books without EPUB, or corrupt NCX
  - Parses PG standard text header for metadata
  - Uses chapter regex on text body
"""
import asyncio
import io
import logging
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from typing import List, Optional
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

EPUB_URL_PATTERN  = "https://www.gutenberg.org/ebooks/{id}.epub.noimages"
TEXT_URL_PATTERNS = [
    "https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt",
    "https://www.gutenberg.org/files/{id}/{id}-0.txt",
    "https://www.gutenberg.org/files/{id}/{id}.txt",
]

# ── Plain-text chapter detection (fallback) ─────────────────────────────────
CHAPTER_RE = re.compile(
    r'^[ \t]*CHAPTER[ \t]+[IVXLCDM\d]+[^\n]*$',
    re.IGNORECASE | re.MULTILINE,
)
META_RE     = re.compile(r'^(Title|Author|Language)\s*:\s*(.+?)\s*$', re.MULTILINE)
START_MARKER = re.compile(
    r'\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG', re.IGNORECASE
)
LANG_NAME_TO_CODE = {
    "english":  "en", "french":  "fr", "german":   "de", "spanish":  "es",
    "italian":  "it", "portuguese": "pt", "chinese": "zh", "japanese": "ja",
    "dutch":    "nl", "finnish": "fi", "swedish":  "sv", "latin":    "la",
    "russian":  "ru", "greek":   "el",
}

# ── EPUB chapter classification ──────────────────────────────────────────────
# Anchored match: must start the label. Catches "CHAPTER I", "Chapter 1",
# "Letter 1", "CHAPTER I. Title", "I The Old Sea-dog" (bare Roman), etc.
CHAPTER_ANCHORED_RE = re.compile(
    r'^(?:(chapter|letter)\s+)?([IVXLCDM]+|\d+)(?=[\s\.\-:]|$)',
    re.IGNORECASE,
)
# Anywhere match: for malformed NCX labels like "I hope... CHAPTER II."
# where the previous chapter's content is concatenated with the new heading.
CHAPTER_ANYWHERE_RE = re.compile(
    r'\b(CHAPTER|Chapter|chapter|Letter|LETTER)\s+([IVXLCDM]+|\d+)\b'
)
PART_RE = re.compile(r'^part\s+[a-z0-9]+', re.IGNORECASE)
FRONTMATTER_TERMS = (
    "title page", "contents", "illustrations", "preface", "foreword",
    "dedication", "colophon", "transcriber", "epigraph",
    "advertisement", "errata", "imprint",
)

# XML namespaces
NS_NCX = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
NS_OPF = {"opf": "http://www.idpf.org/2007/opf",
          "dc":  "http://purl.org/dc/elements/1.1/"}
NS_HTML = {"x": "http://www.w3.org/1999/xhtml"}


# ── HTML stripping (for EPUB chapter XHTML) ────────────────────────────────

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_html(html: str) -> str:
    """Strip HTML tags from a chapter XHTML, preserving text content."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = "".join(parser.parts)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


# ── HTTP helpers ────────────────────────────────────────────────────────────

async def _get_with_retry(
    client: httpx.AsyncClient, url: str, max_attempts: int = 3
) -> httpx.Response:
    delay = 0.5
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await client.get(url)
        except (httpx.RequestError, httpx.RemoteProtocolError) as e:
            last_exc = e
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
    raise last_exc  # type: ignore[misc]


# ── Plain-text metadata + chunking (fallback) ───────────────────────────────

def _lang_to_code(name: str) -> str:
    return LANG_NAME_TO_CODE.get(name.lower(), name.lower()[:2] or "en")


def parse_header_metadata(text: str, fallback_book_id: int = 0) -> dict:
    """Extract title/authors/language from the PG text header (fallback)."""
    head = text[:2000]
    start = START_MARKER.search(head)
    header = head[: start.start()] if start else head

    title = ""
    authors: List[str] = []
    language = ""

    for match in META_RE.finditer(header):
        key, value = match.group(1), match.group(2).strip()
        if key == "Title" and not title:
            title = value
        elif key == "Author" and not authors:
            authors = [
                a.strip()
                for a in re.split(r',\s*(?![^()]*\))', value)
                if a.strip()
            ]
        elif key == "Language" and not language:
            language = _lang_to_code(value)

    return {
        "title":    title or f"Gutenberg Book {fallback_book_id}",
        "authors":  authors,
        "language": language or "en",
    }


def split_text_structured(text: str) -> List[str]:
    """Split text into structural chunks. Priority: chapters -> paragraphs -> sentences."""
    chapter_splits = CHAPTER_RE.split(text)
    if len(chapter_splits) > 1:
        return _clean_chunks(chapter_splits)
    paragraph_splits = re.split(r'\n\s*\n', text)
    if len(paragraph_splits) > 1:
        return _clean_chunks(paragraph_splits)
    sentence_splits = re.split(r'(?<=[.!?])\s+', text)
    return _clean_chunks(sentence_splits)


def _clean_chunks(raw: List[str]) -> List[str]:
    return [c.strip() for c in raw if c and c.strip()]


def count_words(text: str) -> int:
    return len(re.findall(r'\b\w+\b', text))


# ── Plain-text fetching (fallback) ──────────────────────────────────────────

async def fetch_text(book_id: int) -> str:
    """Fetch plain-text body of a Gutenberg book. Tries multiple URL patterns."""
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for pattern in TEXT_URL_PATTERNS:
            url = pattern.format(id=book_id)
            try:
                resp = await _get_with_retry(client, url)
                if resp.status_code == 200:
                    logger.info(f"Fetched book {book_id} from {url}")
                    return resp.text
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    continue
                raise
        raise ValueError(
            f"No text file found for Gutenberg book {book_id} "
            f"(tried {len(TEXT_URL_PATTERNS)} patterns)"
        )


# ── EPUB fetching (primary) ────────────────────────────────────────────────

async def fetch_epub_bytes(book_id: int) -> Optional[bytes]:
    """Download EPUB, return raw bytes or None on failure."""
    url = EPUB_URL_PATTERN.format(id=book_id)
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await _get_with_retry(client, url)
            if resp.status_code == 200 and len(resp.content) > 1000:
                logger.info(f"Fetched EPUB for book {book_id} ({len(resp.content)} bytes)")
                return resp.content
    except Exception as e:
        logger.warning(f"Failed to download EPUB for book {book_id}: {e}")
    return None


def _parse_opf(zf: zipfile.ZipFile) -> dict:
    opf_name = next((n for n in zf.namelist() if n.endswith(".opf")), None)
    if not opf_name:
        raise ValueError("No .opf in EPUB")
    root = ET.fromstring(zf.read(opf_name).decode("utf-8"))
    title    = root.find(".//dc:title",    NS_OPF)
    creator  = root.find(".//dc:creator",  NS_OPF)
    language = root.find(".//dc:language", NS_OPF)
    return {
        "title":    (title.text   or "").strip(),
        "authors":  [creator.text.strip()] if creator is not None and creator.text else [],
        "language": (
            (language.text or "en").split("-")[0].lower()
            if language is not None and language.text else "en"
        ),
    }


def _parse_nav(zf: zipfile.ZipFile) -> tuple[list[dict], str | None]:
    """Parse NCX (EPUB2) or nav.xhtml (EPUB3). Returns (navpoints, ref_path).

    Each navpoint has:
        "label": str           — display label
        "src":   str           — HTML file path (may have fragment stripped)
        "anchor": str | None   — fragment id without '#' (e.g. "pgepubid00008")
                                 None when src has no fragment or nav is EPUB3 <a href>
    """
    ncx_name = next((n for n in zf.namelist() if n.endswith(".ncx")), None)
    if ncx_name:
        root = ET.fromstring(zf.read(ncx_name).decode("utf-8"))
        result = []
        for np in root.findall(".//ncx:navPoint", NS_NCX):
            label = np.find("ncx:navLabel/ncx:text", NS_NCX)
            content = np.find("ncx:content", NS_NCX)
            if label is None or content is None:
                continue
            raw_src = content.get("src") or ""
            if "#" in raw_src:
                src, _, anchor = raw_src.partition("#")
            else:
                src, anchor = raw_src, None
            result.append({
                "label":  (label.text or "").strip(),
                "src":    src,
                "anchor": anchor or None,
            })
        return result, ncx_name

    nav_name = next((n for n in zf.namelist() if n.endswith("nav.xhtml")), None)
    if nav_name:
        root = ET.fromstring(zf.read(nav_name).decode("utf-8"))
        result = []
        for a in root.findall(".//x:a", NS_HTML):
            label = "".join(a.itertext()).strip()
            raw_href = a.get("href") or ""
            if "#" in raw_href:
                src, _, anchor = raw_href.partition("#")
            else:
                src, anchor = raw_href, None
            if label and src:
                result.append({"label": label, "src": src, "anchor": anchor or None})
        return result, nav_name

    return [], None


def _is_chapter_label(label: str) -> bool:
    """Decide if an EPUB navPoint label represents a real chapter."""
    label_clean = label.strip().lower()
    if not label_clean:
        return False
    if PART_RE.match(label_clean):
        return False
    if any(label_clean == t or label_clean.startswith(t + " ") for t in FRONTMATTER_TERMS):
        return False
    if CHAPTER_ANCHORED_RE.match(label.strip()):
        return True
    if CHAPTER_ANYWHERE_RE.search(label):
        return True
    return False


def _resolve_epub_path(zf: zipfile.ZipFile, src: str, ref_path: str) -> Optional[str]:
    if not src:
        return None
    ref_dir = posixpath.dirname(ref_path)
    full = posixpath.normpath(posixpath.join(ref_dir, src)) if ref_dir else src
    return full if full in zf.namelist() else None


def _extract_anchored_section(xhtml: str, anchor: Optional[str]) -> str:
    """
    If anchor is None, return the full XHTML unchanged.
    Otherwise extract the slice of XHTML that begins at the element
    with id="anchor" and ends at the next element with any id attribute
    (or the end of the document). Used to handle EPUBs where multiple
    navPoints point into the same XHTML file using #fragment anchors —
    without this, the whole file would be returned for every chapter and
    the same text would be counted multiple times.
    """
    if not anchor:
        return xhtml
    # Split on the start of any element carrying an id="..." attribute
    # (covers <a id="...">, <div id="...">, <h1 id="...">, etc.).
    parts = re.split(r'(<[^>]*\sid="[^"]+"[^>]*>)', xhtml)
    start_idx = None
    for i, part in enumerate(parts):
        if f'id="{anchor}"' in part:
            start_idx = i
            break
    if start_idx is None:
        return xhtml
    # Find next part that opens another id="..." element
    for j in range(start_idx + 1, len(parts)):
        if re.match(r'<[^>]*\sid="[^"]+"', parts[j]):
            return "".join(parts[start_idx:j])
    return "".join(parts[start_idx:])


def parse_epub(epub_bytes: bytes, fallback_book_id: int = 0) -> dict:
    """
    Parse an EPUB and return book structure:
      {
        "book_id": int,
        "title": str,
        "authors": list[str],
        "language": str,
        "chapters": [{"index": int, "title": str, "text": str}, ...],
      }
    """
    zf = zipfile.ZipFile(io.BytesIO(epub_bytes))
    metadata = _parse_opf(zf)
    nav_points, ref_path = _parse_nav(zf)
    if not nav_points or not ref_path:
        raise ValueError("EPUB has no NCX/nav TOC")

    chapters: List[dict] = []
    for np in nav_points:
        if not _is_chapter_label(np["label"]):
            continue
        full_path = _resolve_epub_path(zf, np["src"], ref_path)
        if not full_path:
            continue
        try:
            xhtml = zf.read(full_path).decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Failed to read {full_path}: {e}")
            continue
        section = _extract_anchored_section(xhtml, np.get("anchor"))
        text = _strip_html(section)
        if not text:
            continue
        chapters.append({
            "index": len(chapters),
            "title": np["label"],
            "text":  text,
        })

    if not chapters:
        raise ValueError("No chapters extracted from EPUB (NCX had no chapter navPoints)")

    return {
        "book_id":  fallback_book_id,
        "title":    metadata["title"] or f"Gutenberg Book {fallback_book_id}",
        "authors":  metadata["authors"],
        "language": metadata["language"] or "en",
        "chapters": chapters,
    }


# ── Unified book fetcher (EPUB primary, text fallback) ──────────────────────

async def fetch_book(book_id: int) -> dict:
    """
    Fetch a Gutenberg book and return structured data with chapters.
    Tries EPUB first; falls back to plain text if EPUB unavailable.
    """
    epub_bytes = await fetch_epub_bytes(book_id)
    if epub_bytes:
        try:
            return parse_epub(epub_bytes, fallback_book_id=book_id)
        except Exception as e:
            logger.warning(f"EPUB parse failed for book {book_id}: {e}, falling back to text")

    text = await fetch_text(book_id)
    meta = parse_header_metadata(text, fallback_book_id=book_id)
    chunks = split_text_structured(text)
    chapters = [
        {"index": i, "title": f"Part {i+1}", "text": c}
        for i, c in enumerate(chunks)
    ]
    return {
        "book_id":  book_id,
        "title":    meta["title"],
        "authors":  meta["authors"],
        "language": meta["language"],
        "chapters": chapters,
    }


# ── Public API used by the admin preview endpoint ──────────────────────────

async def preview_book(book_id: int) -> dict:
    """
    Fetch book from Gutenberg and return preview payload matching
    GutenbergBookInfo (book_id, title, authors, language,
    word_count, num_chapters, num_chunks).
    """
    book = await fetch_book(book_id)
    chapters = book["chapters"]
    word_count = sum(count_words(ch["text"]) for ch in chapters)
    return {
        "book_id":      book_id,
        "title":        book["title"],
        "authors":      book["authors"],
        "language":     book["language"],
        "word_count":   word_count,
        "num_chapters": len(chapters),
        "num_chunks":   len(chapters),
    }


async def fetch_metadata(book_id: int) -> dict:
    """Fetch book metadata only (title, authors, language)."""
    book = await fetch_book(book_id)
    return {
        "book_id":  book_id,
        "title":    book["title"],
        "authors":  book["authors"],
        "language": book["language"],
    }
