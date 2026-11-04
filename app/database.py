import logging

from sqlalchemy import create_engine, inspect, literal, text
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


def add_missing_columns():
    """Tiny migration step: create_all() makes new tables but never adds columns to
    existing ones. This adds any column defined on a model but missing in the database
    (e.g. after an upgrade), using the column's default for existing rows."""
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}'
                default = col.default.arg if col.default is not None and col.default.is_scalar else None
                if default is not None:
                    lit = literal(default).compile(dialect=engine.dialect,
                                                   compile_kwargs={"literal_binds": True})
                    ddl += f" DEFAULT {lit}"
                    if not col.nullable:
                        ddl += " NOT NULL"
                logging.getLogger("planner.db").info("migrating: %s", ddl)
                conn.execute(text(ddl))
