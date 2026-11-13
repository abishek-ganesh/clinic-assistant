"""One question, end to end: judge and retrieve in parallel, gate in code, then generate."""

import asyncio
import time
from dataclasses import dataclass

from app.generate import generate
from app.judge import judge
from app.models import Judgments, Passage
from app.retrieval import check_corpus_model, retrieve
from app.thresholds import EMERGENCY_MIN, IN_SCOPE_MIN, RELEVANCE_MIN

# Written by code, never by a model, so they are the same every time.
EMERGENCY_MESSAGE = (
    "This may be an emergency. Call 911 now or go to the nearest emergency room. "
    "Do not wait for a reply from the clinic."
)
DECLINE_MESSAGE = (
    "I can't find that in the Family Clinic handbook. I can help with hours, appointments, "
    "refills, billing, telehealth, records and vaccines. For medical questions, call the clinic."
)

_corpus_checked = False


@dataclass
class AskResult:
    """The /api/ask response body plus what the usage log needs."""

    body: dict
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def gate(judgments: Judgments, passages: list[Passage]) -> str | None:
    """Decide whether to generate. Returns the reason not to, or None to go ahead."""
    if judgments.is_emergency >= EMERGENCY_MIN:
        return "emergency"
    if judgments.in_scope < IN_SCOPE_MIN:
        return "out_of_scope"
    if not passages or passages[0].relevance < RELEVANCE_MIN:
        return "low_relevance"
    return None


async def _check_corpus_once() -> None:
    """Refuse to answer from vectors made by a different embedding model. Once per instance."""
    global _corpus_checked
    if not _corpus_checked:
        await check_corpus_model()
        _corpus_checked = True


async def ask(question: str) -> AskResult:
    """Run the whole pipeline for one question."""
    started = time.perf_counter()

    # Nothing reaches the user until the gate runs, so these can overlap safely.
    # The latency is the slowest branch, not the sum.
    judgments, passages, _ = await asyncio.gather(
        judge(question), retrieve(question), _check_corpus_once()
    )

    result = AskResult(body={})
    prompt = None
    citations: list[int] = []
    reason = gate(judgments, passages)

    if reason == "emergency":
        decision, text, passages = "emergency", EMERGENCY_MESSAGE, []
    elif reason is not None:
        decision, text = "declined", DECLINE_MESSAGE
    else:
        generation = await generate(question, passages)
        prompt = generation.prompt.model_dump()
        result.model = generation.model
        result.input_tokens = generation.input_tokens
        result.output_tokens = generation.output_tokens
        if generation.refused:
            decision, reason, text = "declined", "refusal", DECLINE_MESSAGE
        elif not generation.answerable:
            decision, reason, text = "declined", "not_in_handbook", DECLINE_MESSAGE
        else:
            decision, text, citations = "answered", generation.text, generation.citations

    result.body = {
        "question": question,
        "judgments": judgments.model_dump(),
        # Sent so the page draws the same lines the gate used
        "thresholds": {
            "emergency_min": EMERGENCY_MIN,
            "in_scope_min": IN_SCOPE_MIN,
            "relevance_min": RELEVANCE_MIN,
        },
        "decision": decision,
        "decision_reason": reason,
        "passages": [passage.model_dump() for passage in passages],
        "answer": {"text": text, "citations": citations},
        "prompt": prompt,
        "model": result.model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }
    return result
