"""Routes for the token page.

GET  /token             the page
POST /api/token         step 1: check the class code, email a one-time link
POST /api/token/redeem  step 2: trade the link for a token, shown once

The class code shows the person is in the class. The emailed link shows they own the address.
"""

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from app.access import (
    DENIALS,
    authorize_demo,
    client_ip,
    hash_ip,
    log_usage,
    redeem_token_link,
    start_token_request,
)
from app.config import get_settings
from app.mailer import send_token_link
from app.thresholds import MAX_CLASS_CODE_CHARS, MAX_EMAIL_CHARS, TOKEN_LINK_TTL_SECONDS

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

# Loose on purpose: something@something.something. The emailed link is the real check.
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Longer than any link secret we issue; anything bigger is not one of ours.
MAX_LINK_CHARS = 100

DEMO_CODES = {"rate_limit_minute": "rate_limited", "spend_cap": "spend_cap"}
# reason from the database -> (HTTP status, message)
REFUSALS = {
    "wrong_code": (403, "That class code is not right. Check the class Slack for the current one."),
    "signups_closed": (403, "Token requests are closed right now. Ask your instructor."),
    "too_many_links": (
        429,
        "Several links were already sent to this address. Try again in an hour.",
    ),
    "link_invalid": (410, "This link is not valid. Request a new one with the form above."),
    "link_used": (410, "This link was already used. Request a new one with the form above."),
    "link_expired": (410, "This link has expired. Request a new one with the form above."),
}
UPSTREAM_MESSAGE = "Something on our side failed. Try again in a moment."

router = APIRouter()


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


def _clean(value, max_chars: int) -> str | None:
    """The stripped string, or None when it is missing, empty or too long."""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= max_chars:
        return None
    return value.strip()


async def _body(request: Request) -> dict:
    """The JSON object in the request, or an empty dict when there is none."""
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _check_visitor(request: Request) -> tuple[str | None, JSONResponse | None]:
    """The shared per-visitor limit. Returns (ip hash, None) to go ahead, or (None, error)."""
    # This limit is what stops someone guessing class codes or links: fail closed without it
    try:
        ip = client_ip(request)
        ip_hash = hash_ip(ip)
        decision = await authorize_demo(ip)
    except Exception:
        logger.exception("token page access check failed")
        return None, _error(502, "upstream_error", UPSTREAM_MESSAGE)
    if decision.allowed:
        return ip_hash, None

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
    return None, _error(denial.status, code, denial.message, headers)


async def _refuse(reason: str, ip_hash: str) -> JSONResponse:
    """Log and report a refusal. The outcome counts toward the visitor's per-minute allowance."""
    status, message = REFUSALS[reason]
    await log_usage(
        surface="token", outcome="refused", status=status, client_ip_hash=ip_hash, detail=reason
    )
    return _error(status, reason, message)


@router.get("/token")
def token_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "token.html")


@router.post("/api/token")
async def api_token(request: Request) -> JSONResponse:
    """Step 1: email a one-time link to a student who knows the class code."""
    body = await _body(request)
    email = _clean(body.get("email"), MAX_EMAIL_CHARS)
    class_code = _clean(body.get("class_code"), MAX_CLASS_CODE_CHARS)
    if not email or not EMAIL_PATTERN.fullmatch(email):
        return _error(422, "invalid_email", "Enter the email address you use for the course.")
    if not class_code:
        return _error(422, "invalid_class_code", "Enter the class code from the class Slack.")

    ip_hash, denied = await _check_visitor(request)
    if denied:
        return denied

    try:
        link_secret, result = await start_token_request(email, class_code, ip_hash)
        if link_secret is None:
            return await _refuse(result["reason"], ip_hash)
        # The secret rides in the fragment (after #), which browsers never send to a server,
        # so it stays out of request logs
        await send_token_link(email, f"{get_settings().base_url}/token#link={link_secret}")
    except Exception:
        logger.exception("token link request failed")
        await log_usage(surface="token", outcome="error", status=502, client_ip_hash=ip_hash)
        return _error(502, "upstream_error", UPSTREAM_MESSAGE)

    await log_usage(surface="token", outcome="link_sent", status=200, client_ip_hash=ip_hash)
    return JSONResponse(
        {"sent": True, "email": email.lower(), "expires_minutes": TOKEN_LINK_TTL_SECONDS // 60}
    )


@router.post("/api/token/redeem")
async def api_token_redeem(request: Request) -> JSONResponse:
    """Step 2: show the token, once, to whoever opened the emailed link."""
    link_secret = _clean((await _body(request)).get("link"), MAX_LINK_CHARS)
    if not link_secret:
        return _error(422, "invalid_link", "The link is missing. Open it from your email again.")

    ip_hash, denied = await _check_visitor(request)
    if denied:
        return denied

    try:
        token, result = await redeem_token_link(link_secret)
    except Exception:
        logger.exception("token link redeem failed")
        await log_usage(surface="token", outcome="error", status=502, client_ip_hash=ip_hash)
        return _error(502, "upstream_error", UPSTREAM_MESSAGE)
    if token is None:
        return await _refuse(result["reason"], ip_hash)

    await log_usage(surface="token", outcome="issued", status=200, client_ip_hash=ip_hash)
    # no-store: the token is shown once and must not sit in any cache
    return JSONResponse(
        {"token": token, "email": result["email"], "replaced": result["replaced"]},
        headers={"Cache-Control": "no-store"},
    )
