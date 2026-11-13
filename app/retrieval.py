"""Embeddings and reranking (Voyage AI over plain HTTP) and vector search (pgvector)."""

import asyncio
from typing import Literal

import httpx

from app.chunking import titled_text
from app.config import get_settings
from app.db import get_db
from app.models import Passage
from app.thresholds import RERANK_CANDIDATES, TOP_K

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank"
MAX_ATTEMPTS = 3

InputType = Literal["query", "document"]


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Honor retry-after when the server sends it, else back off 0.5s, 1.5s, 4.5s."""
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.replace(".", "", 1).isdigit():
            return float(retry_after)
    return 0.5 * 3**attempt


async def _post_voyage(url: str, payload: dict, client: httpx.AsyncClient | None) -> dict:
    """POST to a Voyage endpoint with retries on 429, 5xx and dropped connections."""
    settings = get_settings()
    if not settings.voyage_api_key:
        raise RuntimeError("VOYAGE_API_KEY is not set")
    headers = {"Authorization": f"Bearer {settings.voyage_api_key}"}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        for attempt in range(MAX_ATTEMPTS):
            response = None
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.TransportError:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
            else:
                # 429 and 5xx are worth retrying; any other error is our bug, so fail fast
                if response.status_code != 429 and response.status_code < 500:
                    response.raise_for_status()
                    return response.json()
                if attempt == MAX_ATTEMPTS - 1:
                    response.raise_for_status()
            await asyncio.sleep(_retry_delay(response, attempt))
        raise RuntimeError("unreachable")
    finally:
        if owns_client:
            await client.aclose()


async def embed(
    texts: list[str],
    input_type: InputType,
    client: httpx.AsyncClient | None = None,
) -> list[list[float]]:
    """Embed texts with Voyage. Questions use input_type "query", handbook chunks "document"."""
    if not texts:
        return []
    settings = get_settings()
    payload = {
        "input": texts,
        "model": settings.embedding_model,
        "input_type": input_type,
        "output_dimension": settings.embedding_dimensions,
    }
    body = await _post_voyage(VOYAGE_URL, payload, client)
    data = sorted(body["data"], key=lambda item: item["index"])
    return [item["embedding"] for item in data]


async def rerank(
    query: str,
    documents: list[str],
    top_k: int,
    client: httpx.AsyncClient | None = None,
) -> list[tuple[int, float]]:
    """Score each document against the query with Voyage's reranker, which reads both together.

    Returns (index into documents, relevance from 0 to 1) for the top_k, most relevant first.
    """
    if not documents:
        return []
    settings = get_settings()
    payload = {
        "query": query,
        "documents": documents,
        "model": settings.rerank_model,
        "top_k": top_k,
    }
    body = await _post_voyage(VOYAGE_RERANK_URL, payload, client)
    data = sorted(body["data"], key=lambda item: item["relevance_score"], reverse=True)
    return [(item["index"], item["relevance_score"]) for item in data[:top_k]]


async def match_chunks(query_embedding: list[float], match_count: int = 4) -> list[dict]:
    """Nearest handbook chunks by cosine similarity, computed in Postgres by match_chunks()."""
    db = await get_db()
    result = await db.rpc(
        "match_chunks", {"query_embedding": query_embedding, "match_count": match_count}
    ).execute()
    return result.data


async def retrieve(question: str, k: int = TOP_K) -> list[Passage]:
    """The k most relevant handbook passages, best first.

    Two steps: vector search casts a wide net (RERANK_CANDIDATES passages, fast and cheap), then
    the reranker reads the question next to each candidate and picks the best k from those.
    """
    [query_embedding] = await embed([question], "query")
    rows = await match_chunks(query_embedding, max(k, RERANK_CANDIDATES))
    documents = [titled_text(row["section_title"], row["heading"], row["content"]) for row in rows]
    ranked = await rerank(question, documents, k)
    return [
        Passage(
            rank=rank,
            section_slug=rows[index]["section_slug"],
            section=rows[index]["section_title"],
            heading=rows[index]["heading"],
            content=rows[index]["content"],
            similarity=rows[index]["similarity"],
            relevance=relevance,
        )
        for rank, (index, relevance) in enumerate(ranked, start=1)
    ]


async def check_corpus_model() -> None:
    """Raise if any stored chunk was embedded by a model other than the configured one."""
    configured = get_settings().embedding_model
    db = await get_db()
    result = await (
        db.table("handbook_chunks")
        .select("embedding_model")
        .neq("embedding_model", configured)
        .limit(1)
        .execute()
    )
    if result.data:
        stored = result.data[0]["embedding_model"]
        raise RuntimeError(
            f"handbook_chunks holds vectors from {stored!r} but EMBEDDING_MODEL is {configured!r}. "
            "Vectors from different models are not comparable: run scripts/ingest.py to re-embed."
        )
