"""The token page's routes with access, the database and email replaced by fakes."""

import hashlib

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import access, mailer, token_routes
from app.models import AccessDecision

app = FastAPI()
app.include_router(token_routes.router)
client = TestClient(app)
GOOD = {"email": " Ada@Example.com ", "class_code": "open-sesame"}
TOKEN = "sk-bootcamp-" + "a" * 32


@pytest.fixture
def wired(monkeypatch, use_settings):
    """Allow every visitor, accept one class code and one link, record emails and log rows."""
    use_settings(supabase_secret_key="sb_secret_test", public_base_url="https://clinic.test/")
    state = {"decision": AccessDecision(allowed=True), "logged": [], "sent": [], "started": []}

    async def fake_authorize_demo(_ip):
        return state["decision"]

    async def fake_log_usage(**row):
        state["logged"].append(row)

    async def fake_start(email, class_code, _ip_hash):
        state["started"].append((email, class_code))
        if class_code != "open-sesame":
            return None, {"started": False, "reason": state.get("reason", "wrong_code")}
        return "link-secret", {"started": True, "reason": None}

    async def fake_redeem(link_secret):
        if link_secret != "link-secret":
            return None, {"issued": False, "reason": state.get("reason", "link_invalid")}
        return TOKEN, {"issued": True, "reason": None, "email": "ada@example.com", "replaced": True}

    async def fake_send(to, link):
        state["sent"].append((to, link))

    monkeypatch.setattr(token_routes, "authorize_demo", fake_authorize_demo)
    monkeypatch.setattr(token_routes, "log_usage", fake_log_usage)
    monkeypatch.setattr(token_routes, "start_token_request", fake_start)
    monkeypatch.setattr(token_routes, "redeem_token_link", fake_redeem)
    monkeypatch.setattr(token_routes, "send_token_link", fake_send)
    return state


def test_page_is_served():
    assert client.get("/token").status_code == 200


def test_right_code_emails_a_link_and_returns_no_token(wired):
    response = client.post("/api/token", json=GOOD)
    assert response.status_code == 200
    assert response.json() == {"sent": True, "email": "ada@example.com", "expires_minutes": 15}
    # The link uses the configured address and carries the secret after the #
    assert wired["sent"] == [("Ada@Example.com", "https://clinic.test/token#link=link-secret")]
    assert "link-secret" not in response.text
    [row] = wired["logged"]
    assert (row["surface"], row["outcome"], row["status"]) == ("token", "link_sent", 200)
    assert row["client_ip_hash"]


def test_wrong_code_sends_nothing_and_counts_toward_the_limit(wired):
    response = client.post("/api/token", json={**GOOD, "class_code": "guess"})
    assert (response.status_code, response.json()["error"]["code"]) == (403, "wrong_code")
    assert wired["sent"] == []
    # Any outcome other than "denied" counts toward the per-minute limit
    assert (wired["logged"][0]["outcome"], wired["logged"][0]["detail"]) == (
        "refused",
        "wrong_code",
    )


@pytest.mark.parametrize(("reason", "status"), [("signups_closed", 403), ("too_many_links", 429)])
def test_other_refusals_use_their_own_status(wired, reason, status):
    wired["reason"] = reason
    response = client.post("/api/token", json={**GOOD, "class_code": "guess"})
    assert (response.status_code, response.json()["error"]["code"]) == (status, reason)
    assert wired["sent"] == []


@pytest.mark.parametrize(
    "email",
    [None, "", "   ", 5, "no-at-sign", "two@@signs", "a@b", "a b@c.de", "x" * 250 + "@a.co"],
)
def test_bad_email_is_422_and_never_reaches_the_database(wired, email):
    response = client.post("/api/token", json={"email": email, "class_code": "open-sesame"})
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_email")
    assert wired["started"] == [] and wired["logged"] == []


@pytest.mark.parametrize("code", [None, "", "  ", "x" * 101])
def test_missing_class_code_is_422(wired, code):
    response = client.post("/api/token", json={"email": "ada@example.com", "class_code": code})
    assert response.json()["error"]["code"] == "invalid_class_code"


def test_body_that_is_not_an_object_is_422(wired):
    assert client.post("/api/token", content="[1, 2]").status_code == 422
    assert client.post("/api/token", content="not json").status_code == 422
    assert client.post("/api/token/redeem", content="not json").status_code == 422


