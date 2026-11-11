"""Generation: the only module that talks to Anthropic (or to Ollama when there is no key)."""

from collections.abc import AsyncIterator

import httpx
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from app.config import get_settings
from app.models import ChatRequest, ChatResult, Generation, Passage, Prompt

# Room for a short cited answer plus the model's thinking, which counts toward max_tokens.
ANSWER_MAX_TOKENS = 1024

# Haiku 5.5 defaults to medium effort. Low thinks less: faster and cheaper for short answers.
EFFORT = "low"

SYSTEM_PROMPT = """\
You are the patient assistant for {clinic_name}. You answer questions using the clinic's \
patient handbook.

Rules:
- Answer only from the numbered handbook passages in the user's message. Do not use outside \
knowledge.
- Cite the passages you used by number, like [1] or [2], right after the sentence they support.
- If the passages do not answer the question, say that the handbook does not cover it and set \
answerable to false.
- Never give medical advice, a diagnosis or medication dosing, even if asked directly.
- Keep the answer short and in plain language: two or three sentences is usually enough.

Reply with three fields: answer (the text the patient reads), citations (the passage numbers \
you cited) and answerable (whether the passages answered the question)."""

# Anthropic stop reasons mapped to OpenAI finish reasons. Anything unlisted counts as "stop".
FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "refusal": "content_filter",
}


class Answer(BaseModel):
    """The structured output the generator must return."""

    answer: str
    citations: list[int]
    answerable: bool


_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    """One Anthropic client per warm serverless instance, created on first use."""
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


def set_client(client) -> None:
    """Swap the client. Tests pass a fake; None makes the next call build a real one."""
    global _client
    _client = client


def _use_ollama() -> bool:
    """True when there is no Anthropic key and we are allowed to fall back (never in production)."""
    settings = get_settings()
    if settings.anthropic_api_key:
        return False
    if settings.is_production:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return True


def build_prompt(question: str, passages: list[Passage]) -> Prompt:
    """The exact system and user text for one question. The demo page shows it verbatim."""
    blocks = []
    for passage in passages:
        title = passage.section
        if passage.heading:
            title = f"{passage.section}: {passage.heading}"
        blocks.append(f"[{passage.rank}] {title}\n{passage.content.strip()}")
    user = "Handbook passages:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {question.strip()}"
    system = SYSTEM_PROMPT.format(clinic_name=get_settings().clinic_name)
    return Prompt(system=system, user=user)


def _valid_citations(citations: list[int], passages: list[Passage]) -> list[int]:
    """Keep only citations that point at a passage we actually sent, without repeats."""
    ranks = {passage.rank for passage in passages}
    return [n for n in dict.fromkeys(citations) if n in ranks]


async def generate(question: str, passages: list[Passage]) -> Generation:
    """Write a short cited answer from the passages, as validated structured output."""
    settings = get_settings()
    prompt = build_prompt(question, passages)
    if _use_ollama():
        return await _ollama_generate(prompt, passages)

    # No temperature, top_p or top_k: Haiku 5.5 rejects them. Behavior is steered by the prompt.
    message = await get_client().messages.parse(
        model=settings.generation_model,
        max_tokens=ANSWER_MAX_TOKENS,
        system=prompt.system,
        messages=[{"role": "user", "content": prompt.user}],
        output_config={"effort": EFFORT},
        output_format=Answer,
    )
    common = {
        "prompt": prompt,
        "model": message.model,
        "provider": "anthropic",
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }

    # A refusal is a normal 200 response, so check for it before reading any content
    if message.stop_reason == "refusal":
        return Generation(text="", citations=[], answerable=False, refused=True, **common)

    # parsed_output finds the text block by type: thinking blocks can come before it
    answer = message.parsed_output
    if answer is None:
        raise RuntimeError(f"generation returned no answer (stop_reason={message.stop_reason})")
    return Generation(
        text=answer.answer,
        citations=_valid_citations(answer.citations, passages),
        answerable=answer.answerable,
        **common,
    )


def _chat_arguments(request: ChatRequest) -> dict:
    """Arguments shared by chat and chat_stream. system is left out when there is none."""
    arguments = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": request.messages,
        "output_config": {"effort": EFFORT},
    }
    if request.system:
        arguments["system"] = request.system
    return arguments


def _chat_result(message) -> ChatResult:
    """Collapse an Anthropic message into text, a finish reason and token counts."""
    # Read blocks by type, never content[0]: thinking blocks can come first
    text = "".join(block.text for block in message.content if block.type == "text")
    return ChatResult(
        text=text,
        finish_reason=FINISH_REASONS.get(message.stop_reason, "stop"),
        model=message.model,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
    )


async def chat(request: ChatRequest) -> ChatResult:
    """One class-endpoint completion, returned whole."""
    if _use_ollama():
        return await _ollama_chat(request)
    message = await get_client().messages.create(**_chat_arguments(request))
    return _chat_result(message)


async def chat_stream(request: ChatRequest) -> AsyncIterator[str | ChatResult]:
    """Yield text deltas as they arrive, then exactly one ChatResult with the usage."""
    if _use_ollama():
        # The development fallback does not stream: the whole answer arrives as one delta
        result = await _ollama_chat(request)
        if result.text:
            yield result.text
        yield result
        return

    async with get_client().messages.stream(**_chat_arguments(request)) as stream:
        async for text in stream.text_stream:
            yield text
        message = await stream.get_final_message()
    yield _chat_result(message)


async def _ollama_post(payload: dict) -> dict:
    """POST to the local Ollama chat endpoint and return its JSON body."""
    settings = get_settings()
    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(url, json={**payload, "stream": False})
        response.raise_for_status()
        return response.json()


async def _ollama_generate(prompt: Prompt, passages: list[Passage]) -> Generation:
    """Development stand-in for generate(): same prompt, same schema, local model."""
    model = get_settings().ollama_model
    body = await _ollama_post(
        {
            "model": model,
            "format": Answer.model_json_schema(),
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
        }
    )
    answer = Answer.model_validate_json(body["message"]["content"])
    return Generation(
        text=answer.answer,
        citations=_valid_citations(answer.citations, passages),
        answerable=answer.answerable,
        prompt=prompt,
        model=model,
        provider="ollama",
        input_tokens=body.get("prompt_eval_count", 0),
        output_tokens=body.get("eval_count", 0),
    )


async def _ollama_chat(request: ChatRequest) -> ChatResult:
    """Development stand-in for chat(): the same conversation sent to a local model."""
    model = get_settings().ollama_model
    messages = list(request.messages)
    if request.system:
        messages.insert(0, {"role": "system", "content": request.system})
    body = await _ollama_post(
        {"model": model, "messages": messages, "options": {"num_predict": request.max_tokens}}
    )
    return ChatResult(
        text=body["message"]["content"],
        finish_reason="length" if body.get("done_reason") == "length" else "stop",
        model=model,
        input_tokens=body.get("prompt_eval_count", 0),
        output_tokens=body.get("eval_count", 0),
    )
