"""Structure-aware chunking: one chunk per handbook heading, split further only when too long."""

import math
import re
from dataclasses import dataclass

from app.thresholds import CHUNK_MAX_TOKENS

HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
# A sentence ends at . ! or ? followed by a space; a line break also ends one (list items)
SENTENCE_BREAK = re.compile(r"((?<=[.!?])[ \t]+|\n+)")


@dataclass(frozen=True)
class Chunk:
    """One passage of the handbook, ready to embed and store."""

    section_slug: str
    section_title: str
    heading: str | None
    content: str
    token_count: int


def estimate_tokens(text: str) -> int:
    """Approximate tokens without a tokenizer: English prose runs about 4 characters a token."""
    return math.ceil(len(text) / 4)


def chunk_markdown(text: str, slug: str) -> list[Chunk]:
    """Split one handbook file into chunks. The H1 is the section title; H2 and H3 start chunks."""
    text = HTML_COMMENT.sub("", text)
    section_title = slug
    heading: str | None = None
    body: list[str] = []
    sections: list[tuple[str | None, str]] = []

    for line in text.splitlines():
        match = HEADING.match(line)
        if not match:
            body.append(line)
        elif len(match.group(1)) == 1:
            section_title = match.group(2)
        else:
            sections.append((heading, "\n".join(body)))
            heading, body = match.group(2), []
    sections.append((heading, "\n".join(body)))

    return [
        Chunk(slug, section_title, heading, content, estimate_tokens(content))
        for heading, body_text in sections
        for content in _split_to_fit(body_text.strip(), CHUNK_MAX_TOKENS)
    ]


def _split_to_fit(body: str, max_tokens: int) -> list[str]:
    """Return the body whole if it fits, else pack sentences into pieces that each fit.

    Each piece after the first starts with the last sentence of the piece before it, so a fact
    that straddles a boundary is still readable in one chunk.
    """
    if not body:
        return []
    if estimate_tokens(body) <= max_tokens:
        return [body]

    # re.split with a capture group keeps the separators, so lists and line breaks survive
    parts = SENTENCE_BREAK.split(body)
    sentences = [
        parts[i] + (parts[i + 1] if i + 1 < len(parts) else "") for i in range(0, len(parts), 2)
    ]
    sentences = [
        piece for sentence in sentences for piece in _split_long_sentence(sentence, max_tokens)
    ]

    pieces: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        if current and estimate_tokens("".join(current) + sentence) > max_tokens:
            pieces.append("".join(current).strip())
            overlap = current[-1]
            fits = estimate_tokens(overlap + sentence) <= max_tokens
            current = [overlap] if fits else []
        current.append(sentence)
    pieces.append("".join(current).strip())
    return pieces


def _split_long_sentence(sentence: str, max_tokens: int) -> list[str]:
    """A single sentence longer than the cap is cut on word boundaries. Rare in a handbook."""
    if estimate_tokens(sentence) <= max_tokens:
        return [sentence]
    pieces: list[str] = []
    current = ""
    for word in re.findall(r"\S+\s*", sentence):
        if current and estimate_tokens(current + word) > max_tokens:
            pieces.append(current)
            current = ""
        current += word
    pieces.append(current)
    return pieces
