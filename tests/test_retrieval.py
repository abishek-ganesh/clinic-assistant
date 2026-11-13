import json

import httpx
import pytest
import respx

from app import retrieval
from app.retrieval import VOYAGE_RERANK_URL, VOYAGE_URL, embed, rerank


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


SUPABASE_URL = "https://example.supabase.co"
MATCH_URL = f"{SUPABASE_URL}/rest/v1/rpc/match_chunks"
CHUNKS_URL = f"{SUPABASE_URL}/rest/v1/handbook_chunks"


@pytest.fixture
def fake_supabase(use_settings, monkeypatch):
    """A real Supabase client pointed at a fake URL, so respx sees the exact PostgREST calls."""
    monkeypatch.setattr("app.db._client", None)
    return use_settings(
        supabase_url=SUPABASE_URL, supabase_secret_key="sb_secret_test", voyage_api_key="pa-test"
    )


def chunk_row(slug: str, title: str, heading: str | None, similarity: float) -> dict:
    return {
        "id": 1,
        "section_slug": slug,
        "section_title": title,
        "heading": heading,
        "content": f"Text about {slug}.",
        "similarity": similarity,
    }


def rerank_response(scores_by_index: dict[int, float]) -> httpx.Response:
    """Voyage's rerank shape: most relevant first, each pointing back at its input index."""
    ranked = sorted(scores_by_index.items(), key=lambda item: item[1], reverse=True)
    data = [{"index": i, "relevance_score": score} for i, score in ranked]
    return httpx.Response(
        200, json={"object": "list", "data": data, "model": "rerank-2.5-lite", "usage": {}}
    )


@respx.mock
async def test_rerank_sends_query_documents_model_and_top_k(use_settings):
    use_settings(voyage_api_key="pa-test")
    route = respx.post(VOYAGE_RERANK_URL).mock(return_value=rerank_response({1: 0.9, 0: 0.2}))

    ranked = await rerank("refill time", ["Hours", "Refills: Turnaround time"], top_k=2)

    assert ranked == [(1, 0.9), (0, 0.2)]
    assert json.loads(route.calls[0].request.content) == {
        "query": "refill time",
        "documents": ["Hours", "Refills: Turnaround time"],
        "model": "rerank-2.5-lite",
        "top_k": 2,
    }
    assert route.calls[0].request.headers["authorization"] == "Bearer pa-test"


@respx.mock
async def test_rerank_retries_a_server_error_like_embed(use_settings):
    use_settings(voyage_api_key="pa-test")
    route = respx.post(VOYAGE_RERANK_URL).mock(
        side_effect=[httpx.Response(503), rerank_response({0: 0.7})]
    )

    assert await rerank("hi", ["doc"], top_k=1) == [(0, 0.7)]
    assert route.call_count == 2


async def test_rerank_requires_a_key(use_settings):
    use_settings()
    with pytest.raises(RuntimeError, match="VOYAGE_API_KEY"):
        await rerank("hi", ["doc"], top_k=1)


async def test_rerank_with_no_documents_makes_no_request(use_settings):
    use_settings()
    assert await rerank("hi", [], top_k=4) == []


@respx.mock
async def test_retrieve_reranks_the_vector_candidates_and_ranks_from_one(fake_supabase):
    voyage = respx.post(VOYAGE_URL).mock(return_value=voyage_response({0: [0.1, 0.2]}))
    rows = [
        chunk_row("hours-locations", "Hours and locations", "Phone line hours", 0.45),
        chunk_row("refills", "Prescription refills", "Turnaround time", 0.31),
        chunk_row("telehealth", "Telehealth visits", None, 0.29),
    ]
    match = respx.post(MATCH_URL).mock(return_value=httpx.Response(200, json=rows))
    reranker = respx.post(VOYAGE_RERANK_URL).mock(
        return_value=rerank_response({0: 0.40, 1: 0.84, 2: 0.55})
    )

    passages = await retrieval.retrieve("refill time", k=2)

    assert json.loads(voyage.calls[0].request.content)["input_type"] == "query"
    # Vector search casts the wider net; the reranker narrows it to k
    assert json.loads(match.calls[0].request.content) == {
        "query_embedding": [0.1, 0.2],
        "match_count": retrieval.RERANK_CANDIDATES,
    }
    sent = json.loads(reranker.calls[0].request.content)
    # The reranker reads the same titled text the ingest embedded
    assert sent["documents"] == [
        "Hours and locations: Phone line hours\n\nText about hours-locations.",
        "Prescription refills: Turnaround time\n\nText about refills.",
        "Telehealth visits\n\nText about telehealth.",
    ]
    assert sent["top_k"] == 2
    # Reranker order wins, and each passage keeps its own vector score next to its relevance
    assert [p.model_dump() for p in passages] == [
        {
            "rank": 1,
            "section_slug": "refills",
            "section": "Prescription refills",
            "heading": "Turnaround time",
            "content": "Text about refills.",
            "similarity": 0.31,
            "relevance": 0.84,
        },
        {
            "rank": 2,
            "section_slug": "telehealth",
            "section": "Telehealth visits",
            "heading": None,
            "content": "Text about telehealth.",
            "similarity": 0.29,
            "relevance": 0.55,
        },
    ]


@respx.mock
async def test_retrieve_keeps_top_k_by_default(fake_supabase):
    respx.post(VOYAGE_URL).mock(return_value=voyage_response({0: [0.1]}))
    rows = [chunk_row(f"s{i}", f"Section {i}", None, 0.5) for i in range(6)]
    respx.post(MATCH_URL).mock(return_value=httpx.Response(200, json=rows))
    reranker = respx.post(VOYAGE_RERANK_URL).mock(
        return_value=rerank_response({i: 0.9 - i / 10 for i in range(retrieval.TOP_K)})
    )

    passages = await retrieval.retrieve("anything")

    assert json.loads(reranker.calls[0].request.content)["top_k"] == retrieval.TOP_K
    assert [p.rank for p in passages] == list(range(1, retrieval.TOP_K + 1))


@respx.mock
async def test_retrieve_with_an_empty_corpus_skips_the_reranker(fake_supabase):
    respx.post(VOYAGE_URL).mock(return_value=voyage_response({0: [0.1]}))
    respx.post(MATCH_URL).mock(return_value=httpx.Response(200, json=[]))
    reranker = respx.post(VOYAGE_RERANK_URL)

    assert await retrieval.retrieve("anything") == []
    assert not reranker.called


@respx.mock
async def test_check_corpus_model_passes_when_no_other_model_is_stored(fake_supabase):
    route = respx.get(CHUNKS_URL).mock(return_value=httpx.Response(200, json=[]))

    await retrieval.check_corpus_model()

    params = route.calls[0].request.url.params
    assert params["embedding_model"] == "neq.voyage-4-lite"
    assert params["limit"] == "1"


@respx.mock
async def test_check_corpus_model_raises_on_a_different_model(fake_supabase):
    respx.get(CHUNKS_URL).mock(
        return_value=httpx.Response(200, json=[{"embedding_model": "voyage-3-lite"}])
    )

    with pytest.raises(RuntimeError, match="voyage-3-lite.*voyage-4-lite"):
        await retrieval.check_corpus_model()
