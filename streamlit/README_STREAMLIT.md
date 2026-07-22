# JobHunt — Streamlit Edition

## What changed from the Render/FastAPI version
| Feature | FastAPI version | Streamlit version |
|---|---|---|
| Auth | Multi-user JWT + bcrypt | Single-user, optional password gate (`APP_PASSWORD` secret) |
| DB | Postgres/SQLite async | SQLite sync (local, ephemeral on Streamlit Cloud) |
| Scraper | Auto-poll every 15 min (background scheduler) | Manual "Scrape now" button |
| Auto-apply | Playwright form-fill + email send | Dropped — generates AI cover letter, you copy-paste + apply manually |
| Telegram session | Encrypted, stored in Postgres, survives redeploys | Encrypted, stored in local SQLite — **wiped when the app sleeps/redeploys** |

## Why the cuts were necessary
Streamlit Cloud runs your script top-to-bottom on every interaction — there's no persistent background process for a scheduler, and Playwright/Chromium isn't reliably installable on the free tier. Multi-user JWT auth adds complexity with no payoff for a solo portfolio demo.

## Setup

1. Copy this repo's `streamlit_app.py` and `requirements.txt` into your `jobhunt` repo root (or a `streamlit/` subfolder — just point Streamlit Cloud at the right path).

2. In Streamlit Cloud → App settings → Secrets, add:
```toml
GROQ_API_KEY = "gsk_..."
FERNET_KEY = "..."          # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
APP_PASSWORD = "yourpassword"   # optional — leave blank for open demo
```

3. Deploy. Entry point: `streamlit_app.py`.

## Known limitation to disclose in your portfolio README
Data (jobs, channels, Telegram session) resets when the app sleeps due to Streamlit Cloud's ephemeral disk. This is fine for a live demo — recruiters see the AI pipeline working end-to-end. If you want persistence, swap the `sqlite3` calls for a Supabase Postgres connection (you already use Supabase pgvector elsewhere — same project works here).

## Local test
```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```
