"""Real round trip: Voyage -> Supabase insert -> match_chunks. Run: uv run pytest -m integration"""

import hashlib

import pytest

from app.config import get_settings
from app.db import get_db
from app.retrieval import embed, match_chunks

pytestmark = pytest.mark.integration

SMOKE_SLUG = "phase0-smoke"
SENTENCES = {
    "parking": "Free parking is available in the lot behind the Main Street location.",
    "refills": "Prescription refill requests are processed within three business days.",
    "flu": "Flu shots are offered on a walk-in basis every Saturday in October.",
}


@pytest.fixture(autouse=True)
def require_credentials():
    providers = get_settings().providers()
    if not (providers["supabase"] and providers["voyage"]):
        pytest.skip("needs SUPABASE_URL, SUPABASE_SECRET_KEY and VOYAGE_API_KEY in .env")


async def test_embed_insert_and_match_returns_the_right_sentence():
    settings = get_settings()
    db = await get_db()
    await db.table("handbook_chunks").delete().eq("section_slug", SMOKE_SLUG).execute()

    vectors = await embed(list(SENTENCES.values()), "document")
    assert all(len(v) == settings.embedding_dimensions for v in vectors)
    rows = [
        {
            "section_slug": SMOKE_SLUG,
            "section_title": key,
            "content": text,
            "token_count": len(text.split()),
            "embedding": vector,
            "embedding_model": settings.embedding_model,
        }
        for (key, text), vector in zip(SENTENCES.items(), vectors, strict=True)
    ]
    await db.table("handbook_chunks").insert(rows).execute()

    try:
        [question] = await embed(["How long does it take to refill my medication?"], "query")
        matches = [m for m in await match_chunks(question, 50) if m["section_slug"] == SMOKE_SLUG]
        assert [m["section_title"] for m in matches][0] == "refills"
        assert matches[0]["similarity"] > matches[1]["similarity"]
    finally:
        await db.table("handbook_chunks").delete().eq("section_slug", SMOKE_SLUG).execute()


async def test_authorize_request_rejects_an_unknown_token():
    db = await get_db()
    unknown = hashlib.sha256(b"sk-bootcamp-not-a-real-token").hexdigest()
    result = await db.rpc("authorize_request", {"p_token_hash": unknown}).execute()
    assert result.data["allowed"] is False
    assert result.data["reason"] == "invalid_token"
