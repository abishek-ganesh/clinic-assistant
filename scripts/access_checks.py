"""Check the class endpoint's access rules from the outside, with the openai package.

uv run python scripts/access_checks.py [BASE_URL]   (or BASE_URL env; default local server)

Mints a throwaway token straight into the database, then checks: an unknown token is a 401,
going over the per-minute limit is a 429, an exhausted budget is a 429 with the code
insufficient_quota, and a revoked token is a 401. The server at BASE_URL must use the same
Supabase project as the local .env.

While the spend cap check runs (a second or two) the real cap is lowered, so every caller is
denied. The cap is restored and the token revoked in a finally block, even on failure.
"""

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import openai

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.access import mint_token  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import get_db  # noqa: E402

LABEL = "access-checks (throwaway)"
# Small on purpose: reaching the limit takes 3 real model calls instead of 20
RPM_LIMIT = 3
TINY_CAP_USD = 0.0001

results: list[bool] = []


def report(name: str, passed: bool, note: str = "") -> None:
    results.append(passed)
    print(f"{'PASS' if passed else 'FAIL'}  {name}{f': {note}' if note else ''}")


async def ask(client: openai.AsyncOpenAI) -> None:
    """One cheap request through the class endpoint."""
    await client.chat.completions.create(
        model=get_settings().model_alias,
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        max_tokens=16,
    )


async def expect_error(
    name: str, client: openai.AsyncOpenAI, error: type[openai.APIStatusError], code: str
) -> None:
    """Pass when one request raises exactly this openai error with this error code."""
    try:
        await ask(client)
    except error as exc:
        report(name, exc.code == code, f"{exc.status_code} {exc.code}")
    except Exception as exc:
        report(name, False, f"expected {error.__name__}, got {exc!r}")
    else:
        report(name, False, f"expected {error.__name__}, but the request succeeded")


async def main() -> int:
    base_url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BASE_URL")
    base_url = (base_url or "http://127.0.0.1:8000").rstrip("/")
    print(f"Checking {base_url}/v1")

    def client_for(token: str) -> openai.AsyncOpenAI:
        # max_retries=0: the openai package retries 429s by default, which would hide them
        return openai.AsyncOpenAI(base_url=f"{base_url}/v1", api_key=token, max_retries=0)

    db = await get_db()
    setting = await db.table("app_settings").select("value").eq("key", "spend_cap_usd").execute()
    original_cap = setting.data[0]["value"]
    print(f"spend_cap_usd is {original_cap}; it is restored to that at the end")

    token, token_hash, prefix = mint_token()
    inserted = (
        await db.table("student_tokens")
        .insert(
            {
                "label": LABEL,
                "token_hash": token_hash,
                "token_prefix": prefix,
                "rpm_limit": RPM_LIMIT,
            }
        )
        .execute()
    )
    token_id = inserted.data[0]["id"]
    client = client_for(token)
    probe_id = None

    try:
        # Well formed but never stored, so this exercises the database lookup
        unknown_token, _, _ = mint_token()
        await expect_error(
            "unknown token is a 401",
            client_for(unknown_token),
            openai.AuthenticationError,
            "invalid_api_key",
        )

        try:
            for _ in range(RPM_LIMIT):
                await ask(client)
        except Exception as exc:
            report(f"{RPM_LIMIT} requests inside the limit succeed", False, repr(exc))
        else:
            report(f"{RPM_LIMIT} requests inside the limit succeed", True)
        await expect_error(
            f"request {RPM_LIMIT + 1} inside a minute is a 429",
            client,
            openai.RateLimitError,
            "rate_limit_exceeded",
        )

        # The spend cap is checked before the rate limits, so the same token shows it.
        # The probe row puts this month's spend at or above the tiny cap no matter how
        # little has been spent so far.
        probe = (
            await db.table("usage_log")
            .insert(
                {
                    "token_id": token_id,
                    "surface": "endpoint",
                    "outcome": "denied",
                    "detail": "access_checks spend cap probe",
                    "estimated_cost_usd": TINY_CAP_USD,
                }
            )
            .execute()
        )
        probe_id = probe.data[0]["id"]
        await (
            db.table("app_settings")
            .update({"value": TINY_CAP_USD})
            .eq("key", "spend_cap_usd")
            .execute()
        )
        await expect_error(
            "exhausted budget is a 429 insufficient_quota",
            client,
            openai.RateLimitError,
            "insufficient_quota",
        )
        await (
            db.table("app_settings")
            .update({"value": original_cap})
            .eq("key", "spend_cap_usd")
            .execute()
        )

        # Revoked is checked before the limits, so the rate-limited token still shows it
        await revoke(db, token_id)
        await expect_error(
            "revoked token is a 401", client, openai.AuthenticationError, "invalid_api_key"
        )
    finally:
        # Each step runs even if an earlier one fails: a stuck tiny cap would deny everyone
        cleanup = {
            f"restore spend_cap_usd to {original_cap}": db.table("app_settings")
            .update({"value": original_cap})
            .eq("key", "spend_cap_usd")
            .execute(),
            f"revoke token {prefix}": revoke(db, token_id),
        }
        if probe_id is not None:
            cleanup["delete the probe row"] = (
                db.table("usage_log").delete().eq("id", probe_id).execute()
            )
        for step, action in cleanup.items():
            try:
                await action
                print(f"Cleanup: {step}")
            except Exception as exc:
                report(f"cleanup: {step}", False, f"DO THIS BY HAND. {exc!r}")

    failed = results.count(False)
    print(f"{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


async def revoke(db, token_id: str) -> None:
    """Revoke the throwaway token. Safe to call twice: the second call matches no row."""
    await (
        db.table("student_tokens")
        .update({"revoked_at": datetime.now(UTC).isoformat()})
        .eq("id", token_id)
        .is_("revoked_at", "null")
        .execute()
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
