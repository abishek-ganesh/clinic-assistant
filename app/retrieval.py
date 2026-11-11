"""Embeddings (Voyage AI over plain HTTP) and vector search (pgvector via a Postgres function)."""

import asyncio
from typing import Literal

import httpx

from app.config import get_settings
from app.db import get_db
from app.models import Passage
from app.thresholds import TOP_K

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
MAX_ATTEMPTS = 3

InputType = Literal["query", "document"]


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Honor retry-after when the server sends it, else back off 0.5s, 1.5s, 4.5s."""
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.replace(".", "", 1).isdigit():
            return float(retry_after)
    return 0.5 * 3**attempt


async def embed(
    texts: list[str],
    input_type: InputType,
    client: httpx.AsyncClient | None = None,
) -> list[list[float]]:
    """Embed texts with Voyage. Questions use input_type "query", handbook chunks "document"."""
    if not texts:
        return []
    settings = get_settings()
    if not settings.voyage_api_key:
        raise RuntimeError("VOYAGE_API_KEY is not set")

    payload = {
        "input": texts,
        "model": settings.embedding_model,
        "input_type": input_type,
        "output_dimension": settings.embedding_dimensions,
    }
    headers = {"Authorization": f"Bearer {settings.voyage_api_key}"}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        for attempt in range(MAX_ATTEMPTS):
            response = None
            try:
                response = await client.post(VOYAGE_URL, json=payload, headers=headers)
            except httpx.TransportError:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
            else:
                # 429 and 5xx are worth retrying; any other error is our bug, so fail fast
                if response.status_code != 429 and response.status_code < 500:
                    response.raise_for_status()
                    data = sorted(response.json()["data"], key=lambda item: item["index"])
                    return [item["embedding"] for item in data]
                if attempt == MAX_ATTEMPTS - 1:
                    response.raise_for_status()
            await asyncio.sleep(_retry_delay(response, attempt))
        raise RuntimeError("unreachable")
    finally:
        if owns_client:
            await client.aclose()


async def match_chunks(query_embedding: list[float], match_count: int = 4) -> list[dict]:
    """Nearest handbook chunks by cosine similarity, computed in Postgres by match_chunks()."""
    db = await get_db()
    result = await db.rpc(
        "match_chunks", {"query_embedding": query_embedding, "match_count": match_count}
    ).execute()
    return result.data


async def retrieve(question: str, k: int = TOP_K) -> list[Passage]:
    """The k handbook passages closest in meaning to the question, best first."""
    [query_embedding] = await embed([question], "query")
    rows = await match_chunks(query_embedding, k)
    return [
        Passage(
            rank=rank,
            section_slug=row["section_slug"],
            section=row["section_title"],
            heading=row["heading"],
            content=row["content"],
            similarity=row["similarity"],
        )
        for rank, row in enumerate(rows, start=1)
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
