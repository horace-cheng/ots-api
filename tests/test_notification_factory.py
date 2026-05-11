import os
from unittest.mock import patch

from services.notification.provider import NotificationProvider
from services.notification.smtp import SmtpNotificationProvider


def test_factory_smtp_default():
    from services.notification.factory import get_notification_provider

    with patch.dict(os.environ, {
        "EMAIL_PROVIDER": "smtp",
        "SMTP_HOST": "localhost",
        "SMTP_PORT": "1025",
    }, clear=False):
        provider = get_notification_provider()
        assert isinstance(provider, SmtpNotificationProvider)
        assert isinstance(provider, NotificationProvider)


def test_factory_unknown_provider():
    from services.notification.factory import get_notification_provider

    with patch.dict(os.environ, {"EMAIL_PROVIDER": "unknown"}, clear=False):
        try:
            get_notification_provider.cache_clear()
            get_notification_provider()
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "Unknown EMAIL_PROVIDER" in str(e)
