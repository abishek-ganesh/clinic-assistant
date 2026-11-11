"""Answer-quality eval: does the whole pipeline give the right answer to ten known questions?

uv run python scripts/eval_answers.py

Each question in eval/answers.jsonl runs through pipeline.ask in-process, exactly as the demo
page would run it, and is graded by plain code:

  1. the decision matches (answered, declined or emergency)
  2. an answered question cites at least one passage, and only passages it was shown
  3. every expected handbook section is among the cited passages
  4. every required fact appears in the answer (a small case-insensitive pattern per fact)

No model grades a model here. The exit code is non-zero when the pass bar is missed, which is
what turns the GitHub Actions job red.
"""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import pipeline  # noqa: E402
from app.access import estimate_cost, log_usage  # noqa: E402
from app.config import get_settings  # noqa: E402

ANSWERS_PATH = Path(__file__).resolve().parent.parent / "eval" / "answers.jsonl"

# Share of questions that must pass. The checks are deterministic and the handbook is fixed,
# so a miss is a real change in behavior, and one of the ten is the emergency question.
PASS_BAR = 1.0

ROW = "{:<58} {:<10} {:<10} {:>7}  {}"


def shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def normalize(text: str) -> str:
    """Lowercase, straighten dashes and collapse whitespace, so patterns stay small."""
    text = re.sub(r"[\u2010-\u2015]", "-", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def cited_sections(body: dict) -> set[str]:
    """Section slugs of the passages the answer cites. A citation [n] means the passage ranked n."""
    cited = set(body["answer"]["citations"])
    return {p["section_slug"] for p in body["passages"] if p["rank"] in cited}


def missing_facts(facts: list[dict], answer_text: str) -> list[str]:
    """Labels of the required facts whose pattern does not appear in the answer."""
    text = normalize(answer_text)
    return [fact["label"] for fact in facts if not re.search(fact["pattern"], text)]


def grade(item: dict, body: dict) -> list[str]:
    """Every reason this answer fails, in plain words. An empty list is a pass."""
    if body["decision"] != item["decision"]:
        reason = f" ({body['decision_reason']})" if body.get("decision_reason") else ""
        return [f"decision was {body['decision']}{reason}, expected {item['decision']}"]
    if item["decision"] != "answered":
        return []

    failures = []
    citations = body["answer"]["citations"]
    ranks = {p["rank"] for p in body["passages"]}
    if not citations:
        failures.append("no citations")
    elif not set(citations) <= ranks:
        failures.append(f"cites a passage it was not shown: {sorted(set(citations) - ranks)}")
    uncited = sorted(set(item.get("cited_sections", [])) - cited_sections(body))
    if uncited:
        failures.append(f"expected section not cited: {', '.join(uncited)}")
    missing = missing_facts(item.get("facts", []), body["answer"]["text"])
    if missing:
        failures.append(f"missing fact: {'; '.join(missing)}")
    return failures


def meets_bar(passed: int, total: int) -> bool:
    return total > 0 and passed / total >= PASS_BAR


def summary_markdown(rows: list[dict], cost_usd: float, latency_ms: int) -> str:
    """The run as a Markdown table, for the GitHub Actions job summary."""
    passed = sum(not row["failures"] for row in rows)
    verdict = "passed" if meets_bar(passed, len(rows)) else "FAILED"
    lines = [
        f"### Answer eval {verdict}: {passed}/{len(rows)}",
        "",
        f"Estimated generation cost ${cost_usd:.6f}, pipeline time {latency_ms / 1000:.1f} s.",
        "",
        "| Question | Expected | Got | Result |",
        "|---|---|---|---|",
    ]
    for row in rows:
        result = "pass" if not row["failures"] else "FAIL: " + "; ".join(row["failures"])
        question = row["question"].replace("|", "\\|")
        lines.append(f"| {question} | {row['expected']} | {row['got']} | {result} |")
    return "\n".join(lines) + "\n"


class _CountErrors(logging.Handler):
    """Counts log records instead of printing them: one summary line beats ten tracebacks."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1


async def run_one(item: dict) -> dict:
    """Ask one question, grade it, and log it to usage_log like any other request."""
    row = {"question": item["question"], "expected": item["decision"], "cost_usd": 0.0}
    try:
        result = await pipeline.ask(item["question"])
    except Exception as error:
        await log_usage(surface="eval", outcome="error", status=502)
        return row | {
            "got": "error",
            "latency_ms": 0,
            "failures": [f"pipeline raised {type(error).__name__}"],
            "answer": "",
        }

    body = result.body
    # Awaited so the row exists before the script exits: CI spend counts toward the monthly cap
    await log_usage(
        surface="eval",
        outcome=body["decision"],
        status=200,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=body["latency_ms"],
        detail=body["decision_reason"],
        judgments=body["judgments"],
    )
    return row | {
        "got": body["decision"],
        "latency_ms": body["latency_ms"],
        "cost_usd": estimate_cost(result.model, result.input_tokens, result.output_tokens),
        "failures": grade(item, body),
        "answer": body["answer"]["text"],
    }


async def main() -> int:
    # A missing key would send development runs to the local Ollama fallback, which is not
    # the system under test. Stop instead, naming the provider but never the key.
    missing = [name for name, ready in get_settings().providers().items() if not ready]
    if missing:
        print(f"Missing credentials for: {', '.join(missing)}. Nothing was run.")
        return 2

    log_errors = _CountErrors()
    access_logger = logging.getLogger("app.access")
    access_logger.addHandler(log_errors)
    access_logger.propagate = False

    lines = ANSWERS_PATH.read_text().splitlines()
    items = [json.loads(line) for line in lines if line.strip()]
    # One at a time: the eval measures the same path a single visitor takes
    rows = [await run_one(item) for item in items]

    print(ROW.format("question", "expected", "got", "ms", "result"))
    print("-" * 100)
    for row in rows:
        print(
            ROW.format(
                shorten(row["question"], 58),
                row["expected"],
                row["got"],
                row["latency_ms"],
                "pass" if not row["failures"] else "FAIL",
            )
        )
        for failure in row["failures"]:
            print(f"    {failure}")
        if row["failures"] and row["answer"]:
            print(f"    answer: {row['answer']}")

    passed = sum(not row["failures"] for row in rows)
    cost_usd = sum(row["cost_usd"] for row in rows)
    latency_ms = sum(row["latency_ms"] for row in rows)
    ok = meets_bar(passed, len(rows))
    print()
    print(f"{passed}/{len(rows)} passed (bar: {PASS_BAR:.0%})")
    print(f"estimated generation cost ${cost_usd:.6f}")
    slowest = max(row["latency_ms"] for row in rows)
    print(f"pipeline time {latency_ms / 1000:.1f} s total, slowest question {slowest} ms")
    if log_errors.count:
        print(
            f"usage_log: {log_errors.count} of {len(rows)} rows could not be written, "
            "so this run's spend is not counted toward the monthly cap"
        )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as summary:
            summary.write(summary_markdown(rows, cost_usd, latency_ms))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
