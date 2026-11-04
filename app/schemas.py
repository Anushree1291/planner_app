import datetime as dt
from typing import Optional, Literal
from pydantic import BaseModel, Field, model_validator

Energy = Literal["high", "medium", "low"]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
EMAIL = r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$"


class SignupRequest(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(default="", max_length=80)
    timezone: str = Field(default="UTC", max_length=64)


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class DemoRequest(BaseModel):
    timezone: str = Field(default="UTC", max_length=64)


class SettingsUpdate(BaseModel):
    timezone: Optional[str] = Field(default=None, max_length=64)
    name: Optional[str] = Field(default=None, max_length=80)
    email_morning_plan: Optional[bool] = None
    email_risk_alerts: Optional[bool] = None
    email_morning_hour: Optional[int] = Field(default=None, ge=6, le=9)
    email_weekly_digest: Optional[bool] = None
    day_pattern: Optional[Literal["steady", "varies"]] = None


class GoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    deadline: Optional[dt.date] = None
    auto_decompose: bool = False  # AI breaks it into tasks (rate-limited)


class TaskCreate(BaseModel):
    goal_id: Optional[int] = None
    title: str = Field(min_length=1, max_length=200)
    estimated_minutes: int = Field(default=30, ge=5, le=600)
    priority: int = Field(default=3, ge=1, le=5)
    energy_required: Energy = "medium"
    deadline: Optional[dt.date] = None
    depends_on_id: Optional[int] = None


class TaskUpdate(BaseModel):
    status: Optional[Literal["pending", "done", "skipped", "postponed"]] = None
    actual_minutes: Optional[int] = Field(default=None, ge=1, le=1440)


class CalendarBlockCreate(BaseModel):
    date: dt.date
    start_time: str = Field(pattern=HHMM)
    end_time: str = Field(pattern=HHMM)
    title: str = Field(default="Busy", max_length=120)

    @model_validator(mode="after")
    def end_after_start(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class WhatNowRequest(BaseModel):
    free_minutes: int = Field(ge=5, le=720)
    energy: Energy = "medium"


Weekday = Field(ge=0, le=6)


class RoutineCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    kind: Literal["work", "gym", "class", "commute", "other"] = "other"
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    weeks_of_month: list[int] = Field(default_factory=list, max_length=5)
    start_time: str = Field(pattern=HHMM)
    end_time: str = Field(pattern=HHMM)
    auto: bool = True
    note: str = Field(default="", max_length=300)
    replaces_block_id: Optional[int] = None   # the one-off block this routine came from

    @model_validator(mode="after")
    def check(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        if any(d < 0 or d > 6 for d in self.weekdays):
            raise ValueError("weekdays are 0 (Mon) to 6 (Sun)")
        if any(w < 1 or w > 5 for w in self.weeks_of_month):
            raise ValueError("weeks_of_month are 1 to 5")
        if self.auto and not self.weekdays:
            raise ValueError("pick at least one day")
        return self


class DayUpdate(BaseModel):
    kind: Literal["usual", "off", "light"]
    note: str = Field(default="", max_length=120)


class GoalChatStart(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    deadline: Optional[dt.date] = None


class ChatMessage(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
