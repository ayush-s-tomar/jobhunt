"""
telegram_scraper.py — Per-user scraping using their own session
"""
import os, re, asyncio, logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

try:
    from telethon import TelegramClient
    from telethon.tl.types import MessageEntityUrl, MessageEntityTextUrl
except ImportError:
    raise SystemExit("Run: pip install telethon tgcrypto")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from backend.database import AsyncSessionLocal, Channel, Job
from sqlalchemy import select

SESSIONS_DIR  = Path("data/sessions")
MAX_PER_POLL  = int(os.getenv("MAX_JOBS_PER_POLL", "50"))

JOB_KEYWORDS = [
    "hiring", "we are hiring", "job opening", "job opportunity",
    "urgent hiring", "vacancy", "looking for", "apply now", "apply here",
    "software engineer", "developer", "backend", "frontend", "fullstack",
    "data engineer", "ml engineer", "devops", "sde ", "sde1", "sde2",
    "fresher", "experience required", "yrs exp", "lpa", "ctc",
]

EMAIL_RE  = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
URL_RE    = re.compile(r"https?://[^\s<>\"]+")
SALARY_RE = re.compile(r"(?:\d+(?:\.\d+)?)\s*[-–to]+\s*(?:\d+(?:\.\d+)?)\s*(?:lpa|lakh|ctc|k|per month)", re.I)


def is_job_post(text): return len(text) > 50
def extract_email(text):
    m = EMAIL_RE.search(text); return m.group(0) if m else None
def extract_url(text, entities=None):
    if entities:
        for ent in entities:
            if isinstance(ent, MessageEntityTextUrl): return ent.url
    m = URL_RE.search(text); return m.group(0) if m else None
def extract_salary(text):
    m = SALARY_RE.search(text); return m.group(0) if m else None
def parse_title(text):
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#","🔥","📢","👉","✅","💼")): return line[:100]
    return text[:80] if text else None


async def scrape_for_user(user_id: int, api_id: int, api_hash: str, channels: list):
    """Scrape all channels for a specific user using their session."""
    from telethon.sessions import StringSession
    from backend.auth import decrypt
    async with AsyncSessionLocal() as db:
        from backend.database import User
        user = await db.get(User, user_id)
        if not user or not user.tg_session:
            log.error(f"No session for user {user_id}")
            return
        session_string = decrypt(user.tg_session)
    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            log.error(f"User {user_id} session expired")
            return

        total = 0
        for ch in channels:
            try:
                entity = await client.get_entity(ch.username)
                new_jobs = 0

                async with AsyncSessionLocal() as db:
                    async for msg in client.iter_messages(entity, limit=100):
                        if not msg.text: continue

                        # Check duplicate
                        dup = await db.execute(
                            select(Job).where(Job.channel_id == ch.id, Job.message_id == msg.id)
                        )
                        if dup.scalar_one_or_none(): continue

                        email = extract_email(msg.text)
                        url   = extract_url(msg.text, msg.entities)
                        if url and "t.me" in url and not email:
                            url = None

                        job = Job(
                            user_id     = user_id,
                            channel_id  = ch.id,
                            message_id  = msg.id,
                            raw_text    = msg.text,
                            title       = parse_title(msg.text),
                            salary      = extract_salary(msg.text),
                            apply_email = email,
                            apply_url   = url,
                            apply_type  = "email" if email else ("url" if url else "manual"),
                            is_remote   = "remote" in msg.text.lower(),
                            posted_at   = msg.date.replace(tzinfo=None),
                        )
                        db.add(job)
                        new_jobs += 1

                    ch.last_polled = datetime.utcnow()
                    ch.job_count   = (ch.job_count or 0) + new_jobs
                    await db.commit()

                log.info(f"User {user_id} | {ch.username}: +{new_jobs} jobs")
                total += new_jobs

            except Exception as e:
                log.warning(f"Error scraping {ch.username} for user {user_id}: {e}")

        log.info(f"User {user_id}: scrape complete, +{total} total")
    finally:
        await client.disconnect()
