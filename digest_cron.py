"""
Standalone digest sender — run this on a schedule (GitHub Actions, cron, etc.)
since Streamlit Cloud has no built-in scheduler.

Requires the same env vars as the app: SUPABASE_DB_URL, TELEGRAM_BOT_TOKEN,
TELEGRAM_DIGEST_CHAT_ID. Without SUPABASE_DB_URL this has nothing durable to
read, since the app's SQLite fallback doesn't persist between runs anyway.

Usage: python digest_cron.py
"""
import os
import requests
import psycopg2
import psycopg2.extras

SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_DIGEST_CHAT_ID = os.environ.get("TELEGRAM_DIGEST_CHAT_ID")
MIN_SCORE = float(os.environ.get("DIGEST_MIN_SCORE", 70))
LIMIT = int(os.environ.get("DIGEST_LIMIT", 10))

REQUIRED = {
    "SUPABASE_DB_URL": SUPABASE_DB_URL,
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_DIGEST_CHAT_ID": TELEGRAM_DIGEST_CHAT_ID,
}


def main():
    missing = [k for k, v in REQUIRED.items() if not v]
    if missing:
        raise RuntimeError(f"Missing required env var(s): {', '.join(missing)}")

    conn = psycopg2.connect(SUPABASE_DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, company, match_score FROM jobs "
        "WHERE status='new' AND match_score>=%s AND digested=%s "
        "ORDER BY match_score DESC LIMIT %s",
        (MIN_SCORE, False, LIMIT),
    )
    jobs = cur.fetchall()

    if not jobs:
        print("No new high-match jobs to send.")
        return

    lines = [f"📡 JobHunt Digest — {len(jobs)} new match(es)\n"]
    for j in jobs:
        lines.append(f"• {j['title'] or 'Job Opening'} — {j['company'] or 'Unknown'} ({j['match_score']:.0f}% match)")
    text = "\n".join(lines)

    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_DIGEST_CHAT_ID, "text": text},
        timeout=10,
    )
    resp.raise_for_status()

    ids = tuple(j["id"] for j in jobs)
    cur.execute("UPDATE jobs SET digested=%s WHERE id IN %s", (True, ids))
    conn.commit()
    print(f"Sent digest with {len(jobs)} job(s).")


if __name__ == "__main__":
    main()