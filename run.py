"""
run.py  —  Start the FastAPI server + background scraper together
Usage:
    python run.py           # starts both
    python run.py --api     # API only (no scraper)
"""

import os, sys, asyncio, subprocess, argparse, logging
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", action="store_true", help="API only, no scraper")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    interval = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "15"))

    if not args.api:
        # Run scraper in a separate process on schedule
        def run_scraper():
            try:
                subprocess.run(
                    [sys.executable, "scraper/telegram_scraper.py", "--once"],
                    timeout=300
                )
            except Exception as e:
                log.error(f"Scraper error: {e}")

        scheduler = BackgroundScheduler()
        scheduler.add_job(run_scraper, IntervalTrigger(minutes=interval))

        # Daily junk cleaner
        from apscheduler.triggers.interval import IntervalTrigger as IT
        async def clean_junk():
            from sqlalchemy.ext.asyncio import create_async_engine
            from sqlalchemy import text
            engine = create_async_engine(os.getenv("DATABASE_URL"))
            async with engine.begin() as conn:
                result = await conn.execute(text("""
                    DELETE FROM jobs WHERE
                    raw_text ILIKE '%free course%' OR
                    raw_text ILIKE '%certificate%' OR
                    raw_text ILIKE '%udemy%' OR
                    raw_text ILIKE '%masterclass%' OR
                    raw_text ILIKE '%enroll now%' OR
                    raw_text ILIKE '%placement support%' OR
                    raw_text ILIKE '%online course%' OR
                    title IS NULL OR
                    length(raw_text) < 100
                """))
                log.info(f"🧹 Auto-cleaned {result.rowcount} junk posts")

        def run_cleanup():
            asyncio.run(clean_junk())

        scheduler.add_job(run_cleanup, IntervalTrigger(hours=24))
        scheduler.start()
        log.info(f"✅ Scraper scheduled every {interval} minutes")

    uvicorn.run("main:app", host="0.0.0.0", port=args.port, reload=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
