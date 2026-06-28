"""
ai_scorer.py — Uses Groq (LLaMA 3.3-70B) for job parsing, scoring, cover letters
"""
import os, json, logging
from groq import AsyncGroq

log    = logging.getLogger(__name__)
client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY", ""))
MODEL  = "llama-3.3-70b-versatile"

PARSE_SYSTEM = """You are a job post parser. Extract structured information from raw Telegram job post text.
Return ONLY valid JSON — no preamble, no markdown fences.
Schema: {"title":string|null,"company":string|null,"location":string|null,"salary":string|null,"skills_required":[],"is_remote":bool,"apply_email":string|null,"apply_url":string|null}"""

SCORE_SYSTEM = """Score how well a candidate matches a job. Return ONLY valid JSON:
{"score":0-100,"strengths":[],"gaps":[]}"""

COVER_SYSTEM = """Write a concise cover letter (150-200 words) for a software developer in India. 
No fluff. Tailor it to the job. Plain text only. Don't start with "I am writing to..."."""


async def parse_job(raw_text: str) -> dict:
    try:
        resp = await client.chat.completions.create(
            model=MODEL, max_tokens=500,
            messages=[{"role":"system","content":PARSE_SYSTEM},{"role":"user","content":raw_text[:2000]}]
        )
        return json.loads(resp.choices[0].message.content.strip())
    except Exception as e:
        log.error(f"parse_job: {e}"); return {}


async def score_match(job_text: str, profile: dict) -> dict:
    profile_summary = f"Skills: {', '.join(profile.get('skills',[]))}\nExp: {profile.get('years_exp',0)}yrs\nSummary: {profile.get('summary','')}"
    prompt = f"JOB:\n{job_text[:1500]}\n\nCANDIDATE:\n{profile_summary}"
    try:
        resp = await client.chat.completions.create(
            model=MODEL, max_tokens=300,
            messages=[{"role":"system","content":SCORE_SYSTEM},{"role":"user","content":prompt}]
        )
        return json.loads(resp.choices[0].message.content.strip())
    except Exception as e:
        log.error(f"score_match: {e}"); return {"score":0,"strengths":[],"gaps":[]}


async def generate_cover_letter(job_text: str, profile: dict) -> str:
    prompt = f"Job:\n{job_text[:1500]}\n\nCandidate: {profile.get('full_name')}, {profile.get('years_exp',0)}yrs, skills: {', '.join(profile.get('skills',[]))}\nGitHub: {profile.get('github','')}\nSummary: {profile.get('summary','')}"
    try:
        resp = await client.chat.completions.create(
            model=MODEL, max_tokens=400,
            messages=[{"role":"system","content":COVER_SYSTEM},{"role":"user","content":prompt}]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error(f"cover_letter: {e}"); return "Please see my attached resume."


async def enrich_pending_jobs(profile: dict, user_id: int):
    from backend.database import AsyncSessionLocal, Job
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Job).where(Job.user_id == user_id, Job.match_score == 0.0, Job.status == "new").limit(20)
        )
        jobs = result.scalars().all()
        for job in jobs:
            parsed     = await parse_job(job.raw_text)
            score_data = await score_match(job.raw_text, profile)
            job.title           = parsed.get("title") or job.title
            job.company         = parsed.get("company") or job.company
            job.location        = parsed.get("location") or job.location
            job.salary          = parsed.get("salary") or job.salary
            job.skills_required = parsed.get("skills_required", [])
            job.is_remote       = parsed.get("is_remote", job.is_remote)
            job.match_score     = score_data.get("score", 0)
        await session.commit()
        log.info(f"Enriched {len(jobs)} jobs for user {user_id}")
