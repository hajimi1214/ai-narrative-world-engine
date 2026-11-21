import os
from datetime import datetime
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./narrative.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)

# Older SQLite migrations use ``now()`` as a server default. SQLite does not
# provide that function by default, while PostgreSQL does. Registering a
# deterministic UTC implementation keeps fresh and upgraded local databases
# compatible without changing the historical migration records.
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _register_sqlite_now(dbapi_connection, connection_record):
        dbapi_connection.create_function("now", 0, lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass
