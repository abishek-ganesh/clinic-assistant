"""OpenAI chat-completions shapes in, Anthropic arguments out, and back. Pure functions."""

import json
import time
import uuid
from dataclasses import dataclass, field

from app.config import get_settings
from app.models import ChatRequest, ChatResult
from app.thresholds import DEFAULT_MAX_TOKENS, MAX_MAX_TOKENS

# Sampling and scoring parameters the Claude API no longer accepts. Dropped, then reported.
IGNORED_PARAMS = (
    "temperature",
    "top_p",
    "top_k",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "logprobs",
    "top_logprobs",
)
UNSUPPORTED_PARAMS = ("tools", "tool_choice", "functions", "function_call")
UNSUPPORTED_PARTS = ("image_url", "input_audio", "file")
SYSTEM_ROLES = ("system", "developer")
STREAM_DONE = "data: [DONE]\n\n"


class UnsupportedRequest(Exception):
    """A request we refuse before calling any model. Carries what OpenAI's error JSON needs."""

    def __init__(
        self, message: str, param: str | None = None, code: str | None = None, status: int = 400
    ):
        super().__init__(message)
        self.message = message
        self.param = param
        self.code = code
        self.status = status


def _resolve_model(body: dict) -> tuple[str, str]:
    """Return (the name the student sent, the real model id). Unknown names are a 404."""
    settings = get_settings()
    requested = body.get("model")
    if not isinstance(requested, str) or not requested:
        raise UnsupportedRequest("you must provide a model parameter", "model", "missing_model")
    if requested not in (settings.model_alias, settings.generation_model):
        raise UnsupportedRequest(
            f"The model '{requested}' does not exist. Use '{settings.model_alias}'.",
            "model",
            "model_not_found",
            status=404,
        )
    return requested, settings.generation_model


def _reject_unsupported(body: dict) -> None:
    """Refuse features this endpoint does not implement instead of silently dropping them."""
    for name in UNSUPPORTED_PARAMS:
        if body.get(name):
            raise UnsupportedRequest(
                f"'{name}' is not supported on this endpoint yet", name, "unsupported_parameter"
            )
    n = body.get("n")
    if n is not None and n != 1:
        raise UnsupportedRequest("only n=1 is supported", "n", "unsupported_value")


def _content_text(content, param: str) -> str:
    """Flatten OpenAI message content (a string or a list of parts) to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise UnsupportedRequest("content must be a string or a list of parts", param)
    texts = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind in UNSUPPORTED_PARTS:
            raise UnsupportedRequest(
                f"'{kind}' content parts are not supported on this endpoint yet",
                param,
                "unsupported_content",
            )
        if kind != "text" or not isinstance(part.get("text"), str):
            raise UnsupportedRequest("each content part must be a text part", param)
        texts.append(part["text"])
    return "\n".join(texts)


def _translate_messages(messages) -> tuple[str | None, list[dict]]:
    """Split OpenAI messages into Anthropic's system string and alternating turns."""
    if not isinstance(messages, list) or not messages:
        raise UnsupportedRequest("messages must be a non-empty list", "messages")

    system_parts: list[str] = []
    turns: list[dict] = []
    for index, message in enumerate(messages):
        param = f"messages[{index}]"
        if not isinstance(message, dict):
            raise UnsupportedRequest("each message must be an object", param)
        role = message.get("role")
        if role in ("tool", "function") or message.get("tool_calls"):
            raise UnsupportedRequest(
                "tool messages are not supported on this endpoint yet",
                param,
                "unsupported_parameter",
            )
        if role not in (*SYSTEM_ROLES, "user", "assistant"):
            raise UnsupportedRequest(f"unknown message role: {role!r}", f"{param}.role")

        text = _content_text(message.get("content"), f"{param}.content")
        if role in SYSTEM_ROLES:
            # Anthropic takes the system prompt as one top-level string, not as a message
            if text.strip():
                system_parts.append(text)
        elif not text.strip():
            raise UnsupportedRequest("message content must not be empty", f"{param}.content")
        elif turns and turns[-1]["role"] == role:
            # Anthropic expects user and assistant turns to alternate, so merge neighbors
            turns[-1]["content"] += "\n\n" + text
        else:
            turns.append({"role": role, "content": text})

    if not turns or turns[0]["role"] != "user":
        raise UnsupportedRequest("the conversation must start with a user message", "messages")
    if turns[-1]["role"] != "user":
        # Ending on an assistant turn asks the model to continue it, which Haiku 5.5 rejects
        raise UnsupportedRequest("the last message must be from the user", "messages")
    return "\n\n".join(system_parts) or None, turns


def _max_tokens(body: dict) -> int:
    """max_completion_tokens (the newer name) or max_tokens, defaulted and capped."""
    for name in ("max_completion_tokens", "max_tokens"):
        value = body.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise UnsupportedRequest(f"{name} must be a positive integer", name)
        return min(value, MAX_MAX_TOKENS)
    return DEFAULT_MAX_TOKENS


def translate_request(body: dict) -> ChatRequest:
    """Validate an OpenAI chat-completions body and translate it to Anthropic's shape."""
    if not isinstance(body, dict):
        raise UnsupportedRequest("the request body must be a JSON object")
    requested_model, model = _resolve_model(body)
    _reject_unsupported(body)
    system, messages = _translate_messages(body.get("messages"))
    return ChatRequest(
        requested_model=requested_model,
        model=model,
        system=system,
        messages=messages,
        max_tokens=_max_tokens(body),
        stream=bool(body.get("stream", False)),
        ignored_params=[name for name in IGNORED_PARAMS if body.get(name) is not None],
    )


def _usage(result: ChatResult) -> dict:
    return {
        "prompt_tokens": result.input_tokens,
        "completion_tokens": result.output_tokens,
        "total_tokens": result.input_tokens + result.output_tokens,
    }


def _new_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def completion_body(result: ChatResult, request: ChatRequest) -> dict:
    """An OpenAI chat.completion object, plus the list of parameters we ignored."""
    return {
        "id": _new_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        # Echo the name the student asked for, so an alias stays an alias
        "model": request.requested_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text, "refusal": None},
                "logprobs": None,
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": _usage(result),
        "ignored_params": request.ignored_params,
    }


@dataclass(frozen=True)
class Stream:
    """What every chunk of one streamed completion shares: OpenAI repeats id and created."""

    request: ChatRequest
    id: str = field(default_factory=_new_id)
    created: int = field(default_factory=lambda: int(time.time()))


def _sse(stream: Stream, choices: list[dict], **extra) -> str:
    """Format one chat.completion.chunk as a server-sent event line."""
    chunk = {
        "id": stream.id,
        "object": "chat.completion.chunk",
        "created": stream.created,
        "model": stream.request.requested_model,
        "choices": choices,
        **extra,
    }
    return f"data: {json.dumps(chunk)}\n\n"


def _choice(delta: dict, finish_reason: str | None = None) -> dict:
    return {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}


def stream_chunk(stream: Stream, item: str | ChatResult | None = None) -> str:
    """SSE text for one item from chat_stream. Call it once with no item to open the stream.

    A str becomes a content delta. The final ChatResult becomes the closing events: the chunk
    with finish_reason, the usage chunk and the [DONE] terminator.
    """
    if item is None:
        return _sse(stream, [_choice({"role": "assistant", "content": ""})])
    if isinstance(item, str):
        return _sse(stream, [_choice({"content": item})])
    return (
        _sse(stream, [_choice({}, item.finish_reason)])
        + _sse(stream, [], usage=_usage(item), ignored_params=stream.request.ignored_params)
        + STREAM_DONE
    )


def error_body(message: str, type: str, code: str | None = None, param: str | None = None) -> dict:
    """OpenAI's error JSON, which the openai package turns into a typed exception."""
    return {"error": {"message": message, "type": type, "param": param, "code": code}}


def models_body() -> dict:
    """The model list for GET /v1/models: the class alias first, then the real model id."""
    settings = get_settings()
    names = dict.fromkeys([settings.model_alias, settings.generation_model])
    # created is required by OpenAI's shape; we have no meaningful timestamp to report
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": "anthropic"} for name in names
        ],
    }
