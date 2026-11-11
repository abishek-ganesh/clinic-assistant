"""Shared fixtures. Unit tests never read the real .env; they build Settings explicitly."""

import sys

import pytest

from app.config import Settings

ALL_KEYS = {
    "supabase_url": "https://example.supabase.co",
    "supabase_secret_key": "sb_secret_test",
    "anthropic_api_key": "sk-ant-test",
    "typesafe_api_key": "ts-test",
    "voyage_api_key": "pa-test",
}


@pytest.fixture
def use_settings(monkeypatch):
    """Point every module at a Settings built from the given overrides, ignoring .env."""

    def _use(**overrides) -> Settings:
        settings = Settings(_env_file=None, **overrides)
        # Every app module that imported get_settings holds its own reference to it
        for name, module in list(sys.modules.items()):
            if name.startswith("app.") and hasattr(module, "get_settings"):
                monkeypatch.setattr(module, "get_settings", lambda: settings)
        return settings

    return _use
