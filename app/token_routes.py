"""Routes for the token page: GET /token serves it, POST /api/token issues one class token.

A student proves they are in the class with the class code, not with their email: no email
is sent, so the address is only a label that lets a later request replace an earlier token.
"""

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from app.access import DENIALS, authorize_demo, client_ip, hash_ip, log_usage, request_token
from app.thresholds import MAX_CLASS_CODE_CHARS, MAX_EMAIL_CHARS

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

# Loose on purpose: something@something.something. Only a sent email can prove an address.
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

DEMO_CODES = {"rate_limit_minute": "rate_limited", "spend_cap": "spend_cap"}
REFUSALS = {
    "wrong_code": "That class code is not right. Check the class Slack for the current one.",
    "signups_closed": "Token requests are closed right now. Ask your instructor.",
}
UPSTREAM_MESSAGE = "The token could not be created. Try again in a moment."

router = APIRouter()


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


def _clean(value, max_chars: int) -> str | None:
    """The stripped string, or None when it is missing, empty or too long."""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= max_chars:
        return None
    return value.strip()


@router.get("/token")
def token_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "token.html")


@router.post("/api/token")
async def api_token(request: Request) -> JSONResponse:
    """Issue one class token to a student who knows the class code."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    body = body if isinstance(body, dict) else {}
    email = _clean(body.get("email"), MAX_EMAIL_CHARS)
    class_code = _clean(body.get("class_code"), MAX_CLASS_CODE_CHARS)
    if not email or not EMAIL_PATTERN.fullmatch(email):
        return _error(422, "invalid_email", "Enter the email address you use for the course.")
    if not class_code:
        return _error(422, "invalid_class_code", "Enter the class code from the class Slack.")

    # The per-visitor limit is what stops someone guessing class codes: fail closed without it
    try:
        ip = client_ip(request)
        ip_hash = hash_ip(ip)
        decision = await authorize_demo(ip)
    except Exception:
        logger.exception("token page access check failed")
        return _error(502, "upstream_error", UPSTREAM_MESSAGE)

    if not decision.allowed:
        denial = DENIALS[decision.reason]
        await log_usage(
            surface="token",
            outcome="denied",
            status=denial.status,
            client_ip_hash=ip_hash,
            detail=decision.reason,
        )
        headers = {}
        if decision.retry_after_seconds:
            headers["Retry-After"] = str(decision.retry_after_seconds)
        code = DEMO_CODES.get(decision.reason, decision.reason)
        return _error(denial.status, code, denial.message, headers)

    try:
        token, result = await request_token(email, class_code)
    except Exception:
        logger.exception("token request failed")
        await log_usage(surface="token", outcome="error", status=502, client_ip_hash=ip_hash)
        return _error(502, "upstream_error", UPSTREAM_MESSAGE)

    if token is None:
        # Logged with a counted outcome, so wrong guesses use up the visitor's allowance
        await log_usage(
            surface="token",
            outcome="refused",
            status=403,
            client_ip_hash=ip_hash,
            detail=result["reason"],
        )
        return _error(403, result["reason"], REFUSALS[result["reason"]])

    await log_usage(surface="token", outcome="issued", status=200, client_ip_hash=ip_hash)
    # no-store: the token is shown once and must not sit in any cache
    return JSONResponse(
        {"token": token, "email": email.lower(), "replaced": result["replaced"]},
        headers={"Cache-Control": "no-store"},
    )
