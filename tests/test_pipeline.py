import pytest

from app import pipeline
from app.models import Generation, Judgments, Passage, Prompt
from app.pipeline import DECLINE_MESSAGE, EMERGENCY_MESSAGE, ask, gate


def judgments(is_emergency=0.02, in_scope=0.9) -> Judgments:
    return Judgments(is_emergency=is_emergency, in_scope=in_scope, provider="jev", calibrated=True)


def passage(similarity=0.6, rank=1) -> Passage:
    return Passage(
        rank=rank,
        section_slug="refills",
        section="Prescription refills",
        heading="Turnaround time",
        content="Refills take up to three business days.",
        similarity=similarity,
    )


def generation(**overrides) -> Generation:
    fields = {
        "text": "Refills take up to three business days [1].",
        "citations": [1],
        "answerable": True,
        "prompt": Prompt(system="s", user="u"),
        "model": "claude-haiku-5-5",
        "provider": "anthropic",
        "input_tokens": 700,
        "output_tokens": 50,
    }
    return Generation(**{**fields, **overrides})


@pytest.mark.parametrize(
    ("is_emergency", "in_scope", "similarity", "expected"),
    [
        (0.02, 0.90, 0.60, None),
        (0.35, 0.90, 0.60, "emergency"),  # at the threshold counts
        (0.34, 0.90, 0.60, None),
        (0.90, 0.05, 0.10, "emergency"),  # emergency wins over every other reason
        (0.02, 0.24, 0.60, "out_of_scope"),
        (0.02, 0.25, 0.60, None),
        (0.02, 0.90, 0.44, "low_similarity"),
        (0.02, 0.90, 0.45, None),
    ],
)
def test_gate_compares_each_number_to_its_threshold(is_emergency, in_scope, similarity, expected):
    assert gate(judgments(is_emergency, in_scope), [passage(similarity)]) == expected


def test_gate_declines_when_nothing_was_retrieved():
    assert gate(judgments(), []) == "low_similarity"


@pytest.fixture
def stub(monkeypatch):
    """Replace the three model calls with canned results and record whether generate ran."""
    calls = {"generate": 0}

    def _stub(judged=None, passages=None, generated=None):
        async def fake_judge(_question):
            return judged or judgments()

        async def fake_retrieve(_question):
            return [passage()] if passages is None else passages

        async def fake_generate(_question, _passages):
            calls["generate"] += 1
            return generated or generation()

        async def no_check():
            return None

        monkeypatch.setattr(pipeline, "judge", fake_judge)
        monkeypatch.setattr(pipeline, "retrieve", fake_retrieve)
        monkeypatch.setattr(pipeline, "generate", fake_generate)
        monkeypatch.setattr(pipeline, "_check_corpus_once", no_check)
        return calls

    return _stub


async def test_answered_carries_every_stage(stub):
    stub()
    result = await ask("How long do refills take?")
    body = result.body
    assert body["decision"] == "answered"
    assert body["decision_reason"] is None
    assert body["answer"] == {
        "text": "Refills take up to three business days [1].",
        "citations": [1],
    }
    assert body["passages"][0]["section"] == "Prescription refills"
    assert body["prompt"] == {"system": "s", "user": "u"}
    assert body["model"] == "claude-haiku-5-5"
    assert (result.input_tokens, result.output_tokens) == (700, 50)


async def test_emergency_is_written_by_code_and_never_generates(stub):
    calls = stub(judged=judgments(is_emergency=0.98, in_scope=0.1))
    result = await ask("My father has crushing chest pain")
    body = result.body
    assert body["decision"] == "emergency"
    assert body["answer"] == {"text": EMERGENCY_MESSAGE, "citations": []}
    assert body["passages"] == []
    assert body["prompt"] is None and body["model"] is None
    assert calls["generate"] == 0
    assert result.input_tokens == 0


async def test_out_of_scope_declines_without_generating_but_shows_passages(stub):
    calls = stub(judged=judgments(in_scope=0.1))
    body = (await ask("What dose of ibuprofen?")).body
    assert (body["decision"], body["decision_reason"]) == ("declined", "out_of_scope")
    assert body["answer"]["text"] == DECLINE_MESSAGE
    assert len(body["passages"]) == 1
    assert calls["generate"] == 0


async def test_generator_saying_unanswerable_becomes_a_decline(stub):
    stub(generated=generation(answerable=False, text="The handbook does not say.", citations=[]))
    body = (await ask("Do you validate parking?")).body
    assert (body["decision"], body["decision_reason"]) == ("declined", "not_in_handbook")
    assert body["answer"] == {"text": DECLINE_MESSAGE, "citations": []}
    # The prompt is still shown: a model was asked, and it said no
    assert body["prompt"] is not None


async def test_generator_refusal_becomes_a_decline(stub):
    stub(generated=generation(refused=True, answerable=False, text="", citations=[]))
    body = (await ask("something odd")).body
    assert (body["decision"], body["decision_reason"]) == ("declined", "refusal")
