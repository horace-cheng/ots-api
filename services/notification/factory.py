import logging
from functools import lru_cache
import os

from .provider import NotificationProvider

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_notification_provider() -> NotificationProvider:
    provider = os.environ.get("EMAIL_PROVIDER", "smtp").lower().strip()
    from_email = os.environ.get("EMAIL_FROM_ADDRESS", "noreply@ots.tw")
    from_name = os.environ.get("EMAIL_FROM_NAME", "OTS 翻譯服務")
    logger.info(f"Initializing notification provider: {provider}, from={from_email}, name={from_name!r}")

    if provider == "brevo":
        api_key = os.environ.get("BREVO_API_KEY", "")
        if not api_key:
            raise ValueError("BREVO_API_KEY is required when EMAIL_PROVIDER=brevo")
        from .brevo import BrevoNotificationProvider
        return BrevoNotificationProvider(api_key, from_email, from_name)

    elif provider == "smtp":
        host = os.environ.get("SMTP_HOST", "localhost")
        port = int(os.environ.get("SMTP_PORT", "1025"))
        username = os.environ.get("SMTP_USERNAME", "")
        password = os.environ.get("SMTP_PASSWORD", "")
        use_tls = os.environ.get("SMTP_USE_TLS", "false").lower() == "true"
        logger.info(f"SMTP config: host={host}, port={port}, tls={use_tls}, auth={bool(username)}")
        from .smtp import SmtpNotificationProvider
        return SmtpNotificationProvider(
            from_email, from_name, host, port, username, password, use_tls,
        )

    else:
        raise ValueError(
            f"Unknown EMAIL_PROVIDER: '{provider}'. "
            f"Supported values: brevo, smtp"
        )
