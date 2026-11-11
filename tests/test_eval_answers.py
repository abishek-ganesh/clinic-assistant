"""The answer eval's grading functions: pure code, no models involved."""

import json
import re

import pytest

from scripts.eval_answers import (
    ANSWERS_PATH,
    cited_sections,
    grade,
    meets_bar,
    missing_facts,
    normalize,
    summary_markdown,
)

ITEMS = [json.loads(line) for line in ANSWERS_PATH.read_text().splitlines() if line.strip()]
REFILLS = {
    "question": "How long do refills take?",
    "decision": "answered",
    "cited_sections": ["refills"],
    "facts": [{"label": "up to 3 business days", "pattern": "(3|three) business days"}],
}


def body(text="Refills take up to 3 business days [1].", citations=(1,), decision="answered"):
    """A pipeline response with two retrieved passages: refills ranked 1, hours ranked 2."""
    return {
        "decision": decision,
        "decision_reason": None if decision == "answered" else "out_of_scope",
        "passages": [
            {"rank": 1, "section_slug": "refills"},
            {"rank": 2, "section_slug": "hours-locations"},
        ],
        "answer": {"text": text, "citations": list(citations)},
    }


def pattern(question_start: str, label: str) -> str:
    """The pattern for one fact in eval/answers.jsonl."""
    [item] = [item for item in ITEMS if item["question"].startswith(question_start)]
    [fact] = [fact for fact in item["facts"] if fact["label"] == label]
    return fact["pattern"]


def test_a_correct_cited_answer_passes():
    assert grade(REFILLS, body()) == []


def test_a_wrong_decision_is_the_only_failure_reported():
    failures = grade(REFILLS, body(decision="declined"))
    assert failures == ["decision was declined (out_of_scope), expected answered"]


def test_declined_and_emergency_questions_are_graded_on_the_decision_alone():
    assert (
        grade({"decision": "declined"}, body(text="I can't find that.", decision="declined")) == []
    )
    assert grade({"decision": "emergency"}, body(decision="answered")) == [
        "decision was answered, expected emergency"
    ]


def test_an_answer_with_no_citations_fails():
    failures = grade(REFILLS, body(citations=()))
    assert "no citations" in failures
    assert "expected section not cited: refills" in failures


def test_citing_only_another_section_fails():
    assert grade(REFILLS, body(citations=(2,))) == ["expected section not cited: refills"]


def test_citing_a_passage_that_was_never_shown_fails():
    assert grade(REFILLS, body(citations=(1, 7))) == ["cites a passage it was not shown: [7]"]


def test_a_two_section_question_needs_both_sections_cited():
    item = REFILLS | {"cited_sections": ["refills", "hours-locations"]}
    assert grade(item, body(citations=(1,))) == ["expected section not cited: hours-locations"]
    assert grade(item, body(citations=(1, 2))) == []


def test_a_missing_fact_is_named_by_its_label():
    failures = grade(REFILLS, body(text="Refills are quick [1]."))
    assert failures == ["missing fact: up to 3 business days"]


def test_cited_sections_maps_citation_numbers_to_passage_ranks():
    assert cited_sections(body(citations=(2,))) == {"hours-locations"}


def test_normalize_lowercases_straightens_dashes_and_collapses_whitespace():
    assert normalize("9:00\u00a0AM \u2013 1:00 PM\n") == "9:00 am - 1:00 pm"


@pytest.mark.parametrize(
    "wording",
    [
        "Saturdays from 9:00 am to 1:00 pm",
        "9 AM to 1 PM",
        "9 a.m. to 1 p.m.",
        "9:00 AM\u20131:00 PM",
        "9am - 1pm",
        "from 9 until 1 pm",
    ],
)
def test_harmless_rewording_of_a_time_range_still_matches(wording):
    saturday = pattern("Are you open on Saturdays", "9 am to 1 pm")
    assert missing_facts([{"label": "hours", "pattern": saturday}], wording) == []


@pytest.mark.parametrize("wording", ["9:00 am to 5:00 pm", "8:00 am to 1:00 pm", "closed"])
def test_a_different_time_range_does_not_match(wording):
    saturday = pattern("Are you open on Saturdays", "9 am to 1 pm")
    assert missing_facts([{"label": "hours", "pattern": saturday}], wording) == ["hours"]


def test_money_patterns_do_not_match_a_longer_amount():
    fee = pattern("How much is the no-show fee", "$40 no-show fee")
    assert missing_facts([{"label": "fee", "pattern": fee}], "The fee is $40.") == []
    assert missing_facts([{"label": "fee", "pattern": fee}], "The fee is $400.") == ["fee"]


def test_the_question_file_is_well_formed():
    assert len(ITEMS) == 10
    decisions = [item["decision"] for item in ITEMS]
    assert decisions.count("declined") == 2 and decisions.count("emergency") == 1
    for item in ITEMS:
        if item["decision"] == "answered":
            assert item["cited_sections"] and item["facts"]
            for fact in item["facts"]:
                assert fact["label"] and re.compile(fact["pattern"])
        else:
            assert "facts" not in item and "cited_sections" not in item


def test_every_expected_section_is_a_handbook_file():
    handbook = ANSWERS_PATH.parent.parent / "handbook"
    for item in ITEMS:
        for slug in item.get("cited_sections", []):
            assert (handbook / f"{slug}.md").exists(), slug


def test_every_fact_pattern_matches_the_handbook_section_it_came_from():
    handbook = ANSWERS_PATH.parent.parent / "handbook"
    for item in ITEMS:
        text = " ".join(
            (handbook / f"{slug}.md").read_text() for slug in item.get("cited_sections", [])
        )
        assert missing_facts(item.get("facts", []), text) == [], item["question"]


def test_the_bar_is_every_question():
    assert meets_bar(10, 10)
    assert not meets_bar(9, 10)
    assert not meets_bar(0, 0)


def test_summary_markdown_shows_the_score_and_each_failure():
    rows = [
        {"question": "How long do refills take?", "expected": "answered", "got": "answered",
         "failures": []},
        {"question": "Open | closed?", "expected": "answered", "got": "declined",
         "failures": ["decision was declined, expected answered"]},
    ]  # fmt: skip
    text = summary_markdown(rows, cost_usd=0.0011, latency_ms=2500)
    assert text.startswith("### Answer eval FAILED: 1/2")
    assert "$0.001100" in text and "2.5 s" in text
    assert "| How long do refills take? | answered | answered | pass |" in text
    assert "Open \\| closed?" in text and "FAIL: decision was declined" in text
