"""services/lt_segment_retranslate.py — re-translate a single Literary Track segment.

Synchronous Gemini call for the editor "retranslate segment" feature.
Reuses the pipeline's literary prompt style (LT_PROMPT rules + tai-lo hanzi
instruction) and the order's translation memory for terminology/style
consistency. The LLM call is injectable for testability (translate_func
pattern, same as ots-common).
"""

import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core import storage

try:
    from ots_common.rag.file_search import (
        get_or_create_file_search_store as _get_or_create_file_search_store,
        upload_raw_file_to_store as _upload_raw_file_to_store,
        file_search_tool as _file_search_tool,
    )
except ImportError:
    _candidates = [
        Path(__file__).resolve().parent.parent / "ots-common",          # submodule: ots-api/ots-common/
        Path(__file__).resolve().parent.parent.parent / "ots-common",  # dev: repo root
    ]
    for _root in _candidates:
        if _root.exists():
            sys.path.insert(0, str(_root))
            try:
                from ots_common.rag.file_search import (
                    get_or_create_file_search_store as _get_or_create_file_search_store,
                    upload_raw_file_to_store as _upload_raw_file_to_store,
                    file_search_tool as _file_search_tool,
                )
                break
            except ImportError:
                sys.path.pop(0)
    else:
        _get_or_create_file_search_store = None
        _upload_raw_file_to_store = None
        _file_search_tool = None

try:
    from ots_common.usage.token_usage import (
        build_usage_record as _build_usage_record,
        TOKEN_USAGE_INSERT_SQL as _TOKEN_USAGE_INSERT_SQL,
        build_insert_sql_params as _build_insert_sql_params,
    )
except ImportError:
    _candidates = [
        Path(__file__).resolve().parent.parent / "ots-common",          # submodule: ots-api/ots-common/
        Path(__file__).resolve().parent.parent.parent / "ots-common",  # dev: repo root
    ]
    for _root in _candidates:
        if _root.exists():
            sys.path.insert(0, str(_root))
            try:
                from ots_common.usage.token_usage import (
                    build_usage_record as _build_usage_record,
                    TOKEN_USAGE_INSERT_SQL as _TOKEN_USAGE_INSERT_SQL,
                    build_insert_sql_params as _build_insert_sql_params,
                )
                break
            except ImportError:
                sys.path.pop(0)
    else:
        _build_usage_record = None
        _TOKEN_USAGE_INSERT_SQL = None
        _build_insert_sql_params = None

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

# Appended to the prompt only when the order's File Search store is available.
# The File Search tool alone does not reliably steer the model — gemini-3.5-flash
# frequently translates character names from its own knowledge (阿章 → "Ah-Chang")
# instead of consulting the attached reference. The explicit instruction forces
# it to retrieve the publisher's name table/glossary and adopt its romanizations.
REFERENCE_FILES_INSTRUCTION = (
    "IMPORTANT — The publisher's reference files (character name table, glossary, style guide) are "
    "attached to this request via file-search. Consult them BEFORE translating and use EXACTLY the "
    "romanizations and terms they provide for character names, place names, and glossary terms "
    "(e.g. 阿章 = A-tsiong). Never substitute a different romanization.\n"
)


class SegmentRetranslateError(Exception):
    """Raised when the segment cannot be re-translated (blocked, empty, etc.)."""


class SegmentContentBlocked(SegmentRetranslateError):
    """All translators refused on content-policy grounds or returned empty —
    the editor must translate this segment manually."""


@dataclass
class RetranslateResult:
    translated: str
    used_fallback: bool = False
    # Token usage records from every real Gemini call (prompt + no-context
    # retry). Each entry mirrors the token_usage table row minus order_id and
    # job_type: {model, prompt_tokens, candidates_tokens, total_tokens,
    # cost_usd, input_rate, output_rate}. Empty when translate_func was
    # injected (tests) — usage can only be captured from the real client.
    gemini_usage: list = field(default_factory=list)


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


def _resolve_file_search_store(order_id: str) -> Optional[str]:
    """Return the per-order Gemini File Search store (get-or-create).

    Uses the same store name the pipeline creates/uses, so both services share
    one index per order. Returns None (non-fatal) when the shared helper module
    is unavailable or the GenAI store call fails — the retranslate then runs
    without RAG rather than failing the editor request.
    """
    if _get_or_create_file_search_store is None:
        logger.warning("ots_common File Search helpers unavailable — retranslate running without RAG")
        return None
    try:
        import google.genai as genai

        from core.config import settings

        client = genai.Client(api_key=settings.gemini_api_key)
        return _get_or_create_file_search_store(client, order_id, settings.env)
    except Exception as e:
        logger.warning(f"File Search store resolution failed (non-fatal): {e}")
        return None


def _load_indexed_files(order_id: str) -> list[dict]:
    """Read the tracked list of already-indexed support files (name + md5)."""
    try:
        data = storage.read_temp_json(order_id, "file_search_indexed.json")
        if isinstance(data, list):
            return [f for f in data if isinstance(f, dict)]
    except Exception as e:
        logger.warning(f"Indexed-files read failed (non-fatal): {e}")
    return []


def _save_indexed_files(order_id: str, indexed: list[dict]) -> None:
    """Persist the tracked list of already-indexed support files."""
    try:
        storage.write_temp_json(order_id, "file_search_indexed.json", indexed)
    except Exception as e:
        logger.warning(f"Indexed-files save failed (non-fatal): {e}")


