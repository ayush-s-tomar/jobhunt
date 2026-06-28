"""
ai_scorer.py – Uses Groq (LLaMA 3.3-70B) for job parsing, scoring, cover letters
Optimized: single API call per job, rate limit handling, batch of 50
"""
import os, json, logging, asyncio
from groq import AsyncGroq

log    = logging.getLogger(__name__)
client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY", ""))
MODEL  = "llama-3.3-70b-versatile"

COMBINED_SYSTEM = """Parse this job post and score the candidate match. Return ONLY valid JSON, no markdown:
{"title":string|null,"company":string|null,"location":string|null,"salary":string|null,"skills_required":[],"is_remote":bool,"apply_email":string|null,"apply_url":string|null,"score":0-100}"""

COVER_SYSTEM = """Write a concise cover letter (150-200 words) for a software developer in India. 
No fluff. Tailor it to the job. Plain text only. Don't start with "I am writing to..."."""


async def parse_job(raw_text: str) -> dict:
    """Legacy wrapper — just returns empty, combined call handles this."""
    return {}


async def score_match(job_text: str, profile: dict) -> dict:
    """Legacy wrapper."""
    return {"score": 0, "strengths": [], "gaps": []}


async def generate_cover_letter(job_text: str, profile: dict) -> str:
    prompt = f"Job:\n{job_text[:1500]}\n\nCandidate: {profile.get('full_name')}, {profile.get('years_exp',0)}yrs, skills: {', '.join(profile.get('skills',[]))}\nGitHub: {profile.get('github','')}\nSummary: {profile.get('summary','')}"
    try:
        resp = await client.chat.completions.create(
            model=MODEL, max_tokens=400,
            messages=[{"role":"system","content":COVER_SYSTEM},{"role":"user","content":prompt}]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error(f"cover_letter: {e}")
        return "Please see my attached resume."


async def _enrich_one(job, profile: dict) -> dict:
    """Single API call to parse + score a job."""
    profile_summary = f"Skills: {', '.join(profile.get('skills', []))}\nExp: {profile.get('years_exp', 0)}yrs\nSummary: {profile.get('summary', '')}"
    prompt = f"JOB:\n{job.raw_text[:1500]}\n\nCANDIDATE:\n{profile_summary}"
    for attempt in range(3):
        try:
            resp = await client.chat.completions.create(
                model=MODEL, max_tokens=400,
                messages=[
                    {"role": "system", "content": COMBINED_SYSTEM},
                    {"role": "user", "content": prompt}
                ]
            )
            text = resp.choices[0].message.content.strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text)
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                await asyncio.sleep(3 * (attempt + 1))
            else:
                log.error(f"enrich_one: {e}")
                return {}
    return {}


async def enrich_pending_jobs(profile: dict, user_id: int):
    from backend.database import AsyncSessionLocal, Job
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Job).where(
                Job.user_id == user_id,
                Job.match_score == 0.0,
                Job.status == "new"
            ).limit(50)
        )
        jobs = result.scalars().all()

        for i, job in enumerate(jobs):
            if not job.raw_text or len(job.raw_text) < 50:
                continue

            data = await _enrich_one(job, profile)
            if data:
                job.title           = data.get("title") or job.title
                job.company         = data.get("company") or job.company
                job.location        = data.get("location") or job.location
                job.salary          = data.get("salary") or job.salary
                job.skills_required = data.get("skills_required", [])
                job.is_remote       = data.get("is_remote", job.is_remote)
                job.match_score     = float(data.get("score", 0))

            # Save every 10 jobs so progress isn't lost on error
            if (i + 1) % 10 == 0:
                await session.commit()

            # Rate limit: 1 request per second
            await asyncio.sleep(1)

        await session.commit()
        log.info(f"Enriched {len(jobs)} jobs for user {user_id}")