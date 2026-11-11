"""FastAPI entry point. Vercel's FastAPI preset finds the `app` object in this file.

Routes only: each one checks access, calls one function that does the work, logs usage,
and shapes the response. The work itself lives in the modules this file imports.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import pipeline
from app.access import DENIALS, authorize, authorize_demo, hash_ip, log_usage
from app.config import get_settings
from app.generate import chat, chat_stream
from app.models import AccessDecision, ChatRequest, ChatResult
from app.openai_compat import (
    STREAM_DONE,
    Stream,
    UnsupportedRequest,
    completion_body,
    error_body,
    models_body,
    stream_chunk,
    translate_request,
)
from app.thresholds import MAX_QUESTION_CHARS

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

# The demo page reports denials in its own short codes; the class endpoint uses OpenAI's.
DEMO_CODES = {"rate_limit_minute": "rate_limited", "spend_cap": "spend_cap"}
UPSTREAM_MESSAGE = "A model provider failed to respond. Try again in a moment."

app = FastAPI(title="Family Clinic Assistant")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _client_ip(request: Request) -> str:
    """The caller's address. Behind Vercel's proxy it is the first hop of x-forwarded-for."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _retry_headers(decision: AccessDecision) -> dict[str, str]:
    if decision.retry_after_seconds:
        return {"Retry-After": str(decision.retry_after_seconds)}
    return {}


def _demo_error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


@app.get("/")
def demo_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    """Reports which providers are configured, without calling them (cheap and safe to poll)."""
    settings = get_settings()
    providers = settings.providers()
    return {
        "status": "ok" if all(providers.values()) else "degraded",
        "app_env": settings.app_env,
        "providers": providers,
        "generation_model": settings.generation_model,
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
    }


@app.post("/api/ask")
async def api_ask(request: Request) -> JSONResponse:
    """The demo page's one call: a question in, every pipeline stage out."""
    started = time.perf_counter()
    try:
        question = (await request.json()).get("question")
    except (ValueError, AttributeError):
        question = None
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= MAX_QUESTION_CHARS:
        return _demo_error(
            422, "invalid_question", f"Ask a question of 1 to {MAX_QUESTION_CHARS} characters."
        )
    question = question.strip()

    # If the limit check itself fails we refuse, rather than serve requests nobody is counting
    ip = _client_ip(request)
    try:
        ip_hash = hash_ip(ip)
        decision = await authorize_demo(ip)
    except Exception:
        logger.exception("demo access check failed")
        return _demo_error(502, "upstream_error", UPSTREAM_MESSAGE)

    if not decision.allowed:
        denial = DENIALS[decision.reason]
        await log_usage(
            surface="demo",
            outcome="denied",
            status=denial.status,
            client_ip_hash=ip_hash,
            detail=decision.reason,
        )
        code = DEMO_CODES.get(decision.reason, decision.reason)
        return _demo_error(denial.status, code, denial.message, _retry_headers(decision))

    try:
        result = await pipeline.ask(question)
    except Exception:
        logger.exception("pipeline failed")
        await log_usage(
            surface="demo",
            outcome="error",
            status=502,
            client_ip_hash=ip_hash,
            latency_ms=_elapsed_ms(started),
        )
        return _demo_error(502, "upstream_error", UPSTREAM_MESSAGE)

    # Awaited, not a background task: the next request's limit check counts this row
    await log_usage(
        surface="demo",
        outcome=result.body["decision"],
        status=200,
        client_ip_hash=ip_hash,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.body["latency_ms"],
        detail=result.body["decision_reason"],
        judgments=result.body["judgments"],
    )
    return JSONResponse(result.body)


@app.get("/v1/models")
def v1_models() -> dict:
    return models_body()


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else None


async def _log_chat(
    token_id: str | None, request: ChatRequest, result: ChatResult, started: float
) -> None:
    await log_usage(
        surface="endpoint",
        outcome="answered",
        status=200,
        token_id=token_id,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=_elapsed_ms(started),
        ignored_params=request.ignored_params or None,
    )


async def _sse(
    token_id: str | None, chat_request: ChatRequest, started: float
) -> AsyncIterator[str]:
    """OpenAI-format server-sent events for one streamed completion."""
    stream = Stream(chat_request)
    try:
        yield stream_chunk(stream)
        async for item in chat_stream(chat_request):
            if isinstance(item, ChatResult):
                # Log before the final chunks so the row exists when the client sees [DONE]
                await _log_chat(token_id, chat_request, item, started)
            yield stream_chunk(stream, item)
    except Exception:
        # The 200 status line has already been sent, so the error travels as an event
        logger.exception("streamed completion failed")
        await log_usage(
            surface="endpoint",
            outcome="error",
            status=502,
            token_id=token_id,
            latency_ms=_elapsed_ms(started),
        )
        yield f"data: {json.dumps(error_body(UPSTREAM_MESSAGE, 'api_error', 'upstream_error'))}\n\n"
        yield STREAM_DONE


@app.post("/v1/chat/completions")
async def v1_chat_completions(request: Request):
    """The class endpoint: OpenAI's chat completions shape in front of Claude."""
    started = time.perf_counter()
    try:
        decision = await authorize(_bearer_token(request))
    except Exception:
        logger.exception("token check failed")
        return JSONResponse(error_body(UPSTREAM_MESSAGE, "api_error", "upstream_error"), 502)

    if not decision.allowed:
        denial = DENIALS[decision.reason]
        await log_usage(
            surface="endpoint",
            outcome="denied",
            status=denial.status,
            token_id=decision.token_id,
            detail=decision.reason,
        )
        return JSONResponse(
            error_body(denial.message, denial.error_type, denial.code),
            denial.status,
            headers=_retry_headers(decision),
        )

    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise UnsupportedRequest("The request body must be a JSON object.")
        chat_request = translate_request(body)
    except (ValueError, UnsupportedRequest) as error:
        if not isinstance(error, UnsupportedRequest):
            error = UnsupportedRequest("The request body is not valid JSON.")
        await log_usage(
            surface="endpoint",
            outcome="invalid_request",
            status=error.status,
            token_id=decision.token_id,
            detail=error.message[:200],
        )
        return JSONResponse(
            error_body(error.message, "invalid_request_error", error.code, error.param),
            error.status,
        )

    headers = {}
    if chat_request.ignored_params:
        headers["X-Ignored-Params"] = ", ".join(chat_request.ignored_params)

    if chat_request.stream:
        return StreamingResponse(
            _sse(decision.token_id, chat_request, started),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache"},
        )

    try:
        result = await chat(chat_request)
    except Exception:
        logger.exception("completion failed")
        await log_usage(
            surface="endpoint",
            outcome="error",
            status=502,
            token_id=decision.token_id,
            latency_ms=_elapsed_ms(started),
        )
        return JSONResponse(error_body(UPSTREAM_MESSAGE, "api_error", "upstream_error"), 502)

    await _log_chat(decision.token_id, chat_request, result, started)
    return JSONResponse(completion_body(result, chat_request), headers=headers)
