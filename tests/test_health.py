from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import ALL_KEYS

client = TestClient(app)


def test_health_ok_when_every_provider_is_configured(use_settings):
    use_settings(**ALL_KEYS)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert all(body["providers"].values())
    assert body["embedding_model"] == "voyage-4-lite"


def test_health_degraded_and_specific_when_keys_are_missing(use_settings):
    use_settings(voyage_api_key="pa-test")
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["providers"] == {
        "supabase": False,
        "anthropic": False,
        "typesafe": False,
        "voyage": True,
    }


def test_health_never_leaks_key_values(use_settings):
    use_settings(**ALL_KEYS)
    text = client.get("/health").text
    for value in ALL_KEYS.values():
        if value.startswith("https://"):
            continue
        assert value not in text


def test_root_serves_the_demo_page_with_the_fictional_notice(use_settings):
    use_settings()
    page = client.get("/")
    assert page.headers["content-type"].startswith("text/html")
    assert "Family Clinic Assistant" in page.text
    assert "Not medical advice" in page.text
