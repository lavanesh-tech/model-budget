from collections.abc import Generator

from sqlalchemy import URL, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def build_database_url() -> URL:
    settings = get_settings()
    return URL.create(
        drivername="postgresql+psycopg",
        username=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


engine = create_engine(
    build_database_url(),
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def verify_connection() -> None:
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1")).scalar_one()
        if result != 1:
            raise RuntimeError("Database connectivity check returned an unexpected result")