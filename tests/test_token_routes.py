"""The token page's route with access and the database replaced by fakes."""

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import access, token_routes
from app.models import AccessDecision

app = FastAPI()
app.include_router(token_routes.router)
client = TestClient(app)
GOOD = {"email": " Ada@Example.com ", "class_code": "open-sesame"}


@pytest.fixture
def wired(monkeypatch, use_settings):
    """Allow every visitor, issue a fixed token for the right code, record log rows."""
    use_settings(supabase_secret_key="sb_secret_test")
    state = {"decision": AccessDecision(allowed=True), "logged": [], "requests": []}

    async def fake_authorize_demo(_ip):
        return state["decision"]

    async def fake_log_usage(**row):
        state["logged"].append(row)

    async def fake_request_token(email, class_code):
        state["requests"].append((email, class_code))
        if class_code != "open-sesame":
            return None, {"issued": False, "reason": state.get("reason", "wrong_code")}
        return "sk-bootcamp-" + "a" * 32, {"issued": True, "reason": None, "replaced": False}

    monkeypatch.setattr(token_routes, "authorize_demo", fake_authorize_demo)
    monkeypatch.setattr(token_routes, "log_usage", fake_log_usage)
    monkeypatch.setattr(token_routes, "request_token", fake_request_token)
    return state


def test_page_is_served():
    assert client.get("/token").status_code == 200


def test_right_code_returns_the_token_once_and_logs_it(wired):
    response = client.post("/api/token", json=GOOD)
    assert response.status_code == 200
    assert response.json() == {
        "token": "sk-bootcamp-" + "a" * 32,
        "email": "ada@example.com",
        "replaced": False,
    }
    assert response.headers["cache-control"] == "no-store"
    assert wired["requests"] == [("Ada@Example.com", "open-sesame")]
    [row] = wired["logged"]
    assert (row["surface"], row["outcome"], row["status"]) == ("token", "issued", 200)
    assert row["client_ip_hash"]


def test_wrong_code_is_403_and_counts_toward_the_limit(wired):
    response = client.post("/api/token", json={**GOOD, "class_code": "guess"})
    assert (response.status_code, response.json()["error"]["code"]) == (403, "wrong_code")
    assert "token" not in response.json()
    # Any outcome other than "denied" counts toward the per-minute limit
    assert (wired["logged"][0]["outcome"], wired["logged"][0]["detail"]) == (
        "refused",
        "wrong_code",
    )


def test_closed_signups_are_403(wired):
    wired["reason"] = "signups_closed"
    response = client.post("/api/token", json={**GOOD, "class_code": "guess"})
    assert (response.status_code, response.json()["error"]["code"]) == (403, "signups_closed")


@pytest.mark.parametrize(
    "email",
    [None, "", "   ", 5, "no-at-sign", "two@@signs", "a@b", "a b@c.de", "x" * 250 + "@a.co"],
)
def test_bad_email_is_422_and_never_reaches_the_database(wired, email):
    response = client.post("/api/token", json={"email": email, "class_code": "open-sesame"})
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_email")
    assert wired["requests"] == [] and wired["logged"] == []


@pytest.mark.parametrize("code", [None, "", "  ", "x" * 101])
def test_missing_class_code_is_422(wired, code):
    response = client.post("/api/token", json={"email": "ada@example.com", "class_code": code})
    assert response.json()["error"]["code"] == "invalid_class_code"


def test_body_that_is_not_an_object_is_422(wired):
    assert client.post("/api/token", content="[1, 2]").status_code == 422
    assert client.post("/api/token", content="not json").status_code == 422


def test_rate_limit_is_429_with_retry_after(wired):
    wired["decision"] = AccessDecision(
        allowed=False, reason="rate_limit_minute", retry_after_seconds=60
    )
    response = client.post("/api/token", json=GOOD)
    assert (response.status_code, response.json()["error"]["code"]) == (429, "rate_limited")
    assert response.headers["retry-after"] == "60"
    assert wired["requests"] == []
    assert wired["logged"][0]["outcome"] == "denied"


def test_fails_closed_when_the_limit_check_breaks(wired, monkeypatch):
    async def broken(_ip):
        raise RuntimeError("database down")

    monkeypatch.setattr(token_routes, "authorize_demo", broken)
    assert client.post("/api/token", json=GOOD).status_code == 502
    assert wired["requests"] == []


def test_database_failure_is_502_and_logged(wired, monkeypatch):
    async def broken(_email, _code):
        raise RuntimeError("database down")

    monkeypatch.setattr(token_routes, "request_token", broken)
    response = client.post("/api/token", json=GOOD)
    assert (response.status_code, response.json()["error"]["code"]) == (502, "upstream_error")
    assert wired["logged"][0]["outcome"] == "error"


def test_class_code_hash_ignores_case_and_outer_spaces():
    expected = hashlib.sha256(b"open-sesame").hexdigest()
    assert access.hash_class_code("  Open-Sesame ") == expected
