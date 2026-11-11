import hashlib
import logging
import re
from typing import get_args

import pytest

from app import access
from app.access import (
    DENIALS,
    authorize,
    authorize_demo,
    estimate_cost,
    hash_ip,
    hash_token,
    log_usage,
    mint_token,
)
from app.models import AccessDecision


class FakeQuery:
    """Stands in for a supabase-py query: execute() returns canned data or raises."""

    def __init__(self, data=None, error: Exception | None = None):
        self.data = data
        self.error = error

    async def execute(self):
        if self.error:
            raise self.error
        return self


class FakeTable:
    def __init__(self, db: "FakeDb"):
        self.db = db

    def insert(self, row: dict) -> FakeQuery:
        self.db.inserts.append(row)
        return FakeQuery(error=self.db.error)


class FakeDb:
    """Records every rpc call and insert instead of talking to Supabase."""

    def __init__(self, rpc_data: dict | None = None, error: Exception | None = None):
        self.rpc_data = rpc_data
        self.error = error
        self.rpc_calls: list[tuple[str, dict]] = []
        self.inserts: list[dict] = []

    def rpc(self, name: str, params: dict) -> FakeQuery:
        self.rpc_calls.append((name, params))
        return FakeQuery(data=self.rpc_data, error=self.error)

    def table(self, name: str) -> FakeTable:
        assert name == "usage_log"
        return FakeTable(self)


@pytest.fixture
def fake_db(monkeypatch):
    def _use(**kwargs) -> FakeDb:
        db = FakeDb(**kwargs)

        async def get_db():
            return db

        monkeypatch.setattr(access, "get_db", get_db)
        return db

    return _use


def test_mint_token_format():
    token, token_hash, prefix = mint_token()
    assert re.fullmatch(r"sk-bootcamp-[A-Za-z0-9_-]{32}", token)
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert prefix == token[:18]
    assert len(prefix) > len("sk-bootcamp-"), "a prefix with no random part can't tell tokens apart"


def test_mint_token_is_unique():
    minted = [mint_token() for _ in range(200)]
    assert len({token for token, _, _ in minted}) == 200
    assert len({token_hash for _, token_hash, _ in minted}) == 200


def test_hash_token_is_sha256_hex():
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()
    assert len(hash_token("abc")) == 64


def test_hash_ip_is_stable_and_keyed(use_settings):
    use_settings(supabase_secret_key="secret-one")
    first = hash_ip("203.0.113.7")
    assert first == hash_ip("203.0.113.7")
    assert first != hash_ip("203.0.113.8")
    assert first != hashlib.sha256(b"203.0.113.7").hexdigest(), "a bare sha256 can be brute-forced"
    assert re.fullmatch(r"[0-9a-f]{64}", first)

    use_settings(supabase_secret_key="secret-two")
    assert hash_ip("203.0.113.7") != first


def test_hash_ip_requires_the_secret(use_settings):
    use_settings()
    with pytest.raises(RuntimeError, match="SUPABASE_SECRET_KEY"):
        hash_ip("203.0.113.7")


def test_estimate_cost_arithmetic():
    assert estimate_cost("claude-haiku-5-5", 1_000_000, 0) == pytest.approx(0.10)
    assert estimate_cost("claude-haiku-5-5", 0, 1_000_000) == pytest.approx(0.50)
    assert estimate_cost("claude-haiku-5-5", 1200, 300) == pytest.approx(0.00027)
    assert estimate_cost("claude-haiku-5-5", 0, 0) == 0


def test_estimate_cost_is_zero_for_unknown_models():
    assert estimate_cost("llama3.2", 5000, 5000) == 0
    assert estimate_cost(None, 5000, 5000) == 0


def test_generation_model_has_a_price(use_settings):
    settings = use_settings()
    assert settings.generation_model in access.PRICES_PER_MILLION, (
        "the configured model has no price, so the spend cap would never trigger"
    )


def test_denials_cover_every_reason():
    reason_type = AccessDecision.model_fields["reason"].annotation
    reasons = set(get_args(get_args(reason_type)[0]))
    assert reasons == set(DENIALS)


