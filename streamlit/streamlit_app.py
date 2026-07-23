"""
JobHunt — Streamlit Edition
Single-user demo build. See README_STREAMLIT.md for what changed vs the FastAPI version.

v2 additions:
  - Optional Supabase Postgres persistence (falls back to ephemeral SQLite if unset)
  - Kanban-style application tracker (Board tab)
  - Duplicate job detection (same title+company posted across channels)
  - Telegram digest of new high-match jobs (manual trigger + cron-friendly script)
"""
import os, json, sqlite3, asyncio, time, re
from datetime import datetime, timedelta
from pathlib import Path

import requests
import streamlit as st
from groq import Groq
from cryptography.fernet import Fernet

st.set_page_config(page_title="JobHunt", page_icon="📡", layout="wide")

# ── Config / secrets ──────────────────────────────────────────────────────
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")
GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", os.getenv("GROQ_API_KEY", ""))
FERNET_KEY   = st.secrets.get("FERNET_KEY", os.getenv("FERNET_KEY", ""))

# Set SUPABASE_DB_URL in secrets (postgresql://...) to switch on persistence.
# Leave unset and the app falls back to ephemeral SQLite, same as before.
SUPABASE_DB_URL = st.secrets.get("SUPABASE_DB_URL", os.getenv("SUPABASE_DB_URL", ""))
USE_POSTGRES = bool(SUPABASE_DB_URL)

# Set these to enable the "Send digest now" button / cron script.
TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_DIGEST_CHAT_ID = st.secrets.get("TELEGRAM_DIGEST_CHAT_ID", os.getenv("TELEGRAM_DIGEST_CHAT_ID", ""))

DB_PATH = "jobhunt.db"  # sqlite fallback path — ephemeral on Streamlit Cloud

fernet = Fernet(FERNET_KEY.encode()) if FERNET_KEY else None
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

MODEL = "llama-3.3-70b-versatile"
COMBINED_SYSTEM = """Parse this job post and score the candidate match. Return ONLY valid JSON, no markdown:
{"title":string|null,"company":string|null,"location":string|null,"salary":string|null,"skills_required":[],"is_remote":bool,"apply_email":string|null,"apply_url":string|null,"score":0-100}"""
COVER_SYSTEM = """Write a concise cover letter (150-200 words) for a software developer in India.
No fluff. Tailor it to the job. Plain text only. Don't start with "I am writing to..."."""

# ── DB layer — SQLite (demo) or Supabase Postgres (persistent) ───────────
# ConnWrapper lets a psycopg2 connection support the same chaining pattern
# sqlite3.Connection gives us: conn.execute(sql, params).fetchone()/.fetchall()
if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    IntegrityErrors = (psycopg2.IntegrityError,)

    class ConnWrapper:
        def __init__(self, raw_conn):
            self._conn = raw_conn

        def execute(self, sql, params=()):
            cur = self._conn.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            return cur

        def commit(self):
            self._conn.commit()

        def rollback(self):
            self._conn.rollback()

        def close(self):
            self._conn.close()
else:
    IntegrityErrors = (sqlite3.IntegrityError,)


