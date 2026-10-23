"""Supabase access over HTTPS (PostgREST). The app never opens a direct Postgres connection."""

from supabase import AsyncClient, acreate_client

from app.config import get_settings

_client: AsyncClient | None = None


async def get_db() -> AsyncClient:
    """One client per warm serverless instance, created on first use."""
    global _client
    if _client is None:
        settings = get_settings()
        if not (settings.supabase_url and settings.supabase_secret_key):
            raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY must be set")
        _client = await acreate_client(settings.supabase_url, settings.supabase_secret_key)
    return _client
