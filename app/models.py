import datetime as dt
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, ForeignKey, DateTime, Date, UniqueConstraint
)
from sqlalchemy.orm import relationship
from .database import Base


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(254), unique=True, index=True, nullable=True)  # null for guests
    password_hash = Column(String(100), nullable=True)
    name = Column(String(80), default="me")
    timezone = Column(String(64), default="UTC")        # IANA name, e.g. Asia/Kolkata
    is_guest = Column(Boolean, default=False)
    ntfy_topic = Column(String(64), nullable=True)      # for push notifications
    created_at = Column(DateTime, default=utcnow)
    # idempotency for the hourly job: don't send the same daily push twice
    last_morning_run = Column(Date, nullable=True)
    last_risk_run = Column(Date, nullable=True)

    profile = relationship("UserProfile", back_populates="user", uselist=False,
                           cascade="all, delete-orphan")
    goals = relationship("Goal", back_populates="user", cascade="all, delete-orphan")


class UserProfile(Base):
    """Long-term memory: stable preferences that make planning feel personal."""
    __tablename__ = "user_profiles"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)

    deep_work_start = Column(String(5), default="09:00")   # "HH:MM"
    deep_work_end = Column(String(5), default="12:00")
    avoid_heavy_after = Column(String(5), default="21:00")
    preferred_session_min = Column(Integer, default=45)
    break_min = Column(Integer, default=10)
    avg_overrun_factor = Column(Float, default=1.0)  # learned: actual/estimated ratio

    user = relationship("User", back_populates="profile")


class Goal(Base):
    __tablename__ = "goals"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title = Column(String(200))
    deadline = Column(Date, nullable=True)
    status = Column(String(20), default="active")  # active, done, paused

    user = relationship("User", back_populates="goals")
    tasks = relationship("Task", back_populates="goal")


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    goal_id = Column(Integer, ForeignKey("goals.id", ondelete="CASCADE"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)

    title = Column(String(200))
    estimated_minutes = Column(Integer, default=30)
    actual_minutes = Column(Integer, nullable=True)
    priority = Column(Integer, default=3)          # 1 (low) - 5 (critical)
    energy_required = Column(String(10), default="medium")  # high, medium, low
    status = Column(String(20), default="pending")     # pending, done, skipped, postponed
    deadline = Column(Date, nullable=True)
    depends_on_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    postponed_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)

    goal = relationship("Goal", back_populates="tasks")


class CalendarBlock(Base):
    """Manually entered busy blocks (meetings, commute, meals) — stand-in for
    a real Google/Outlook calendar sync in Phase 4."""
    __tablename__ = "calendar_blocks"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    date = Column(Date)
    start_time = Column(String(5))  # "HH:MM"
    end_time = Column(String(5))
    title = Column(String(120))


class PlanItem(Base):
    __tablename__ = "plan_items"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    date = Column(Date)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"))
    start_time = Column(String(5))
    end_time = Column(String(5))
    status = Column(String(20), default="scheduled")  # scheduled, done, missed


class AiUsage(Base):
    """Counts AI calls per user per UTC day so the public demo can't run up the bill."""
    __tablename__ = "ai_usage"
    __table_args__ = (UniqueConstraint("user_id", "day"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    day = Column(Date, index=True)
    count = Column(Integer, default=0)
