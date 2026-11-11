import json

import httpx
import pytest
import respx

from app import judge as judge_module
from app.judge import QUESTIONS, judge

JEV_URL = "https://api.typesafe.ai/v1/systemone"
OLLAMA_URL = "http://localhost:11434/api/chat"


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Replace asyncio.sleep and record how long each retry would have waited."""
    waited = []

    async def instant(seconds):
        waited.append(seconds)

    monkeypatch.setattr(judge_module.asyncio, "sleep", instant)
    return waited


def jev_response(is_emergency: float = 0.03, in_scope: float = 0.91) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "jev-1",
            "answers": {"is_emergency": {"noul": is_emergency}, "in_scope": {"noul": in_scope}},
            "usage": {"input_tokens": 120},
        },
    )


def ollama_response(content: dict) -> httpx.Response:
    return httpx.Response(
        200, json={"message": {"role": "assistant", "content": json.dumps(content)}}
    )


@respx.mock
async def test_judge_sends_both_questions_in_one_request(use_settings):
    use_settings(typesafe_api_key="ts-test")
    route = respx.post(JEV_URL).mock(return_value=jev_response())

    await judge("Are you open on Saturdays?")

    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer ts-test"
    sent = json.loads(request.content)
    assert sent == {
        "model": "jev-latest",
        "state": {"question": "Are you open on Saturdays?"},
        "questions": QUESTIONS,
    }
    assert set(sent["questions"]) == {"is_emergency", "in_scope"}
    for spec in sent["questions"].values():
        assert spec["type"] == "noul"
        assert spec["instructions"]
        assert set(spec["criteria"]) == {"true", "false"}


@respx.mock
async def test_judge_parses_both_probabilities(use_settings):
    use_settings(typesafe_api_key="ts-test")
    respx.post(JEV_URL).mock(return_value=jev_response(is_emergency=0.97, in_scope=0.12))

    result = await judge("My chest hurts and I can't breathe")

    assert result.is_emergency == 0.97
    assert result.in_scope == 0.12
    assert result.provider == "jev"
    assert result.calibrated is True


@respx.mock
async def test_judge_respects_a_custom_base_url(use_settings):
    use_settings(typesafe_api_key="ts-test", jev_base_url="https://jev.example.com/")
    route = respx.post("https://jev.example.com/v1/systemone").mock(return_value=jev_response())

    await judge("hi")

    assert route.call_count == 1


@respx.mock
async def test_rate_limit_honors_retry_after(use_settings, sleeps):
    use_settings(typesafe_api_key="ts-test")
    route = respx.post(JEV_URL).mock(
        side_effect=[httpx.Response(429, headers={"retry-after": "2"}), jev_response()]
    )

    result = await judge("hi")

    assert result.in_scope == 0.91
    assert route.call_count == 2
    assert sleeps == [2.0]


@respx.mock
async def test_rate_limit_gives_up_after_four_retries(use_settings, sleeps):
    use_settings(typesafe_api_key="ts-test")
    route = respx.post(JEV_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(httpx.HTTPStatusError):
        await judge("hi")

    assert route.call_count == 5
    assert sleeps == [0.5, 1.0, 2.0, 4.0]


@respx.mock
async def test_server_error_retries_then_succeeds(use_settings, sleeps):
    use_settings(typesafe_api_key="ts-test")
    route = respx.post(JEV_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(500), jev_response()]
    )

    result = await judge("hi")

    assert result.provider == "jev"
    assert route.call_count == 3
    assert sleeps == [0.5, 1.0]


@respx.mock
async def test_server_error_gives_up_after_three_retries(use_settings):
    use_settings(typesafe_api_key="ts-test")
    route = respx.post(JEV_URL).mock(return_value=httpx.Response(502))

    with pytest.raises(httpx.HTTPStatusError):
        await judge("hi")

    assert route.call_count == 4


@respx.mock
async def test_network_error_retries_then_succeeds(use_settings):
    use_settings(typesafe_api_key="ts-test")
    route = respx.post(JEV_URL).mock(side_effect=[httpx.ConnectError("down"), jev_response()])

    result = await judge("hi")

    assert result.provider == "jev"
    assert route.call_count == 2


@respx.mock
async def test_network_error_gives_up_after_three_retries(use_settings):
    use_settings(typesafe_api_key="ts-test")
    route = respx.post(JEV_URL).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(httpx.ConnectError):
        await judge("hi")

    assert route.call_count == 4


@respx.mock
async def test_network_and_server_errors_share_one_retry_budget(use_settings):
    use_settings(typesafe_api_key="ts-test")
    route = respx.post(JEV_URL).mock(
        side_effect=[
            httpx.ConnectError("down"),
            httpx.Response(503),
            httpx.ConnectError("down"),
            httpx.Response(503),
        ]
    )

    with pytest.raises(httpx.HTTPStatusError):
        await judge("hi")

    assert route.call_count == 4


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_other_client_errors_raise_without_retrying(use_settings, sleeps, status):
    use_settings(typesafe_api_key="ts-test")
    route = respx.post(JEV_URL).mock(return_value=httpx.Response(status, json={"error": "no"}))

    with pytest.raises(httpx.HTTPStatusError):
        await judge("hi")

    assert route.call_count == 1
    assert sleeps == []


@respx.mock
async def test_missing_key_in_production_raises_and_calls_nothing(use_settings):
    use_settings(app_env="production")
    jev = respx.post(JEV_URL).mock(return_value=jev_response())
    ollama = respx.post(OLLAMA_URL).mock(return_value=ollama_response({}))

    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        await judge("hi")

    assert jev.call_count == 0
    assert ollama.call_count == 0


@respx.mock
async def test_missing_key_in_development_falls_back_to_ollama(use_settings):
    use_settings()
    jev = respx.post(JEV_URL).mock(return_value=jev_response())
    ollama = respx.post(OLLAMA_URL).mock(
        return_value=ollama_response({"is_emergency": 0.1, "in_scope": 0.8})
    )

    result = await judge("Are you open on Saturdays?")

    assert jev.call_count == 0
    assert result.is_emergency == 0.1
    assert result.in_scope == 0.8
    assert result.provider == "ollama"
    assert result.calibrated is False

    sent = json.loads(ollama.calls[0].request.content)
    assert sent["model"] == "llama3.2"
    assert sent["stream"] is False
    assert sent["format"]["required"] == ["is_emergency", "in_scope"]
    assert set(sent["format"]["properties"]) == {"is_emergency", "in_scope"}
    assert sent["messages"][-1] == {"role": "user", "content": "Are you open on Saturdays?"}


@respx.mock
async def test_ollama_guesses_are_clamped_to_probabilities(use_settings):
    use_settings()
    respx.post(OLLAMA_URL).mock(
        return_value=ollama_response({"is_emergency": 1.4, "in_scope": -0.2})
    )

    result = await judge("hi")

    assert result.is_emergency == 1.0
    assert result.in_scope == 0.0
