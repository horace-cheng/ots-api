"""services/lt_segment_retranslate.py — re-translate a single Literary Track segment.

Synchronous Gemini call for the editor "retranslate segment" feature.
Reuses the pipeline's literary prompt style (LT_PROMPT rules + tai-lo hanzi
instruction) and the order's translation memory for terminology/style
consistency. The LLM call is injectable for testability (translate_func
pattern, same as ots-common).
"""

import logging
import re
from typing import Callable, Optional

from core import storage

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-3.5-flash"
MAX_OUTPUT_TOKENS = 16384
TM_CONTEXT_COUNT = 20

# English labels — mirrors ots-pipeline/shared/db.py get_lang_labels("en")
LANG_LABELS: dict[str, str] = {
    "tai-lo":     "Taiwanese Hokkien",
    "hakka":      "Hakka",
    "indigenous": "Taiwanese Indigenous",
    "zh-tw":      "Traditional Chinese",
    "zh-cn":      "Simplified Chinese",
    "en":         "English",
    "ja":         "Japanese",
    "ko":         "Korean",
    "fr":         "French",
    "de":         "German",
    "es":         "Spanish",
    "vi":         "Vietnamese",
    "th":         "Thai",
    "cs":         "Czech",
}

RETRANSLATE_PROMPT = """You are a professional literary translator specializing in {source_lang_label} to {target_lang} translation.

Re-translate the following segment, producing a fresh, high-quality {target_lang} translation.

Rules:
1. Preserve paragraph structure exactly — do not merge or split paragraphs
2. Translate ALL content faithfully. Maintain cultural references with brief contextual hints only when essential for comprehension
3. Preserve proper nouns, place names, and cultural terms with appropriate romanization
4. Maintain the original tone (formal, colloquial, poetic, etc.)
5. Literary devices (metaphor, alliteration, rhythm) should be preserved where possible
6. Begin IMMEDIATELY with the {target_lang} translation. Output ONLY the translation. Do NOT add any preamble, reasoning, or commentary. Do NOT write "Segment N:" labels or quotes around the translation.
7. Do NOT translate or modify footnote/annotation/remark numbers (e.g., [1], ①, (a), Note 1) — keep them exactly as in the source
{hanzi_instruction}
Previously translated segments from this document (source → translation) are provided as reference — use them to keep terminology, style, and tone consistent:

{context_text}
Segment to translate:
{source_text}

{target_lang} translation:"""


class SegmentRetranslateError(Exception):
    """Raised when the segment cannot be re-translated (blocked, empty, etc.)."""


def _get_hanzi_instruction(target_lang: str) -> str:
    """Return extra instruction for Hanzi output when target is tai-lo."""
    if target_lang != "tai-lo":
        return ""
    return (
        "8. CRITICAL — Taiwanese Hokkien output MUST be written in Han characters (台語漢字), "
        "NOT in Pe̍h-ōe-jī romanization.\n"
        "   Correct examples: 我 (not góa), 的 (not ê), 是 (not sī), 有 (not ū), 人 (not lâng), "
        "愛 (not ài), 講 (not kóng), 看 (not khòaⁿ), 這 (not che), 佇 (not tī).\n"
        "   Use Tailo romanization ONLY in parentheses after the Han form for terms without "
        "standard Han characters (e.g., 泅水 (siû-chúi)).\n"
        "   IMPORTANT: Pure romanization output will be rejected. You must produce Han-dominant text "
        "readable by native Taiwanese speakers.\n"
    )


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text or "")
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def _read_translation_memory(order_id: str) -> list[dict]:
    """Read accumulated TM entries from GCS temp bucket."""
    try:
        from google.cloud import storage as gcs

        from core.config import settings

        client = gcs.Client(project=settings.project_id)
        bucket = client.bucket(settings.gcs_temp_bucket)
        blob = bucket.blob(f"temp/{order_id}/translation_memory.jsonl")
        if not blob.exists():
            return []
        entries = []
        for line in blob.download_as_text().strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(__import__("json").loads(line))
            except Exception:
                continue
        return entries
    except Exception as e:
        logger.warning(f"TM read failed (non-fatal): {e}")
        return []


