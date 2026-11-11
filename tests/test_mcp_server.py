"""The MCP server: tool functions, the access wrapper, and real JSON-RPC over in-process HTTP."""

import sys
import types
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request

from app import mcp_server
from app.models import AccessDecision, Passage, Slot

PASSAGE = Passage(
    rank=1, section_slug="hours", section="Hours and locations", heading="Saturday hours",
    content="Open Saturdays from 9:00 am to 1:00 pm.", similarity=0.56649,
)  # fmt: skip
SLOT = Slot(
    slot_id="slot-1", provider="Dr. Patel", visit_type="physical",
    starts_at="2026-12-01T09:30:00-05:00", display="Tue Dec 1, 9:30 AM with Dr. Patel",
)  # fmt: skip

# What an MCP client sends on every POST. The forwarded header is what Vercel's proxy adds.
HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "x-forwarded-for": "203.0.113.9, 10.0.0.1",
}


@pytest.fixture
def wired(monkeypatch, use_settings):
    """Allow every call by default, record usage rows, and stub retrieval and the schedule."""
    use_settings(supabase_secret_key="sb_secret_test")
    state = {"decision": AccessDecision(allowed=True), "logged": [], "ips": [], "schedule": []}

    async def fake_authorize_demo(ip):
        state["ips"].append(ip)
        if isinstance(state["decision"], Exception):
            raise state["decision"]
        return state["decision"]

    async def fake_log_usage(**row):
        state["logged"].append(row)

    async def fake_retrieve(question):
        return [PASSAGE]

    async def fake_check_availability(visit_type=None, on_or_after=None, limit=5):
        state["schedule"].append((visit_type, on_or_after))
        return [SLOT]

    # app/schedule.py belongs to another slice: the tool imports it at call time, so a
    # stand-in module is enough here whether or not the real one exists yet
    schedule = types.ModuleType("app.schedule")
    schedule.check_availability = fake_check_availability
    monkeypatch.setitem(sys.modules, "app.schedule", schedule)
    monkeypatch.setattr(mcp_server, "authorize_demo", fake_authorize_demo)
    monkeypatch.setattr(mcp_server, "log_usage", fake_log_usage)
    monkeypatch.setattr(mcp_server, "retrieve", fake_retrieve)
    return state


@pytest.fixture
def client():
    """A small app that includes the MCP routes the same way app/main.py does."""
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=Path(mcp_server.__file__).parent / "static"))

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    app.router.routes.extend(mcp_server.mcp_routes)
    # Not used as a context manager, so lifespan events never run: the endpoint must not
    # need them. base_url gives the Host header a local server would see.
    return TestClient(app, base_url="http://localhost:8000")


