# JobHunt — Telegram Job Aggregator + Auto-Apply Bot

> Watches your Telegram job channels (public & private), scores every post against your profile using Claude AI, and auto-applies with one click.

---

## 🗂 Project Structure

```
jobhunt/
├── backend/
│   ├── database.py       ← SQLAlchemy models + DB init
│   ├── ai_scorer.py      ← Claude AI: parse + score + cover letter
│   └── apply_bot.py      ← Email sender + Playwright form-filler
├── scraper/
│   └── telegram_scraper.py  ← Polls Telegram channels
├── frontend/
│   └── index.html        ← Full dashboard UI
├── data/                 ← Auto-created: SQLite DB + session file
├── main.py               ← FastAPI routes
├── run.py                ← Launch everything
├── requirements.txt
└── .env.example
```

---

## ⚡ Setup — Step by Step

### Step 1: Clone and create virtual environment

```bash
cd jobhunt
py -3.11 -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### Step 2: Install Playwright browsers (one-time)

```bash
playwright install chromium
```

### Step 3: Create your .env file

```bash
copy .env.example .env
```

Open `.env` and fill in:

| Variable | Where to get it | Required |
|---|---|---|
| `TELEGRAM_API_ID` | [my.telegram.org](https://my.telegram.org) → API development tools | ✅ |
| `TELEGRAM_API_HASH` | Same page | ✅ |
| `TELEGRAM_PHONE` | Your phone number with country code | ✅ |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | ✅ |
| `SMTP_USER` | Your Gmail address | For email apply |
| `SMTP_PASS` | Gmail **App Password** (not your main password) | For email apply |
| `PUBLIC_CHANNELS` | @channelname, comma-separated | ✅ |
| `PRIVATE_CHANNELS` | Numeric channel IDs, comma-separated | Optional |

**To get a Gmail App Password:** Gmail → Settings → Security → 2-Step Verification → App Passwords → Create one for "Mail".

**To get a private channel ID:**
1. Forward a message from the private channel to [@username_to_id_bot](https://t.me/username_to_id_bot)
2. It'll give you the numeric ID (starts with -100...)

### Step 4: Authenticate with Telegram (one-time)

```bash
python scraper/telegram_scraper.py --auth
```

Enter your phone number and the OTP Telegram sends you. A `data/session.session` file is created — keep it safe, don't share it.

### Step 5: Test scraper manually

```bash
python scraper/telegram_scraper.py --once
```

You'll see logs like:
```
@jobsfordevs: +12 new jobs
@IndiaJobsHiring: +8 new jobs
✅ Poll complete — 20 new jobs total
```

### Step 6: Start the server

```bash
python run.py
```

Open: **http://localhost:8000**

---

## 👤 First Time: Set Up Your Profile

1. Click **Profile** (top right)
2. Fill in your name, email, phone, skills, years experience
3. Upload your **resume PDF**
4. Click **Save Profile**

This gets used for every application automatically.

---

## 🔄 Full Auto-Apply Flow

```
Telegram channel posts job
         ↓
Scraper picks it up every 15 min
         ↓
AI (Claude) enriches: title, company, skills, match score, apply path
         ↓
Job appears in dashboard with match % score
         ↓
You click "Save Job" → then "Confirm & Auto-Apply"   ← only human step
         ↓
Bot detects apply type:
  • Email post? → Sends email + resume via SMTP
  • URL post?   → Playwright fills the form, uploads resume, submits
         ↓
Status → Applied ✅  (or CAPTCHA flag if blocked)
```

---

## 🚧 CAPTCHA Edge Cases

~1 in 10 job board forms has CAPTCHA. When this happens:
- The bot flags the job with 🚧
- Job stays as "Confirmed" — not wasted
- An "apply manually" link appears in the detail panel
- You open the link and apply yourself (30 seconds)

---

## 📡 Adding Channels

**Public channels** (easiest):
- Click **+ Channel**
- Enter `@channelname`
- Click Add

Good Indian software job channels to start with:
```
@jobsfordevs
@IndiaJobsHiring
@techjobsindia
@startupjobsindia
@DevHiringIndia
```

**Private channels:**
- Find the numeric ID (see Step 3 above)
- Enter it as `-1001234567890` with the Private checkbox checked
- You must already be a member of the channel

---

## 🔁 Scraper Schedule

By default scrapes every **15 minutes**. Change in `.env`:
```
SCRAPE_INTERVAL_MINUTES=10
```

The scraper is gentle — it reads, never writes to Telegram. Polling every 10-15 min is safe.

---

## 🚀 Deployment (Render)

1. Push this repo to GitHub
2. Create a Render **Web Service** pointing to `main.py`
   - Build: `pip install -r requirements.txt && playwright install chromium`
   - Start: `python run.py`
3. Add all `.env` variables in Render's Environment tab
4. Upload your `data/session.session` file via Render's persistent disk

---

## 🔧 Troubleshooting

| Problem | Fix |
|---|---|
| `No module named telethon` | `pip install telethon tgcrypto` |
| `FloodWaitError` from Telegram | Scraper polling too fast — increase interval |
| Session expired | Re-run `python scraper/telegram_scraper.py --auth` |
| SMTP auth failed | Use Gmail App Password, not your login password |
| Playwright not found | `playwright install chromium` |
| No jobs showing | Check channels are added and try `--once` manually |
