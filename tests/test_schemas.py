import pytest
from pydantic import ValidationError
from models.schemas import (
    UserProfileUpdate, ClientType,
    OrderCreate, TrackType, LangCode,
    QAFlagResolve,
)


class TestUserProfileUpdate:
    def test_b2b_requires_tax_id(self):
        with pytest.raises(ValidationError, match="tax_id is required"):
            UserProfileUpdate(client_type=ClientType.B2B)

    def test_b2b_with_tax_id_ok(self):
        p = UserProfileUpdate(client_type=ClientType.B2B, tax_id="12345678")
        assert p.tax_id == "12345678"

    def test_b2c_without_tax_id_ok(self):
        p = UserProfileUpdate(client_type=ClientType.B2C)
        assert p.tax_id is None

    def test_b2c_with_tax_id_ok(self):
        p = UserProfileUpdate(client_type=ClientType.B2C, tax_id="12345678")
        assert p.tax_id == "12345678"


class TestOrderCreate:
    def _valid(self, **overrides):
        defaults = dict(
            track_type=TrackType.FAST,
            source_lang=LangCode.ZH_TW,
            target_lang=LangCode.EN,
            word_count=1000,
        )
        return OrderCreate(**{**defaults, **overrides})

    def test_valid_order(self):
        order = self._valid()
        assert order.word_count == 1000

    def test_same_source_target_lang_raises(self):
        with pytest.raises(ValidationError, match="must be different"):
            self._valid(source_lang=LangCode.ZH_TW, target_lang=LangCode.ZH_TW)

    def test_word_count_zero_raises(self):
        with pytest.raises(ValidationError):
            self._valid(word_count=0)

    def test_word_count_negative_raises(self):
        with pytest.raises(ValidationError):
            self._valid(word_count=-1)

    def test_notes_max_length(self):
        with pytest.raises(ValidationError):
            self._valid(notes="x" * 501)

    def test_notes_optional(self):
        order = self._valid(notes=None)
        assert order.notes is None


class TestQAFlagResolve:
    def test_empty_note_raises(self):
        with pytest.raises(ValidationError):
            QAFlagResolve(reviewer_note="")

    def test_note_too_long_raises(self):
        with pytest.raises(ValidationError):
            QAFlagResolve(reviewer_note="x" * 1001)

    def test_valid_note(self):
        q = QAFlagResolve(reviewer_note="Looks good")
        assert q.reviewer_note == "Looks good"