def rpc(client, method, params=None, path="/mcp", headers=HEADERS):
    """POST one JSON-RPC request the way an MCP client does."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post(path, json=body, headers=headers, follow_redirects=False)


def call_tool(client, name, arguments):
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200
    return response.json()["result"]


def http_request(headers=None, client=("10.0.0.7", 5000)) -> Request:
    raw = [(key.encode(), value.encode()) for key, value in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": client})


# Tool functions


async def test_find_passages_returns_the_fields_a_model_needs(wired):
    [passage] = await mcp_server.find_passages("Are you open on Saturdays?")
    assert passage.model_dump() == {
        "section": "Hours and locations",
        "heading": "Saturday hours",
        "content": "Open Saturdays from 9:00 am to 1:00 pm.",
        "similarity": 0.566,
    }


async def test_find_slots_passes_the_filters_to_the_schedule(wired):
    slots = await mcp_server.find_slots("physical", date(2026, 12, 1))
    assert slots == [SLOT]
    assert wired["schedule"] == [("physical", date(2026, 12, 1))]


# The access wrapper


async def test_metered_runs_the_work_and_logs_one_row(wired):
    async def work():
        return "done"

    request = http_request({"x-forwarded-for": "203.0.113.9, 10.0.0.1"})
    assert await mcp_server.metered("search_handbook", request, work) == "done"

    assert wired["ips"] == ["203.0.113.9"]
    [row] = wired["logged"]
    assert row["surface"] == "mcp"
    assert row["outcome"] == "tool_call"
    assert row["status"] == 200
    assert row["detail"] == "search_handbook"
    assert row["client_ip_hash"] == mcp_server.hash_ip("203.0.113.9")
    assert row["latency_ms"] >= 0


async def test_metered_falls_back_to_the_socket_address(wired):
    async def work():
        return "done"

    await mcp_server.metered("search_handbook", http_request(), work)
    assert wired["ips"] == ["10.0.0.7"]


async def test_metered_denies_without_running_the_work(wired):
    wired["decision"] = AccessDecision(
        allowed=False, reason="rate_limit_minute", retry_after_seconds=60
    )
    ran = []

    async def work():
        ran.append(True)

    with pytest.raises(ToolError, match="Wait 60 seconds"):
        await mcp_server.metered("check_availability", http_request(), work)

    assert ran == []
    [row] = wired["logged"]
    assert (row["outcome"], row["status"], row["detail"]) == ("denied", 429, "check_availability")


async def test_metered_fails_closed_when_the_access_check_errors(wired):
    wired["decision"] = RuntimeError("database unreachable")
    ran = []

    async def work():
        ran.append(True)

    with pytest.raises(ToolError, match="did not respond"):
        await mcp_server.metered("search_handbook", http_request(), work)

    assert ran == []
    assert wired["logged"] == []


async def test_metered_logs_an_error_row_when_the_work_fails(wired):
    async def work():
        raise RuntimeError("voyage is down: secret detail")

    with pytest.raises(ToolError) as error:
        await mcp_server.metered("search_handbook", http_request(), work)

    # The caller gets a fixed message, never the internal error text
    assert "secret detail" not in str(error.value)
    [row] = wired["logged"]
    assert (row["outcome"], row["status"], row["detail"]) == ("error", 502, "search_handbook")


# JSON-RPC over HTTP, through an app that mounts the server like main.py


def test_initialize_and_tools_list_are_not_counted(wired, client):
    response = rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    # Stateless: the server hands out no session id for the client to send back
    assert "mcp-session-id" not in response.headers
    assert response.json()["result"]["serverInfo"]["name"] == "family-clinic"

    tools = {tool["name"]: tool for tool in rpc(client, "tools/list").json()["result"]["tools"]}
    assert set(tools) == {"search_handbook", "check_availability"}
    assert tools["search_handbook"]["inputSchema"]["required"] == ["question"]
    assert set(tools["check_availability"]["inputSchema"]["properties"]) == {
        "visit_type",
        "on_or_after",
    }
    for tool in tools.values():
        assert tool["annotations"]["readOnlyHint"] is True
        assert len(tool["description"]) > 100

    assert wired["ips"] == []
    assert wired["logged"] == []


def test_search_handbook_call_returns_passages_and_logs(wired, client):
    result = call_tool(client, "search_handbook", {"question": "Are you open on Saturdays?"})
    assert result["isError"] is False
    assert result["structuredContent"]["result"] == [
        {
            "section": "Hours and locations",
            "heading": "Saturday hours",
            "content": "Open Saturdays from 9:00 am to 1:00 pm.",
            "similarity": 0.566,
        }
    ]
    assert "Saturday hours" in result["content"][0]["text"]

    assert wired["ips"] == ["203.0.113.9"]
    assert [(row["outcome"], row["detail"]) for row in wired["logged"]] == [
        ("tool_call", "search_handbook")
    ]


def test_check_availability_call_parses_the_date(wired, client):
    result = call_tool(
        client, "check_availability", {"visit_type": "physical", "on_or_after": "2026-12-01"}
    )
    assert result["isError"] is False
    assert result["structuredContent"]["result"] == [SLOT.model_dump()]
    assert wired["schedule"] == [("physical", date(2026, 12, 1))]

    # Both arguments are optional
    assert call_tool(client, "check_availability", {})["isError"] is False
    assert wired["schedule"][-1] == (None, None)


def test_a_denial_reaches_the_client_as_a_tool_error(wired, client):
    wired["decision"] = AccessDecision(
        allowed=False, reason="rate_limit_minute", retry_after_seconds=60
    )
    result = call_tool(client, "search_handbook", {"question": "Are you open on Saturdays?"})
    assert result["isError"] is True
    assert "Too many requests in the last minute" in result["content"][0]["text"]
    assert [row["outcome"] for row in wired["logged"]] == ["denied"]


def test_invalid_arguments_are_rejected_before_the_access_check(wired, client):
    result = call_tool(client, "search_handbook", {"question": ""})
    assert result["isError"] is True
    assert wired["ips"] == []
    assert wired["logged"] == []


def test_both_path_spellings_answer_without_a_redirect(wired, client):
    assert rpc(client, "tools/list", path="/mcp").status_code == 200
    assert rpc(client, "tools/list", path="/mcp/").status_code == 200


def test_only_post_is_served_and_other_routes_still_work(wired, client):
    assert client.get("/mcp", headers={"accept": "text/event-stream"}).status_code == 405
    assert client.delete("/mcp").status_code == 405
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/static/styles.css").status_code == 200


def test_host_header_is_checked_locally_but_not_in_production(wired, client, use_settings):
    foreign_host = {**HEADERS, "host": "clinic.example"}
    assert rpc(client, "tools/list", headers=foreign_host).status_code == 421

    use_settings(app_env="production", supabase_secret_key="sb_secret_test")
    assert rpc(client, "tools/list", headers=foreign_host).status_code == 200
