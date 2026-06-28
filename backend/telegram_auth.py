"""
telegram_auth.py
─────────────────
Handles per-user Telegram authentication flow.
Session is stored in the DATABASE (not filesystem) so it survives Render redeploys.
"""
import os, logging
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

log = logging.getLogger(__name__)

# Temp dir for pending auth only (not persisted)
SESSIONS_DIR = Path("data/sessions")
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# In-memory store for pending auth clients (user_id → TelegramClient)
_pending_clients: dict[int, TelegramClient] = {}
_pending_strings: dict[int, str] = {}  # stores StringSession during OTP flow


def has_session(user_id: int) -> bool:
    """Check if user has a session string stored (checked via DB in scraper)."""
    return Path(f"data/sessions/{user_id}.session").exists() or user_id in _pending_strings


async def has_db_session(user_id: int, db: AsyncSession) -> bool:
    """Check if user has a session string in the database."""
    from backend.database import User
    user = await db.get(User, user_id)
    return bool(user and user.tg_session)


async def send_otp(user_id: int, api_id: int, api_hash: str, phone: str) -> dict:
    """Start Telethon client and request OTP using StringSession."""
    if user_id in _pending_clients:
        try:
            await _pending_clients[user_id].disconnect()
        except Exception:
            pass

    # Use StringSession so we can store it in DB later
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
        result = await client.send_code_request(phone)
        _pending_clients[user_id] = client
        return {"ok": True, "phone_code_hash": result.phone_code_hash}
    except Exception as e:
        await client.disconnect()
        log.error(f"send_otp error for user {user_id}: {e}")
        return {"ok": False, "error": str(e)}


async def verify_otp(user_id: int, phone: str, code: str, phone_code_hash: str, db: AsyncSession) -> dict:
    """Submit OTP, save session string to database."""
    from backend.database import User
    from backend.auth import encrypt

    client = _pending_clients.get(user_id)
    if not client:
        return {"ok": False, "error": "No pending auth session. Request OTP again."}
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        me = await client.get_me()

        # Save session string to database (encrypted)
        session_string = client.session.save()
        await client.disconnect()
        del _pending_clients[user_id]

        # Store in user record
        user = await db.get(User, user_id)
        if user:
            user.tg_session = encrypt(session_string)
            await db.commit()

        log.info(f"User {user_id} authenticated as @{me.username}, session saved to DB")
        return {"ok": True, "telegram_username": me.username or me.first_name}
    except PhoneCodeInvalidError:
        return {"ok": False, "error": "Invalid OTP code. Try again."}
    except SessionPasswordNeededError:
        return {"ok": False, "error": "2FA enabled. Disable it temporarily or use an account without 2FA."}
    except Exception as e:
        log.error(f"verify_otp error for user {user_id}: {e}")
        return {"ok": False, "error": str(e)}


async def disconnect_user(user_id: int):
    """Clean up pending client if user cancels auth."""
    if user_id in _pending_clients:
        try:
            await _pending_clients[user_id].disconnect()
        except Exception:
            pass
        del _pending_clients[user_id]