"""Unit tests for services/lt_segment_retranslate.py"""

import pytest
from unittest.mock import patch, MagicMock

from services.lt_segment_retranslate import (
    retranslate_segment,
    SegmentRetranslateError,
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
        assert result == "He stood on the hill, gazing afar."
        assert mock_write.call_count == 2
        written_trans = mock_write.call_args_list[0].args[2]
        written_raw = mock_write.call_args_list[1].args[2]
        # comments preserved, translated updated
        assert written_trans[1]["translated"] == result
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
    def test_empty_response_raises(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        with pytest.raises(SegmentRetranslateError):
            retranslate_segment("order-1", 1, "zh-tw", "en", translate_func=lambda p: "")

    @patch("core.storage.write_temp_json")
    @patch("core.storage.read_temp_json")
    def test_blocked_preamble_stripped(self, mock_read, mock_write, mock_tm):
        mock_read.side_effect = [SEGMENTS, TRANSLATIONS, TRANS_RAW]
        result = retranslate_segment(
            "order-1", 1, "zh-tw", "en",
            translate_func=lambda p: "Segment 1: He stood on the hill.\n<<<TRANSLATION_END>>>",
        )
        assert result == "He stood on the hill."

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
