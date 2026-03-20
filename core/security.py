"""
core/security.py
─────────────────
JWT token creation + verification
Password hashing with bcrypt
API key encryption with Fernet (AES-128-CBC)
"""

from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from cryptography.fernet import Fernet
import base64
import hashlib
import structlog

from config.settings import get_settings

settings = get_settings()
log = structlog.get_logger(__name__)

# ── Password hashing ──────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

# ── JWT tokens ────────────────────────────────────────────────────────────────

def create_access_token(subject: str, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.utcnow() + (
        expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )
    payload = {
        "sub": subject,
        "exp": expire,
        "iat": datetime.utcnow(),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

def create_refresh_token(subject: str) -> str:
    expire = datetime.utcnow() + timedelta(days=settings.jwt_refresh_token_expire_days)
    payload = {
        "sub": subject,
        "exp": expire,
        "iat": datetime.utcnow(),
        "type": "refresh",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

def decode_token(token: str) -> dict:
    """Decode and validate a JWT. Raises JWTError on failure."""
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])

# ── API key encryption (for BYOK keys stored in DB) ──────────────────────────

def _fernet() -> Fernet:
    """
    Derive a Fernet key from the app secret key.
    Fernet requires a 32-byte URL-safe base64 key.
    """
    raw = hashlib.sha256(settings.app_secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))

def encrypt_api_key(plaintext: str) -> str:
    """Encrypt a user's AI provider API key for storage."""
    return _fernet().encrypt(plaintext.encode()).decode()

def decrypt_api_key(ciphertext: str) -> str:
    """Decrypt a stored AI provider API key for runtime use."""
    return _fernet().decrypt(ciphertext.encode()).decode()

# ── MQTT key generation ───────────────────────────────────────────────────────

import secrets

def generate_mqtt_key() -> str:
    """Generate a secure random MQTT password for a farm."""
    return secrets.token_urlsafe(32)
