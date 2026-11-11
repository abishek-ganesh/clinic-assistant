# Architecture

*As of Phase 1 (Nov 2026): the demo page and the class endpoint are built.*

## The idea in one picture

A patient asks the fictional Family Clinic a question. The app finds the handbook passages that answer it, checks whether it should answer at all, and then has a model write a short answer that cites those passages. Every step is visible on the demo page, because the point is to make RAG something students can see.

Three kinds of models, each with one job:

| Kind | Job | Model | Analogy |
|---|---|---|---|
| Embedding | represents text as vectors so similar meanings sit close together | Voyage `voyage-4-lite` | the librarian's card catalog |
| Judgment | answers a typed question with a calibrated probability | TypeSafe Jev | the triage nurse |
| Generative | writes the answer from the passages it is handed | Claude Haiku 5.5 | the doctor who explains |

**Code owns every threshold and every side effect.** Jev never writes text. Haiku never decides whether to answer. The embedding model only measures similarity.

## System diagram

```
Browser (demo page)            Student notebook (openai package)
      |                                   |
      v                                   v
 GET /   POST /api/ask         POST /v1/chat/completions   GET /v1/models
      |                                   |
      +----------------+------------------+
                       v
          FastAPI on Vercel (Python 3.13, one serverless function)
                       |
     +-----------------+------------------+-------------------+
     v                 v                  v                   v
  Voyage AI       TypeSafe Jev      Anthropic API        Supabase
  embeddings      judgments         Claude Haiku 5.5     Postgres + pgvector, over HTTPS
```

## One question, end to end

```
question
  +--> (in parallel) Jev: is_emergency p, in_scope p
  +--> (in parallel) embed the question -> match_chunks() -> top 4 passages
  |
  gate (code):
    emergency p >= 0.35                          -> fixed "call 911" response, no generation
    in_scope p < 0.25 or best similarity < 0.45  -> "I can't find that in the handbook"
    otherwise                                    -> generate
  |
  Haiku: structured output {answer, citations, answerable}
  |
  usage_log row: tokens, latency, outcome, raw probabilities
```

Judgment and retrieval run in parallel because nothing reaches the user until the gate runs. Safety is identical and the response is faster: a latency budget is the slowest branch, not the sum. Raw probabilities are stored on every request so thresholds can be re-tuned later from real traffic.

## Contracts

Each module has one job and a small surface. The data shapes are in `app/models.py`; the numbers are in `app/thresholds.py`. Modules never import each other sideways: only `app/pipeline.py` and `app/main.py` compose them.

| Module | Surface | Notes |
|---|---|---|
| `app/retrieval.py` | `embed(texts, input_type) -> list[list[float]]`, `retrieve(question, k=TOP_K) -> list[Passage]`, `check_corpus_model() -> None` | `check_corpus_model` raises if any stored chunk was embedded by a different model than the one configured; the pipeline runs it once per warm instance |
| `app/chunking.py` | `chunk_markdown(text, slug) -> list[Chunk]` | pure function: split on headings, cap at `CHUNK_MAX_TOKENS`, one-sentence overlap |
| `app/judge.py` | `judge(question) -> Judgments` | Jev over HTTP; Ollama fallback in development, labeled `calibrated=False` |
| `app/generate.py` | `build_prompt(question, passages) -> Prompt`, `generate(question, passages) -> Generation`, `chat(request) -> ChatResult`, `chat_stream(request)` yielding text deltas then one `ChatResult` | the only module that talks to Anthropic (or Ollama for generation) |
| `app/openai_compat.py` | `translate_request(body) -> ChatRequest` (raises `UnsupportedRequest`), `completion_body(...)`, `Stream` + `stream_chunk(...)`, `error_body(...)`, `models_body()` | pure functions, no network: OpenAI shapes in, Anthropic arguments out, and back |
| `app/access.py` | `mint_token()`, `hash_token(token)`, `hash_ip(ip)`, `authorize(token) -> AccessDecision`, `authorize_demo(ip) -> AccessDecision`, `log_usage(...)`, `estimate_cost(model, input_tokens, output_tokens)`, `DENIALS` | `DENIALS` maps each denial reason to an HTTP status, an OpenAI error type and a message |
| `app/pipeline.py` | `gate(judgments, passages) -> reason or None`, `ask(question) -> AskResult` | judge and retrieve in parallel, gate, generate; `AskResult.body` is the `/api/ask` response |
| `app/main.py` | the routes | checks access, calls one function, logs usage, shapes the response |

