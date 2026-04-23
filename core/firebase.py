import firebase_admin
from firebase_admin import auth
import logging

logger = logging.getLogger(__name__)


def init_firebase():
    """
    Cloud Run 環境下使用 Application Default Credentials（ADC）。
    本機開發時需先執行：
        gcloud auth application-default login
    或設定 GOOGLE_APPLICATION_CREDENTIALS 指向 Service Account JSON。
    """
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
        logger.info("Firebase Admin SDK initialized")


def verify_firebase_token(token: str) -> dict:
    """
    驗證 Firebase ID Token，回傳 decoded payload。
    失敗時拋出 ValueError。
    """
    try:
        decoded = auth.verify_id_token(token)
        return decoded
    except auth.ExpiredIdTokenError:
        raise ValueError("Token expired")
    except auth.RevokedIdTokenError:
        raise ValueError("Token revoked")
    except auth.InvalidIdTokenError:
        raise ValueError("Invalid token")
    except Exception as e:
        raise ValueError(f"Token verification failed: {e}")
