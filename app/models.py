import datetime as dt
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, ForeignKey, DateTime, Date, UniqueConstraint, Text
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
    # email: only verified addresses get scheduled mail; each kind can be switched off
    email_verified = Column(Boolean, default=False, nullable=False)
    email_morning_plan = Column(Boolean, default=True, nullable=False)
    email_risk_alerts = Column(Boolean, default=True, nullable=False)
    email_morning_hour = Column(Integer, default=7, nullable=False)   # local hour, 6-9
    email_bounced = Column(Boolean, default=False, nullable=False)    # set from delivery reports
    email_weekly_digest = Column(Boolean, default=True, nullable=False)  # book/newsletter ideas
    last_digest_date = Column(Date, nullable=True)
    # "steady": days look alike, plan ahead.  "varies": ask each morning, plan that day only.
    day_pattern = Column(String(10), default="steady", nullable=False)
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
    status = Column(String(20), default="active")  # planning, active, done, paused
    kind = Column(String(20), default="general")   # book, fitness, course, exam, habit, general
    details = Column(Text, default="{}")           # JSON: book facts, chat state, proposed plan

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
    preferred_window = Column(String(10), nullable=True)   # morning, afternoon, evening
    recurring_id = Column(Integer, nullable=True, index=True)  # set on a habit's daily copy
    allowed_days = Column(String(20), nullable=True)        # "0,2,4": only plan on these weekdays

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


class EmailSend(Base):
    """One row per email the app tried to send: used to stay under the provider's
    per-minute and per-hour limits across runs, and as a send log."""
    __tablename__ = "email_sends"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind = Column(String(20))          # plan, risk, verify, test
    status = Column(String(20))        # sent, throttled, failed
    sent_at = Column(DateTime, default=utcnow, index=True)


class Routine(Base):
    """A regular part of someone's week: office, college, gym, commute.
    `auto` routines block time on their days by themselves. Non-auto ones ("only when I go")
    are offered as a one-tap 'going today' in the morning instead."""
    __tablename__ = "routines"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title = Column(String(120))
    kind = Column(String(20), default="other")     # work, gym, class, commute, other
    weekdays = Column(String(20), default="")      # "0,1,2,3,4" (Mon=0)
    weeks_of_month = Column(String(20), default="")  # "" every week, "2,4" = 2nd and 4th
    start_time = Column(String(5))
    end_time = Column(String(5))
    auto = Column(Boolean, default=True, nullable=False)
    note = Column(String(300), default="")         # e.g. "Mon legs, Wed upper body"


class RoutineSkip(Base):
    """'Not today' for one routine on one date."""
    __tablename__ = "routine_skips"
    __table_args__ = (UniqueConstraint("routine_id", "date"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    routine_id = Column(Integer, ForeignKey("routines.id", ondelete="CASCADE"))
    date = Column(Date)


class DayStatus(Base):
    """How a particular day looks: usual, off (leave, holiday) or light (busy, plan little)."""
    __tablename__ = "day_status"
    __table_args__ = (UniqueConstraint("user_id", "date"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    date = Column(Date)
    kind = Column(String(10))                      # usual, off, light
    note = Column(String(120), default="")


class Recurring(Base):
    """A repeating step from a habit or fitness plan (e.g. 'Meditate 10 min' every day).
    The planner makes that day's copy as a Task when it plans the day."""
    __tablename__ = "recurring"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id = Column(Integer, ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    title = Column(String(200))
    minutes = Column(Integer, default=30)
    energy = Column(String(10), default="medium")
    weekdays = Column(String(20), default="0,1,2,3,4,5,6")
    preferred_window = Column(String(10), nullable=True)


class GoalMessage(Base):
    """The planning chat for a goal."""
    __tablename__ = "goal_messages"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    goal_id = Column(Integer, ForeignKey("goals.id", ondelete="CASCADE"), index=True)
    role = Column(String(10))                      # user, assistant, tool
    content = Column(Text)
    extra = Column(Text, default="{}")             # JSON: quick replies, plan preview, lookup card
    created_at = Column(DateTime, default=utcnow)
