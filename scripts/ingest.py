"""Chunk, embed and store the handbook. Safe to run again: it replaces the whole corpus.

uv run python scripts/ingest.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chunking import Chunk, chunk_markdown, titled_text  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_db  # noqa: E402
from app.retrieval import check_corpus_model, embed  # noqa: E402

HANDBOOK_DIR = Path(__file__).resolve().parent.parent / "handbook"
# Rows the integration test writes and cleans up itself
SMOKE_SLUG = "phase0-smoke"
EMBED_BATCH_SIZE = 32


def embedding_text(chunk: Chunk) -> str:
    """What gets embedded: the chunk with its titles on top, the same text the reranker reads."""
    return titled_text(chunk.section_title, chunk.heading, chunk.content)


async def ingest() -> None:
    settings = get_settings()
    chunks: list[Chunk] = []
    for path in sorted(HANDBOOK_DIR.glob("*.md")):
        file_chunks = chunk_markdown(path.read_text(), path.stem)
        print(f"{path.name:<28}{len(file_chunks):>3} chunks")
        chunks.extend(file_chunks)
    if not chunks:
        raise SystemExit(f"no handbook files found in {HANDBOOK_DIR}")

    vectors: list[list[float]] = []
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start : start + EMBED_BATCH_SIZE]
        vectors.extend(await embed([embedding_text(c) for c in batch], "document"))

    rows = [
        {
            "section_slug": chunk.section_slug,
            "section_title": chunk.section_title,
            "heading": chunk.heading,
            "content": chunk.content,
            "token_count": chunk.token_count,
            "embedding": vector,
            "embedding_model": settings.embedding_model,
        }
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]

    # Insert the new corpus before deleting the old one, so search never sees an empty table
    db = await get_db()
    old = await db.table("handbook_chunks").select("id").neq("section_slug", SMOKE_SLUG).execute()
    await db.table("handbook_chunks").insert(rows).execute()
    old_ids = [row["id"] for row in old.data]
    if old_ids:
        await db.table("handbook_chunks").delete().in_("id", old_ids).execute()

    await check_corpus_model()
    print(f"{'total':<28}{len(rows):>3} chunks, embedded with {settings.embedding_model}")
    print(f"replaced {len(old_ids)} old rows")


if __name__ == "__main__":
    asyncio.run(ingest())
