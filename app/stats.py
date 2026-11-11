"""Usage numbers for the public /stats page. Postgres does the counting; this is one call."""

from app.db import get_db

# The window the page asks for, in days. The usage_stats function clamps to the same range.
DEFAULT_DAYS = 30
MIN_DAYS = 1
MAX_DAYS = 90


def clamp_days(raw: str | None) -> int:
    """The ?days= value as a number inside the allowed window, or the default if unreadable."""
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(days, MAX_DAYS))


async def get_stats(days: int = DEFAULT_DAYS) -> dict:
    """Aggregates from usage_log for the last `days` days. Totals only: no rows, no identities."""
    db = await get_db()
    result = await db.rpc("usage_stats", {"p_days": days}).execute()
    return result.data
