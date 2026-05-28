"""
models/schemas.py

所有 API 的 Pydantic request / response schema。
"""

from pydantic import BaseModel, Field, field_validator, model_validator, BeforeValidator
from typing import Optional, List, Annotated
from datetime import datetime
from enum import Enum
import uuid as _uuid

# asyncpg returns UUID columns as uuid.UUID objects; coerce to str for JSON responses
UUIDStr = Annotated[str, BeforeValidator(lambda v: str(v) if isinstance(v, _uuid.UUID) else v)]


# ── 共用 Enum ─────────────────────────────────────────────────────────────────
class TrackType(str, Enum):
    FAST     = "fast"
    LITERARY = "literary"

class OrderStatus(str, Enum):
    PENDING_PAYMENT = "pending_payment"
    AWAITING_QUOTE  = "awaiting_quote"
    QUOTED          = "quoted"
    PAID            = "paid"
    PROCESSING      = "processing"
    QA_REVIEW       = "qa_review"
    EDITOR_VERIFY   = "editor_verify"
    DELIVERED       = "delivered"
    CANCELLED       = "cancelled"

class FlagLevel(str, Enum):
    MUST_FIX = "must_fix"
    REVIEW   = "review"
    PASS     = "pass"

class ClientType(str, Enum):
    B2C = "b2c"
    B2B = "b2b"


# ── User ──────────────────────────────────────────────────────────────────────
class UserProfileUpdate(BaseModel):
    client_type:     Optional[ClientType] = None
    company_name:    Optional[str] = None
    tax_id:          Optional[str] = None
    invoice_carrier: Optional[str] = None
    bio:             Optional[str] = None

    @model_validator(mode="after")
    def validate_b2b_fields(self):
        if self.client_type == ClientType.B2B and not self.tax_id:
            raise ValueError("tax_id is required for B2B clients")
        return self

class UserProfileResponse(BaseModel):
    id:              UUIDStr
    uid_firebase:    str
    client_type:     str
    company_name:    Optional[str]
    tax_id:          Optional[str]
    invoice_carrier: Optional[str]
    is_admin:        bool
    is_editor:       bool
    is_qa:           bool
    roles:           List[str] = []
    languages:       List['UserLanguage'] = []
    bio:             str = ""
    created_at:      datetime

class UserLanguage(BaseModel):
    source_lang: str
    target_lang: str

class UserLanguageUpdate(BaseModel):
    languages: List[UserLanguage]


# ── Order ─────────────────────────────────────────────────────────────────────
class OrderCreate(BaseModel):
    track_type:      TrackType
    source_lang:     str
    target_lang:     str
    word_count:      int   = Field(..., gt=0, description="原文字數")
    title:           Optional[str] = Field(None, max_length=50, description="訂單標題（選填，不填則自動產生）")
    notes:           Optional[str] = Field(None, max_length=500)
    sample_package:  bool  = False

    @field_validator("target_lang")
    @classmethod
    def validate_lang_pair(cls, v, info):
        src = info.data.get("source_lang")
        if src and src == v:
            raise ValueError("source_lang and target_lang must be different")
        return v

class OrderUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=50, description="訂單標題")

class OrderResponse(BaseModel):
    order_id:            str
    status:              str
    payment_url:         str
    track_type:          str
    word_count:          int
    price_ntd:           int
    has_sample_package:  bool = False
    created_at:          datetime

class OrderDetail(BaseModel):
    id:                 UUIDStr
    track_type:         str
    status:             str
    source_lang:        str
    target_lang:        str
    word_count:         int
    price_ntd:          Optional[int]
    quoted_price:       Optional[int] = None
    reference_price:    Optional[int] = None
    title:              Optional[str]
    notes:              Optional[str]
    has_sample_package: bool = False
    created_at:         datetime
    deadline_at:        Optional[datetime]
    delivered_at:       Optional[datetime]
    payment_status:     Optional[str]
    invoice_no:         Optional[str]
    gcs_output_path:    Optional[str]
    gcs_bilingual_output_path: Optional[str] = None
    gcs_plain_text_output_path: Optional[str] = None
    gcs_upload_path:    Optional[str] = None
    proofreader_notes:  Optional[str] = None

class AdminOrderDetail(OrderDetail):
    qa_result: Optional[dict] = None
    editor_id: Optional[UUIDStr] = None
    qa_id:     Optional[UUIDStr] = None
    proofreader_id: Optional[UUIDStr] = None
    assignment_status: Optional[str] = None

class OrderListResponse(BaseModel):
    orders: List[OrderDetail]
    total:  int


