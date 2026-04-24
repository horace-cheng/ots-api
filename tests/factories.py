"""Shared test constants and builders."""

MOCK_FIREBASE_DECODED = {
    "uid": "firebase-uid-001",
    "email": "user@ots.tw",
    "email_verified": True,
    "name": "Test User",
}

MOCK_USER = {
    "uid": "firebase-uid-001",
    "email": "user@ots.tw",
    "user_id": "db-user-id-001",
    "client_type": "b2c",
}

MOCK_ADMIN_USER = {
    "uid": "admin-uid-001",
    "email": "admin@ots.tw",
    "user_id": "admin-db-id-001",
    "client_type": "b2c",
}
