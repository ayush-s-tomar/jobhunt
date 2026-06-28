"""
database.py — All SQLAlchemy models + async engine
Multi-user version: every resource is scoped to a user_id
"""
import os
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean, Float, ForeignKey, JSON
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, relationship

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/jobhunt.db")
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


# ── Users ─────────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String(256), unique=True, index=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)
    full_name       = Column(String(128), nullable=True)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    # Telegram credentials — stored encrypted
    tg_api_id       = Column(Text, nullable=True)    # encrypted
    tg_api_hash     = Column(Text, nullable=True)    # encrypted
    tg_phone        = Column(Text, nullable=True)    # encrypted
    tg_session      = Column(Text, nullable=True)    # encrypted StringSession

    profile     = relationship("UserProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    channels    = relationship("Channel", back_populates="user", cascade="all, delete-orphan")
    jobs        = relationship("Job", back_populates="user", cascade="all, delete-orphan")


# ── User profile ──────────────────────────────────────────────────────────────
class UserProfile(Base):
    __tablename__ = "user_profile"

    id          = Column(Integer, primary_key=True)
    user_id     = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    phone       = Column(String(32), nullable=True)
    linkedin    = Column(String(256), nullable=True)
    github      = Column(String(256), nullable=True)
    portfolio   = Column(String(256), nullable=True)
    years_exp   = Column(Float, default=0)
    skills      = Column(JSON, default=list)
    resume_path = Column(String(512), nullable=True)
    summary     = Column(Text, nullable=True)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="profile")


# ── Channel registry ──────────────────────────────────────────────────────────
class Channel(Base):
    __tablename__ = "channels"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    username    = Column(String(128), index=True)
    title       = Column(String(256))
    is_private  = Column(Boolean, default=False)
    is_active   = Column(Boolean, default=True)
    last_polled = Column(DateTime, nullable=True)
    job_count   = Column(Integer, default=0)
    created_at  = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="channels")
    jobs = relationship("Job", back_populates="channel", cascade="all, delete-orphan")


# ── Job posts ─────────────────────────────────────────────────────────────────
class Job(Base):
    __tablename__ = "jobs"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    channel_id      = Column(Integer, ForeignKey("channels.id"))
    message_id      = Column(Integer)
    raw_text        = Column(Text)
    title           = Column(String(256), nullable=True)
    company         = Column(String(128), nullable=True)
    location        = Column(String(128), nullable=True)
    salary          = Column(String(64), nullable=True)
    skills_required = Column(JSON, default=list)
    apply_email     = Column(String(256), nullable=True)
    apply_url       = Column(String(512), nullable=True)
    apply_type      = Column(String(16), nullable=True)
    match_score     = Column(Float, default=0.0)
    is_remote       = Column(Boolean, default=False)
    posted_at       = Column(DateTime, nullable=True)
    scraped_at      = Column(DateTime, default=datetime.utcnow)
    status          = Column(String(32), default="new")
    is_duplicate    = Column(Boolean, default=False)

    user        = relationship("User", back_populates="jobs")
    channel     = relationship("Channel", back_populates="jobs")
    application = relationship("Application", back_populates="job", uselist=False, cascade="all, delete-orphan")


# ── Application tracker ───────────────────────────────────────────────────────
class Application(Base):
    __tablename__ = "applications"

    id              = Column(Integer, primary_key=True, index=True)
    job_id          = Column(Integer, ForeignKey("jobs.id"), unique=True)
    cover_letter    = Column(Text, nullable=True)
    email_sent      = Column(Boolean, default=False)
    form_filled     = Column(Boolean, default=False)
    captcha_blocked = Column(Boolean, default=False)
    applied_at      = Column(DateTime, nullable=True)
    log             = Column(JSON, default=list)

    job = relationship("Job", back_populates="application")


async def init_db():
    import os
    os.makedirs("data/sessions", exist_ok=True)
    os.makedirs("data/resumes", exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
