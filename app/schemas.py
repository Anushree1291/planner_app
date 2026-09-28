import datetime as dt
from typing import Optional, List
from pydantic import BaseModel


class GoalCreate(BaseModel):
    title: str
    deadline: Optional[dt.date] = None
    auto_decompose: bool = False  # if true and ANTHROPIC_API_KEY set, AI breaks it into tasks


class TaskCreate(BaseModel):
    goal_id: Optional[int] = None
    title: str
    estimated_minutes: int = 30
    priority: int = 3
    energy_required: str = "medium"
    deadline: Optional[dt.date] = None
    depends_on_id: Optional[int] = None


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    actual_minutes: Optional[int] = None


class CalendarBlockCreate(BaseModel):
    date: dt.date
    start_time: str
    end_time: str
    title: str


class WhatNowRequest(BaseModel):
    free_minutes: int
    energy: str = "medium"  # high, medium, low
