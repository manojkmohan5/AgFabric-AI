from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


# Bounded waits: an unreachable database fails a request in seconds rather than
# hanging it, and an exhausted pool says so instead of queueing indefinitely.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_timeout=10,
    connect_args={"connect_timeout": 5},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
