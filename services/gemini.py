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
            model="gemini-2.5-flash",
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
