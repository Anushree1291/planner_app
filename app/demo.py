"""Sample data for one-click guest sandboxes, so a visitor sees a real plan immediately."""
import datetime as dt
from sqlalchemy.orm import Session
from . import models

SAMPLE_GOALS = [
    ("Learn GenAI fundamentals", 45, [
        ("Watch intro lecture on transformers", 60, 4, "high"),
        ("Read 'Attention Is All You Need' summary", 45, 3, "high"),
        ("Build a tiny RAG demo over my notes", 90, 5, "high"),
        ("Write a blog post on what I learned", 60, 2, "medium"),
    ]),
    ("Run a 10K", 60, [
        ("Easy 3 km run", 30, 3, "medium"),
        ("Stretching + mobility", 20, 2, "low"),
        ("Plan weekly running schedule", 15, 3, "low"),
    ]),
    ("Ship portfolio website", 20, [
        ("Pick a template and domain", 30, 4, "medium"),
        ("Write project case studies", 90, 5, "high"),
        ("Deploy and share on LinkedIn", 30, 3, "low"),
    ]),
]

SAMPLE_BLOCKS = [("10:00", "11:00", "Team stand-up"), ("13:00", "14:00", "Lunch"),
                 ("16:00", "17:00", "Gym")]


def seed_sample_data(db: Session, user: models.User, today: dt.date):
    for title, days, tasks in SAMPLE_GOALS:
        goal = models.Goal(user_id=user.id, title=title, deadline=today + dt.timedelta(days=days))
        db.add(goal)
        db.flush()
        for t_title, minutes, prio, energy in tasks:
            db.add(models.Task(user_id=user.id, goal_id=goal.id, title=t_title,
                               estimated_minutes=minutes, priority=prio, energy_required=energy))
    for start, end, title in SAMPLE_BLOCKS:
        db.add(models.CalendarBlock(user_id=user.id, date=today, start_time=start,
                                    end_time=end, title=title))
