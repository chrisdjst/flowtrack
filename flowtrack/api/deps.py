from collections.abc import Generator

from sqlalchemy.orm import Session

from flowtrack.core.database import SessionLocal


def db_session() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a Session, commits on success, rolls back on raise.

    Pattern differs from core.database.get_db (context manager) because FastAPI
    needs a generator-style dependency.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
