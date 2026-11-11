"""Judgments: one request to TypeSafe Jev asks both questions and returns two probabilities."""

import asyncio
import json

import httpx

from app.config import get_settings
from app.models import Judgments

MAX_RATE_LIMIT_RETRIES = 4
MAX_SERVER_RETRIES = 3
RETRY_BASE_SECONDS = 0.5

# "noul" is Jev's yes/no question type: the answer is the probability that it is true.
QUESTIONS = {
    "is_emergency": {
        "type": "noul",
        "instructions": (
            "Does the text describe symptoms or a situation that needs emergency care right now?"
        ),
        "criteria": {
            "true": (
                "The person describes something happening now that needs emergency care: "
                "chest pain, trouble breathing, signs of a stroke, severe bleeding, "
                "an overdose, or intent to end their life."
            ),
            "false": (
                "A routine or administrative question, including questions about "
                "urgent care or the emergency room when nobody is in danger right now."
            ),
        },
    },
    "in_scope": {
        "type": "noul",
        "instructions": (
            "Could this question be answered from a primary-care clinic's patient handbook?"
        ),
        "criteria": {
            "true": (
                "It is about clinic logistics: hours, locations, appointments, cancellations, "
                "prescription refills, billing, insurance, telehealth, privacy and medical "
                "records, when to use urgent care or the emergency room, or vaccines offered."
            ),
            "false": (
                "It asks for a diagnosis, treatment advice or medication dosing, "
                "or it is about anything other than clinic logistics."
            ),
        },
    },
}

OLLAMA_FORMAT = {
    "type": "object",
    "properties": {name: {"type": "number", "minimum": 0, "maximum": 1} for name in QUESTIONS},
    "required": list(QUESTIONS),
}


def _retry_delay(response: httpx.Response | None, retries: int) -> float:
    """Honor a numeric retry-after when the server sends one, else back off 0.5s, 1s, 2s, 4s."""
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.replace(".", "", 1).isdigit():
            return float(retry_after)
    return RETRY_BASE_SECONDS * 2**retries


async def _ask_jev(question: str, client: httpx.AsyncClient) -> dict:
    """POST the question to Jev. Rate limits and server errors are retried on separate budgets."""
    settings = get_settings()
    url = f"{settings.jev_base_url.rstrip('/')}/v1/systemone"
    payload = {
        "model": settings.jev_model,
        "state": {"question": question},
        "questions": QUESTIONS,
    }
    headers = {"Authorization": f"Bearer {settings.typesafe_api_key}"}
    rate_limit_retries = 0
    server_retries = 0

    while True:
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.TransportError:
            if server_retries >= MAX_SERVER_RETRIES:
                raise
            await asyncio.sleep(_retry_delay(None, server_retries))
            server_retries += 1
            continue

        if response.status_code == 429:
            if rate_limit_retries >= MAX_RATE_LIMIT_RETRIES:
                response.raise_for_status()
            await asyncio.sleep(_retry_delay(response, rate_limit_retries))
            rate_limit_retries += 1
            continue

        if response.status_code >= 500:
            if server_retries >= MAX_SERVER_RETRIES:
                response.raise_for_status()
            await asyncio.sleep(_retry_delay(response, server_retries))
            server_retries += 1
            continue

        # Any other 4xx is our bug (bad key, bad request shape), so fail fast
        response.raise_for_status()
        return response.json()


async def _ask_ollama(question: str, client: httpx.AsyncClient) -> dict:
    """Development stand-in for Jev: a local model guesses both probabilities as JSON."""
    settings = get_settings()
    described = "\n".join(
        f"- {name}: {spec['instructions']} "
        f"Yes when: {spec['criteria']['true']} No when: {spec['criteria']['false']}"
        for name, spec in QUESTIONS.items()
    )
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "format": OLLAMA_FORMAT,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You triage questions sent to a primary-care clinic. For each item, "
                    "give the probability from 0 to 1 that the answer is yes.\n" + described
                ),
            },
            {"role": "user", "content": question},
        ],
    }
    response = await client.post(f"{settings.ollama_base_url.rstrip('/')}/api/chat", json=payload)
    response.raise_for_status()
    return json.loads(response.json()["message"]["content"])


def _clamp(probability: float) -> float:
    return min(1.0, max(0.0, float(probability)))


async def judge(question: str, client: httpx.AsyncClient | None = None) -> Judgments:
    """Probabilities that the question is an emergency and that the handbook can answer it."""
    settings = get_settings()
    if not settings.typesafe_api_key and settings.is_production:
        raise RuntimeError("TYPESAFE_API_KEY is not set")

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        if settings.typesafe_api_key:
            answers = (await _ask_jev(question, client))["answers"]
            return Judgments(
                is_emergency=answers["is_emergency"]["noul"],
                in_scope=answers["in_scope"]["noul"],
                provider="jev",
                calibrated=True,
            )
        # A general chat model's "0.8" is a guess, not a calibrated probability: label it
        guesses = await _ask_ollama(question, client)
        return Judgments(
            is_emergency=_clamp(guesses["is_emergency"]),
            in_scope=_clamp(guesses["in_scope"]),
            provider="ollama",
            calibrated=False,
        )
    finally:
        if owns_client:
            await client.aclose()
