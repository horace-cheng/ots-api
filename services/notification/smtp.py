import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .provider import NotificationProvider

logger = logging.getLogger(__name__)


class SmtpNotificationProvider(NotificationProvider):

    def __init__(
        self,
        from_email: str,
        from_name: str,
        host: str = "localhost",
        port: int = 1025,
        username: str = "",
        password: str = "",
        use_tls: bool = False,
    ):
        self._from_email = from_email
        self._from_name = from_name
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls

    def send_email(
        self,
        to: str,
        subject: str,
        body_html: str,
        body_text: str = "",
    ) -> dict:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{self._from_name} <{self._from_email}>"
        msg["To"] = to
        msg["Subject"] = subject

        if body_text:
            msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(self._host, self._port) as server:
            if self._use_tls:
                server.starttls()
            if self._username:
                server.login(self._username, self._password)
            server.sendmail(self._from_email, [to], msg.as_string())

        logger.info(f"SMTP email sent to {to}: subject={subject!r}")
        return {"provider": "smtp", "to": to, "subject": subject}
