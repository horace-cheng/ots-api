import pytest
from datetime import datetime, timezone
from pydantic import ValidationError
from models.schemas import (
    UserProfileUpdate, ClientType,
    OrderCreate, TrackType, LangCode,
    QAFlagResolve,
    UserUpdateRequest, UserProfileResponse,
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

    def test_title_optional(self):
        order = self._valid()
        assert order.title is None

    def test_title_accepted_when_provided(self):
        order = self._valid(title="My Translation Project")
        assert order.title == "My Translation Project"

    def test_title_max_length(self):
        with pytest.raises(ValidationError):
            self._valid(title="x" * 101)


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


class TestUserUpdateRequest:
    def test_all_fields_optional(self):
        req = UserUpdateRequest()
        assert req.disabled is None
        assert req.is_admin is None
        assert req.is_editor is None
        assert req.is_qa is None

    def test_is_qa_accepted(self):
        req = UserUpdateRequest(is_qa=True)
        assert req.is_qa is True

    def test_is_qa_revoke(self):
        req = UserUpdateRequest(is_qa=False)
        assert req.is_qa is False

    def test_multiple_fields(self):
        req = UserUpdateRequest(is_editor=True, is_qa=False, disabled=False)
        assert req.is_editor is True
        assert req.is_qa is False
        assert req.disabled is False


class TestUserProfileResponse:
    def _make(self, roles=None, languages=None):
        return UserProfileResponse(
            id="550e8400-e29b-41d4-a716-446655440000",
            uid_firebase="uid-001",
            client_type="b2c",
            company_name=None,
            tax_id=None,
            invoice_carrier=None,
            is_admin="admin" in (roles or []),
            is_editor="editor" in (roles or []),
            is_qa="qa" in (roles or []),
            roles=roles or [],
            languages=languages or [],
            created_at=datetime.now(timezone.utc),
        )

    def test_empty_roles_default(self):
        r = self._make()
        assert r.roles == []
        assert r.is_admin is False
        assert r.is_editor is False
        assert r.is_qa is False

    def test_qa_role(self):
        r = self._make(roles=["qa"])
        assert r.is_qa is True
        assert r.is_editor is False

    def test_multiple_roles(self):
        r = self._make(roles=["admin", "editor"])
        assert r.is_admin is True
        assert r.is_editor is True
        assert r.is_qa is False

