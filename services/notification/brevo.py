import logging

from .provider import NotificationProvider

logger = logging.getLogger(__name__)


class BrevoNotificationProvider(NotificationProvider):

    def __init__(self, api_key: str, from_email: str, from_name: str):
        self._api_key = api_key
        self._from_email = from_email
        self._from_name = from_name
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        import sib_api_v3_sdk
        config = sib_api_v3_sdk.Configuration()
        config.api_key["api-key"] = self._api_key
        self._client = sib_api_v3_sdk.TransactionalEmailsApi(
            sib_api_v3_sdk.ApiClient(config)
        )
        return self._client

    def send_email(
        self,
        to: str,
        subject: str,
        body_html: str,
        body_text: str = "",
    ) -> dict:
        import sib_api_v3_sdk
        client = self._get_client()
        send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
            sender={"name": self._from_name, "email": self._from_email},
            to=[{"email": to}],
            subject=subject,
            html_content=body_html,
            text_content=body_text or None,
        )
        resp = client.send_transac_email(send_smtp_email)
        logger.info(f"Brevo email sent to {to}: subject={subject!r}, message_id={resp.message_id}")
        return {"message_id": resp.message_id, "provider": "brevo"}
