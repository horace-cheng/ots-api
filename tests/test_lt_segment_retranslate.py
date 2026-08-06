"""Unit tests for services/lt_segment_retranslate.py"""

import pytest
from unittest.mock import patch, MagicMock

from services.lt_segment_retranslate import (
    retranslate_segment,
    SegmentRetranslateError,
    SegmentContentBlocked,
    _clean_translation,
    _select_tm_context,
)

SEGMENTS = [
    {"index": 0, "text": "第一章\n少年阿章的故事。"},
    {"index": 1, "text": "他站在山頂，望向遠方。"},
    {"index": 2, "text": "風吹過他的臉龐。"},
]

TRANSLATIONS = [
    {"index": 0, "translated": "Chapter One\n...", "editor_comments": "ok"},
    {"index": 1, "translated": "He stood...", "editor_comments": None},
    {"index": 2, "translated": "The wind...", "editor_comments": None},
]

TRANS_RAW = [
    {"index": 0, "translated": "Chapter One raw"},
    {"index": 1, "translated": "He stood raw"},
    {"index": 2, "translated": "The wind raw"},
]


@patch("services.lt_segment_retranslate._read_translation_memory", return_value=[])
class TestRetranslateSegment:
    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_success(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        result = retranslate_segment("order-1", 1, "zh-tw", "en", translate_func=lambda p: "He stood on the hill, gazing afar.")
        assert result.translated == "He stood on the hill, gazing afar."
        assert result.used_fallback is False
        assert mock_write.call_count == 2
        written_trans = mock_write.call_args_list[0].args[2]
        written_raw = mock_write.call_args_list[1].args[2]
        # comments preserved, translated updated
        assert written_trans[1]["translated"] == result.translated
        assert written_trans[0]["editor_comments"] == "ok"
        # raw now holds the previous translation (from translations.json)
        assert written_raw[1]["translated"] == "He stood..."

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_out_of_range(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        with pytest.raises(ValueError):
            retranslate_segment("order-1", 99, "zh-tw", "en", translate_func=lambda p: "x")

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_real_gemini_path_resolves_store_and_syncs(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        with patch("services.lt_segment_retranslate._resolve_file_search_store", return_value="stores/order-1") as mock_resolve, \
             patch("services.lt_segment_retranslate._sync_support_files") as mock_sync, \
             patch("services.lt_segment_retranslate._call_gemini", return_value="He stood on the hill, gazing afar.") as mock_gemini:
            result = retranslate_segment("order-1", 1, "zh-tw", "en")
        assert result.translated == "He stood on the hill, gazing afar."
        assert result.used_fallback is False
        mock_resolve.assert_called_once_with("order-1")
        mock_sync.assert_called_once_with("order-1", "stores/order-1")
        assert mock_gemini.call_args.kwargs["store_name"] == "stores/order-1"
        assert mock_write.call_count == 2

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_real_gemini_path_store_unavailable_still_works(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        with patch("services.lt_segment_retranslate._resolve_file_search_store", return_value=None) as mock_resolve, \
             patch("services.lt_segment_retranslate._sync_support_files") as mock_sync, \
             patch("services.lt_segment_retranslate._call_gemini", return_value="He stood on the hill, gazing afar.") as mock_gemini:
            result = retranslate_segment("order-1", 1, "zh-tw", "en")
        assert result.translated == "He stood on the hill, gazing afar."
        mock_sync.assert_not_called()
        assert mock_gemini.call_args.kwargs["store_name"] is None

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_injected_translate_func_skips_store(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        with patch("services.lt_segment_retranslate._resolve_file_search_store") as mock_resolve, \
             patch("services.lt_segment_retranslate._sync_support_files") as mock_sync, \
             patch("services.lt_segment_retranslate._call_gemini") as mock_gemini:
            result = retranslate_segment(
                "order-1", 1, "zh-tw", "en",
                translate_func=lambda p: "He stood on the hill, gazing afar.",
            )
        assert result.translated == "He stood on the hill, gazing afar."
        mock_resolve.assert_not_called()
        mock_sync.assert_not_called()
        mock_gemini.assert_not_called()

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_empty_response_raises(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        with pytest.raises(SegmentRetranslateError):
            retranslate_segment("order-1", 1, "zh-tw", "en",
                                translate_func=lambda p: "",
                                fallback_translate_func=lambda p: "")

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_blocked_with_context_retries_without(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        mock_tm.return_value = [{"source": "風吹過山林。", "translation": "The wind blew."}]
        calls = []
        def fake_caller(prompt):
            calls.append(prompt)
            return "" if len(calls) == 1 else "He stood on the hill, gazing afar."
        result = retranslate_segment("order-1", 1, "zh-tw", "en", translate_func=fake_caller)
        assert result.translated == "He stood on the hill, gazing afar."
        assert len(calls) == 2
        assert "(none available)" in calls[1]
        assert "風吹過山林" in calls[0]

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_blocked_with_and_without_context_raises(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        mock_tm.return_value = [{"source": "風吹過山林。", "translation": "The wind blew."}]
        with pytest.raises(SegmentContentBlocked):
            retranslate_segment("order-1", 1, "zh-tw", "en",
                                translate_func=lambda p: "",
                                fallback_translate_func=lambda p: "")

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_fallback_used_when_primary_blocked(self, mock_read, mock_write, mock_tm):
        from services.lt_segment_retranslate import SegmentContentBlocked as Blocked
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        calls = []
        def primary(prompt):
            calls.append(prompt)
            raise Blocked("Gemini refused")
        result = retranslate_segment(
            "order-1", 1, "zh-tw", "en",
            translate_func=primary,
            fallback_translate_func=lambda p: "He stood on the hill, gazing afar.",
        )
        assert result.translated == "He stood on the hill, gazing afar."
        assert result.used_fallback is True
        assert mock_write.call_count == 2
        written_trans = mock_write.call_args_list[0].args[2]
        assert written_trans[1]["translated"] == result.translated

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_fallback_failure_raises(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        with pytest.raises(SegmentContentBlocked):
            retranslate_segment("order-1", 1, "zh-tw", "en",
                                translate_func=lambda p: "",
                                fallback_translate_func=lambda p: "")

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_blocked_preamble_stripped(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        result = retranslate_segment(
            "order-1", 1, "zh-tw", "en",
            translate_func=lambda p: "Segment 1: He stood on the hill.\n<<<TRANSLATION_END>>>",
        )
        assert result.translated == "He stood on the hill."

    def test_missing_segments_file(self, mock_tm):
        with patch("core.storage.read_temp_json", return_value=None) as mock_read:
            with pytest.raises(SegmentRetranslateError):
                retranslate_segment("order-1", 1, "zh-tw", "en", translate_func=lambda p: "x")


class TestHelpers:
    def test_clean_translation(self):
        assert _clean_translation("  \nHello\n<<<TRANSLATION_END>>>\n") == "Hello"
        assert _clean_translation("Translation: Hello") == "Hello"
        assert _clean_translation("Segment 5: Hello") == "Hello"
        assert _clean_translation("") == ""

    def test_select_tm_context_prefers_similar(self):
        entries = [
            {"source": "他站在山頂。", "translation": "A"},
            {"source": "無關內容測試", "translation": "B"},
            {"source": "風吹過山林。", "translation": "C"},
        ]
        selected = _select_tm_context(entries, "風吹過他的臉龐。", count=2)
        assert selected[0]["translation"] == "C"

    def test_select_tm_context_char_budget(self):
        long_source = "他" * 5000
        entries = [{"source": long_source, "translation": "L"}, {"source": "風吹過山林。", "translation": "C"}]
        selected = _select_tm_context(entries, "風吹過他的臉龐。", count=20)
        assert all(
            min(len(e.get("source", "")), 600) + min(len(e.get("translation", "")), 600) <= 6000
            for e in selected
        )

    def test_build_context_text_truncates(self):
        from services.lt_segment_retranslate import _build_context_text
        text = _build_context_text([{"source": "字" * 900, "translation": "x" * 900}])
        assert "…" in text
        assert len(text) < 1300


class TestCallGemini:
    @staticmethod
    def _patch_settings():
        from types import SimpleNamespace
        import core.config
        return patch.object(core.config, "settings", SimpleNamespace(gemini_api_key="test-key"))

    def test_prohibited_content_raises(self):
        from services.lt_segment_retranslate import _call_gemini, SegmentContentBlocked
        mock_client = MagicMock()
        resp = MagicMock()
        resp.candidates = [MagicMock(finish_reason="FinishReason.PROHIBITED_CONTENT")]
        resp.text = None
        resp.prompt_feedback = MagicMock(block_reason=None)
        mock_client.models.generate_content.return_value = resp
        with self._patch_settings(), patch("google.genai.Client", return_value=mock_client):
            with pytest.raises(SegmentContentBlocked):
                _call_gemini("some prompt")

    def test_empty_without_block_returns_empty(self):
        from services.lt_segment_retranslate import _call_gemini
        mock_client = MagicMock()
        resp = MagicMock()
        resp.candidates = [MagicMock(finish_reason="FinishReason.MAX_TOKENS")]
        resp.text = None
        resp.prompt_feedback = MagicMock(block_reason=None)
        mock_client.models.generate_content.return_value = resp
        with self._patch_settings(), patch("google.genai.Client", return_value=mock_client):
            assert _call_gemini("some prompt") == ""

    def test_store_name_attaches_file_search_tool(self):
        from services.lt_segment_retranslate import _call_gemini, _file_search_tool
        if _file_search_tool is None:
            pytest.skip("ots_common File Search helpers unavailable in this environment")
        mock_client = MagicMock()
        resp = MagicMock()
        resp.candidates = [MagicMock(finish_reason="FinishReason.STOP")]
        resp.text = "hello"
        resp.prompt_feedback = MagicMock(block_reason=None)
        mock_client.models.generate_content.return_value = resp
        with self._patch_settings(), patch("google.genai.Client", return_value=mock_client):
            assert _call_gemini("some prompt", store_name="stores/order-1") == "hello"
        config = mock_client.models.generate_content.call_args.kwargs["config"]
        assert config.tools and config.tools[0].file_search
        assert config.tools[0].file_search.file_search_store_names == ["stores/order-1"]

    def test_no_store_no_tool(self):
        from services.lt_segment_retranslate import _call_gemini
        mock_client = MagicMock()
        resp = MagicMock()
        resp.candidates = [MagicMock(finish_reason="FinishReason.STOP")]
        resp.text = "hello"
        resp.prompt_feedback = MagicMock(block_reason=None)
        mock_client.models.generate_content.return_value = resp
        with self._patch_settings(), patch("google.genai.Client", return_value=mock_client):
            assert _call_gemini("some prompt") == "hello"
        config = mock_client.models.generate_content.call_args.kwargs["config"]
        assert not config.tools


class TestFileSearchStore:
    @staticmethod
    def _patch_settings():
        from types import SimpleNamespace
        import core.config
        return patch.object(core.config, "settings", SimpleNamespace(gemini_api_key="test-key", env="dev"))

    def test_resolve_unavailable_helpers_returns_none(self):
        from services.lt_segment_retranslate import _resolve_file_search_store
        with patch("services.lt_segment_retranslate._get_or_create_file_search_store", None):
            assert _resolve_file_search_store("order-1") is None

    def test_resolve_failure_returns_none(self):
        from services.lt_segment_retranslate import _resolve_file_search_store
        failing = MagicMock(side_effect=RuntimeError("boom"))
        with self._patch_settings(), \
             patch("google.genai.Client"), \
             patch("services.lt_segment_retranslate._get_or_create_file_search_store", failing):
            assert _resolve_file_search_store("order-1") is None

    def test_resolve_uses_env_and_order(self):
        from services.lt_segment_retranslate import _resolve_file_search_store
        mock_genai = MagicMock()
        getter = MagicMock(return_value="stores/order-1")
        with self._patch_settings(), \
             patch("google.genai.Client", return_value=mock_genai), \
             patch("services.lt_segment_retranslate._get_or_create_file_search_store", getter):
            assert _resolve_file_search_store("order-1") == "stores/order-1"
        getter.assert_called_once_with(mock_genai, "order-1", "dev")

    def test_shared_get_or_create_reuses_store_from_iterable_pager(self):
        from types import SimpleNamespace
        from ots_common.rag.file_search import get_or_create_file_search_store
        existing = SimpleNamespace(
            display_name="ots-order-o1-dev",
            name="fileSearchStores/order-o1-suffix",
        )
        class FakePager:
            def __iter__(self):
                return iter([existing])
        mock_client = MagicMock()
        mock_client.file_search_stores.list.return_value = FakePager()
        result = get_or_create_file_search_store(mock_client, "o1", "dev")
        assert result == "fileSearchStores/order-o1-suffix"
        mock_client.file_search_stores.create.assert_not_called()

    def test_shared_get_or_create_creates_when_missing(self):
        from types import SimpleNamespace
        from ots_common.rag.file_search import get_or_create_file_search_store
        other = SimpleNamespace(display_name="ots-order-other-dev", name="fileSearchStores/other")
        class FakePager:
            def __iter__(self):
                return iter([other])
        mock_client = MagicMock()
        mock_client.file_search_stores.list.return_value = FakePager()
        mock_client.file_search_stores.create.return_value.name = "fileSearchStores/o1-new"
        result = get_or_create_file_search_store(mock_client, "o1", "dev")
        assert result == "fileSearchStores/o1-new"
        mock_client.file_search_stores.create.assert_called_once()

    @staticmethod
    def _fake_storage(blobs):
        from types import SimpleNamespace
        fake_bucket = MagicMock()
        fake_bucket.list_blobs.return_value = blobs
        fake_client = MagicMock()
        fake_client.bucket.return_value = fake_bucket
        return fake_client

    def test_sync_uploads_new_files_and_tracks(self):
        from types import SimpleNamespace
        import core.config
        from services.lt_segment_retranslate import _sync_support_files
        blobs = [
            SimpleNamespace(
                name="orders/o1/support/keywords.docx", md5_hash="def",
                content_type="text/plain", download_as_bytes=lambda: b"old",
            ),
            SimpleNamespace(
                name="orders/o1/support/人名對照表.docx", md5_hash="abc",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                download_as_bytes=lambda: b"DOCX",
            ),
        ]
        fake_client = self._fake_storage(blobs)
        mock_upload = MagicMock()
        mock_load = MagicMock(return_value=[{"name": "keywords.docx", "md5": "def"}])
        mock_save = MagicMock()
        with patch.object(core.config, "settings",
                          SimpleNamespace(gemini_api_key="test-key", gcs_uploads_bucket="uploads")), \
             patch("google.genai.Client", return_value=MagicMock()), \
             patch("core.storage.get_storage_client", return_value=fake_client), \
             patch("services.lt_segment_retranslate._upload_raw_file_to_store", mock_upload), \
             patch("services.lt_segment_retranslate._load_indexed_files", mock_load), \
             patch("services.lt_segment_retranslate._save_indexed_files", mock_save):
            _sync_support_files("o1", "stores/o1")
        assert mock_upload.call_count == 1
        args, kwargs = mock_upload.call_args
        assert args[1] == "stores/o1"
        assert args[2] == b"DOCX"
        assert args[3] == "人名對照表.docx"
        assert args[4] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        saved = mock_save.call_args.args[1]
        assert saved == [
            {"name": "keywords.docx", "md5": "def"},
            {"name": "人名對照表.docx", "md5": "abc"},
        ]

    def test_sync_no_blobs_skips(self):
        from types import SimpleNamespace
        import core.config
        from services.lt_segment_retranslate import _sync_support_files
        fake_client = self._fake_storage([])
        mock_upload = MagicMock()
        mock_save = MagicMock()
        with patch.object(core.config, "settings",
                          SimpleNamespace(gemini_api_key="test-key", gcs_uploads_bucket="uploads")), \
             patch("google.genai.Client"), \
             patch("core.storage.get_storage_client", return_value=fake_client), \
             patch("services.lt_segment_retranslate._upload_raw_file_to_store", mock_upload), \
             patch("services.lt_segment_retranslate._load_indexed_files", MagicMock(return_value=[])), \
             patch("services.lt_segment_retranslate._save_indexed_files", mock_save):
            _sync_support_files("o1", "stores/o1")
        mock_upload.assert_not_called()
        mock_save.assert_not_called()


class TestCallReplicate:
    @staticmethod
    def _patch_settings():
        from types import SimpleNamespace
        import core.config
        return patch.object(core.config, "settings", SimpleNamespace(replicate_api_token="test-token"))

    @staticmethod
    def _mock_requests(create_resp, poll_resp=None):
        mock_post = MagicMock()
        mock_post.return_value = MagicMock(json=lambda: create_resp)
        mock_get = MagicMock()
        mock_get.return_value = MagicMock(json=lambda: poll_resp)
        return patch("requests.post", mock_post), patch("requests.get", mock_get), mock_post, mock_get

    def test_success_joins_output(self):
        from services.lt_segment_retranslate import _call_replicate
        create = {"id": "pred-1", "urls": {"get": "https://api.replicate.com/v1/predictions/pred-1"}}
        poll = {"status": "succeeded", "output": ["Hello, ", "world."]}
        p_post, p_get, mock_post, mock_get = self._mock_requests(create, poll)
        with self._patch_settings(), p_post, p_get:
            out = _call_replicate("some prompt")
        assert out == "Hello, world."
        assert mock_post.call_args.args[0].endswith("/models/meta/meta-llama-3-70b-instruct/predictions")

    def test_failed_prediction_raises(self):
        from services.lt_segment_retranslate import _call_replicate, SegmentRetranslateError
        create = {"id": "pred-1", "urls": {"get": "https://api.replicate.com/v1/predictions/pred-1"}}
        poll = {"status": "failed", "error": "boom"}
        p_post, p_get, _, _ = self._mock_requests(create, poll)
        with self._patch_settings(), p_post, p_get, pytest.raises(SegmentRetranslateError, match="boom"):
            _call_replicate("some prompt")

    def test_missing_token_raises(self):
        from types import SimpleNamespace
        import core.config
        from services.lt_segment_retranslate import _call_replicate, SegmentRetranslateError
        with patch.object(core.config, "settings", SimpleNamespace(replicate_api_token="")), \
                pytest.raises(SegmentRetranslateError, match="REPLICATE_API_TOKEN"):
            _call_replicate("some prompt")
