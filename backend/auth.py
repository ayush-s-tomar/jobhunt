"""
auth.py — Authentication, encryption, JWT
Security principles:
  - Passwords: bcrypt (never stored plain)
  - Telegram credentials: Fernet symmetric encryption (needs to be decryptable)
  - JWT: HS256, 24hr expiry, HttpOnly cookie
  - Rate limiting: applied at route level via slowapi
"""
import os, bcrypt
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Cookie, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from backend.database import get_db, User

# ── Keys — loaded from .env, never hardcoded ──────────────────────────────────
SECRET_KEY    = os.getenv("SECRET_KEY", "")
FERNET_KEY    = os.getenv("FERNET_KEY", "").encode()   # 32-byte base64 key
ALGORITHM     = "HS256"
TOKEN_EXPIRE  = 24   # hours

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY not set in .env")
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY not set in .env — run: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")

fernet = Fernet(FERNET_KEY)


# ── Password helpers ──────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── Encryption helpers (for Telegram credentials) ─────────────────────────────
def encrypt(value: str) -> str:
    """Encrypt a string before storing in DB."""
    return fernet.encrypt(value.encode()).decode()

def decrypt(value: str) -> str:
    """Decrypt a string retrieved from DB."""
    return fernet.decrypt(value.encode()).decode()


# ── JWT helpers ───────────────────────────────────────────────────────────────
def create_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE)
    return jwt.encode({"sub": str(user_id), "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        return None


# ── Current user dependency ───────────────────────────────────────────────────
async def get_current_user(
    access_token: Optional[str] = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI dependency — validates JWT from HttpOnly cookie."""
    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_id = decode_token(access_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    return user
