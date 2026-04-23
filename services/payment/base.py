"""
services/payment/base.py

金流服務抽象層。
所有金流實作必須繼承 PaymentGateway 並實作全部 abstract method。
業務邏輯（routers/payments.py）只依賴這個介面，不直接碰任何金流 SDK。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PaymentMethod(str, Enum):
    CREDIT_CARD  = "credit_card"
    ATM          = "atm"
    CVS          = "cvs"           # 超商代碼
    WIRE         = "wire_transfer"  # 銀行電匯（B2B 手動）


class PaymentStatus(str, Enum):
    PENDING   = "pending"
    PAID      = "paid"
    FAILED    = "failed"
    REFUNDED  = "refunded"


class InvoiceType(str, Enum):
    B2C_CLOUD      = "b2c_cloud"       # 二聯式雲端發票
    B2B_TRIPLICATE = "b2b_triplicate"  # 三聯式（B2B 統編）


@dataclass
class PaymentRequest:
    """建立付款所需資料"""
    order_id:    str
    amount_ntd:  int
    description: str
    return_url:  str         # 付款完成後導回的頁面
    notify_url:  str         # Webhook callback URL
    method:      PaymentMethod = PaymentMethod.CREDIT_CARD


@dataclass
class PaymentResult:
    """建立付款回傳結果"""
    gateway_trade_no: str         # 金流端的交易編號
    payment_url:      str         # 導引客戶前往的付款頁面 URL
    raw:              dict        # 金流原始回應（留存備查）


@dataclass
class WebhookPayload:
    """Webhook 回調解析後的結構化資料"""
    order_id:         str
    gateway_trade_no: str
    status:           PaymentStatus
    amount_ntd:       int
    paid_at:          Optional[str]  # ISO 8601 string
    raw:              dict


@dataclass
class InvoiceRequest:
    """開立電子發票所需資料"""
    order_id:     str
    amount_ntd:   int
    invoice_type: InvoiceType
    # B2C
    carrier:      Optional[str] = None  # 手機條碼（/XXXXXXX 格式）
    email:        Optional[str] = None
    # B2B
    tax_id:       Optional[str] = None  # 統一編號
    company_name: Optional[str] = None


@dataclass
class InvoiceResult:
    """開立電子發票回傳結果"""
    invoice_no:  str          # 發票號碼（例：AB12345678）
    issued_at:   str          # ISO 8601 string
    raw:         dict


class PaymentGateway(ABC):
    """
    金流服務抽象介面。

    實作時需注意：
    - create_payment()  必須是冪等的（相同 order_id 重複呼叫應回傳相同結果）
    - parse_webhook()   必須驗證簽章，驗證失敗拋出 ValueError
    - issue_invoice()   B2C 自動呼叫，B2B 由出納手動在後台操作
    """

    @abstractmethod
    def create_payment(self, req: PaymentRequest) -> PaymentResult:
        """
        建立付款交易，回傳付款頁面 URL。
        失敗時拋出 PaymentError。
        """
        ...

    @abstractmethod
    def parse_webhook(self, raw_body: dict) -> WebhookPayload:
        """
        解析並驗證金流 Webhook 回調。
        簽章驗證失敗時拋出 ValueError。
        """
        ...

    @abstractmethod
    def issue_invoice(self, req: InvoiceRequest) -> InvoiceResult:
        """
        開立電子發票。
        失敗時拋出 InvoiceError。
        """
        ...

    @abstractmethod
    def refund(self, gateway_trade_no: str, amount_ntd: int) -> bool:
        """
        退款。成功回傳 True，失敗拋出 PaymentError。
        """
        ...


# ── 自訂例外 ──────────────────────────────────────────────────────────────────
class PaymentError(Exception):
    def __init__(self, message: str, code: str = "", raw: dict = None):
        super().__init__(message)
        self.code = code
        self.raw  = raw or {}

class InvoiceError(Exception):
    def __init__(self, message: str, code: str = "", raw: dict = None):
        super().__init__(message)
        self.code = code
        self.raw  = raw or {}
