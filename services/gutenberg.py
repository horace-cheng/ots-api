"""
services/gutenberg.py

Project Gutenberg client that hits gutenberg.org directly (no third-party
dependency on gutendex.com). Parses metadata (title, authors, language) from
the standard PG text header, which is always present in the first ~2KB of
every book file.

URL patterns tried in order:
  1. https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt    (modern UTF-8)
  2. https://www.gutenberg.org/files/{id}/{id}-0.txt         (older multi-file)
  3. https://www.gutenberg.org/files/{id}/{id}.txt           (simple)
"""
import asyncio
import logging
import re
from typing import List

import httpx

logger = logging.getLogger(__name__)

TEXT_URL_PATTERNS = [
    "https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt",
    "https://www.gutenberg.org/files/{id}/{id}-0.txt",
    "https://www.gutenberg.org/files/{id}/{id}.txt",
]

CHAPTER_RE = re.compile(
    r'^[ \t]*CHAPTER[ \t]+[IVXLCDM\d]+[^\n]*$',
    re.IGNORECASE | re.MULTILINE,
)
META_RE = re.compile(r'^(Title|Author|Language)\s*:\s*(.+?)\s*$', re.MULTILINE)
START_MARKER = re.compile(
    r'\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG', re.IGNORECASE
)

LANG_NAME_TO_CODE = {
    "english":  "en", "french":  "fr", "german":   "de", "spanish":  "es",
    "italian":  "it", "portuguese": "pt", "chinese": "zh", "japanese": "ja",
    "dutch":    "nl", "finnish": "fi", "swedish":  "sv", "latin":    "la",
    "russian":  "ru", "greek":   "el",
}


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, max_attempts: int = 3
) -> httpx.Response:
    """GET with exponential backoff. Re-raises HTTPStatusError on 4xx/5xx."""
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


async def fetch_text(book_id: int) -> str:
    """Fetch plain-text body of a Gutenberg book, trying multiple URL patterns."""
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


def _lang_to_code(name: str) -> str:
    return LANG_NAME_TO_CODE.get(name.lower(), name.lower()[:2] or "en")


def parse_header_metadata(text: str, fallback_book_id: int = 0) -> dict:
    """
    Extract title/authors/language from the PG text header.

    PG standard header format (always present at top of every book file):
        Title: Pride and Prejudice
        Author: Jane Austen
        Language: English
    """
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


def count_chapters(text: str) -> int:
    return len(CHAPTER_RE.findall(text))


async def fetch_metadata(book_id: int) -> dict:
    """Fetch book metadata by downloading text and parsing the PG header."""
    text = await fetch_text(book_id)
    meta = parse_header_metadata(text, fallback_book_id=book_id)
    return {
        "book_id":  book_id,
        "title":    meta["title"],
        "authors":  meta["authors"],
        "language": meta["language"],
    }


async def preview_book(book_id: int) -> dict:
    """
    Fetch text from gutenberg.org and return a preview payload
    matching GutenbergBookInfo (book_id, title, authors, language,
    word_count, num_chapters, num_chunks).
    """
    text = await fetch_text(book_id)
    meta = parse_header_metadata(text, fallback_book_id=book_id)
    chunks = split_text_structured(text)
    return {
        "book_id":      book_id,
        "title":        meta["title"],
        "authors":      meta["authors"],
        "language":     meta["language"],
        "word_count":   sum(count_words(c) for c in chunks),
        "num_chapters": count_chapters(text),
        "num_chunks":   len(chunks),
    }