def get_conn():
    if USE_POSTGRES:
        raw = psycopg2.connect(SUPABASE_DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return ConnWrapper(raw)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_missing(conn, table, coldef):
    """Idempotent ALTER TABLE, safe to call every startup on old + new DBs."""
    if USE_POSTGRES:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {coldef}")
        conn.commit()
    else:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
            conn.commit()
        except Exception:
            conn.rollback()


def init_db():
    conn = get_conn()
    pk = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    stmts = [
        f"""CREATE TABLE IF NOT EXISTS channels (
            id {pk},
            username TEXT UNIQUE, title TEXT, is_private INTEGER DEFAULT 0,
            job_count INTEGER DEFAULT 0, last_polled TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS jobs (
            id {pk},
            channel_id INTEGER, message_id INTEGER, raw_text TEXT,
            title TEXT, company TEXT, location TEXT, salary TEXT,
            skills_required TEXT DEFAULT '[]', apply_email TEXT, apply_url TEXT,
            is_remote INTEGER DEFAULT 0, match_score REAL DEFAULT 0,
            status TEXT DEFAULT 'new', cover_letter TEXT,
            posted_at TEXT, scraped_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY CHECK (id=1),
            full_name TEXT, phone TEXT, linkedin TEXT, github TEXT,
            portfolio TEXT, years_exp REAL DEFAULT 0, skills TEXT DEFAULT '[]', summary TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS tg_creds (
            id INTEGER PRIMARY KEY CHECK (id=1),
            api_id TEXT, api_hash TEXT, phone TEXT, session_str TEXT
        )""",
    ]
    for s in stmts:
        conn.execute(s)
    conn.commit()

    # Migrations for DBs created before duplicate detection / digest existed
    _add_column_if_missing(conn, "jobs", "duplicate_of INTEGER")
    _add_column_if_missing(conn, "jobs", "digested INTEGER DEFAULT 0")

    conn.close()

init_db()

# ── Auth gate ──────────────────────────────────────────────────────────
def check_password():
    if not APP_PASSWORD:
        return True  # no password set in secrets = open demo
    if st.session_state.get("authed"):
        return True
    pw = st.text_input("Password", type="password")
    if pw == APP_PASSWORD:
        st.session_state.authed = True
        st.rerun()
    elif pw:
        st.error("Wrong password")
    return False

if not check_password():
    st.stop()

# ── Type-safety helpers ───────────────────────────────────────────────
# Groq doesn't always honor the schema exactly — it can return skills_required
# as a comma-separated string instead of a list, or is_remote as "true"/"false"
# strings instead of a real bool. These normalize whatever comes back so
# json.dumps() / int() never crash on it.
def coerce_skills_list(value) -> list:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return []

def coerce_bool_int(value) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(bool(value))
    if isinstance(value, str):
        return int(value.strip().lower() in ("true", "yes", "1", "remote"))
    return 0

def coerce_score(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

# ── Duplicate detection ───────────────────────────────────────────────
def _normalize_key(title, company):
    def clean(s):
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"{clean(title)}|{clean(company)}"

def find_duplicate(conn, job_id, title, company):
    """Same normalized title+company, scraped in the last 21 days, not itself
    already a duplicate. Returns the id of the original if one is found."""
    if not title or not company:
        return None
    key = _normalize_key(title, company)
    cutoff = (datetime.utcnow() - timedelta(days=21)).isoformat()
    candidates = conn.execute(
        "SELECT id, title, company FROM jobs WHERE id != ? AND duplicate_of IS NULL AND scraped_at >= ?",
        (job_id, cutoff)
    ).fetchall()
    for c in candidates:
        if _normalize_key(c["title"], c["company"]) == key:
            return c["id"]
    return None

# ── Telegram digest ───────────────────────────────────────────────────
def send_digest(conn, min_score=70, limit=10):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_DIGEST_CHAT_ID:
        return False, "Set TELEGRAM_BOT_TOKEN and TELEGRAM_DIGEST_CHAT_ID in secrets first."
    jobs = conn.execute(
        "SELECT * FROM jobs WHERE status='new' AND match_score>=? AND digested=0 "
        "ORDER BY match_score DESC LIMIT ?",
        (min_score, limit)
    ).fetchall()
    if not jobs:
        return False, "No new high-match jobs to send."

    lines = [f"📡 JobHunt Digest — {len(jobs)} new match(es)\n"]
    for j in jobs:
        lines.append(f"• {j['title'] or 'Job Opening'} — {j['company'] or 'Unknown'} ({j['match_score']:.0f}% match)")
    text = "\n".join(lines)

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_DIGEST_CHAT_ID, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        return False, f"Send failed: {e}"

    ids = [j["id"] for j in jobs]
    placeholders = ",".join(["?"] * len(ids))
    conn.execute(f"UPDATE jobs SET digested=1 WHERE id IN ({placeholders})", ids)
    conn.commit()
    return True, f"Sent digest with {len(jobs)} job(s)."

# ── Groq helpers ───────────────────────────────────────────────────────
def enrich_job(raw_text: str, profile: dict) -> dict:
    if not groq_client:
        return {}
    profile_summary = f"Skills: {', '.join(profile.get('skills', []))}\nExp: {profile.get('years_exp', 0)}yrs\nSummary: {profile.get('summary', '')}"
    prompt = f"JOB:\n{raw_text[:1500]}\n\nCANDIDATE:\n{profile_summary}"
    for attempt in range(3):
        try:
            resp = groq_client.chat.completions.create(
                model=MODEL, max_tokens=400,
                messages=[{"role": "system", "content": COMBINED_SYSTEM},
                          {"role": "user", "content": prompt}]
            )
            text = resp.choices[0].message.content.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text)
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                time.sleep(3 * (attempt + 1))
            else:
                st.warning(f"Enrich failed: {e}")
                return {}
    return {}

def generate_cover_letter(job_text: str, profile: dict) -> str:
    if not groq_client:
        return "Set GROQ_API_KEY in secrets to generate cover letters."
    prompt = f"Job:\n{job_text[:1500]}\n\nCandidate: {profile.get('full_name')}, {profile.get('years_exp',0)}yrs, skills: {', '.join(profile.get('skills',[]))}\nGitHub: {profile.get('github','')}\nSummary: {profile.get('summary','')}"
    try:
        resp = groq_client.chat.completions.create(
            model=MODEL, max_tokens=400,
            messages=[{"role": "system", "content": COVER_SYSTEM},
                      {"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Could not generate cover letter: {e}"

# ── Telegram (sync wrapper around Telethon async client) ─────────────────
def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)

def tg_send_otp(api_id, api_hash, phone):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    async def _run():
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.connect()
        result = await client.send_code_request(phone)
        session_str = client.session.save()
        await client.disconnect()
        return result.phone_code_hash, session_str
    return run_async(_run())

def tg_verify_otp(api_id, api_hash, phone, code, phone_code_hash, session_str):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    async def _run():
        client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
        await client.connect()
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        me = await client.get_me()
        final_session = client.session.save()
        await client.disconnect()
        return me.username or me.first_name, final_session
    return run_async(_run())

def tg_scrape_channels(api_id, api_hash, session_str, channels, limit_per_channel=50):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    async def _run():
        client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
        await client.connect()
        new_count = 0
        conn = get_conn()
        for ch in channels:
            try:
                entity = await client.get_entity(ch["username"])
                async for msg in client.iter_messages(entity, limit=limit_per_channel):
                    if not msg.text or len(msg.text) < 40:
                        continue
                    exists = conn.execute(
                        "SELECT 1 FROM jobs WHERE channel_id=? AND message_id=?",
                        (ch["id"], msg.id)
                    ).fetchone()
                    if exists:
                        continue
                    conn.execute(
                        "INSERT INTO jobs (channel_id, message_id, raw_text, posted_at, scraped_at) VALUES (?,?,?,?,?)",
                        (ch["id"], msg.id, msg.text, str(msg.date), datetime.utcnow().isoformat())
                    )
                    new_count += 1
                conn.execute("UPDATE channels SET last_polled=?, job_count=job_count+? WHERE id=?",
                             (datetime.utcnow().isoformat(), new_count, ch["id"]))
                conn.commit()
            except Exception as e:
                st.warning(f"Channel {ch['username']}: {e}")
        await client.disconnect()
        conn.close()
        return new_count
    return run_async(_run())

# ── UI ─────────────────────────────────────────────────────────────────
st.title("📡 JobHunt")
st.caption("Telegram job aggregator + AI scoring — Streamlit demo build")

with st.expander("ℹ️ About this build", expanded=False):
    if USE_POSTGRES:
        st.caption("Connected to Supabase Postgres — data persists across sleeps and redeploys.")
    else:
        st.caption(
            "Running on ephemeral SQLite — data resets when this app sleeps or redeploys "
            "(Streamlit Cloud has ephemeral disk). Set SUPABASE_DB_URL in secrets to persist data."
        )

conn = get_conn()

tab_feed, tab_board, tab_channels, tab_profile, tab_telegram = st.tabs(
    ["📋 Job Feed", "📊 Board", "📡 Channels", "👤 Profile", "🔌 Telegram Setup"]
)

# ── Profile tab ───────────────────────────────────────────────────────
with tab_profile:
    row = conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
    with st.form("profile_form"):
        c1, c2 = st.columns(2)
        full_name = c1.text_input("Full name", value=row["full_name"] if row else "")
        years_exp = c2.number_input("Years experience", value=float(row["years_exp"]) if row else 0.0, step=0.5)
        skills = st.text_input("Skills (comma-separated)", value=", ".join(json.loads(row["skills"])) if row else "")
        c3, c4 = st.columns(2)
        linkedin = c3.text_input("LinkedIn URL", value=row["linkedin"] if row else "")
        github = c4.text_input("GitHub URL", value=row["github"] if row else "")
        portfolio = st.text_input("Portfolio URL", value=row["portfolio"] if row else "")
        summary = st.text_area("Summary", value=row["summary"] if row else "")
        if st.form_submit_button("Save profile", type="primary"):
            skills_list = [s.strip() for s in skills.split(",") if s.strip()]
            conn.execute("""
                INSERT INTO profile (id, full_name, years_exp, skills, linkedin, github, portfolio, summary)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name, years_exp=excluded.years_exp,
                    skills=excluded.skills, linkedin=excluded.linkedin, github=excluded.github,
                    portfolio=excluded.portfolio, summary=excluded.summary
            """, (full_name, years_exp, json.dumps(skills_list), linkedin, github, portfolio, summary))
            conn.commit()
            st.success("Profile saved")

def get_profile_dict():
    row = conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
    if not row:
        return {}
    return {
        "full_name": row["full_name"], "years_exp": row["years_exp"],
        "skills": json.loads(row["skills"]), "linkedin": row["linkedin"],
        "github": row["github"], "portfolio": row["portfolio"], "summary": row["summary"],
    }

# ── Telegram tab ──────────────────────────────────────────────────────
with tab_telegram:
    tg_row = conn.execute("SELECT * FROM tg_creds WHERE id=1").fetchone()
    connected = bool(tg_row and tg_row["session_str"])

    if connected:
        st.success("✅ Telegram connected")
        if st.button("Disconnect"):
            conn.execute("DELETE FROM tg_creds WHERE id=1")
            conn.commit()
            st.rerun()
    else:
        st.write("Get credentials from [my.telegram.org](https://my.telegram.org) → API development tools")
        step = st.session_state.get("tg_step", 1)

        if step == 1:
            api_id = st.text_input("API ID")
            api_hash = st.text_input("API Hash")
            phone = st.text_input("Phone number", placeholder="+91XXXXXXXXXX")
            if st.button("Send OTP →", type="primary"):
                if not (api_id.isdigit() and api_hash and phone):
                    st.error("Fill all fields — API ID must be numeric")
                else:
                    try:
                        phone_code_hash, session_str = tg_send_otp(api_id, api_hash, phone)
                        st.session_state.tg_pending = {
                            "api_id": api_id, "api_hash": api_hash, "phone": phone,
                            "phone_code_hash": phone_code_hash, "session_str": session_str
                        }
                        st.session_state.tg_step = 2
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")

        elif step == 2:
            code = st.text_input("OTP code (5 digits)", max_chars=5)
            if st.button("Verify & Connect ✅", type="primary"):
                p = st.session_state.tg_pending
                try:
                    username, final_session = tg_verify_otp(
                        p["api_id"], p["api_hash"], p["phone"], code,
                        p["phone_code_hash"], p["session_str"]
                    )
                    enc_id = fernet.encrypt(p["api_id"].encode()).decode() if fernet else p["api_id"]
                    enc_hash = fernet.encrypt(p["api_hash"].encode()).decode() if fernet else p["api_hash"]
                    enc_phone = fernet.encrypt(p["phone"].encode()).decode() if fernet else p["phone"]
                    enc_sess = fernet.encrypt(final_session.encode()).decode() if fernet else final_session
                    conn.execute("""
                        INSERT INTO tg_creds (id, api_id, api_hash, phone, session_str)
                        VALUES (1, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET api_id=excluded.api_id, api_hash=excluded.api_hash,
                            phone=excluded.phone, session_str=excluded.session_str
                    """, (enc_id, enc_hash, enc_phone, enc_sess))
                    conn.commit()
                    st.session_state.tg_step = 1
                    st.success(f"Connected as {username}")
                    time.sleep(1)
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
            if st.button("← Back"):
                st.session_state.tg_step = 1
                st.rerun()

# ── Channels tab ──────────────────────────────────────────────────────
with tab_channels:
    with st.form("add_channel", clear_on_submit=True):
        c1, c2, c3 = st.columns([3, 1, 1])
        username = c1.text_input("Channel username or ID", placeholder="@fresheroffcampus")
        is_private = c2.checkbox("Private")
        submitted = c3.form_submit_button("Add", type="primary")
        if submitted and username:
            try:
                conn.execute("INSERT INTO channels (username, title, is_private) VALUES (?,?,?)",
                             (username, username, int(is_private)))
                conn.commit()
                st.success(f"Added {username}")
            except IntegrityErrors:
                conn.rollback()
                st.warning("Channel already added")

    channels = conn.execute("SELECT * FROM channels").fetchall()
    for ch in channels:
        c1, c2, c3 = st.columns([3, 2, 1])
        c1.write(f"{'🔒' if ch['is_private'] else '📢'} **{ch['title']}**")
        c2.write(f"{ch['job_count']} jobs · last polled: {ch['last_polled'] or 'never'}")
        if c3.button("Remove", key=f"del_{ch['id']}"):
            conn.execute("DELETE FROM channels WHERE id=?", (ch["id"],))
            conn.commit()
            st.rerun()

    st.divider()
    if st.button("⚡ Scrape now", type="primary", disabled=not connected):
        if not channels:
            st.warning("Add a channel first")
        else:
            tg_row = conn.execute("SELECT * FROM tg_creds WHERE id=1").fetchone()
            api_id = fernet.decrypt(tg_row["api_id"].encode()).decode() if fernet else tg_row["api_id"]
            api_hash = fernet.decrypt(tg_row["api_hash"].encode()).decode() if fernet else tg_row["api_hash"]
            session_str = fernet.decrypt(tg_row["session_str"].encode()).decode() if fernet else tg_row["session_str"]
            with st.spinner("Scraping channels…"):
                n = tg_scrape_channels(api_id, api_hash, session_str,
                                        [dict(c) for c in channels])
            st.success(f"Scraped {n} new posts")
    if not connected:
        st.caption("Connect Telegram in the Telegram Setup tab first")

# ── Board tab (Kanban tracker) ────────────────────────────────────────
PIPELINE_STAGES = ["saved", "applied", "interview", "offer", "rejected"]
STAGE_LABELS = {
    "saved": "🔖 Saved", "applied": "📤 Applied", "interview": "🗣️ Interview",
    "offer": "🎉 Offer", "rejected": "❌ Rejected",
}

with tab_board:
    st.caption("Jobs move here once you save or apply to them from the Job Feed tab.")
    board_cols = st.columns(len(PIPELINE_STAGES))
    for col, stage in zip(board_cols, PIPELINE_STAGES):
        with col:
            st.markdown(f"**{STAGE_LABELS[stage]}**")
            stage_jobs = conn.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY scraped_at DESC", (stage,)
            ).fetchall()
            st.caption(f"{len(stage_jobs)} job(s)")
            idx = PIPELINE_STAGES.index(stage)
            for j in stage_jobs:
                with st.container(border=True):
                    st.markdown(f"**{j['title'] or 'Job Opening'}**")
                    st.caption(j["company"] or "Unknown")
                    b1, b2 = st.columns(2)
                    if idx > 0 and b1.button("←", key=f"back_{stage}_{j['id']}"):
                        conn.execute("UPDATE jobs SET status=? WHERE id=?",
                                     (PIPELINE_STAGES[idx - 1], j["id"]))
                        conn.commit(); st.rerun()
                    if idx < len(PIPELINE_STAGES) - 1 and b2.button("→", key=f"fwd_{stage}_{j['id']}"):
                        conn.execute("UPDATE jobs SET status=? WHERE id=?",
                                     (PIPELINE_STAGES[idx + 1], j["id"]))
                        conn.commit(); st.rerun()

# ── Feed tab ──────────────────────────────────────────────────────────
with tab_feed:
    c1, c2, c3 = st.columns([2, 1, 1])
    search = c1.text_input("🔍 Search jobs, skills, companies…")
    status_filter = c2.selectbox(
        "Status", ["all", "new", "saved", "applied", "interview", "offer", "rejected", "duplicate"]
    )
    min_score = c3.slider("Min match %", 0, 100, 0)

    action_col1, action_col2 = st.columns([1, 1])
    with action_col1:
        if st.button("✨ Enrich pending jobs", type="primary"):
            profile = get_profile_dict()
            if not profile:
                st.warning("Fill your profile first")
            else:
                pending = conn.execute(
                    "SELECT * FROM jobs WHERE match_score=0 AND status='new' LIMIT 50"
                ).fetchall()
                if not pending:
                    st.info("Nothing pending to enrich")
                else:
                    bar = st.progress(0, text=f"Enriching {len(pending)} jobs…")
                    for i, job in enumerate(pending):
                        data = enrich_job(job["raw_text"], profile)
                        if data:
                            skills_required_val = coerce_skills_list(data.get("skills_required", []))
                            is_remote_val = coerce_bool_int(data.get("is_remote", False))
                            score_val = coerce_score(data.get("score", 0))
                            new_title = data.get("title") or job["title"]
                            new_company = data.get("company") or job["company"]
                            conn.execute("""
                                UPDATE jobs SET title=?, company=?, location=?, salary=?,
                                skills_required=?, is_remote=?, match_score=? WHERE id=?
                            """, (
                                new_title, new_company,
                                data.get("location") or job["location"], data.get("salary") or job["salary"],
                                json.dumps(skills_required_val), is_remote_val,
                                score_val, job["id"]
                            ))
                            conn.commit()

                            dup_id = find_duplicate(conn, job["id"], new_title, new_company)
                            if dup_id:
                                conn.execute(
                                    "UPDATE jobs SET status='duplicate', duplicate_of=? WHERE id=?",
                                    (dup_id, job["id"])
                                )
                                conn.commit()
                        bar.progress((i + 1) / len(pending), text=f"Enriched {i+1}/{len(pending)}")
                        time.sleep(1)  # rate limit
                    st.success(f"Enriched {len(pending)} jobs")
                    st.rerun()
    with action_col2:
        if st.button("📨 Send digest now"):
            ok, msg = send_digest(conn)
            (st.success if ok else st.warning)(msg)

    q = "SELECT * FROM jobs WHERE 1=1"
    params = []
    if status_filter == "all":
        q += " AND status != 'duplicate'"
    else:
        q += " AND status=?"
        params.append(status_filter)
    if search:
        q += " AND raw_text LIKE ?"
        params.append(f"%{search}%")
    if min_score > 0:
        q += " AND match_score>=?"
        params.append(min_score)
    q += " ORDER BY match_score DESC, scraped_at DESC LIMIT 60"

    jobs = conn.execute(q, params).fetchall()
    st.caption(f"{len(jobs)} jobs")

    if not jobs:
        st.info("No jobs yet. Connect Telegram, add channels, and scrape.")

    for j in jobs:
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 1, 1])
            c1.markdown(f"**{j['title'] or 'Job Opening'}** — {j['company'] or 'Unknown'}")
            c2.markdown(f"`{j['status']}`")
            score = j["match_score"] or 0
            c3.markdown(f"**{score:.0f}% match**")
            st.progress(min(score / 100, 1.0))

            if j["status"] == "duplicate" and j["duplicate_of"]:
                st.caption(f"🔁 Possible duplicate of job #{j['duplicate_of']}")

            meta = []
            if j["salary"]: meta.append(f"💰 {j['salary']}")
            if j["is_remote"]: meta.append("🌐 Remote")
            if j["location"]: meta.append(f"📍 {j['location']}")
            if meta: st.caption(" · ".join(meta))

            skills = json.loads(j["skills_required"] or "[]")
            if skills: st.caption(", ".join(skills[:6]))

            with st.expander("Raw post + actions"):
                clean_text = j["raw_text"][:1000].replace("**", "")
                st.markdown(clean_text)
                b1, b2, b3, b4 = st.columns(4)
                if b1.button("🔖 Save", key=f"save_{j['id']}"):
                    conn.execute("UPDATE jobs SET status='saved' WHERE id=?", (j["id"],))
                    conn.commit(); st.rerun()
                if b2.button("✍️ Generate cover letter", key=f"cover_{j['id']}"):
                    profile = get_profile_dict()
                    letter = generate_cover_letter(j["raw_text"], profile)
                    conn.execute("UPDATE jobs SET cover_letter=?, status='confirmed' WHERE id=?",
                                 (letter, j["id"]))
                    conn.commit(); st.rerun()
                if j["apply_url"]:
                    b3.link_button("🔗 Apply link", j["apply_url"])
                if j["apply_email"]:
                    b4.link_button("✉️ Email", f"mailto:{j['apply_email']}")

                if j["cover_letter"]:
                    st.text_area("Cover letter (copy this)", value=j["cover_letter"], height=150, key=f"letter_{j['id']}")

                s1, s2 = st.columns(2)
                if j["status"] == "applied":
                    if s1.button("🗣️ Got interview", key=f"int_{j['id']}"):
                        conn.execute("UPDATE jobs SET status='interview' WHERE id=?", (j["id"],)); conn.commit(); st.rerun()
                    if s2.button("❌ Rejected", key=f"rej_{j['id']}"):
                        conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (j["id"],)); conn.commit(); st.rerun()
                elif j["status"] == "interview":
                    if s1.button("🎉 Got offer", key=f"offer_{j['id']}"):
                        conn.execute("UPDATE jobs SET status='offer' WHERE id=?", (j["id"],)); conn.commit(); st.rerun()
                    if s2.button("❌ Rejected", key=f"rejint_{j['id']}"):
                        conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (j["id"],)); conn.commit(); st.rerun()
                elif j["status"] in ("saved", "confirmed"):
                    if s1.button("📤 Mark applied", key=f"appl_{j['id']}"):
                        conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (j["id"],)); conn.commit(); st.rerun()

conn.close()
