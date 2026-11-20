"""Small helpers shared by the test modules (the DB is one temp file for the whole test session)."""

from sqlalchemy import delete

from app.db import init_db, session_scope
from app.models import Analysis, NewsItem, Signal


def wipe_db() -> None:
    """Empty the news/analysis tables so a test neither sees nor leaves rows (test_pipeline asserts exact counts)."""
    init_db()
    with session_scope() as session:
        session.execute(delete(Signal))
        session.execute(delete(Analysis))
        session.execute(delete(NewsItem))