# ── File ──────────────────────────────────────────────────────────────────────
class UploadUrlRequest(BaseModel):
    order_id:     str
    filename:     str = Field(..., description="原始檔案名稱（含副檔名）")
    content_type: str = Field("text/plain", description="MIME type")
    file_size:    int = Field(..., description="File size in bytes")

class UploadUrlResponse(BaseModel):
    signed_url: str
    gcs_path:   str
    expires_in: int = 1800  # 秒

class DownloadUrlResponse(BaseModel):
    signed_url: str
    expires_in: int = 900

class OriginalContentResponse(BaseModel):
    filename: str
    content_type: str
    html: str


# ── Pipeline Job ──────────────────────────────────────────────────────────────
class QAResultLayer(BaseModel):
    pass_: bool = Field(..., alias="pass")
    flags: int  = 0
    score: Optional[float] = None

    class Config:
        populate_by_name = True

class QAResult(BaseModel):
    layer1_structure:   Optional[QAResultLayer] = None
    layer2_semantic:    Optional[QAResultLayer] = None
    layer3_terminology: Optional[QAResultLayer] = None
    layer4_llm_judge:   Optional[QAResultLayer] = None

class PipelineJobResponse(BaseModel):
    id:              UUIDStr
    job_type:        str
    status:          str
    qa_result:       Optional[dict]
    retry_count:     int
    error_message:   Optional[str]
    started_at:      Optional[datetime]
    finished_at:     Optional[datetime]


# ── QA Flag ───────────────────────────────────────────────────────────────────
class QAFlagResponse(BaseModel):
    id:                 UUIDStr
    job_id:             UUIDStr
    order_id:           UUIDStr
    paragraph_index:    int
    flag_level:         str
    flag_type:          str
    source_segment:     Optional[str]
    translated_segment: Optional[str]
    reviewer_note:      Optional[str]
    resolved:           bool
    flagged_at:         datetime

class QAFlagResolve(BaseModel):
    reviewer_note: str = Field(..., min_length=1, max_length=1000)


# ── Admin: 付款確認（手動匯款用）────────────────────────────────────────────
class PaymentConfirm(BaseModel):
    confirmed_amount_ntd: int = Field(..., gt=0, description="確認的匯款金額")
    note:                 Optional[str] = None


# ── Admin: Literary Track 指派 ────────────────────────────────────────────────
class QuoteUpdate(BaseModel):
    quoted_price: int = Field(..., gt=0, description="報價金額（NTD）")
    admin_notes: Optional[str] = Field(None, description="報價備注")

class AssignmentUpdate(BaseModel):
    editor_id:       Optional[str] = None
    proofreader_id:  Optional[str] = None

class AssignmentResponse(BaseModel):
    id:                     UUIDStr
    order_id:               UUIDStr
    editor_id:              Optional[UUIDStr]
    qa_id:                  Optional[UUIDStr] = None
    proofreader_id:         Optional[UUIDStr]
    status:                 str
    assigned_at:            datetime
    editor_submitted_at:    Optional[datetime]
    proofread_submitted_at: Optional[datetime]
    qa_submitted_at:        Optional[datetime] = None
    editor_notes:           Optional[str] = None
    proofreader_notes:      Optional[str] = None


class QAFlagListResponse(BaseModel):
    flags: List[QAFlagResponse]
    total: int

class AssignmentListResponse(BaseModel):
    assignments: List[AssignmentResponse]
    total: int

# ── Literary Track: Assignment Actions ───────────────────────────────────────
class AssignmentAction(BaseModel):
    """Assign editor or proofreader to a literary track order."""
    role: str = Field(..., description="editor or proofreader")
    user_id: Optional[str] = None
    email: Optional[str] = None

class AssignmentComplete(BaseModel):
    """Mark editor or proofreader work as complete."""
    role: str = Field(..., description="editor or proofreader")

# ── Admin: 帳號管理 ───────────────────────────────────────────────────────────
class UserListItem(BaseModel):
    id:           UUIDStr
    uid_firebase: str
    email:        Optional[str]
    client_type:  str
    disabled:     bool
    created_at:   datetime
    is_admin:     bool
    is_editor:    bool
    is_qa:        bool
    admin_role:   Optional[str]
    languages:    List[UserLanguage] = []

class UserListResponse(BaseModel):
    users: List[UserListItem]
    total: int

class UserUpdateRequest(BaseModel):
    disabled:  Optional[bool] = None
    is_admin:  Optional[bool] = None
    is_editor: Optional[bool] = None
    is_qa:     Optional[bool] = None


# ── Admin: QA Review Editor ──────────────────────────────────────────────────
class QASegment(BaseModel):
    index:          int
    source:         str
    translated:     str
    raw:            Optional[str] = None
    comments:       Optional[str] = None
    editor_comments: Optional[str] = None
    proofreader_comments: Optional[str] = None
    flags:          List[QAFlagResponse] = []

