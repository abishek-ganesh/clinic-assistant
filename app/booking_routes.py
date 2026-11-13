"""Routes for the booking page: GET /book serves it, POST /api/book runs one agent turn.

Same shape as /api/ask in main.py: validate, check access, call one function, log usage.
The server keeps no conversation: the page sends the earlier turns back each time, and
nothing in them can cause a booking. Only a Confirm press carrying a confirmation this server
signed can, and that goes to code, not to the model.
"""

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from app import agent
from app.access import DENIALS, authorize_demo, client_ip, hash_ip, log_usage
from app.schedule import BookingError, parse_slot_id
from app.thresholds import (
    MAX_BOOKING_HISTORY,
    MAX_BOOKING_MESSAGE_CHARS,
    MAX_BOOKING_TURN_CHARS,
)

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

# The same short codes and wording the demo page uses (see main.py).
DEMO_CODES = {"rate_limit_minute": "rate_limited", "spend_cap": "spend_cap"}
UPSTREAM_MESSAGE = "A model provider failed to respond. Try again in a moment."

router = APIRouter()


class InvalidRequest(Exception):
    """The request body is not something this route accepts. The message is shown to the user."""


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


def _history(body: dict) -> list[dict]:
    """The earlier turns the page sent back: plain text, alternating user then assistant."""
    history = body.get("history", [])
    if not isinstance(history, list) or len(history) > MAX_BOOKING_HISTORY or len(history) % 2:
        raise InvalidRequest("This conversation is too long to continue. Start a new one.")
    turns = []
    for index, turn in enumerate(history):
        role = "assistant" if index % 2 else "user"
        if not isinstance(turn, dict) or turn.get("role") != role:
            raise InvalidRequest("The conversation history is not in the expected shape.")
        content = turn.get("content")
        if not isinstance(content, str) or not 1 <= len(content) <= MAX_BOOKING_TURN_CHARS:
            raise InvalidRequest("The conversation history is not in the expected shape.")
        # Rebuilt from the two fields we checked, so nothing else the page sent travels on
        turns.append({"role": role, "content": content})
    return turns


def _user_message(body: dict) -> tuple[str, dict | None]:
    """This turn's user text and, for a Confirm click, the confirmation that came with it."""
    action = body.get("action", "message")
    if action == "decline":
        return agent.DECLINE_MESSAGE, None

    if action == "confirm":
        confirmation = body.get("confirmation")
        slot_id = confirmation.get("slot_id") if isinstance(confirmation, dict) else None
        try:
            # Shape only. Whether the signature is good is decided in agent.confirm_turn.
            parse_slot_id(slot_id)
        except BookingError:
            raise InvalidRequest("That confirmation is not in the expected shape.") from None
        return agent.confirm_message(slot_id), confirmation

    message = body.get("message")
    if action != "message" or not isinstance(message, str):
        raise InvalidRequest("Send a message, or press Confirm or Decline on a proposal.")
    message = message.strip()
    if not 1 <= len(message) <= MAX_BOOKING_MESSAGE_CHARS:
        raise InvalidRequest(f"Messages need 1 to {MAX_BOOKING_MESSAGE_CHARS} characters.")
    return message, None


@router.get("/book")
def book_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "book.html")


@router.post("/api/book")
async def api_book(request: Request) -> JSONResponse:
    """One turn of the booking conversation: text or a button press in, the agent's turn out."""
    started = time.perf_counter()
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise InvalidRequest("The request body must be a JSON object.")
        history = _history(body)
        message, confirmation = _user_message(body)
    except InvalidRequest as error:
        return _error(422, "invalid_request", str(error))
    except ValueError:
        return _error(422, "invalid_request", "The request body is not valid JSON.")

    # If the limit check itself fails we refuse, rather than serve requests nobody is counting
    ip = client_ip(request)
    try:
        ip_hash = hash_ip(ip)
        decision = await authorize_demo(ip)
    except Exception:
        logger.exception("booking access check failed")
        return _error(502, "upstream_error", UPSTREAM_MESSAGE)

    if not decision.allowed:
        denial = DENIALS[decision.reason]
        await log_usage(
            surface="booking",
            outcome="denied",
            status=denial.status,
            client_ip_hash=ip_hash,
            detail=decision.reason,
        )
        code = DEMO_CODES.get(decision.reason, decision.reason)
        headers = {}
        if decision.retry_after_seconds:
            headers["Retry-After"] = str(decision.retry_after_seconds)
        return _error(denial.status, code, denial.message, headers)

    messages = [*history, {"role": "user", "content": message}]
    try:
        if confirmation is None:
            turn = await agent.run_turn(messages)
        else:
            turn = await agent.confirm_turn(messages, confirmation, ip_hash)
    except Exception:
        logger.exception("booking agent failed")
        await log_usage(
            surface="booking",
            outcome="error",
            status=502,
            client_ip_hash=ip_hash,
            latency_ms=_elapsed_ms(started),
        )
        return _error(502, "upstream_error", UPSTREAM_MESSAGE)

    latency_ms = _elapsed_ms(started)
    # Awaited, not a background task: the next request's limit check counts this row.
    # Tokens are the sum over every model call in the turn.
    await log_usage(
        surface="booking",
        outcome=turn.outcome,
        status=200,
        client_ip_hash=ip_hash,
        model=turn.model,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        latency_ms=latency_ms,
        detail=turn.booking["confirmation_code"] if turn.booking else turn.booking_error,
    )
    return JSONResponse(
        {
            "outcome": turn.outcome,
            "reply": turn.reply,
            "steps": turn.steps,
            "proposal": turn.proposal,
            "booking": turn.booking,
            # What the page sends back as "history" on the next turn
            "history": [
                *messages,
                {"role": "assistant", "content": turn.reply[:MAX_BOOKING_TURN_CHARS]},
            ],
            "prompt": {"system": turn.system, "tools": agent.TOOLS},
            "model": turn.model,
            "model_calls": turn.model_calls,
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "latency_ms": latency_ms,
        }
    )
