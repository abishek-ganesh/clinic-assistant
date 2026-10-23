"""FastAPI entry point. Vercel's FastAPI preset finds the `app` object in this file."""

from fastapi import FastAPI

from app.config import get_settings

app = FastAPI(title="Family Clinic Assistant")


@app.get("/")
def root() -> dict:
    """Placeholder until the demo page ships in Phase 1."""
    settings = get_settings()
    return {
        "name": f"{settings.clinic_name} Assistant",
        "note": "Fictional clinic. Not medical advice.",
        "status": "Phase 0: the demo page arrives in Phase 1",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict:
    """Reports which providers are configured, without calling them (cheap and safe to poll)."""
    settings = get_settings()
    providers = settings.providers()
    return {
        "status": "ok" if all(providers.values()) else "degraded",
        "app_env": settings.app_env,
        "providers": providers,
        "generation_model": settings.generation_model,
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
    }
