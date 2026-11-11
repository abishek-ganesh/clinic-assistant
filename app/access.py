"""Who may call the app and what each call cost: tokens, limits, the usage log, denial messages."""

import hashlib
import hmac
import logging
import re
import secrets
from typing import NamedTuple

from app.config import get_settings
from app.db import get_db
from app.models import AccessDecision

logger = logging.getLogger(__name__)

TOKEN_START = "sk-bootcamp-"
TOKEN_PATTERN = re.compile(r"sk-bootcamp-[A-Za-z0-9_-]{32}")
# What `tokens.py list` shows: the fixed start plus 6 random characters, enough to tell
# tokens apart when a student pastes the beginning of theirs, far too few to guess the rest.
PREFIX_LENGTH = len(TOKEN_START) + 6

# USD per million tokens (input, output), for prompts up to 100K tokens. Models not listed
# here (the local Ollama fallback) cost nothing.
PRICES_PER_MILLION = {
    "claude-haiku-5-5": (0.10, 0.50),
}


class Denial(NamedTuple):
    """How one denial reason is reported, in the terms OpenAI's error JSON uses."""

    status: int
    error_type: str
    code: str
    message: str


DENIALS: dict[str, Denial] = {
    "invalid_token": Denial(
        401,
        "invalid_request_error",
        "invalid_api_key",
        "That API key is not recognized. Check that api_key is your full class token "
        "(it starts with sk-bootcamp-) with no extra spaces or quotes.",
    ),
    "revoked": Denial(
        401,
        "invalid_request_error",
        "invalid_api_key",
        "This class token has been revoked. Ask your instructor for a new one.",
    ),
    "rate_limit_minute": Denial(
        429,
        "rate_limit_error",
        "rate_limit_exceeded",
        "Too many requests in the last minute. Wait 60 seconds and try again.",
    ),
    "rate_limit_day": Denial(
        429,
        "rate_limit_error",
        "rate_limit_exceeded",
        "You have reached today's request limit. It resets at midnight Eastern time.",
    ),
    "spend_cap": Denial(
        429,
        "insufficient_quota",
        "insufficient_quota",
        "The class budget for this month is used up, so no requests are being served. "
        "Retrying will not help. Let your instructor know.",
    ),
    "kill_switch": Denial(
        503,
        "server_error",
        "service_unavailable",
        "The service is paused by the instructor. Try again later.",
    ),
}


def mint_token() -> tuple[str, str, str]:
    """A new student token as (plain token, sha256 hex, prefix). Only the last two are stored."""
    token = TOKEN_START + secrets.token_urlsafe(24)  # 24 random bytes encode to 32 characters
    return token, hash_token(token), token[:PREFIX_LENGTH]


def hash_token(token: str) -> str:
    """sha256 hex of a token. A fast hash is fine here: the token is long and random."""
    return hashlib.sha256(token.encode()).hexdigest()


def hash_class_code(code: str) -> str:
    """sha256 hex of the class code. Case and outer spaces are ignored so typing it is forgiving."""
    return hashlib.sha256(code.strip().lower().encode()).hexdigest()


def hash_ip(ip: str) -> str:
    """Keyed hash of a client IP, so usage_log can count per address without storing addresses."""
    secret = get_settings().supabase_secret_key
    if not secret:
        raise RuntimeError("SUPABASE_SECRET_KEY must be set")
    # There are only about 4 billion IPv4 addresses, so a bare sha256 can be reversed by
    # hashing them all. HMAC with a server-only key prevents that. The key is derived from
    # the Supabase secret rather than being the secret itself, so one value has one use.
    key = hashlib.sha256(b"clinic-assistant:ip-hash:" + secret.encode()).digest()
    return hmac.new(key, ip.encode(), hashlib.sha256).hexdigest()


def client_ip(request) -> str:
    """The caller's address. Behind Vercel's proxy it is the first hop of x-forwarded-for."""
    if request is None:
        return "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def authorize(token: str | None) -> AccessDecision:
    """Allow or deny a class-endpoint request. Takes the bare token, without "Bearer "."""
    # Anything that isn't shaped like a token can't be in the table: skip the round trip
    if not token or not TOKEN_PATTERN.fullmatch(token):
        return AccessDecision(allowed=False, reason="invalid_token")
    db = await get_db()
    result = await db.rpc("authorize_request", {"p_token_hash": hash_token(token)}).execute()
    return AccessDecision.model_validate(result.data)


async def authorize_demo(ip: str) -> AccessDecision:
    """Allow or deny a demo-page request from this client IP."""
    db = await get_db()
    result = await db.rpc("authorize_demo_request", {"p_ip_hash": hash_ip(ip)}).execute()
    return AccessDecision.model_validate(result.data)


async def request_token(email: str, class_code: str) -> tuple[str | None, dict]:
    """Mint a token for a student who knows the class code. Returns (token, database result).

    The token is None unless result["issued"] is true; result["reason"] says why not.
    """
    token, token_hash, prefix = mint_token()
    db = await get_db()
    result = await db.rpc(
        "request_token",
        {
            "p_email": email,
            "p_code_hash": hash_class_code(class_code),
            "p_token_hash": token_hash,
            "p_token_prefix": prefix,
        },
    ).execute()
    return (token if result.data["issued"] else None), result.data


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int) -> float:
    """Estimated USD for one model call, from the price table above."""
    input_price, output_price = PRICES_PER_MILLION.get(model, (0.0, 0.0))
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


async def log_usage(
    *,
    surface: str,
    outcome: str,
    status: int,
    token_id: str | None = None,
    client_ip_hash: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    detail: str | None = None,
    ignored_params: list[str] | None = None,
    judgments: dict | None = None,
) -> None:
    """Write one usage_log row. Denials use outcome "denied" with the reason in detail."""
    row = {
        "surface": surface,
        "outcome": outcome,
        "status": status,
        "token_id": token_id,
        "client_ip_hash": client_ip_hash,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "detail": detail,
        "ignored_params": ignored_params,
        "judgments": judgments,
        "estimated_cost_usd": estimate_cost(model, input_tokens or 0, output_tokens or 0),
    }
    # The user already has (or is about to get) their answer: a failed log write must not
    # turn that into an error.
    try:
        db = await get_db()
        await db.table("usage_log").insert(row).execute()
    except Exception:
        logger.exception("usage_log insert failed (surface=%s outcome=%s)", surface, outcome)
