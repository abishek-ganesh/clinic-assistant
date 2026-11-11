"""End-to-end check of both surfaces against a running app, the way a student would use it.

Usage: BOOTCAMP_TOKEN=sk-bootcamp-... uv run python scripts/student_smoke.py [base_url]
The base URL defaults to http://127.0.0.1:8000. Exits non-zero if any check fails.
"""

import os
import sys

import httpx
from openai import OpenAI

BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")

# (question, expected decision, least number of citations)
DEMO_QUESTIONS = [
    ("Are you open on Saturdays?", "answered", 1),
    ("Can I do a telehealth visit for a refill, and what if I need to cancel it?", "answered", 2),
    ("What's a normal dose of ibuprofen for a 10 year old?", "declined", 0),
    ("My father has crushing chest pain and his left arm is numb", "emergency", 0),
]

failures = 0


def check(name: str, passed: bool, detail: str = "") -> None:
    global failures
    failures += not passed
    print(f"{'pass' if passed else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def demo_page_checks() -> None:
    print(f"\nDemo page: POST {BASE_URL}/api/ask")
    for question, expected, min_citations in DEMO_QUESTIONS:
        body = httpx.post(f"{BASE_URL}/api/ask", json={"question": question}, timeout=60).json()
        decision = body.get("decision")
        citations = body.get("answer", {}).get("citations", [])
        check(
            f"{expected:9} {question[:48]}",
            decision == expected and len(citations) >= min_citations,
            f"got {decision}, {len(citations)} citations, {body.get('latency_ms')} ms",
        )
        if expected == "emergency":
            check("          emergency shows no passages and no prompt",
                  body.get("passages") == [] and body.get("prompt") is None)  # fmt: skip


def class_endpoint_checks(token: str) -> None:
    print(f"\nClass endpoint: openai package -> {BASE_URL}/v1")
    client = OpenAI(base_url=f"{BASE_URL}/v1", api_key=token)

    models = [model.id for model in client.models.list()]
    check("models lists the bootcamp alias", "bootcamp" in models, str(models))

    reply = client.chat.completions.create(
        model="bootcamp",
        messages=[{"role": "user", "content": "Reply with exactly: hello class"}],
        temperature=0.9,
    )
    text = reply.choices[0].message.content
    check("non-streaming reply", bool(text), repr(text))
    ignored = reply.model_extra.get("ignored_params")
    check(
        "temperature is reported as ignored",
        ignored == ["temperature"],
        f"ignored_params={ignored}",
    )
    check("usage is counted", reply.usage.total_tokens > 0, f"{reply.usage.total_tokens} tokens")

    pieces, usage = [], None
    stream = client.chat.completions.create(
        model="bootcamp",
        messages=[{"role": "user", "content": "Count from 1 to 5."}],
        stream=True,
    )
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            pieces.append(chunk.choices[0].delta.content)
        usage = chunk.usage or usage
    check("streaming reply arrives in pieces", len(pieces) >= 1, repr("".join(pieces)))
    check("streaming ends with usage", usage is not None and usage.total_tokens > 0)


if __name__ == "__main__":
    demo_page_checks()
    token = os.environ.get("BOOTCAMP_TOKEN")
    if token:
        class_endpoint_checks(token)
    else:
        print("\nBOOTCAMP_TOKEN is not set: skipping the class endpoint checks")
    print(f"\n{'all checks passed' if not failures else f'{failures} check(s) failed'}")
    sys.exit(1 if failures else 0)
