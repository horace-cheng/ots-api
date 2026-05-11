from abc import ABC, abstractmethod


class NotificationProvider(ABC):

    @abstractmethod
    def send_email(
        self,
        to: str,
        subject: str,
        body_html: str,
        body_text: str = "",
    ) -> dict:
        ...
