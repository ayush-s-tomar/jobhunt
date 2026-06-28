"""
main.py — FastAPI backend (multi-user, secure)
"""
import os, asyncio, logging, shutil
from datetime import datetime
from typing import Optional, List
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, BackgroundTasks, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

from backend.database import init_db, get_db, Job, Channel, Application, UserProfile, User
from backend.auth import (
    hash_password, verify_password, create_token, get_current_user, encrypt, decrypt
)
from backend.telegram_auth import send_otp, verify_otp, has_session, disconnect_user
from backend.ai_scorer import parse_job, score_match, generate_cover_letter, enrich_pending_jobs
from backend.apply_bot import apply

# ── Rate limiter ──────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="JobHunt API", version="2.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

DATA_DIR   = Path("data")
RESUME_DIR = DATA_DIR / "resumes"


@app.on_event("startup")
async def startup():
    await init_db()
    log.info("DB initialised")


# ══ AUTH ROUTES ═══════════════════════════════════════════════════════════════

class RegisterBody(BaseModel):
    email: str
    password: str
    full_name: str

class LoginBody(BaseModel):
    email: str
    password: str


@app.post("/api/auth/register")
@limiter.limit("3/hour")
async def register(request: Request, body: RegisterBody, db: AsyncSession = Depends(get_db)):
    # Check email not taken
    existing = await db.execute(select(User).where(User.email == body.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")

    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")

    user = User(
        email           = body.email.lower().strip(),
        hashed_password = hash_password(body.password),
        full_name       = body.full_name.strip(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"ok": True, "message": "Account created. Please log in."}


@app.post("/api/auth/login")
@limiter.limit("5/minute")
async def login(request: Request, body: LoginBody, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email.lower()))
    user = result.scalar_one_or_none()

    # Always run verify to prevent timing attacks
    valid = user and verify_password(body.password, user.hashed_password)
    if not valid:
        raise HTTPException(401, "Invalid email or password")

    token = create_token(user.id)
    response.set_cookie(
        key      = "access_token",
        value    = token,
        httponly = True,       # JS cannot read this
        secure   = False,      # Set True when using HTTPS in production
        samesite = "lax",
        max_age  = 86400,      # 24 hours
    )
    return {"ok": True, "user": {"id": user.id, "email": user.email, "full_name": user.full_name}}


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(current_user: User = Depends(get_current_user)):
    return {
        "id":           current_user.id,
        "email":        current_user.email,
        "full_name":    current_user.full_name,
        "tg_connected": current_user.tg_connected,
    }


# ══ TELEGRAM AUTH ═════════════════════════════════════════════════════════════

class TelegramCredsBody(BaseModel):
    api_id:   str
    api_hash: str
    phone:    str

class OTPBody(BaseModel):
    code:            str
    phone_code_hash: str


@app.post("/api/telegram/setup")
async def telegram_setup(
    body: TelegramCredsBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save encrypted Telegram credentials and send OTP."""
    # Validate api_id is numeric
    if not body.api_id.strip().isdigit():
        raise HTTPException(400, "api_id must be a number")

    # Encrypt and save credentials
    current_user.tg_api_id   = encrypt(body.api_id.strip())
    current_user.tg_api_hash = encrypt(body.api_hash.strip())
    current_user.tg_phone    = encrypt(body.phone.strip())
    await db.commit()

    # Send OTP via Telegram
    result = await send_otp(
        user_id  = current_user.id,
        api_id   = int(body.api_id.strip()),
        api_hash = body.api_hash.strip(),
        phone    = body.phone.strip(),
    )
    if not result["ok"]:
        raise HTTPException(400, result["error"])

    return {"ok": True, "phone_code_hash": result["phone_code_hash"]}


@app.post("/api/telegram/verify")
async def telegram_verify(
    body: OTPBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify OTP and complete Telegram connection."""
    phone = decrypt(current_user.tg_phone)
    result = await verify_otp(
        user_id          = current_user.id,
        phone            = phone,
        code             = body.code.strip(),
        phone_code_hash  = body.phone_code_hash,
        db               = db,
    )
    if not result["ok"]:
        raise HTTPException(400, result["error"])

    current_user.tg_connected = True
    await db.commit()
    return {"ok": True, "telegram_username": result.get("telegram_username")}


@app.delete("/api/telegram/disconnect")
async def telegram_disconnect(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove Telegram session and credentials."""
    import os
    session_file = Path(f"data/sessions/{current_user.id}.session")
    if session_file.exists():
        session_file.unlink()

    current_user.tg_api_id   = None
    current_user.tg_api_hash = None
    current_user.tg_phone    = None
    current_user.tg_connected = False
    await db.commit()
    await disconnect_user(current_user.id)
    return {"ok": True}


# ══ STATS ════════════════════════════════════════════════════════════════════

@app.get("/api/stats")
async def get_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    uid = current_user.id
    total_jobs = (await db.execute(select(func.count(Job.id)).where(Job.user_id == uid))).scalar()
    total_ch   = (await db.execute(select(func.count(Channel.id)).where(Channel.user_id == uid))).scalar()
    private_ch = (await db.execute(select(func.count(Channel.id)).where(Channel.user_id == uid, Channel.is_private == True))).scalar()
    applied    = (await db.execute(select(func.count(Job.id)).where(Job.user_id == uid, Job.status == "applied"))).scalar()
    interviews = (await db.execute(select(func.count(Job.id)).where(Job.user_id == uid, Job.status == "interview"))).scalar()
    confirmed  = (await db.execute(select(func.count(Job.id)).where(Job.user_id == uid, Job.status == "confirmed"))).scalar()
    return {
        "total_jobs": total_jobs, "total_channels": total_ch,
        "private_channels": private_ch, "public_channels": total_ch - private_ch,
        "applied": applied, "interviews": interviews, "confirmed": confirmed,
    }


# ══ JOBS ═════════════════════════════════════════════════════════════════════

@app.get("/api/jobs")
async def list_jobs(
    status: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    min_score: float = 0,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    uid = current_user.id
    q = select(Job).where(Job.user_id == uid, Job.is_duplicate == False)
    if status:     q = q.where(Job.status == status)
    if source == "private": q = q.join(Channel).where(Channel.is_private == True)
    elif source == "public": q = q.join(Channel).where(Channel.is_private == False)
    if search:     q = q.where(Job.raw_text.ilike(f"%{search}%"))
    if min_score > 0: q = q.where(Job.match_score >= min_score)
    q = q.order_by(desc(Job.match_score), desc(Job.scraped_at)).limit(limit).offset(offset)
    result = await db.execute(q)
    jobs = result.scalars().all()

    out = []
    for j in jobs:
        ch = await db.get(Channel, j.channel_id)
        out.append({
            "id": j.id, "title": j.title, "company": j.company,
            "location": j.location, "salary": j.salary,
            "skills": j.skills_required or [], "apply_type": j.apply_type,
            "apply_email": j.apply_email, "apply_url": j.apply_url,
            "is_remote": j.is_remote, "match_score": j.match_score,
            "status": j.status,
            "posted_at": j.posted_at.isoformat() if j.posted_at else None,
            "scraped_at": j.scraped_at.isoformat() if j.scraped_at else None,
            "channel": ch.title if ch else "Unknown",
            "is_private": ch.is_private if ch else False,
            "raw_text": (j.raw_text or "")[:300] + "..." if len(j.raw_text or "") > 300 else j.raw_text,
        })
    return out


@app.get("/api/jobs/{job_id}")
async def get_job(
    job_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(Job, job_id)
    # CRITICAL: verify ownership
    if not job or job.user_id != current_user.id:
        raise HTTPException(404, "Job not found")
    ch = await db.get(Channel, job.channel_id)
    app_obj = job.application
    return {
        "id": job.id, "title": job.title, "company": job.company,
        "location": job.location, "salary": job.salary,
        "skills": job.skills_required or [], "apply_type": job.apply_type,
        "apply_email": job.apply_email, "apply_url": job.apply_url,
        "is_remote": job.is_remote, "match_score": job.match_score,
        "status": job.status,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "raw_text": job.raw_text,
        "channel": ch.title if ch else "Unknown",
        "is_private": ch.is_private if ch else False,
        "application": {
            "cover_letter": app_obj.cover_letter if app_obj else None,
            "email_sent": app_obj.email_sent if app_obj else False,
            "form_filled": app_obj.form_filled if app_obj else False,
            "captcha_blocked": app_obj.captcha_blocked if app_obj else False,
            "log": app_obj.log if app_obj else [],
        } if app_obj else None,
    }


class StatusUpdate(BaseModel):
    status: str

@app.patch("/api/jobs/{job_id}/status")
async def update_status(
    job_id: int, body: StatusUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(Job, job_id)
    if not job or job.user_id != current_user.id:
        raise HTTPException(404)
    job.status = body.status
    await db.commit()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/confirm")
async def confirm_job(
    job_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    job = await db.get(Job, job_id)
    if not job or job.user_id != current_user.id:
        raise HTTPException(404)
    job.status = "confirmed"
    await db.commit()
    background_tasks.add_task(_run_apply, job_id, current_user.id)
    return {"ok": True, "message": "Applying in background…"}


async def _run_apply(job_id: int, user_id: int):
    from backend.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        job = await db.get(Job, job_id)
        if not job or job.user_id != user_id:
            return
        user = await db.get(User, user_id)
        profile_obj = user.profile

        profile = {
            "full_name":   user.full_name,
            "email":       user.email,
            "phone":       profile_obj.phone if profile_obj else "",
            "linkedin":    profile_obj.linkedin if profile_obj else "",
            "github":      profile_obj.github if profile_obj else "",
            "portfolio":   profile_obj.portfolio if profile_obj else "",
            "years_exp":   profile_obj.years_exp if profile_obj else 0,
            "skills":      profile_obj.skills if profile_obj else [],
            "resume_path": profile_obj.resume_path if profile_obj else "",
            "summary":     profile_obj.summary if profile_obj else "",
        }

        cover = await generate_cover_letter(job.raw_text, profile)
        job_dict = {
            "apply_type": job.apply_type, "apply_email": job.apply_email,
            "apply_url": job.apply_url, "title": job.title, "company": job.company,
        }
        res = await apply(job_dict, profile, cover)

        if not job.application:
            app_obj = Application(job_id=job_id)
            db.add(app_obj)
            await db.flush()
            app_obj = job.application or app_obj

        app_obj = job.application
        if not app_obj:
            app_obj = Application(job_id=job_id)
            db.add(app_obj)

        app_obj.cover_letter    = cover
        app_obj.log             = res.logs
        app_obj.email_sent      = res.method == "email" and res.success
        app_obj.form_filled     = res.method == "form" and res.success
        app_obj.captcha_blocked = res.captcha_blocked
        if res.success:
            job.status = "applied"
            app_obj.applied_at = datetime.utcnow()
        await db.commit()


# ══ CHANNELS ════════════════════════════════════════════════════════════════

@app.get("/api/channels")
async def list_channels(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Channel).where(Channel.user_id == current_user.id).order_by(desc(Channel.job_count))
    )
    return [
        {"id": ch.id, "username": ch.username, "title": ch.title,
         "is_private": ch.is_private, "is_active": ch.is_active,
         "job_count": ch.job_count,
         "last_polled": ch.last_polled.isoformat() if ch.last_polled else None}
        for ch in result.scalars().all()
    ]


class ChannelCreate(BaseModel):
    username: str
    is_private: bool = False

@app.post("/api/channels")
async def add_channel(
    body: ChannelCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ch = Channel(user_id=current_user.id, username=body.username, title=body.username, is_private=body.is_private)
    db.add(ch)
    await db.commit()
    await db.refresh(ch)
    return {"id": ch.id, "username": ch.username}

@app.delete("/api/channels/{ch_id}")
async def delete_channel(
    ch_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ch = await db.get(Channel, ch_id)
    if not ch or ch.user_id != current_user.id:
        raise HTTPException(404)
    await db.delete(ch)
    await db.commit()
    return {"ok": True}


# ══ PROFILE ══════════════════════════════════════════════════════════════════

@app.get("/api/profile")
async def get_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(UserProfile).where(UserProfile.user_id == current_user.id))
    p = result.scalar_one_or_none()
    return {
        "full_name":   current_user.full_name,
        "email":       current_user.email,
        "phone":       p.phone if p else "",
        "linkedin":    p.linkedin if p else "",
        "github":      p.github if p else "",
        "portfolio":   p.portfolio if p else "",
        "years_exp":   p.years_exp if p else 0,
        "skills":      p.skills if p else [],
        "summary":     p.summary if p else "",
        "resume_path": p.resume_path if p else "",
    }


class ProfileUpdate(BaseModel):
    phone:     Optional[str] = None
    linkedin:  Optional[str] = None
    github:    Optional[str] = None
    portfolio: Optional[str] = None
    years_exp: float = 0
    skills:    List[str] = []
    summary:   Optional[str] = None

@app.put("/api/profile")
async def update_profile(
    body: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(UserProfile).where(UserProfile.user_id == current_user.id))
    p = result.scalar_one_or_none()
    if not p:
        p = UserProfile(user_id=current_user.id)
        db.add(p)
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    await db.commit()
    return {"ok": True}


@app.post("/api/profile/resume")
async def upload_resume(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Store resume in user-specific folder
    user_resume_dir = RESUME_DIR / str(current_user.id)
    user_resume_dir.mkdir(parents=True, exist_ok=True)
    path = user_resume_dir / file.filename
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    result = await db.execute(select(UserProfile).where(UserProfile.user_id == current_user.id))
    p = result.scalar_one_or_none()
    if not p:
        p = UserProfile(user_id=current_user.id)
        db.add(p)
    p.resume_path = str(path)
    await db.commit()
    return {"ok": True, "filename": file.filename}


# ══ SCRAPE / ENRICH ══════════════════════════════════════════════════════════

@app.post("/api/scrape/trigger")
async def trigger_scrape(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.tg_connected:
        raise HTTPException(400, "Connect your Telegram account first")
    background_tasks.add_task(_run_scrape_for_user, current_user.id)
    return {"ok": True, "message": "Scraping your channels…"}


async def _run_scrape_for_user(user_id: int):
    from backend.database import AsyncSessionLocal
    from scraper.telegram_scraper import scrape_for_user
    async with AsyncSessionLocal() as db:
        user = await db.get(User, user_id)
        if not user or not user.tg_connected:
            return
        api_id   = int(decrypt(user.tg_api_id))
        api_hash = decrypt(user.tg_api_hash)
        result   = await db.execute(select(Channel).where(Channel.user_id == user_id, Channel.is_active == True))
        channels = result.scalars().all()
    await scrape_for_user(user_id, api_id, api_hash, channels)


@app.post("/api/enrich/trigger")
async def trigger_enrich(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(UserProfile).where(UserProfile.user_id == current_user.id))
    p = result.scalar_one_or_none()
    profile = {
        "full_name": current_user.full_name, "email": current_user.email,
        "skills": p.skills if p else [], "years_exp": p.years_exp if p else 0,
        "summary": p.summary if p else "", "github": p.github if p else "",
        "portfolio": p.portfolio if p else "",
    }
    background_tasks.add_task(enrich_pending_jobs, profile, current_user.id)
    return {"ok": True}


# ══ SERVE FRONTEND ════════════════════════════════════════════════════════════
FRONTEND_DIR = Path("frontend")
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")