def test_rate_limit_is_429_with_retry_after(wired):
    wired["decision"] = AccessDecision(
        allowed=False, reason="rate_limit_minute", retry_after_seconds=60
    )
    for path, payload in [("/api/token", GOOD), ("/api/token/redeem", {"link": "link-secret"})]:
        response = client.post(path, json=payload)
        assert (response.status_code, response.json()["error"]["code"]) == (429, "rate_limited")
        assert response.headers["retry-after"] == "60"
    assert wired["started"] == [] and wired["sent"] == []
    assert [row["outcome"] for row in wired["logged"]] == ["denied", "denied"]


def test_fails_closed_when_the_limit_check_breaks(wired, monkeypatch):
    async def broken(_ip):
        raise RuntimeError("database down")

    monkeypatch.setattr(token_routes, "authorize_demo", broken)
    assert client.post("/api/token", json=GOOD).status_code == 502
    assert client.post("/api/token/redeem", json={"link": "link-secret"}).status_code == 502
    assert wired["started"] == []


def test_email_failure_is_502_and_logged(wired, monkeypatch):
    async def broken(_to, _link):
        raise RuntimeError("email provider down")

    monkeypatch.setattr(token_routes, "send_token_link", broken)
    response = client.post("/api/token", json=GOOD)
    assert (response.status_code, response.json()["error"]["code"]) == (502, "upstream_error")
    assert wired["logged"][0]["outcome"] == "error"


def test_redeeming_the_link_shows_the_token_once_and_is_never_cached(wired):
    response = client.post("/api/token/redeem", json={"link": "link-secret"})
    assert response.status_code == 200
    assert response.json() == {"token": TOKEN, "email": "ada@example.com", "replaced": True}
    assert response.headers["cache-control"] == "no-store"
    assert wired["logged"][0]["outcome"] == "issued"


@pytest.mark.parametrize("reason", ["link_invalid", "link_used", "link_expired"])
def test_a_bad_link_is_410_and_counts_toward_the_limit(wired, reason):
    wired["reason"] = reason
    response = client.post("/api/token/redeem", json={"link": "something-else"})
    assert (response.status_code, response.json()["error"]["code"]) == (410, reason)
    assert "token" not in response.json()
    assert (wired["logged"][0]["outcome"], wired["logged"][0]["detail"]) == ("refused", reason)


@pytest.mark.parametrize("link", [None, "", 7, "x" * 101])
def test_missing_link_is_422(wired, link):
    response = client.post("/api/token/redeem", json={"link": link})
    assert (response.status_code, response.json()["error"]["code"]) == (422, "invalid_link")


def test_class_code_hash_ignores_case_and_outer_spaces():
    expected = hashlib.sha256(b"open-sesame").hexdigest()
    assert access.hash_class_code("  Open-Sesame ") == expected


@respx.mock
async def test_mailer_posts_the_link_to_resend_with_the_configured_sender(use_settings):
    use_settings(resend_api_key="re_test", email_from="Clinic <tokens@clinic.test>")
    route = respx.post(mailer.RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "1"}))
    await mailer.send_token_link("ada@example.com", "https://clinic.test/token#link=abc")
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer re_test"
    sent = httpx.Response(200, content=request.content).json()
    assert sent["from"] == "Clinic <tokens@clinic.test>"
    assert sent["to"] == ["ada@example.com"]
    assert "https://clinic.test/token#link=abc" in sent["text"]
    assert "https://clinic.test/token#link=abc" in sent["html"]
    assert "15 minutes" in sent["text"]


@respx.mock
async def test_mailer_raises_when_resend_refuses(use_settings):
    use_settings(resend_api_key="re_test", email_from="Clinic <tokens@clinic.test>")
    respx.post(mailer.RESEND_URL).mock(return_value=httpx.Response(403, json={"message": "no"}))
    with pytest.raises(httpx.HTTPStatusError):
        await mailer.send_token_link("ada@example.com", "https://clinic.test/token#link=abc")


async def test_mailer_refuses_to_run_without_configuration(use_settings):
    use_settings(resend_api_key=None, email_from=None)
    with pytest.raises(RuntimeError):
        await mailer.send_token_link("ada@example.com", "https://clinic.test/token#link=abc")


def test_base_url_prefers_the_explicit_setting_then_vercel_then_local(use_settings):
    from app.config import Settings

    explicit = Settings(public_base_url="https://a.test/", vercel_project_production_url="b.test")
    assert explicit.base_url == "https://a.test"
    vercel = Settings(public_base_url=None, vercel_project_production_url="b.test")
    assert vercel.base_url == "https://b.test"
