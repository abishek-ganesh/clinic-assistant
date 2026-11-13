"""The stats routes with the database call replaced by a fake, and the one-call query itself."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import stats, stats_routes

# main.py includes this router; the tests mount it alone so they exercise only this slice
app = FastAPI()
app.include_router(stats_routes.router)
client = TestClient(app)

STATS = {"days": 30, "totals": {"requests": 12, "cost_usd": 0.0031}, "daily": []}


@pytest.fixture
def fake_stats(monkeypatch):
    """Record the window each request asked for and answer with a fixed body."""
    asked = []

    async def fake_get_stats(days):
        asked.append(days)
        return STATS | {"days": days}

    monkeypatch.setattr(stats, "get_stats", fake_get_stats)
    return asked


def test_api_stats_returns_the_aggregates_and_lets_caches_keep_them_a_minute(fake_stats):
    response = client.get("/api/stats")
    assert response.status_code == 200
    assert response.json()["totals"] == {"requests": 12, "cost_usd": 0.0031}
    assert fake_stats == [30]
    assert response.headers["cache-control"] == "public, max-age=60, s-maxage=60"


@pytest.mark.parametrize(
    ("query", "days"),
    [("7", 7), ("90", 90), ("0", 1), ("-5", 1), ("365", 90), ("abc", 30), ("", 30), ("7.5", 30)],
)
def test_api_stats_clamps_the_window(fake_stats, query, days):
    response = client.get("/api/stats", params={"days": query})
    assert response.status_code == 200
    assert fake_stats == [days]


def test_api_stats_reports_a_database_failure_in_the_apps_error_shape(monkeypatch):
    async def broken(_days):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(stats, "get_stats", broken)
    response = client.get("/api/stats")
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "upstream_error" and error["message"]
    assert "connection refused" not in response.text
    assert "cache-control" not in response.headers


def test_stats_page_serves_the_static_page_with_its_tab_marked_current():
    response = client.get("/stats")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'href="/stats" aria-current="page"' in response.text


async def test_get_stats_makes_one_rpc_call(monkeypatch):
    calls = []

    class FakeDb:
        def rpc(self, name, params):
            calls.append((name, params))
            return self

        async def execute(self):
            return SimpleNamespace(data={"days": 7})

    async def fake_get_db():
        return FakeDb()

    monkeypatch.setattr(stats, "get_db", fake_get_db)
    assert await stats.get_stats(7) == {"days": 7}
    assert calls == [("usage_stats", {"p_days": 7})]