def _select_tm_context(entries: list[dict], target_source: str, count: int = TM_CONTEXT_COUNT) -> list[dict]:
    """Pick TM entries most lexically similar to the target segment."""
    if not entries:
        return []
    target_bg = _bigrams(target_source)
    if not target_bg:
        return entries[-count:]
    scored = []
    for e in entries:
        src = e.get("source", "")
        if not src or not e.get("translation"):
            continue
        shared = len(target_bg & _bigrams(src))
        if shared:
            scored.append((shared, e))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [e for _, e in scored[:count]] or entries[-count:]


def _build_context_text(entries: list[dict]) -> str:
    if not entries:
        return "(none available)"
    lines = []
    for e in entries:
        lines.append(f"Source:      {e['source']}")
        lines.append(f"Translation: {e['translation']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _clean_translation(text: str) -> str:
    """Strip stray delimiters/labels the model might add around a single translation."""
    if not text:
        return ""
    cleaned = re.sub(r"<<<TRANSLATION_END>>>", "", text)
    cleaned = re.sub(r"(?i)^(segment\s*\d+\s*[:：]|translation\s*[:：])\s*", "", cleaned.strip())
    return cleaned.strip()


def _call_gemini(prompt: str) -> str:
    """Default Gemini client wrapper (injectable for tests)."""
    from google.genai import types

    from core.config import settings

    if not settings.gemini_api_key:
        raise SegmentRetranslateError("GEMINI_API_KEY not configured")

    import google.genai as genai

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.1,
        ),
    )
    if not getattr(response, "candidates", None) or not getattr(response, "text", None):
        logger.warning("Gemini returned empty/blocked response for retranslate")
        return ""
    return response.text.strip()


def retranslate_segment(
    order_id: str,
    index: int,
    source_lang: str,
    target_lang: str,
    translate_func: Optional[Callable[[str], str]] = None,
) -> str:
    """Re-translate a single LT segment synchronously and persist the result.

    Returns the new translated text. Raises SegmentRetranslateError when the
    model blocks/returns empty, and ValueError when index is out of range.
    """
    segments = storage.read_temp_json(order_id, "segments.json")
    translations = storage.read_temp_json(order_id, "translations.json")
    translations_raw = storage.read_temp_json(order_id, "translations_raw.json")

    if not isinstance(segments, list) or not segments:
        raise SegmentRetranslateError("segments.json not found — pipeline has not produced output")
    if not isinstance(translations, list) or not translations:
        raise SegmentRetranslateError("translations.json not found — pipeline has not produced output")

    seg_map = {s.get("index"): s for s in segments if isinstance(s, dict) and "index" in s}
    if index not in seg_map:
        raise ValueError(f"Segment index {index} out of range")

    source_text = seg_map[index].get("text", "")
    if not source_text.strip():
        raise SegmentRetranslateError(f"Segment {index + 1} is empty")

    tm_entries = _select_tm_context(_read_translation_memory(order_id), source_text)
    context_text = _build_context_text(tm_entries)

    prompt = RETRANSLATE_PROMPT.format(
        source_text        = source_text,
        source_lang_label  = LANG_LABELS.get(source_lang, source_lang),
        target_lang        = LANG_LABELS.get(target_lang, target_lang),
        hanzi_instruction  = _get_hanzi_instruction(target_lang),
        context_text       = context_text,
    )

    caller = translate_func or _call_gemini
    result = caller(prompt)
    new_text = _clean_translation(result)
    if not new_text:
        raise SegmentRetranslateError(
            f"Segment {index + 1} could not be re-translated (content blocked or empty response)"
        )

    # Persist: translations.json keeps comments; raw stores the previous
    # translation so the editor can compare old vs new.
    trans_map = {t.get("index"): t for t in translations if isinstance(t, dict) and "index" in t}
    if index not in trans_map:
        raise SegmentRetranslateError(f"Segment {index + 1} has no translation entry")

    old_translated = trans_map[index].get("translated", "")
    trans_map[index]["translated"] = new_text
    storage.write_temp_json(order_id, "translations.json", list(trans_map.values()))

    if isinstance(translations_raw, list) and translations_raw:
        raw_map = {t.get("index"): t for t in translations_raw if isinstance(t, dict) and "index" in t}
        if index in raw_map:
            raw_map[index]["translated"] = old_translated
        else:
            raw_map[index] = {"index": index, "translated": old_translated}
        storage.write_temp_json(order_id, "translations_raw.json", list(raw_map.values()))

    logger.info(f"Retranslated segment {index + 1} for order {order_id}: {len(source_text)} → {len(new_text)} chars")
    return new_text
