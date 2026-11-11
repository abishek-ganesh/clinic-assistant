"""MCP server: the handbook search and the open-slot lookup as tools any MCP client can call.

Served at /mcp on the same FastAPI app, over streamable HTTP in stateless mode: every POST is
one complete JSON-RPC exchange, so no serverless instance has to remember a session.

Both tools are read-only. There is deliberately no booking tool: booking is a side effect,
and a side effect needs a human to confirm it, which this thin slice has no way to ask for.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from app.access import DENIALS, authorize_demo, client_ip, hash_ip, log_usage
from app.config import get_settings
from app.models import Slot
from app.retrieval import retrieve
from app.thresholds import MAX_QUESTION_CHARS

logger = logging.getLogger(__name__)

UPSTREAM_MESSAGE = "The clinic's systems did not respond. Try again in a moment."
# Hints for the client: these tools only read, and only from the clinic's own data
READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

mcp = MCPServer(
    name="family-clinic",
    title="Family Clinic Assistant",
    instructions=(
        "Tools for the fictional Family Clinic (synthetic data only). Use search_handbook for "
        "questions about clinic policy and check_availability for open appointment times. "
        "Booking is not available here: tell the user to book on the clinic's website."
    ),
    # The SDK configures the root logger at this level when the server is created. Its
    # default, INFO, would switch on INFO logs for every library in the app.
    log_level="WARNING",
)


class HandbookPassage(BaseModel):
    """One handbook passage as a tool result. similarity runs from 0 (unrelated) to 1."""

    section: str
    heading: str | None
    content: str
    similarity: float


async def metered[T](tool: str, request: Request | None, work: Callable[[], Awaitable[T]]) -> T:
    """Run one tool call behind the open-surface limits and write its usage_log row.

    Failures are raised as ToolError: the MCP client gets a normal result marked is_error
    with the message as its text, which the model can read and explain to the user.
    """
    started = time.perf_counter()

    # If the limit check itself fails we refuse, rather than serve calls nobody is counting
    try:
        ip = client_ip(request)
        ip_hash = hash_ip(ip)
        decision = await authorize_demo(ip)
    except Exception:
        logger.exception("mcp access check failed")
        raise ToolError(UPSTREAM_MESSAGE) from None

    if not decision.allowed:
        denial = DENIALS[decision.reason]
        await log_usage(
            surface="mcp",
            outcome="denied",
            status=denial.status,
            client_ip_hash=ip_hash,
            detail=tool,
        )
        raise ToolError(denial.message)

    try:
        result = await work()
    except Exception:
        logger.exception("mcp tool %s failed", tool)
        await log_usage(
            surface="mcp",
            outcome="error",
            status=502,
            client_ip_hash=ip_hash,
            latency_ms=round((time.perf_counter() - started) * 1000),
            detail=tool,
        )
        raise ToolError(UPSTREAM_MESSAGE) from None

    # Awaited, not a background task: the next call's limit check counts this row
    await log_usage(
        surface="mcp",
        outcome="tool_call",
        status=200,
        client_ip_hash=ip_hash,
        latency_ms=round((time.perf_counter() - started) * 1000),
        detail=tool,
    )
    return result


async def find_passages(question: str) -> list[HandbookPassage]:
    """The handbook passages closest in meaning to the question, best first."""
    passages = await retrieve(question)
    return [
        HandbookPassage(
            section=passage.section,
            heading=passage.heading,
            content=passage.content,
            similarity=round(passage.similarity, 3),
        )
        for passage in passages
    ]


async def find_slots(visit_type: str | None, on_or_after: date | None) -> list[Slot]:
    """The next open appointment times, soonest first."""
    # Imported here so this module loads even where the schedule module is not deployed
    from app.schedule import check_availability

    return await check_availability(visit_type=visit_type, on_or_after=on_or_after)


# The docstrings and Field descriptions below are sent to the client as the tool's description
# and input schema. They are the only thing the model reads when deciding which tool to call.


@mcp.tool(title="Search the clinic handbook", annotations=READ_ONLY)
async def search_handbook(
    question: Annotated[
        str,
        Field(
            description="A patient's question in plain language, e.g. 'How long do refills take?'",
            min_length=1,
            max_length=MAX_QUESTION_CHARS,
        ),
    ],
    ctx: Context,
) -> list[HandbookPassage]:
    """Search the Family Clinic patient handbook: hours, appointments, refills, billing,
    insurance and other clinic policies.

    Returns the few passages closest in meaning to the question, best first, each with its
    section, heading, text and a similarity score from 0 to 1. A best score under about 0.45
    means the handbook probably does not cover the question: say so instead of guessing.
    Answer only from the returned text and name the section you used. Read-only.
    """
    return await metered(
        "search_handbook", ctx.request_context.request, lambda: find_passages(question)
    )


@mcp.tool(title="Check open appointment times", annotations=READ_ONLY)
async def check_availability(
    ctx: Context,
    visit_type: Annotated[
        Literal["returning", "physical", "telehealth"] | None,
        Field(description="Kind of visit. Omit for any kind."),
    ] = None,
    on_or_after: Annotated[
        date | None,
        Field(description="Earliest acceptable date as YYYY-MM-DD. Omit for the next openings."),
    ] = None,
) -> list[Slot]:
    """List the next open appointment times at the Family Clinic, soonest first.

    Each result has the provider, the visit type, the start time (ISO 8601 with the clinic's
    UTC offset) and a ready-to-show label. An empty list means nothing is open for that
    filter. Read-only: this tool cannot book, hold or cancel an appointment.
    """
    return await metered(
        "check_availability",
        ctx.request_context.request,
        lambda: find_slots(visit_type, on_or_after),
    )


def transport_security() -> TransportSecuritySettings:
    """The SDK's Host and Origin header check, on for local runs and off in production.

    The check stops a web page from reaching a server on your own machine through a DNS
    name it re-pointed at localhost. In production Vercel's proxy only routes requests whose
    Host is one of the project's domains, and this server is public with no cookies or
    credentials to borrow, so the check would only reject hosts we forgot to list.
    """
    if get_settings().is_production:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )


class StatelessEndpoint:
    """ASGI endpoint that answers one MCP request with a session manager built for it.

    The SDK expects its session manager to be started once, in the host app's lifespan, and
    kept running. This endpoint starts one per request instead, so it does not depend on
    lifespan events running and nothing is shared between requests. In stateless mode the
    manager holds no sessions, so starting it costs a task group and nothing else.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # streamable_http_app() is the SDK's public way to build a session manager; the
        # Starlette app it returns is not needed because the route below does the routing.
        # No await sits between these two lines, so the manager read is the one just built.
        mcp.streamable_http_app(
            json_response=True, stateless_http=True, transport_security=transport_security()
        )
        manager = mcp.session_manager
        async with manager.run():
            await manager.handle_request(scope, receive, send)


# POST only: a GET would open a server-to-client event stream that a stateless server never
# writes to, holding a serverless function open for nothing. The MCP spec's answer for "no
# stream offered" is 405, which the route gives for every other method. Both spellings of
# the path are registered so that neither is answered with a redirect.
_endpoint = StatelessEndpoint()
mcp_routes = [
    Route("/mcp", endpoint=_endpoint, methods=["POST"]),
    Route("/mcp/", endpoint=_endpoint, methods=["POST"]),
]
