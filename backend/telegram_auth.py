"""
telegram_auth.py
─────────────────
Handles per-user Telegram authentication flow:
  1. User enters their api_id, api_hash, phone → stored encrypted
  2. We initiate a Telethon session → Telegram sends OTP to their app
  3. User enters OTP → session saved to data/sessions/{user_id}.session
  4. Scraper can now poll channels using that session
"""
import os, asyncio, logging
from pathlib import Path
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

log = logging.getLogger(__name__)
SESSIONS_DIR = Path("data/sessions")
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# In-memory store for pending auth clients (user_id → TelegramClient)
# These are short-lived — created during OTP flow, disposed after
_pending_clients: dict[int, TelegramClient] = {}


def session_path(user_id: int) -> str:
    return str(SESSIONS_DIR / str(user_id))


def has_session(user_id: int) -> bool:
    return Path(f"{session_path(user_id)}.session").exists()


async def send_otp(user_id: int, api_id: int, api_hash: str, phone: str) -> dict:
    """
    Start Telethon client for this user and request OTP.
    Returns {"ok": True, "phone_code_hash": "..."} on success.
    """
    # Clean up any existing pending client for this user
    if user_id in _pending_clients:
        try:
            await _pending_clients[user_id].disconnect()
        except Exception:
            pass

    client = TelegramClient(session_path(user_id), api_id, api_hash)
    try:
        await client.connect()
        result = await client.send_code_request(phone)
        _pending_clients[user_id] = client
        return {"ok": True, "phone_code_hash": result.phone_code_hash}
    except Exception as e:
        await client.disconnect()
        log.error(f"send_otp error for user {user_id}: {e}")
        return {"ok": False, "error": str(e)}


async def verify_otp(user_id: int, phone: str, code: str, phone_code_hash: str) -> dict:
    """
    Submit OTP and complete authentication.
    Session file is saved automatically by Telethon.
    """
    client = _pending_clients.get(user_id)
    if not client:
        return {"ok": False, "error": "No pending auth session. Request OTP again."}

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        me = await client.get_me()
        await client.disconnect()
        del _pending_clients[user_id]
        log.info(f"User {user_id} authenticated as @{me.username}")
        return {"ok": True, "telegram_username": me.username or me.first_name}
    except PhoneCodeInvalidError:
        return {"ok": False, "error": "Invalid OTP code. Try again."}
    except SessionPasswordNeededError:
        return {"ok": False, "error": "2FA enabled on this Telegram account. Disable it temporarily or use an account without 2FA."}
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
