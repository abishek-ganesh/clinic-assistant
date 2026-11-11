"""Settings read from environment variables (and .env locally). One place for every model id."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App configuration. On Vercel these come from the project's environment variables."""

    # env_ignore_empty: a blank line like JEV_BASE_URL= in .env means "use the default"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    app_env: str = "development"
    clinic_name: str = "Family Clinic"

    supabase_url: str | None = None
    supabase_secret_key: str | None = None

    anthropic_api_key: str | None = None
    generation_model: str = "claude-haiku-5-5"
    # The name students put in model=...; it can point at a different model later
    model_alias: str = "bootcamp"

    typesafe_api_key: str | None = None
    jev_base_url: str = "https://api.typesafe.ai"
    jev_model: str = "jev-latest"

    voyage_api_key: str | None = None
    embedding_model: str = "voyage-4-lite"
    embedding_dimensions: int = 512

    # Local fallback when a key is missing (development only)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def providers(self) -> dict[str, bool]:
        """Which providers have credentials. Never exposes the values themselves."""
        return {
            "supabase": bool(self.supabase_url and self.supabase_secret_key),
            "anthropic": bool(self.anthropic_api_key),
            "typesafe": bool(self.typesafe_api_key),
            "voyage": bool(self.voyage_api_key),
        }


@lru_cache
def get_settings() -> Settings:
    """Cached so every request in a warm serverless instance reuses one Settings object."""
    return Settings()
