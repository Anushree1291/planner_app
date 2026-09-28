from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import DATABASE_URL

# Local dev: sqlite file. In the cloud: DATABASE_URL points at Postgres (e.g. Neon), e.g.
# postgresql://user:password@ep-xxx.region.aws.neon.tech/plannerdb?sslmode=require
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    # pool_pre_ping: serverless Postgres drops idle connections; test before use.
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300,
                           pool_size=5, max_overflow=5)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
