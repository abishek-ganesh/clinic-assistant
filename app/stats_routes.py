"""The public usage page and the JSON behind it. main.py includes this router."""

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from app import stats

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

# Anyone can load this page, and every uncached load is a database query. Letting browsers
# and the CDN reuse a response for a minute caps that at one query a minute per window.
CACHE_SECONDS = 60

router = APIRouter()


@router.get("/stats")
def stats_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "stats.html")


@router.get("/api/stats")
async def api_stats(request: Request) -> JSONResponse:
    """Usage aggregates for the last ?days= days (1 to 90, default 30)."""
    days = stats.clamp_days(request.query_params.get("days"))
    try:
        body = await stats.get_stats(days)
    except Exception:
        logger.exception("usage stats query failed")
        return JSONResponse(
            {
                "error": {
                    "code": "upstream_error",
                    "message": "The usage numbers could not be loaded. Try again in a moment.",
                }
            },
            502,
        )
    cache = f"public, max-age={CACHE_SECONDS}, s-maxage={CACHE_SECONDS}"
    return JSONResponse(body, headers={"Cache-Control": cache})
