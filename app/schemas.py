import datetime as dt
from typing import Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

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


class NotificationSetup(BaseModel):
    topic: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")

    @field_validator("topic")
    @classmethod
    def not_trivial(cls, v):
        if v.lower() in {"planner", "test1234", "notifications"}:
            raise ValueError("pick a harder-to-guess topic name")
        return v
