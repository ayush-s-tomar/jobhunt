# JobHunt — Telegram Job Aggregator + Auto-Apply

> Watches Telegram job channels, scores every post against your profile using Groq AI, and auto-applies with one click.

[🔗 Live Demo](https://jobhunt-lonp.onrender.com) &nbsp;|&nbsp; [👤 LinkedIn](https://www.linkedin.com/in/ayush-s-tomar/)

---

## The Problem

Indian job seekers manually check 10+ Telegram job channels every day, copy-paste apply links, and repeat the same cover letter with minor tweaks. Hours wasted. Opportunities missed.

**JobHunt automates the entire pipeline — from Telegram post to submitted application.**

---

## What It Does

Connect your Telegram account. Add job channels. JobHunt scrapes every post, scores it against your profile using AI, and lets you apply in one click.

| Step | What happens |
|------|-------------|
| 📡 **Scrape** | Polls your Telegram channels every 15 minutes via MTProto |
| 🤖 **Enrich** | Groq AI extracts title, company, salary, skills, apply link |
| 🎯 **Score** | Matches job requirements against your skills and experience (0–100%) |
| ✉️ **Apply** | Sends tailored email + resume, or fills forms via Playwright |

```
Telegram channel posts job
         ↓
Scraper picks it up every 15 min
         ↓
AI enriches: title, company, salary, match score
         ↓
Job appears in dashboard with match %
         ↓
You click "Confirm & Auto-Apply"   ← only human step
         ↓
Bot sends email or fills form → Status: Applied ✅
```

---

## Demo

**439 jobs scraped from 5 channels in under 60 seconds.**

Jobs are ranked by AI match score — highest matches float to top. Each card shows salary, location, company, and a one-click apply button.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + SQLAlchemy |
| Database | PostgreSQL (production) / SQLite (local) |
| Telegram | Telethon (MTProto) — per-user sessions stored encrypted in DB |
| AI | Groq API (`llama-3.3-70b-versatile`) |
| Form automation | Playwright (Chromium) |
| Auth | JWT (HttpOnly cookies) + bcrypt passwords + Fernet encryption |
| Deploy | Render (Web Service + PostgreSQL) |
| Frontend | Vanilla JS + CSS (single HTML file, no build step) |

---

## Security

- Passwords → bcrypt hashed (never stored plain)
- Telegram API keys → Fernet encrypted before hitting DB
- Telegram sessions → StringSession stored encrypted in DB (survives redeploys)
- JWT tokens → HttpOnly cookies (XSS-proof), 30-day expiry
- All routes → user_id scoped (no cross-user data leaks)
- Rate limiting → 5 login attempts/min, 3 registrations/hour per IP

---

## Project Structure

```
jobhunt/
├── backend/
│   ├── database.py          # SQLAlchemy models (User, Job, Channel, Application)
│   ├── auth.py              # JWT, bcrypt, Fernet encryption
│   ├── ai_scorer.py         # Groq: parse + score + cover letter
│   ├── telegram_auth.py     # Per-user Telegram OTP flow + session management
│   └── apply_bot.py         # Email sender + Playwright form-filler
├── scraper/
│   └── telegram_scraper.py  # MTProto scraper, per-user StringSession
├── frontend/
│   └── index.html           # Full dashboard UI
├── main.py                  # All FastAPI routes
├── run.py                   # Starts server + scraper scheduler
├── render.yaml              # Render deployment config
└── .env.example
```

---

## Run Locally

```bash
# 1. Clone
git clone https://github.com/ayush-s-tomar/jobhunt.git
cd jobhunt

# 2. Create virtual environment
py -3.11 -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# 3. Generate secret keys
python -c "import secrets; print(secrets.token_hex(32))"          # → SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # → FERNET_KEY

# 4. Set up .env
copy .env.example .env
# Fill in SECRET_KEY, FERNET_KEY, GROQ_API_KEY

# 5. Start
python run.py
# → http://localhost:8000
```

**Free API keys needed:**
- Groq → https://console.groq.com/keys *(free tier: 14,400 req/day)*
- Telegram → https://my.telegram.org → API development tools

---

## Multi-User

JobHunt is built multi-user from the ground up. Each user:
- Logs in with their own account
- Connects their own Telegram (API keys + OTP)
- Gets their own job feed, channels, and profile
- Session stored encrypted in DB — survives server redeploys

Share the link with friends. Everyone gets their own isolated dashboard.

---

## Deployment (Render)

```bash
# 1. Push to GitHub
git push origin main

# 2. Create Render Web Service
# Build: pip install -r requirements.txt && playwright install chromium
# Start: python run.py

# 3. Create Render PostgreSQL (free tier)
# Copy Internal Database URL → set as DATABASE_URL env var
# Change postgres:// → postgresql+asyncpg://

# 4. Set environment variables in Render dashboard:
# SECRET_KEY, FERNET_KEY, GROQ_API_KEY, DATABASE_URL, ALLOWED_ORIGINS
```

---

## What I'd Add Next

- **Email notifications** when a high-match job (>80%) is scraped
- **Resume parser** to auto-fill skills from uploaded PDF
- **Private channel support** via invite link
- **Weekly digest** — top 10 matches emailed every Monday
- **Mobile app** — React Native wrapper around the same API

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `No session for user` | Go to Telegram Setup → reconnect |
| `0 jobs after scrape` | Join channels in Telegram app first, then scrape |
| Enrich shows 0% | Fill your profile with skills first, then click Enrich |
| Site takes 50s to load | Free Render tier sleeps — set up UptimeRobot to keep it awake |
| `pydantic-core` build error | Add `PYTHON_VERSION=3.11.9` to Render env vars |

---

*Part of my AI developer portfolio. See also: [SalesAgent](https://github.com/ayush-s-tomar/salesagent) — autonomous B2B sales AI with LangGraph.*
