"""
JobHunt — Streamlit Edition
Single-user demo build. See README_STREAMLIT.md for what changed vs the FastAPI version.
"""
import os, json, sqlite3, asyncio, time
from datetime import datetime
from pathlib import Path

import streamlit as st
from groq import Groq
from cryptography.fernet import Fernet

st.set_page_config(page_title="JobHunt", page_icon="📡", layout="wide")

# ── Config / secrets ──────────────────────────────────────────────────────
APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")
GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", os.getenv("GROQ_API_KEY", ""))
FERNET_KEY   = st.secrets.get("FERNET_KEY", os.getenv("FERNET_KEY", ""))
DB_PATH      = "jobhunt.db"  # ephemeral on Streamlit Cloud — see README

fernet = Fernet(FERNET_KEY.encode()) if FERNET_KEY else None
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

MODEL = "llama-3.3-70b-versatile"
COMBINED_SYSTEM = """Parse this job post and score the candidate match. Return ONLY valid JSON, no markdown:
{"title":string|null,"company":string|null,"location":string|null,"salary":string|null,"skills_required":[],"is_remote":bool,"apply_email":string|null,"apply_url":string|null,"score":0-100}"""
COVER_SYSTEM = """Write a concise cover letter (150-200 words) for a software developer in India.
No fluff. Tailor it to the job. Plain text only. Don't start with "I am writing to..."."""

# ── DB (sync sqlite — single user, no user_id scoping needed) ────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE, title TEXT, is_private INTEGER DEFAULT 0,
        job_count INTEGER DEFAULT 0, last_polled TEXT
    );
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER, message_id INTEGER, raw_text TEXT,
        title TEXT, company TEXT, location TEXT, salary TEXT,
        skills_required TEXT DEFAULT '[]', apply_email TEXT, apply_url TEXT,
        is_remote INTEGER DEFAULT 0, match_score REAL DEFAULT 0,
        status TEXT DEFAULT 'new', cover_letter TEXT,
        posted_at TEXT, scraped_at TEXT
    );
    CREATE TABLE IF NOT EXISTS profile (
        id INTEGER PRIMARY KEY CHECK (id=1),
        full_name TEXT, phone TEXT, linkedin TEXT, github TEXT,
        portfolio TEXT, years_exp REAL DEFAULT 0, skills TEXT DEFAULT '[]', summary TEXT
    );
    CREATE TABLE IF NOT EXISTS tg_creds (
        id INTEGER PRIMARY KEY CHECK (id=1),
        api_id TEXT, api_hash TEXT, phone TEXT, session_str TEXT
    );
    """)
    conn.commit()
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

if DB_PATH == "jobhunt.db":
    st.info("Demo mode: data resets when this app sleeps or redeploys (Streamlit Cloud has ephemeral disk). "
            "For persistence, point this at Supabase Postgres.", icon="ℹ️")

conn = get_conn()

tab_feed, tab_channels, tab_profile, tab_telegram = st.tabs(["📋 Job Feed", "📡 Channels", "👤 Profile", "🔌 Telegram Setup"])

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
            except sqlite3.IntegrityError:
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

# ── Feed tab ──────────────────────────────────────────────────────────
with tab_feed:
    c1, c2, c3 = st.columns([2, 1, 1])
    search = c1.text_input("🔍 Search jobs, skills, companies…")
    status_filter = c2.selectbox("Status", ["all", "new", "saved", "applied", "interview", "rejected"])
    min_score = c3.slider("Min match %", 0, 100, 0)

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
                        conn.execute("""
                            UPDATE jobs SET title=?, company=?, location=?, salary=?,
                            skills_required=?, is_remote=?, match_score=? WHERE id=?
                        """, (
                            data.get("title") or job["title"], data.get("company") or job["company"],
                            data.get("location") or job["location"], data.get("salary") or job["salary"],
                            json.dumps(data.get("skills_required", [])), int(data.get("is_remote", False)),
                            float(data.get("score", 0)), job["id"]
                        ))
                        conn.commit()
                    bar.progress((i + 1) / len(pending), text=f"Enriched {i+1}/{len(pending)}")
                    time.sleep(1)  # rate limit
                st.success(f"Enriched {len(pending)} jobs")
                st.rerun()

    q = "SELECT * FROM jobs WHERE 1=1"
    params = []
    if status_filter != "all":
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

            meta = []
            if j["salary"]: meta.append(f"💰 {j['salary']}")
            if j["is_remote"]: meta.append("🌐 Remote")
            if j["location"]: meta.append(f"📍 {j['location']}")
            if meta: st.caption(" · ".join(meta))

            skills = json.loads(j["skills_required"] or "[]")
            if skills: st.caption(", ".join(skills[:6]))

            with st.expander("Raw post + actions"):
                st.text(j["raw_text"][:1000])
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
                elif j["status"] in ("saved", "confirmed"):
                    if s1.button("📤 Mark applied", key=f"appl_{j['id']}"):
                        conn.execute("UPDATE jobs SET status='applied' WHERE id=?", (j["id"],)); conn.commit(); st.rerun()

conn.close()
