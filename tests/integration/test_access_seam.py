"""Real round trip for access control: token row -> authorize_request -> usage_log.

Run: uv run pytest -m integration. Writes its own rows and deletes them afterwards.
"""

from datetime import UTC, datetime

import pytest
from postgrest.exceptions import APIError

from app.access import authorize, authorize_demo, hash_ip, log_usage, mint_token
from app.config import get_settings
from app.db import get_db

pytestmark = pytest.mark.integration

LABEL = "access-seam-test"
# From the range reserved for documentation, so it can never be a real visitor
TEST_IP = "203.0.113.7"


@pytest.fixture(autouse=True)
def require_credentials():
    if not get_settings().providers()["supabase"]:
        pytest.skip("needs SUPABASE_URL and SUPABASE_SECRET_KEY in .env")


async def delete_test_rows(db) -> None:
    """Remove everything this file writes, including leftovers from an interrupted run."""
    tokens = await db.table("student_tokens").select("id").eq("label", LABEL).execute()
    for token in tokens.data:
        await db.table("usage_log").delete().eq("token_id", token["id"]).execute()
    await db.table("usage_log").delete().eq("client_ip_hash", hash_ip(TEST_IP)).execute()
    await db.table("student_tokens").delete().eq("label", LABEL).execute()


@pytest.fixture
async def stored_token():
    """A real token row with a tiny per-minute limit. Yields (plain token, row id)."""
    db = await get_db()
    await delete_test_rows(db)
    token, token_hash, prefix = mint_token()
    row = {"label": LABEL, "token_hash": token_hash, "token_prefix": prefix, "rpm_limit": 2}
    inserted = await db.table("student_tokens").insert(row).execute()
    try:
        yield token, inserted.data[0]["id"]
    finally:
        await delete_test_rows(db)


async def test_minted_token_is_authorized_and_an_unknown_one_is_not(stored_token):
    token, token_id = stored_token
    decision = await authorize(token)
    assert decision.allowed and decision.reason is None
    assert decision.token_id == token_id

    unknown, _, _ = mint_token()
    decision = await authorize(unknown)
    assert not decision.allowed and decision.reason == "invalid_token"


async def test_usage_rows_are_written_and_count_toward_the_limit(stored_token):
    token, token_id = stored_token
    db = await get_db()

    await log_usage(
        surface="endpoint",
        outcome="completed",
        status=200,
        token_id=token_id,
        model=get_settings().generation_model,
        input_tokens=1200,
        output_tokens=300,
        latency_ms=840,
        ignored_params=["temperature"],
    )
    await log_usage(
        surface="endpoint", outcome="denied", status=429, token_id=token_id, detail="spend_cap"
    )

    logged = db.table("usage_log").select("*").eq("token_id", token_id).order("id")
    rows = (await logged.execute()).data
    assert [row["outcome"] for row in rows] == ["completed", "denied"]
    assert float(rows[0]["estimated_cost_usd"]) == pytest.approx(0.00027)
    assert rows[0]["ignored_params"] == ["temperature"]
    assert rows[1]["detail"] == "spend_cap"

    # rpm_limit is 2: one counted row leaves room, the denied row doesn't count, a second fills it
    assert (await authorize(token)).allowed
    await log_usage(surface="endpoint", outcome="completed", status=200, token_id=token_id)
    decision = await authorize(token)
    assert decision.reason == "rate_limit_minute"
    assert decision.retry_after_seconds == 60


async def test_revoked_token_is_denied(stored_token):
    token, token_id = stored_token
    db = await get_db()
    revoked_at = datetime.now(UTC).isoformat()
    await db.table("student_tokens").update({"revoked_at": revoked_at}).eq("id", token_id).execute()
    decision = await authorize(token)
    assert decision.reason == "revoked"
    assert decision.token_id == token_id


async def test_demo_request_is_authorized_by_ip():
    db = await get_db()
    try:
        decision = await authorize_demo(TEST_IP)
    except APIError as exc:
        # PGRST202: PostgREST can't find the function
        if exc.code == "PGRST202":
            pytest.skip("authorize_demo_request is not in the live schema yet: apply the migration")
        raise
    assert decision.allowed and decision.token_id is None

    try:
        await log_usage(
            surface="demo", outcome="answered", status=200, client_ip_hash=hash_ip(TEST_IP)
        )
        logged = db.table("usage_log").select("id").eq("client_ip_hash", hash_ip(TEST_IP))
        assert len((await logged.execute()).data) == 1
    finally:
        await db.table("usage_log").delete().eq("client_ip_hash", hash_ip(TEST_IP)).execute()
