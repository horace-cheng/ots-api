"""
services/gemini.py

Lightweight Gemini integration for generating synopsis text
for the Sample Translation Package.
"""

import logging
import json
from typing import Optional

logger = logging.getLogger(__name__)


async def generate_synopsis(
    source_text: str,
    source_lang: str,
    target_lang: str,
    api_key: str,
) -> str:
    """
    Call Gemini to generate a 500-800 word synopsis in the target language
    based on source text extracted from support files.

    Returns an empty string on failure (callers should fall back gracefully).
    """
    if not api_key:
        logger.warning("GEMINI_API_KEY not configured — skipping synopsis generation")
        return ""

    if not source_text.strip():
        return ""

    lang_names = {
        "tai-lo": "Taiwanese Hokkien (Tâi-lô)",
        "hakka": "Hakka",
        "indigenous": "Taiwan Indigenous Language",
        "zh-tw": "Traditional Chinese",
        "en": "English",
        "ja": "Japanese",
        "ko": "Korean",
    }
    src_name = lang_names.get(source_lang, source_lang)
    tgt_name = lang_names.get(target_lang, target_lang)

    prompt = f"""You are a professional literary translation consultant. A publisher is considering translating a book from {src_name} into {tgt_name}.

Based on the following reference material, write a compelling synopsis of 500–800 words in {tgt_name}. The synopsis should:

1. Open with a strong hook (one sentence that captures the book's essence)
2. Describe the core plot, conflict, and themes
3. Give a sense of the author's style and voice
4. Appeal to the target audience in {tgt_name}-speaking markets

Write ONLY the synopsis — no preamble, no notes, no commentary.

Reference material:
---
{source_text[:6000]}
---"""

    try:
        import google.genai as genai

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=prompt,
        )
        text = response.text.strip() if response.text else ""
        if text:
            logger.info(f"Synopsis generated: {len(text)} chars")
            return text
        else:
            logger.warning("Gemini returned empty response")
            return ""
    except Exception as e:
        logger.error(f"Gemini synopsis generation failed: {e}")
        return ""


async def generate_book_fact_sheet(
    source_text: str,
    source_lang: str,
    target_lang: str,
    title: str,
    word_count: int,
    api_key: str,
) -> dict:
    """Extract book metadata from support files via Gemini."""
    if not api_key or not source_text.strip():
        return {"title": title or "", "word_count": str(word_count)}

    lang_names = {
        "tai-lo": "Taiwanese Hokkien (Tâi-lô)",
        "hakka": "Hakka", "indigenous": "Taiwan Indigenous Language",
        "zh-tw": "Traditional Chinese", "en": "English",
        "ja": "Japanese", "ko": "Korean",
    }
    src_name = lang_names.get(source_lang, source_lang)

    tgt_name = lang_names.get(target_lang, target_lang)

    prompt = f"""You are a literary metadata specialist. Based on the following reference material from a book originally in {src_name}, extract as much of the following information as possible. For each field, provide the value both in the original language ({src_name}) and translated into {tgt_name}. Return ONLY a JSON object (no preamble, no markdown) with these keys — use empty string for anything you cannot determine:

{{
  "title_original": "",
  "title_target": "",
  "author_original": "",
  "author_target": "",
  "publisher_original": "",
  "publisher_target": "",
  "pub_date_original": "",
  "pub_date_target": "",
  "category_original": "",
  "category_target": "",
  "sales_original": "",
  "sales_target": ""
}}

For author and publisher names, the "original" and "target" values will often be the same (proper names). For title, category, and sales, they may differ between the two languages.

Reference material:
---
{source_text[:4000]}
---"""

    try:
        import google.genai as genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model="gemini-3.5-flash", contents=prompt)
        raw = response.text.strip() if response.text else ""
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw) if raw else {}
        result = {"word_count": str(word_count)}
        for k in ("title_original", "title_target", "author_original", "author_target",
                   "publisher_original", "publisher_target", "pub_date_original", "pub_date_target",
                   "category_original", "category_target", "sales_original", "sales_target"):
            result[k] = data.get(k, "")
        logger.info(f"Book fact sheet generated: {len(result)} fields")
        return result
    except Exception as e:
        logger.error(f"Gemini book fact sheet generation failed: {e}")
        return {"word_count": str(word_count)}


async def generate_market_analysis(
    source_text: str,
    source_lang: str,
    target_lang: str,
    api_key: str,
) -> str:
    """Generate market analysis text via Gemini."""
    if not api_key or not source_text.strip():
        return ""

    lang_names = {
        "tai-lo": "Taiwanese Hokkien (Tâi-lô)",
        "hakka": "Hakka", "indigenous": "Taiwan Indigenous Language",
        "zh-tw": "Traditional Chinese", "en": "English",
        "ja": "Japanese", "ko": "Korean",
    }
    src_name = lang_names.get(source_lang, source_lang)
    tgt_name = lang_names.get(target_lang, target_lang)

    prompt = f"""You are a publishing market analyst. A publisher is considering translating a book from {src_name} into {tgt_name}. Based on the following reference material, write a concise market analysis (200-300 words) in {tgt_name} covering:

1. Comparable titles in the {tgt_name} market
2. Target readership and audience
3. Marketing angles and selling points

Write ONLY the analysis — no preamble, no notes.

Reference material:
---
{source_text[:4000]}
---"""

    try:
        import google.genai as genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model="gemini-3.5-flash", contents=prompt)
        text = response.text.strip() if response.text else ""
        if text:
            logger.info(f"Market analysis generated: {len(text)} chars")
            return text
        return ""
    except Exception as e:
        logger.error(f"Gemini market analysis generation failed: {e}")
        return ""