### `POST /api/ask`

Request: `{"question": "..."}` (1 to 500 characters). Response, always 200 when the pipeline ran:

```json
{ "question": "How long do refills take?",
  "judgments": { "is_emergency": 0.03, "in_scope": 0.91, "provider": "jev", "calibrated": true },
  "thresholds": { "emergency_min": 0.35, "in_scope_min": 0.25, "similarity_min": 0.45 },
  "decision": "answered",
  "decision_reason": null,
  "passages": [ { "rank": 1, "section_slug": "refills", "section": "Prescription refills",
                  "heading": "Turnaround", "content": "...", "similarity": 0.82 } ],
  "answer": { "text": "Refills take up to three business days [1].", "citations": [1] },
  "prompt": { "system": "...", "user": "..." },
  "model": "claude-haiku-5-5",
  "latency_ms": 1240 }
```

- `decision` is `answered`, `declined` or `emergency`.
- `thresholds` are the numbers the gate compared against, so the page draws its lines from the server's values instead of keeping its own copy.
- `decision_reason` is `null` when answered, otherwise `emergency`, `out_of_scope` (judgment below threshold), `low_similarity` (nothing relevant retrieved), `not_in_handbook` (the generator said the passages don't answer it) or `refusal` (the generator declined).
- `passages` is empty for `emergency` and present otherwise, so a decline still shows what retrieval found.
- `prompt` and `model` are `null` when nothing was generated. `answer.text` is always present: the generated answer or a fixed message written by code.
- Errors use `{"error": {"code": "...", "message": "..."}}`: 422 `invalid_question`, 429 `rate_limited` or `spend_cap`, 503 `kill_switch`, 502 `upstream_error`.

### Class endpoint

`POST /v1/chat/completions` and `GET /v1/models` follow OpenAI's shapes so the `openai` package works unchanged. Sampling parameters the Claude API no longer accepts are dropped and reported in an `X-Ignored-Params` header and an `ignored_params` body field. Errors use OpenAI's error JSON: 401 for a bad or revoked token, 429 for rate limits and the spend cap (`insufficient_quota`), 503 for the kill switch, 400 for features not supported yet (`tools`, `n > 1`, images).

## The handbook and how it is searched

The handbook is eight Markdown files in `handbook/`, one per topic. `scripts/ingest.py` turns them into searchable rows:

1. **Chunk.** `app/chunking.py` splits each file at its headings, so a chunk is one topic a patient might ask about ("Saturday hours", "No-show fee"). A section longer than about 350 tokens is split further with one sentence of overlap. The current handbook makes 60 chunks.
2. **Embed.** Each chunk is embedded together with its section title and heading, so a short passage still carries its topic. Only the passage text is stored and shown.
3. **Store.** Rows go into `handbook_chunks` with the name of the embedding model. Ingest inserts the new rows before deleting the old ones, so search never sees an empty table.

`scripts/eval_retrieval.py` checks the result: for every question in `eval/questions.jsonl` the expected section must rank first. It also prints the best similarity for questions that have no answer in the handbook, which is how the similarity threshold was chosen (see DECISIONS D26).

## What exists today

| Piece | File | What it does |
|---|---|---|
| Entry point | `app/main.py` | The routes: demo page, `/api/ask`, `/v1/chat/completions`, `/v1/models`, `/health` |
| Pipeline | `app/pipeline.py` | One question end to end; the gate that decides in code |
| Settings | `app/config.py` | Every env var and model id in one place; `/health` reports which providers have keys, never the keys |
| Thresholds | `app/thresholds.py` | Every number the code decides with, one comment each |
| Data shapes | `app/models.py` | What passes between modules |
| Database client | `app/db.py` | One Supabase client per warm serverless instance |
| Embeddings + search | `app/retrieval.py`, `app/chunking.py` | Voyage over HTTP with retries; vector search in Postgres; the chunker |
| Judgments | `app/judge.py` | Two questions to Jev in one request, with retries |
| Generation | `app/generate.py` | The grounded answer with structured output; plain chat for the class endpoint |
| OpenAI compatibility | `app/openai_compat.py` | Translation between OpenAI's shapes and Anthropic's |
| Access | `app/access.py` | Tokens, limits, the usage log, denial messages |
| Demo page | `app/static/` | Plain HTML, CSS and JavaScript |
| Schema | `supabase/migrations/` | Tables, indexes, the three functions below, row-level security |
| Scripts | `scripts/` | `ingest.py`, `eval_retrieval.py`, `tokens.py`, `student_smoke.py`, `access_checks.py`, `check_migrations.sh` |

## Data model

| Table | Holds | Notes |
|---|---|---|
| `handbook_chunks` | handbook passages and their 512-dim vectors | `embedding_model` is recorded per row: vectors from two models are not comparable |
| `student_tokens` | one row per student API token | only the sha256 hash is stored; the token is shown once when minted |
| `usage_log` | one row per request on either surface | tokens, latency, status, outcome, ignored params, raw judgments, estimated cost |
| `app_settings` | key/value switches | `kill_switch`, `spend_cap_usd` (monthly), `demo_rpm_per_ip` |

Three Postgres functions do the heavy lifting next to the data:

- **`match_chunks(query_embedding, match_count)`** returns the nearest passages with cosine similarity (1 means same direction, 0 means unrelated). An HNSW index backs it.
- **`authorize_request(token_hash)`** allows or denies a class-endpoint request in one round trip, cheapest checks first: kill switch, unknown token, revoked token, monthly spend cap, per-minute limit, per-day limit. Days and months follow class time (Eastern).
- **`authorize_demo_request(ip_hash)`** does the same for the open demo page, per visitor instead of per token: kill switch, monthly spend cap, then requests in the last minute from that address.

## Why limits live in the database

A serverless function keeps no memory between requests; two requests a second apart may run on different machines. The in-memory counter you would write on a laptop silently fails here. So limits are counted from `usage_log` inside Postgres. At class scale that is cheap, and every decision is auditable after the fact.

Two deliberate details: denied requests are logged but don't count toward limits, so retrying doesn't extend a lockout; and the check-then-log sequence is not atomic, so two simultaneous requests at the limit can both pass. Both are acceptable at class scale and worth discussing in Stage 4.

## Security model

- The FastAPI server is the only thing that talks to Supabase, using the secret key. Row-level security is on with no policies, the `anon` and `authenticated` roles have no table grants, and only `service_role` can execute the functions. The publishable key is never used.
- No provider key ever reaches a browser or a student. Students get their own revocable token for the class endpoint.
- The demo page's limit is per IP address, but addresses are never stored. The log holds a keyed hash (HMAC) of each one: there are few enough IPv4 addresses that a plain hash could be reversed by hashing them all.
- Student tokens are hashed with sha256. A fast hash is fine for a long random token; passwords need a slow hash because humans choose them.
- Secrets live in `.env` locally (gitignored) and in Vercel's environment settings in production.

## Data and subprocessors

All data is synthetic. The clinic is fictional and no real patient information exists anywhere in the system. Even so, the app is documented the way a real health app would be:

| Subprocessor | Receives | Purpose |
|---|---|---|
| Anthropic | the question and retrieved handbook passages | answer generation |
| TypeSafe | the question text | emergency and scope judgments |
| Voyage AI | the question text and handbook passages | embeddings |
| Supabase | everything stored in the tables above | database |
| Vercel | request traffic | hosting |

## Local loop and deploy

`uv run uvicorn app.main:app --reload` locally, with keys in `.env`. With no Anthropic or TypeSafe key, development falls back to a local Ollama model for generation and judgments (labeled uncalibrated); production never falls back. Pushing to `main` deploys to Vercel through the GitHub integration; Vercel's FastAPI preset finds `app` in `app/main.py`. Schema changes go through `supabase migration new`, `scripts/check_migrations.sh`, then `supabase db push`.
