"""Retrieval eval: does the right handbook section come back for each question?

uv run python scripts/eval_retrieval.py

Answerable questions pass when the top passage is from an expected section and its relevance
clears RELEVANCE_MIN (under it, the pipeline would decline). Two-section questions pass when every
expected section appears in the top TOP_K. Out-of-scope and emergency questions have no right
passage, so only their top scores are shown, next to RELEVANCE_MIN.

rel is the reranker's relevance for the top passage, the number the floor checks. sim is that
passage's vector similarity, shown for comparison. Questions marked "terse": true are the short
ones people actually type ("parking", "refill time") and get their own line in the summary.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Passage  # noqa: E402
from app.retrieval import check_corpus_model, retrieve  # noqa: E402
from app.thresholds import RELEVANCE_MIN, TOP_K  # noqa: E402

QUESTIONS_PATH = Path(__file__).resolve().parent.parent / "eval" / "questions.jsonl"
ANSWERABLE = ("single_section", "two_section")
ROW = "{:<52} {:<28} {:<44} {:>5} {:>5}  {}"


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def passed(item: dict, passages: list[Passage]) -> bool:
    """Right section (single: top passage; two: all in the top K) and over the relevance floor."""
    if not passages or passages[0].relevance < RELEVANCE_MIN:
        return False
    if item["type"] == "two_section":
        return set(item["expected_sections"]) <= {p.section_slug for p in passages}
    return passages[0].section_slug in item["expected_sections"]


def scores(passages: list[Passage]) -> tuple[str, str]:
    """The top passage's relevance and similarity, formatted for the table."""
    if not passages:
        return "", ""
    return f"{passages[0].relevance:.3f}", f"{passages[0].similarity:.3f}"


def describe(passage: Passage) -> str:
    return f"{passage.section_slug} / {passage.heading or '(intro)'}"


async def main() -> int:
    await check_corpus_model()
    lines = QUESTIONS_PATH.read_text().splitlines()
    questions = [json.loads(line) for line in lines if line.strip()]
    results = [(item, await retrieve(item["question"], TOP_K)) for item in questions]

    print(ROW.format("question", "expected", "top result", "rel", "sim", "result"))
    print("-" * 140)
    failures = 0
    answerable = [(item, passages) for item, passages in results if item["type"] in ANSWERABLE]
    for item, passages in answerable:
        ok = passed(item, passages)
        failures += not ok
        print(
            ROW.format(
                shorten(item["question"], 52),
                shorten(" + ".join(item["expected_sections"]), 28),
                shorten(describe(passages[0]), 44) if passages else "(nothing retrieved)",
                *scores(passages),
                "pass" if ok else "FAIL",
            )
        )
        if not ok:
            print(f"    top {TOP_K}: {', '.join(describe(p) for p in passages)}")

    print()
    print("No right passage exists for these. Shown: the best passage retrieval found anyway.")
    print(ROW.format("question", "type", "top result", "rel", "sim", ""))
    print("-" * 140)
    others = [(item, passages) for item, passages in results if item["type"] not in ANSWERABLE]
    for item, passages in others:
        print(
            ROW.format(
                shorten(item["question"], 52),
                item["type"],
                shorten(describe(passages[0]), 44) if passages else "(nothing retrieved)",
                *scores(passages),
                "",
            ).rstrip()
        )

    print()
    print(f"{len(answerable) - failures}/{len(answerable)} answerable questions passed")
    groups = {
        "full sentence": [(i, p) for i, p in answerable if not i.get("terse")],
        "terse": [(i, p) for i, p in answerable if i.get("terse")],
    }
    for kind in ("out_of_scope", "emergency"):
        groups[kind] = [(item, passages) for item, passages in others if item["type"] == kind]
    for name, group in groups.items():
        top = [passages[0] for _, passages in group if passages]
        if top:
            rel = [p.relevance for p in top]
            sim = [p.similarity for p in top]
            print(
                f"top passage, {name:<13} relevance {min(rel):.3f} to {max(rel):.3f}"
                f"   similarity {min(sim):.3f} to {max(sim):.3f}"
            )
    print(f"RELEVANCE_MIN is {RELEVANCE_MIN}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