def test_denials_use_openai_statuses_and_codes():
    assert DENIALS["invalid_token"][:3] == (401, "invalid_request_error", "invalid_api_key")
    assert DENIALS["revoked"][:3] == (401, "invalid_request_error", "invalid_api_key")
    assert DENIALS["rate_limit_minute"][:3] == (429, "rate_limit_error", "rate_limit_exceeded")
    assert DENIALS["rate_limit_day"][:3] == (429, "rate_limit_error", "rate_limit_exceeded")
    assert DENIALS["spend_cap"][:3] == (429, "insufficient_quota", "insufficient_quota")
    assert DENIALS["kill_switch"].status == 503
    assert all(denial.message for denial in DENIALS.values())


@pytest.mark.parametrize(
    "token",
    [None, "", "sk-bootcamp-short", "sk-proj-" + "a" * 36, "Bearer sk-bootcamp-" + "a" * 32],
)
async def test_authorize_rejects_a_malformed_token_without_a_database_call(fake_db, token):
    db = fake_db()
    decision = await authorize(token)
    assert decision == AccessDecision(allowed=False, reason="invalid_token")
    assert db.rpc_calls == []


async def test_authorize_sends_only_the_hash(fake_db):
    token, token_hash, _ = mint_token()
    db = fake_db(
        rpc_data={
            "allowed": True,
            "reason": None,
            "token_id": "7d0f2a3e-0000-4000-8000-000000000001",
            "label": "Ada",
            "remaining_minute": 19,
            "remaining_day": 299,
        }
    )
    decision = await authorize(token)
    assert decision.allowed and decision.reason is None
    assert decision.token_id == "7d0f2a3e-0000-4000-8000-000000000001"
    assert db.rpc_calls == [("authorize_request", {"p_token_hash": token_hash})]


async def test_authorize_passes_a_denial_through(fake_db):
    token, _, _ = mint_token()
    fake_db(
        rpc_data={
            "allowed": False,
            "reason": "rate_limit_minute",
            "token_id": "7d0f2a3e-0000-4000-8000-000000000001",
            "retry_after_seconds": 60,
        }
    )
    decision = await authorize(token)
    assert not decision.allowed
    assert decision.reason == "rate_limit_minute"
    assert decision.retry_after_seconds == 60


async def test_authorize_demo_sends_the_hashed_ip(fake_db, use_settings):
    use_settings(supabase_secret_key="secret-one")
    db = fake_db(rpc_data={"allowed": True, "reason": None, "remaining_minute": 9})
    decision = await authorize_demo("203.0.113.7")
    assert decision.allowed
    assert db.rpc_calls == [("authorize_demo_request", {"p_ip_hash": hash_ip("203.0.113.7")})]


async def test_log_usage_writes_one_row_with_the_cost(fake_db):
    db = fake_db()
    await log_usage(
        surface="endpoint",
        outcome="completed",
        status=200,
        token_id="7d0f2a3e-0000-4000-8000-000000000001",
        model="claude-haiku-5-5",
        input_tokens=1200,
        output_tokens=300,
        latency_ms=840,
        ignored_params=["temperature"],
    )
    [row] = db.inserts
    assert row["estimated_cost_usd"] == pytest.approx(0.00027)
    assert row["surface"] == "endpoint" and row["outcome"] == "completed" and row["status"] == 200
    assert row["ignored_params"] == ["temperature"]
    assert row["client_ip_hash"] is None and row["judgments"] is None


async def test_log_usage_records_a_denial_at_no_cost(fake_db):
    db = fake_db()
    await log_usage(surface="demo", outcome="denied", status=429, detail="rate_limit_minute")
    [row] = db.inserts
    assert row["outcome"] == "denied" and row["detail"] == "rate_limit_minute"
    assert row["estimated_cost_usd"] == 0


async def test_log_usage_swallows_a_database_failure(fake_db, caplog):
    fake_db(error=RuntimeError("database is down"))
    with caplog.at_level(logging.ERROR, logger="app.access"):
        await log_usage(surface="endpoint", outcome="completed", status=200)
    assert "usage_log insert failed" in caplog.text


async def test_log_usage_swallows_a_missing_client(monkeypatch, caplog):
    async def no_db():
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY must be set")

    monkeypatch.setattr(access, "get_db", no_db)
    with caplog.at_level(logging.ERROR, logger="app.access"):
        await log_usage(surface="demo", outcome="answered", status=200)
    assert "usage_log insert failed" in caplog.text
