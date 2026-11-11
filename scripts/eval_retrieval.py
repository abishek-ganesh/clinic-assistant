"""Retrieval eval: does the right handbook section come back for each question?

uv run python scripts/eval_retrieval.py

Answerable questions pass when the top passage is from an expected section. Two-section questions
pass when every expected section appears in the top TOP_K. Out-of-scope and emergency questions
have no right passage, so only their top similarity is shown, next to SIMILARITY_MIN.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Passage  # noqa: E402
from app.retrieval import check_corpus_model, retrieve  # noqa: E402
from app.thresholds import SIMILARITY_MIN, TOP_K  # noqa: E402

QUESTIONS_PATH = Path(__file__).resolve().parent.parent / "eval" / "questions.jsonl"
ANSWERABLE = ("single_section", "two_section")
ROW = "{:<52} {:<28} {:<44} {:>5}  {}"


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def passed(item: dict, passages: list[Passage]) -> bool:
    """Single section: top passage is from an expected section. Two: all are in the top K."""
    if not passages:
        return False
    if item["type"] == "two_section":
        return set(item["expected_sections"]) <= {p.section_slug for p in passages}
    return passages[0].section_slug in item["expected_sections"]


def describe(passage: Passage) -> str:
    return f"{passage.section_slug} / {passage.heading or '(intro)'}"


async def main() -> int:
    await check_corpus_model()
    lines = QUESTIONS_PATH.read_text().splitlines()
    questions = [json.loads(line) for line in lines if line.strip()]
    results = [(item, await retrieve(item["question"], TOP_K)) for item in questions]

    print(ROW.format("question", "expected", "top result", "sim", "result"))
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
                f"{passages[0].similarity:.3f}" if passages else "",
                "pass" if ok else "FAIL",
            )
        )
        if not ok:
            print(f"    top {TOP_K}: {', '.join(describe(p) for p in passages)}")

    print()
    print("No right passage exists for these. Shown: the best similarity retrieval found anyway.")
    print(ROW.format("question", "type", "top result", "sim", ""))
    print("-" * 140)
    others = [(item, passages) for item, passages in results if item["type"] not in ANSWERABLE]
    for item, passages in others:
        print(
            ROW.format(
                shorten(item["question"], 52),
                item["type"],
                shorten(describe(passages[0]), 44) if passages else "(nothing retrieved)",
                f"{passages[0].similarity:.3f}" if passages else "",
                "",
            ).rstrip()
        )

    print()
    print(f"{len(answerable) - failures}/{len(answerable)} answerable questions passed")
    groups = {"answerable": answerable}
    for kind in ("out_of_scope", "emergency"):
        groups[kind] = [(item, passages) for item, passages in others if item["type"] == kind]
    for name, group in groups.items():
        top = [passages[0].similarity for _, passages in group if passages]
        if top:
            print(f"top similarity, {name:<13} min {min(top):.3f}  max {max(top):.3f}")
    print(f"SIMILARITY_MIN is {SIMILARITY_MIN}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
