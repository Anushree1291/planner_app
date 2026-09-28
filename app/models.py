import datetime as dt
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, ForeignKey, DateTime, Date, Time
)
from sqlalchemy.orm import relationship
from .database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String, default="me")
    ntfy_topic = Column(String, nullable=True)  # for push notifications

    profile = relationship("UserProfile", back_populates="user", uselist=False)
    goals = relationship("Goal", back_populates="user")


class UserProfile(Base):
    """Long-term memory: stable preferences that make planning feel personal."""
    __tablename__ = "user_profiles"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))

    deep_work_start = Column(String, default="09:00")   # "HH:MM"
    deep_work_end = Column(String, default="12:00")
    avoid_heavy_after = Column(String, default="21:00")
    preferred_session_min = Column(Integer, default=45)
    break_min = Column(Integer, default=10)
    avg_overrun_factor = Column(Float, default=1.0)  # learned: actual/estimated ratio

    user = relationship("User", back_populates="profile")


class Goal(Base):
    __tablename__ = "goals"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    title = Column(String)
    deadline = Column(Date, nullable=True)
    status = Column(String, default="active")  # active, done, paused

    user = relationship("User", back_populates="goals")
    tasks = relationship("Task", back_populates="goal")


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    goal_id = Column(Integer, ForeignKey("goals.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"))

    title = Column(String)
    estimated_minutes = Column(Integer, default=30)
    actual_minutes = Column(Integer, nullable=True)
    priority = Column(Integer, default=3)          # 1 (low) - 5 (critical)
    energy_required = Column(String, default="medium")  # high, medium, low
    status = Column(String, default="pending")     # pending, done, skipped, postponed
    deadline = Column(Date, nullable=True)
    depends_on_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    postponed_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    goal = relationship("Goal", back_populates="tasks")


class CalendarBlock(Base):
    """Manually entered busy blocks (meetings, commute, meals) — stand-in for
    a real Google/Outlook calendar sync in Phase 4."""
    __tablename__ = "calendar_blocks"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    date = Column(Date)
    start_time = Column(String)  # "HH:MM"
    end_time = Column(String)
    title = Column(String)


class PlanItem(Base):
    __tablename__ = "plan_items"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    date = Column(Date)
    task_id = Column(Integer, ForeignKey("tasks.id"))
    start_time = Column(String)
    end_time = Column(String)
    status = Column(String, default="scheduled")  # scheduled, done, missed