def _sync_support_files(order_id: str, store_name: str) -> None:
    """Index the order's raw support files into the File Search store.

    Uploads original bytes of every file under ``orders/{order_id}/support/``
    (arbitrary publisher formats — DOCX, PDF, XLSX, images, ...) letting the
    hosted multi-modal File Search index them as-is. Idempotent: tracks
    (name, md5) in temp ``file_search_indexed.json`` and skips blobs already
    indexed. Any failure is non-fatal.
    """
    if _upload_raw_file_to_store is None:
        return
    try:
        import google.genai as genai

        from core.config import settings
        from core.storage import get_storage_client

        client = genai.Client(api_key=settings.gemini_api_key)
        bucket = get_storage_client().bucket(settings.gcs_uploads_bucket)
        blobs = list(bucket.list_blobs(prefix=f"orders/{order_id}/support/"))
        if not blobs:
            return

        indexed = _load_indexed_files(order_id)
        indexed_keys = {(f.get("name"), f.get("md5")) for f in indexed}
        changed = False
        for blob in blobs:
            if blob.name.endswith("/"):
                continue
            filename = blob.name.split("/")[-1]
            md5 = getattr(blob, "md5_hash", "") or ""
            if (filename, md5) in indexed_keys:
                continue
            raw = blob.download_as_bytes()
            _upload_raw_file_to_store(
                client, store_name, raw, filename,
                blob.content_type or "application/octet-stream",
            )
            indexed.append({"name": filename, "md5": md5})
            indexed_keys.add((filename, md5))
            changed = True
            logger.info(f"Indexed support file into File Search store {store_name}: {filename}")
        if changed:
            _save_indexed_files(order_id, indexed)
    except Exception as e:
        logger.warning(f"Support file sync to File Search failed (non-fatal): {e}")


def _token_usage_record(model: str, usage) -> Optional[dict]:
    """Build a token_usage row dict from a Gemini usage_metadata, or None.

    Delegates to the shared ``ots_common.usage.token_usage.build_usage_record``
    (accepts a genai usage_metadata or a counts dict; cost from shared model
    pricing with env overrides). Returns None when usage is falsy or every
    count is zero.
    """
    if _build_usage_record is None:
        return None
    return _build_usage_record(model, usage)


def _call_gemini(prompt: str, store_name: Optional[str] = None) -> tuple[str, Optional[dict]]:
    """Default Gemini client wrapper (injectable for tests).

    Returns ``(text, usage_record)`` where ``usage_record`` is the token_usage
    row dict (or None when the model reports no usage). When ``store_name`` is
    given (a per-order Gemini File Search store), the call is made with a
    ``FileSearch`` tool so Gemini can retrieve from the order's raw support
    files (glossaries, name tables, style guides). The reference context is
    retrieved by the model rather than inlined into the prompt, which avoids
    the prompt-size/content-policy problems of inline inlining and keeps
    arbitrary publisher file formats working.
    """
    from google.genai import types

    from core.config import settings

    if not settings.gemini_api_key:
        raise SegmentRetranslateError("GEMINI_API_KEY not configured")

    import google.genai as genai

    client = genai.Client(api_key=settings.gemini_api_key)
    config_kwargs: dict = {
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.1,
    }
    if store_name and _file_search_tool is not None:
        try:
            config_kwargs["tools"] = [_file_search_tool(store_name)]
        except Exception as e:
            logger.warning(f"File Search tool setup failed (non-fatal), continuing without RAG: {e}")
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(**config_kwargs),
    )
    usage = _token_usage_record(MODEL_NAME, getattr(response, "usage_metadata", None))
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
        return "", usage
    return response.text.strip(), usage


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

    # The real Gemini path (not injected test callers) attaches the order's
    # File Search store so the model can retrieve publisher reference files
    # (glossaries, name tables, style guides) via RAG instead of inlining.
    # Injected translate_func callers control their own prompt.
    store_name = None
    if translate_func is None:
        store_name = _resolve_file_search_store(order_id)
        if store_name:
            _sync_support_files(order_id, store_name)

    if store_name:
        prompt += REFERENCE_FILES_INSTRUCTION
        no_context_prompt += REFERENCE_FILES_INSTRUCTION

    gemini_usage: list = []

    def _caller(p: str) -> str:
        if translate_func is not None:
            return translate_func(p)
        text, usage = _call_gemini(p, store_name=store_name)
        if usage:
            gemini_usage.append(usage)
        return text

    caller = _caller
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
    # Retranslating with the (possibly edited) source resolves any stale
    # source_edited flag and guarantees translations.json source matches
    # segments.json.
    trans_map[index]["source"] = source_text
    trans_map[index]["source_edited"] = False
    storage.write_temp_json(order_id, "translations.json", list(trans_map.values()))

    if isinstance(translations_raw, list) and translations_raw:
        raw_map = {t.get("index"): t for t in translations_raw if isinstance(t, dict) and "index" in t}
        if index in raw_map:
            raw_map[index]["translated"] = old_translated
        else:
            raw_map[index] = {"index": index, "translated": old_translated}
        storage.write_temp_json(order_id, "translations_raw.json", list(raw_map.values()))

    logger.info(f"Retranslated segment {index + 1} for order {order_id}: {len(source_text)} → {len(new_text)} chars (fallback={used_fallback})")
    return RetranslateResult(
        translated=new_text,
        used_fallback=used_fallback,
        gemini_usage=gemini_usage,
    )
