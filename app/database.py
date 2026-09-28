import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Local dev: sqlite file. On Azure: set DATABASE_URL to your Postgres connection string, e.g.
# postgresql://user:password@your-server.postgres.database.azure.com:5432/plannerdb?sslmode=require
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./planner.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
