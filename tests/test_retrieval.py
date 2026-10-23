import json

import httpx
import pytest
import respx

from app import retrieval
from app.retrieval import VOYAGE_URL, embed


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(retrieval.asyncio, "sleep", instant)


def voyage_response(vectors_by_index: dict[int, list[float]]) -> httpx.Response:
    data = [{"index": i, "embedding": v} for i, v in vectors_by_index.items()]
    return httpx.Response(200, json={"object": "list", "data": data, "usage": {"total_tokens": 7}})


@respx.mock
async def test_embed_sends_model_input_type_and_dimension(use_settings):
    use_settings(voyage_api_key="pa-test")
    route = respx.post(VOYAGE_URL).mock(return_value=voyage_response({0: [0.1, 0.2]}))

    await embed(["When are you open?"], "query")

    sent = json.loads(route.calls[0].request.content)
    assert sent == {
        "input": ["When are you open?"],
        "model": "voyage-4-lite",
        "input_type": "query",
        "output_dimension": 512,
    }
    assert route.calls[0].request.headers["authorization"] == "Bearer pa-test"


@respx.mock
async def test_embed_returns_vectors_in_input_order(use_settings):
    use_settings(voyage_api_key="pa-test")
    respx.post(VOYAGE_URL).mock(return_value=voyage_response({1: [2.0], 0: [1.0]}))

    assert await embed(["first", "second"], "document") == [[1.0], [2.0]]


@respx.mock
async def test_embed_retries_rate_limit_then_succeeds(use_settings):
    use_settings(voyage_api_key="pa-test")
    route = respx.post(VOYAGE_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "1"}),
            voyage_response({0: [0.5]}),
        ]
    )

    assert await embed(["hi"], "query") == [[0.5]]
    assert route.call_count == 2


@respx.mock
async def test_embed_does_not_retry_a_bad_request(use_settings):
    use_settings(voyage_api_key="pa-test")
    route = respx.post(VOYAGE_URL).mock(return_value=httpx.Response(400, json={"detail": "bad"}))

    with pytest.raises(httpx.HTTPStatusError):
        await embed(["hi"], "query")
    assert route.call_count == 1


@respx.mock
async def test_embed_gives_up_after_three_server_errors(use_settings):
    use_settings(voyage_api_key="pa-test")
    route = respx.post(VOYAGE_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(httpx.HTTPStatusError):
        await embed(["hi"], "query")
    assert route.call_count == 3


async def test_embed_requires_a_key(use_settings):
    use_settings()
    with pytest.raises(RuntimeError, match="VOYAGE_API_KEY"):
        await embed(["hi"], "query")


async def test_embed_empty_input_makes_no_request(use_settings):
    use_settings()
    assert await embed([], "query") == []
