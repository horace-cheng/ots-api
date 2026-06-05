"""
services/gutenberg.py

Lightweight Gutendex client used by the admin preview endpoint.
Mirrors the chunking logic in ots-pipeline/gt_fetcher/main.py so admins
see the same chunk count the pipeline will produce, without starting
a full translation job.
"""
import re
from typing import List

import httpx

GUTENDEX_API = "https://gutendex.com/books"

CHAPTER_RE = re.compile(r'(CHAPTER\s+[IVXLCDM\d]+(?:\..*?\n.*?))', re.IGNORECASE | re.DOTALL)


async def fetch_metadata(book_id: int) -> dict:
    """Fetch book metadata (title, authors, language) from Gutendex."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(f"{GUTENDEX_API}/{book_id}")
        if response.status_code == 404:
            raise ValueError(f"Gutenberg book {book_id} not found")
        response.raise_for_status()
        data = response.json()
        return {
            "book_id": data.get("id", book_id),
            "title":   data.get("title", f"Gutenberg Book {book_id}"),
            "authors": [a.get("name", "") for a in data.get("authors", [])],
            "language": data.get("languages", ["en"])[0] if data.get("languages") else "en",
        }


async def fetch_text(book_id: int) -> str:
    """Fetch the plain-text body of a Gutenberg book via Gutendex."""
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        meta_resp = await client.get(f"{GUTENDEX_API}/{book_id}")
        meta_resp.raise_for_status()
        data = meta_resp.json()
        text_url = (
            data.get("formats", {}).get("text/plain; charset=us-ascii")
            or data.get("formats", {}).get("text/plain")
        )
        if not text_url:
            raise ValueError(f"No plain text format found for Gutenberg book {book_id}")
        text_resp = await client.get(text_url)
        text_resp.raise_for_status()
        return text_resp.text


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
    """Count CHAPTER headings in the text (case-insensitive)."""
    return len(CHAPTER_RE.findall(text))


async def preview_book(book_id: int) -> dict:
    """
    Fetch metadata + text from Gutendex and return a preview payload
    matching GutenbergBookInfo (book_id, title, authors, language,
    word_count, num_chapters, num_chunks).
    """
    metadata = await fetch_metadata(book_id)
    text = await fetch_text(book_id)
    chunks = split_text_structured(text)
    return {
        **metadata,
        "word_count":   sum(count_words(c) for c in chunks),
        "num_chapters": count_chapters(text),
        "num_chunks":   len(chunks),
    }
