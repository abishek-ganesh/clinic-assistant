"""Shared data shapes. Every module boundary in the pipeline passes one of these."""

from typing import Literal

from pydantic import BaseModel


class Judgments(BaseModel):
    """Probabilities from the judgment model. Code compares them to app/thresholds.py."""

    is_emergency: float
    in_scope: float
    provider: Literal["jev", "ollama"]
    calibrated: bool


class Passage(BaseModel):
    """One retrieved handbook chunk. rank starts at 1 and is the [n] the answer cites."""

    rank: int
    section_slug: str
    section: str
    heading: str | None
    content: str
    similarity: float


class Prompt(BaseModel):
    """The exact text sent to the generator, shown on the demo page."""

    system: str
    user: str


class Generation(BaseModel):
    """What the generator wrote, plus what it cost. refused means the model declined to answer."""

    text: str
    citations: list[int]
    answerable: bool
    refused: bool = False
    prompt: Prompt
    model: str
    provider: Literal["anthropic", "ollama"]
    input_tokens: int
    output_tokens: int


class ChatRequest(BaseModel):
    """A class-endpoint request after translation from OpenAI's shape to Anthropic's."""

    requested_model: str
    model: str
    system: str | None
    messages: list[dict]
    max_tokens: int
    stream: bool
    ignored_params: list[str]


class ChatResult(BaseModel):
    """One class-endpoint completion in provider-neutral form."""

    text: str
    finish_reason: Literal["stop", "length", "content_filter"]
    model: str
    input_tokens: int
    output_tokens: int


class AccessDecision(BaseModel):
    """Allow or deny for one request. reason is None when allowed."""

    allowed: bool
    reason: (
        Literal[
            "kill_switch",
            "invalid_token",
            "revoked",
            "spend_cap",
            "rate_limit_minute",
            "rate_limit_day",
        ]
        | None
    ) = None
    token_id: str | None = None
    retry_after_seconds: int | None = None
