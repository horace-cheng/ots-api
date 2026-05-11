from unittest.mock import MagicMock, patch

from services.notification.smtp import SmtpNotificationProvider


def test_smtp_send_email():
    provider = SmtpNotificationProvider(
        from_email="noreply@ots.tw",
        from_name="OTS",
        host="localhost",
        port=1025,
    )

    with patch("services.notification.smtp.smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        result = provider.send_email(
            to="user@test.com",
            subject="Test Subject",
            body_html="<p>Hello</p>",
            body_text="Hello",
        )

    assert result["provider"] == "smtp"
    assert result["to"] == "user@test.com"
    mock_server.sendmail.assert_called_once_with(
        "noreply@ots.tw", ["user@test.com"], mock_server.sendmail.call_args[0][2]
    )


def test_smtp_with_tls_and_auth():
    provider = SmtpNotificationProvider(
        from_email="noreply@ots.tw",
        from_name="OTS",
        host="smtp.example.com",
        port=587,
        username="user",
        password="pass",
        use_tls=True,
    )

    with patch("services.notification.smtp.smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        provider.send_email(
            to="user@test.com",
            subject="Test",
            body_html="<p>Test</p>",
        )

    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_once_with("user", "pass")


def test_smtp_no_auth():
    provider = SmtpNotificationProvider(
        from_email="noreply@ots.tw",
        from_name="OTS",
        host="localhost",
        port=1025,
    )

    with patch("services.notification.smtp.smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        provider.send_email(
            to="user@test.com",
            subject="Test",
            body_html="<p>Test</p>",
        )

    mock_server.starttls.assert_not_called()
    mock_server.login.assert_not_called()
