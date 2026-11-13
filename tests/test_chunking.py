from pathlib import Path

import pytest

from app.chunking import Chunk, chunk_markdown, estimate_tokens, titled_text
from app.thresholds import CHUNK_MAX_TOKENS

HANDBOOK = Path(__file__).parent.parent / "handbook"

SAMPLE = """<!-- Fictional clinic.
Synthetic content. -->

# Prescription refills

How refills work.

## Turnaround

Refills take up to 3 business days.

- Portal
- Pharmacy

### Holidays

Holidays do not count.

## Empty section

## 90-day supplies

Ask your provider.
"""


def long_section(sentence_count: int) -> str:
    sentences = " ".join(
        f"Sentence number {i} has a handful of plain words." for i in range(sentence_count)
    )
    return f"# Title\n\n## Long\n\n{sentences}\n"


def test_one_chunk_per_heading_with_title_and_heading():
    chunks = chunk_markdown(SAMPLE, "refills")

    assert [c.heading for c in chunks] == [None, "Turnaround", "Holidays", "90-day supplies"]
    assert {c.section_title for c in chunks} == {"Prescription refills"}
    assert {c.section_slug for c in chunks} == {"refills"}
    assert chunks[0].content == "How refills work."
    assert chunks[1].content == "Refills take up to 3 business days.\n\n- Portal\n- Pharmacy"


def test_html_comment_and_headings_are_not_in_content():
    for chunk in chunk_markdown(SAMPLE, "refills"):
        assert "Fictional" not in chunk.content
        assert "<!--" not in chunk.content
        assert "#" not in chunk.content


def test_token_count_is_characters_over_four_rounded_up():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    chunk = chunk_markdown(SAMPLE, "refills")[0]
    assert chunk.token_count == estimate_tokens(chunk.content)


def test_long_section_is_split_under_the_cap():
    chunks = chunk_markdown(long_section(80), "long")

    assert len(chunks) > 1
    assert all(c.token_count <= CHUNK_MAX_TOKENS for c in chunks)
    assert all(c.heading == "Long" for c in chunks)


def test_split_chunks_overlap_by_one_sentence():
    chunks = chunk_markdown(long_section(80), "long")

    for previous, following in zip(chunks, chunks[1:], strict=False):
        last_sentence = previous.content.split(". ")[-1]
        assert following.content.startswith(last_sentence)
    # Apart from the overlaps, no sentence is lost or repeated
    seen = " ".join(c.content for c in chunks)
    assert all(f"Sentence number {i} has" in seen for i in range(80))
    assert seen.count("Sentence number") == 80 + len(chunks) - 1


def test_one_sentence_longer_than_the_cap_is_cut_on_words():
    text = "# Title\n\n## Run-on\n\n" + "word " * (CHUNK_MAX_TOKENS * 2)
    chunks = chunk_markdown(text, "run-on")

    assert len(chunks) > 1
    assert all(c.token_count <= CHUNK_MAX_TOKENS for c in chunks)


def test_file_without_headings_is_one_chunk_titled_by_slug():
    assert chunk_markdown("Just a paragraph.", "notes") == [
        Chunk("notes", "notes", None, "Just a paragraph.", 5)
    ]


def test_empty_text_gives_no_chunks():
    assert chunk_markdown("", "empty") == []


@pytest.mark.parametrize("path", sorted(HANDBOOK.glob("*.md")), ids=lambda p: p.stem)
def test_every_handbook_file_chunks_cleanly(path):
    chunks = chunk_markdown(path.read_text(), path.stem)

    assert len(chunks) >= 4
    assert chunks[0].section_title != path.stem, "file needs an H1"
    for chunk in chunks:
        assert chunk.content
        assert chunk.token_count <= CHUNK_MAX_TOKENS
        assert "fictional" not in chunk.content.lower()


def test_titled_text_puts_section_and_heading_on_top():
    assert (
        titled_text("Refills", "Turnaround", "Up to 3 days.")
        == "Refills: Turnaround\n\nUp to 3 days."
    )
    assert titled_text("Refills", None, "How refills work.") == "Refills\n\nHow refills work."
