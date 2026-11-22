"""GET /api/link-preview?url=... : title / image / excerpt of a public web page (hover cards in the UI)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services import link_preview

router = APIRouter(prefix="/api", tags=["link-preview"])


@router.get("/link-preview")
async def get_link_preview(url: str = Query(..., min_length=1, max_length=2048, description="http(s) URL to preview")) -> dict:
    """Always 200 with `ok`: false + `error` when the page can't be read; 400 when the URL itself is refused (SSRF guard)."""
    try:
        return (await link_preview.get_preview(url)).to_dict()
    except link_preview.BlockedURL as exc:
        raise HTTPException(400, str(exc)) from exc
    except link_preview.FetchFailed as exc:  # DNS failure during validation
        return link_preview.Preview(ok=False, url=url, error=str(exc)).to_dict()
