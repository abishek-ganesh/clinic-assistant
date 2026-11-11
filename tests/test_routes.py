"""Route behavior with access, the pipeline and the model calls replaced by fakes."""

import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import AccessDecision, ChatResult
from app.pipeline import AskResult

client = TestClient(main.app)
TOKEN = "sk-bootcamp-" + "a" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CHAT = {"model": "bootcamp", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def wired(monkeypatch, use_settings):
    """Allow every request by default and record usage_log writes instead of sending them."""
    use_settings(supabase_secret_key="sb_secret_test")
    state = {"decision": AccessDecision(allowed=True, token_id="tok-1"), "logged": []}

    async def fake_authorize(_token_or_ip):
        return state["decision"]

    async def fake_log_usage(**row):
        state["logged"].append(row)

    async def fake_chat(_request):
        return ChatResult(
            text="Hello!", finish_reason="stop", model="claude-haiku-5-5",
            input_tokens=10, output_tokens=3,
        )  # fmt: skip

    async def fake_chat_stream(request):
        yield "Hel"
        yield "lo!"
        yield await fake_chat(request)

    async def fake_ask(question):
        body = {
            "question": question, "decision": "answered", "decision_reason": None,
            "judgments": {"is_emergency": 0.01, "in_scope": 0.95}, "latency_ms": 5,
        }  # fmt: skip
        return AskResult(body=body, model="claude-haiku-5-5", input_tokens=700, output_tokens=50)

    monkeypatch.setattr(main, "authorize", fake_authorize)
    monkeypatch.setattr(main, "authorize_demo", fake_authorize)
    monkeypatch.setattr(main, "log_usage", fake_log_usage)
    monkeypatch.setattr(main, "chat", fake_chat)
    monkeypatch.setattr(main, "chat_stream", fake_chat_stream)
    monkeypatch.setattr(main.pipeline, "ask", fake_ask)
    return state


def test_ask_returns_the_pipeline_body_and_logs_one_demo_row(wired):
    response = client.post("/api/ask", json={"question": "  Are you open on Saturdays?  "})
    assert response.status_code == 200
    assert response.json()["question"] == "Are you open on Saturdays?"
    [row] = wired["logged"]
    assert (row["surface"], row["outcome"], row["status"]) == ("demo", "answered", 200)
    assert (row["input_tokens"], row["output_tokens"]) == (700, 50)
    assert row["client_ip_hash"] and row["judgments"]["in_scope"] == 0.95


@pytest.mark.parametrize("payload", [{}, {"question": ""}, {"question": "   "}, {"question": 5}])
def test_ask_rejects_a_missing_or_empty_question(wired, payload):
    response = client.post("/api/ask", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_question"
    assert wired["logged"] == []


def test_ask_rejects_a_question_that_is_too_long(wired):
    assert client.post("/api/ask", json={"question": "x" * 501}).status_code == 422


def test_ask_rate_limit_uses_the_demo_code_and_retry_after(wired):
    wired["decision"] = AccessDecision(
        allowed=False, reason="rate_limit_minute", retry_after_seconds=60
    )
    response = client.post("/api/ask", json={"question": "hi"})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert response.headers["retry-after"] == "60"
    assert wired["logged"][0]["outcome"] == "denied"


def test_ask_reports_kill_switch_as_503(wired):
    wired["decision"] = AccessDecision(allowed=False, reason="kill_switch")
    response = client.post("/api/ask", json={"question": "hi"})
    assert (response.status_code, response.json()["error"]["code"]) == (503, "kill_switch")


def test_ask_turns_a_pipeline_failure_into_502_and_logs_it(wired, monkeypatch):
    async def broken(_question):
        raise RuntimeError("provider down")

    monkeypatch.setattr(main.pipeline, "ask", broken)
    response = client.post("/api/ask", json={"question": "hi"})
    assert (response.status_code, response.json()["error"]["code"]) == (502, "upstream_error")
    assert wired["logged"][0]["outcome"] == "error"


def test_ask_fails_closed_when_the_limit_check_breaks(wired, monkeypatch):
    async def broken(_ip):
        raise RuntimeError("database down")

    monkeypatch.setattr(main, "authorize_demo", broken)
    assert client.post("/api/ask", json={"question": "hi"}).status_code == 502


def test_chat_completion_has_openai_shape_and_logs_usage(wired):
    response = client.post("/v1/chat/completions", json=CHAT, headers=AUTH)
    body = response.json()
    assert response.status_code == 200
    message = body["choices"][0]["message"]
    assert (message["role"], message["content"]) == ("assistant", "Hello!")
    assert body["model"] == "bootcamp"
    assert body["usage"]["total_tokens"] == 13
    [row] = wired["logged"]
    assert (row["surface"], row["outcome"], row["token_id"]) == ("endpoint", "answered", "tok-1")


def test_chat_completion_reports_ignored_params_in_header_and_body(wired):
    response = client.post(
        "/v1/chat/completions", json={**CHAT, "temperature": 0.9, "seed": 7}, headers=AUTH
    )
    assert response.headers["x-ignored-params"] == "temperature, seed"
    assert response.json()["ignored_params"] == ["temperature", "seed"]
    assert wired["logged"][0]["ignored_params"] == ["temperature", "seed"]


@pytest.mark.parametrize(
    ("reason", "status", "code"),
    [
        ("invalid_token", 401, "invalid_api_key"),
        ("revoked", 401, "invalid_api_key"),
        ("rate_limit_minute", 429, "rate_limit_exceeded"),
        ("rate_limit_day", 429, "rate_limit_exceeded"),
        ("spend_cap", 429, "insufficient_quota"),
        ("kill_switch", 503, "service_unavailable"),
    ],
)
def test_chat_completion_denials_use_openai_error_json(wired, reason, status, code):
    wired["decision"] = AccessDecision(allowed=False, reason=reason)
    response = client.post("/v1/chat/completions", json=CHAT, headers=AUTH)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert (wired["logged"][0]["outcome"], wired["logged"][0]["detail"]) == ("denied", reason)


def test_chat_completion_rejects_tools_with_a_clear_400(wired):
    response = client.post(
        "/v1/chat/completions", json={**CHAT, "tools": [{"type": "function"}]}, headers=AUTH
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert wired["logged"][0]["outcome"] == "invalid_request"


def test_chat_completion_unknown_model_is_404(wired):
    response = client.post("/v1/chat/completions", json={**CHAT, "model": "gpt-9"}, headers=AUTH)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "model_not_found")


def test_chat_completion_upstream_failure_is_502(wired, monkeypatch):
    async def broken(_request):
        raise RuntimeError("provider down")

    monkeypatch.setattr(main, "chat", broken)
    response = client.post("/v1/chat/completions", json=CHAT, headers=AUTH)
    assert response.status_code == 502
    assert wired["logged"][0]["outcome"] == "error"


def test_streaming_sends_role_deltas_finish_usage_then_done(wired):
    response = client.post("/v1/chat/completions", json={**CHAT, "stream": True}, headers=AUTH)
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
    assert text == "Hello!"
    assert chunks[-1]["usage"]["total_tokens"] == 13
    assert [row["outcome"] for row in wired["logged"]] == ["answered"]


def test_streaming_failure_arrives_as_an_error_event(wired, monkeypatch):
    async def broken(_request):
        yield "Hel"
        raise RuntimeError("provider dropped the connection")

    monkeypatch.setattr(main, "chat_stream", broken)
    response = client.post("/v1/chat/completions", json={**CHAT, "stream": True}, headers=AUTH)
    events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert json.loads(events[-2])["error"]["code"] == "upstream_error"
    assert events[-1] == "[DONE]"
    assert wired["logged"][0]["outcome"] == "error"


def test_models_lists_the_alias_and_the_real_model(wired):
    ids = [model["id"] for model in client.get("/v1/models").json()["data"]]
    assert ids == ["bootcamp", "claude-haiku-5-5"]