class QASegmentListResponse(BaseModel):
    segments: List[QASegment]

class QASegmentUpdate(BaseModel):
    index:      int
    translated: str
    comments:   Optional[str] = None
    editor_comments: Optional[str] = None
    proofreader_comments: Optional[str] = None

class QASegmentsBatchUpdate(BaseModel):
    segments: List[QASegmentUpdate]


class EditorAssignRequest(BaseModel):
    editor_id: Optional[str] = None
    qa_id:     Optional[str] = None


# ── Sample Translation Package ────────────────────────────────────────────────
class BookFactSheet(BaseModel):
    title:      Optional[str] = None
    author:     Optional[str] = None
    publisher:  Optional[str] = None
    pub_date:   Optional[str] = None
    word_count: Optional[str] = None
    category:   Optional[str] = None
    sales:      Optional[str] = None


class SamplePackageResponse(BaseModel):
    id:              UUIDStr
    order_id:        UUIDStr
    status:          str
    translator_bio:  str = ""
    book_fact_sheet: dict = {}
    synopsis:        str = ""
    market_analysis: str = ""
    notes:           Optional[str] = None
    updated_at:      Optional[datetime] = None
    updated_by:      Optional[UUIDStr] = None


class SamplePackageUpdate(BaseModel):
    translator_bio:  Optional[str] = None
    book_fact_sheet: Optional[dict] = None
    synopsis:        Optional[str] = None
    market_analysis: Optional[str] = None
    notes:           Optional[str] = None


class SamplePackageGenerateResponse(BaseModel):
    message:         str
    translator_bio:  str = ""
    book_fact_sheet: dict = {}
    synopsis:        str = ""
    market_analysis: str = ""


# ── Support Files ─────────────────────────────────────────────────────────────
class SupportFileResponse(BaseModel):
    id:           UUIDStr
    order_id:     UUIDStr
    filename:     str
    content_type: str
    file_size:    int
    gcs_path:     str
    file_role:    str
    created_at:   datetime

class SupportFileListResponse(BaseModel):
    files: List[SupportFileResponse]
    total: int


# ── Invitations ──────────────────────────────────────────────────────────────
class InvitationCreate(BaseModel):
    email: str
    role:  str  # 'editor' or 'qa'

class InvitationResponse(BaseModel):
    id:         UUIDStr
    email:      str
    role:       str
    token:      str
    status:     str
    created_at: datetime
    expires_at: datetime

class InvitationAccept(BaseModel):
    token: str


# ── 共用回傳 ──────────────────────────────────────────────────────────────────
class MessageResponse(BaseModel):
    message: str

class ErrorResponse(BaseModel):
    detail: str


# ── Language Configs ────────────────────────────────────────────────────────
class LanguageConfigResponse(BaseModel):
    id:               int
    code:             str
    label_zh:         str
    label_en:         str
    direction:        str
    is_active:        bool
    sort_order:       int
    price_multiplier: float
    created_at:       datetime

class LanguageConfigCreate(BaseModel):
    code:             str
    direction:        str
    sort_order:       Optional[int] = 0
    price_multiplier: Optional[float] = 1.0

class LanguageConfigUpdate(BaseModel):
    label_zh:         Optional[str] = None
    label_en:         Optional[str] = None
    direction:        Optional[str] = None
    is_active:        Optional[bool] = None
    sort_order:       Optional[int] = None
    price_multiplier: Optional[float] = None

class LanguageConfigListResponse(BaseModel):
    languages: List[LanguageConfigResponse]

class SupportedLanguageResponse(BaseModel):
    code:             str
    label_zh:         str
    label_en:         str
    default_direction: str


# ── Token Usage ───────────────────────────────────────────────────────────────
class TokenUsageItem(BaseModel):
    job_type:          str
    model:             str
    prompt_tokens:     int
    candidates_tokens: int
    total_tokens:      int
    input_rate:        float
    output_rate:       float
    cost_usd:          float

class TokenUsageResponse(BaseModel):
    order_id:          str
    total_prompt:      int
    total_candidates:  int
    total_tokens:      int
    total_cost_usd:    float
    breakdown:         list[TokenUsageItem]

class TokenUsageDetailItem(BaseModel):
    job_type:          str
    model:             str
    prompt_tokens:     int
    candidates_tokens: int
    total_tokens:      int
    input_rate:        float
    output_rate:       float
    cost_usd:          float
    created_at:        datetime

class TokenUsageDetailResponse(BaseModel):
    order_id:          str
    items:             list[TokenUsageDetailItem]
