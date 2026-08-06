"""services/lt_segment_retranslate.py — re-translate a single Literary Track segment.

Synchronous Gemini call for the editor "retranslate segment" feature.
Reuses the pipeline's literary prompt style (LT_PROMPT rules + tai-lo hanzi
instruction) and the order's translation memory for terminology/style
consistency. The LLM call is injectable for testability (translate_func
pattern, same as ots-common).
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from core import storage

logger = logging.getLogger(__name__)

MODEL_NAME = "gemini-3.5-flash"
MAX_OUTPUT_TOKENS = 16384
# Open-weight fallback model for content-policy-blocked segments. Replicate's
# hosted Llama 3 70B translates literary content Gemini refuses (verified on
# the PE-teacher scene that hard-blocks gemini-3.5-flash).
REPLICATE_MODEL = "meta/meta-llama-3-70b-instruct"
_REPLICATE_API_BASE = "https://api.replicate.com/v1"
_REPLICATE_POLL_INTERVAL = 3
_REPLICATE_MAX_WAIT_SECONDS = 240
TM_CONTEXT_COUNT = 20
# Keep the inline TM context compact. Inlining many full-length entries
# ballooned prompts to ~80K chars and, combined with sensitive segment
# content, triggered PROHIBITED_CONTENT blocks (same class as the pipeline's
# batch-combination blocks). The pipeline avoids this by uploading TM via the
# Gemini File API instead of inlining it.
TM_CONTEXT_CHAR_BUDGET = 6000
TM_ENTRY_MAX_CHARS = 600

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


class SegmentContentBlocked(SegmentRetranslateError):
    """All translators refused on content-policy grounds or returned empty —
    the editor must translate this segment manually."""


@dataclass
class RetranslateResult:
    translated: str
    used_fallback: bool = False


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
    """Pick TM entries most lexically similar to the target segment.

    Capped by ``TM_CONTEXT_CHAR_BUDGET`` so the prompt stays compact even for
    long documents (oversized inline context triggered PROHIBITED_CONTENT).
    """
    if not entries:
        return []
    target_bg = _bigrams(target_source)
    if target_bg:
        scored = []
        for e in entries:
            src = e.get("source", "")
            if not src or not e.get("translation"):
                continue
            shared = len(target_bg & _bigrams(src))
            if shared:
                scored.append((shared, e))
        scored.sort(key=lambda t: t[0], reverse=True)
        pool = [e for _, e in scored[:count]] or entries[-count:]
    else:
        pool = entries[-count:]

    budget = TM_CONTEXT_CHAR_BUDGET
    selected = []
    for e in pool:
        size = min(len(e.get("source", "")), TM_ENTRY_MAX_CHARS) + min(len(e.get("translation", "")), TM_ENTRY_MAX_CHARS)
        if selected and size > budget:
            break
        selected.append(e)
        budget -= size
        if budget <= 0:
            break
    return selected


def _truncate(text: str, limit: int = TM_ENTRY_MAX_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


def _build_context_text(entries: list[dict]) -> str:
    if not entries:
        return "(none available)"
    lines = []
    for e in entries:
        lines.append(f"Source:      {_truncate(e['source'])}")
        lines.append(f"Translation: {_truncate(e['translation'])}")
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
        fb = getattr(response, "prompt_feedback", None)
        reason = getattr(fb, "block_reason", None)
        candidate = getattr(response, "candidates", None)[0] if getattr(response, "candidates", None) else None
        finish = getattr(candidate, "finish_reason", None) if candidate else None
        logger.warning(
            "Gemini returned empty/blocked response for retranslate (block_reason=%s, finish_reason=%s)",
            reason, finish,
        )
        if "PROHIBITED_CONTENT" in str(reason) or "PROHIBITED_CONTENT" in str(finish):
            raise SegmentContentBlocked(
                "Gemini refused to translate this segment on content-policy grounds"
            )
        return ""
    return response.text.strip()


def _call_replicate(prompt: str) -> str:
    """Replicate fallback translator (Llama 3 70B, open-weight).

    Triggered only when the primary Gemini model blocks/returns empty on
    content-policy grounds. Open-weight models generally do not refuse the
    literary content Gemini filters. Mirrors the create-then-poll pattern in
    video_gen_service. Raises SegmentRetranslateError on any failure.
    """
    import requests

    from core.config import settings

    token = settings.replicate_api_token
    if not token:
        raise SegmentRetranslateError("REPLICATE_API_TOKEN not configured")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "input": {
            "prompt": prompt,
            "max_new_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.1,
        }
    }
    r = requests.post(
        f"{_REPLICATE_API_BASE}/models/{REPLICATE_MODEL}/predictions",
        json=payload, headers=headers, timeout=30,
    )
    r.raise_for_status()
    pred = r.json()
    pred_id = pred.get("id")
    if not pred_id:
        raise SegmentRetranslateError(f"Replicate prediction create failed: {pred}")
    poll_url = pred.get("urls", {}).get("get", f"{_REPLICATE_API_BASE}/predictions/{pred_id}")

    deadline = time.time() + _REPLICATE_MAX_WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(_REPLICATE_POLL_INTERVAL)
        pr = requests.get(poll_url, headers=headers, timeout=30)
        pr.raise_for_status()
        data = pr.json()
        status = data.get("status")
        if status == "succeeded":
            output = data.get("output")
            if isinstance(output, list):
                return "".join(output).strip()
            if isinstance(output, str):
                return output.strip()
            raise SegmentRetranslateError(f"Replicate returned no text output: {output}")
        if status in ("failed", "canceled"):
            raise SegmentRetranslateError(f"Replicate prediction {status}: {data.get('error', 'unknown')}")
    raise TimeoutError("Replicate prediction timed out")


def retranslate_segment(
    order_id: str,
    index: int,
    source_lang: str,
    target_lang: str,
    translate_func: Optional[Callable[[str], str]] = None,
    fallback_translate_func: Optional[Callable[[str], str]] = None,
) -> RetranslateResult:
    """Re-translate a single LT segment synchronously and persist the result.

    Tries the primary translator (Gemini) with TM context, retries without
    context, then falls back to an open-weight model (Replicate) when the
    primary blocks/returns empty. Returns a RetranslateResult; raises
    SegmentRetranslateError when no translator succeeds, and ValueError when
    index is out of range.
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
    no_context_prompt = RETRANSLATE_PROMPT.format(
        source_text       = source_text,
        source_lang_label = LANG_LABELS.get(source_lang, source_lang),
        target_lang       = LANG_LABELS.get(target_lang, target_lang),
        hanzi_instruction = _get_hanzi_instruction(target_lang),
        context_text      = "(none available)",
    )

    caller = translate_func or _call_gemini
    fallback_caller = fallback_translate_func or _call_replicate

    def _attempt(p: str) -> str:
        try:
            return _clean_translation(caller(p))
        except SegmentContentBlocked:
            return ""

    new_text = _attempt(prompt)
    if not new_text and tm_entries:
        # Content-policy blocks are often amplified by a large inline context.
        # Retry with no context before giving up (mirrors the pipeline's
        # split-on-block philosophy).
        logger.warning(f"Segment {index + 1}: blocked/empty with TM context, retrying without context")
        new_text = _attempt(no_context_prompt)

    used_fallback = False
    if not new_text:
        logger.warning(
            f"Segment {index + 1}: primary model blocked/empty — trying fallback translator ({REPLICATE_MODEL})"
        )
        try:
            fallback_text = _clean_translation(fallback_caller(no_context_prompt))
        except Exception as e:
            logger.warning(f"Segment {index + 1}: fallback translator failed: {e}")
            fallback_text = ""
        if fallback_text:
            new_text = fallback_text
            used_fallback = True

    if not new_text:
        raise SegmentContentBlocked(
            f"Segment {index + 1} 的內容觸發 AI 安全過濾，無法自動重譯。"
            "請手動翻譯此段並填寫修正說明後儲存。"
            f"(Segment {index + 1}: content blocked by all AI translators — please translate manually.)"
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

    logger.info(f"Retranslated segment {index + 1} for order {order_id}: {len(source_text)} → {len(new_text)} chars (fallback={used_fallback})")
    return RetranslateResult(translated=new_text, used_fallback=used_fallback)
