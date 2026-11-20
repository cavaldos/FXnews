from __future__ import annotations

from app.models import NewsItem
from app.schemas import NewsOut


def news_out(item: NewsItem) -> NewsOut:
    return NewsOut.model_validate(item)
