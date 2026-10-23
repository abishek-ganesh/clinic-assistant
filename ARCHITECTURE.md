# Architecture

*As of Phase 0 (Oct 2026). Sections marked **Phase 1** describe what is being built next.*

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
 GET /   POST /api/ask         POST /v1/chat/completions   GET /v1/models      (Phase 1)
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

## One question, end to end (Phase 1)

```
question
  +--> (in parallel) Jev: is_emergency p, in_scope p
  +--> (in parallel) embed the question -> match_chunks() -> top 4 passages
  |
  gate (code):
    emergency p >= 0.35                          -> fixed "call 911" response, no generation
    in_scope p < 0.25 or best similarity < 0.30  -> "I can't find that in the handbook"
    otherwise                                    -> generate
  |
  Haiku: structured output {answer, citations, answerable}
  |
  usage_log row: tokens, latency, outcome, raw probabilities
```

Judgment and retrieval run in parallel because nothing reaches the user until the gate runs. Safety is identical and the response is faster: a latency budget is the slowest branch, not the sum. Raw probabilities are stored on every request so thresholds can be re-tuned later from real traffic.

## What exists today (Phase 0)

| Piece | File | What it does |
|---|---|---|
| Entry point | `app/main.py` | FastAPI app; `/` placeholder and `/health` |
| Settings | `app/config.py` | Every env var and model id in one place; `/health` reports which providers have keys, never the keys |
| Database client | `app/db.py` | One Supabase client per warm serverless instance |
| Embeddings + search | `app/retrieval.py` | `embed(texts, input_type)` calls Voyage over HTTP with retries; `match_chunks()` runs vector search in Postgres |
| Schema | `supabase/migrations/` | Tables, indexes, the two functions below, row-level security |
| Migration checks | `scripts/check_migrations.sh` | Applies every migration to a throwaway Postgres in Docker and asserts behavior |

## Data model

| Table | Holds | Notes |
|---|---|---|
| `handbook_chunks` | handbook passages and their 512-dim vectors | `embedding_model` is recorded per row: vectors from two models are not comparable |
| `student_tokens` | one row per student API token | only the sha256 hash is stored; the token is shown once when minted |
| `usage_log` | one row per request on either surface | tokens, latency, status, outcome, ignored params, raw judgments, estimated cost |
| `app_settings` | key/value switches | `kill_switch`, `spend_cap_usd` (monthly), `demo_rpm_per_ip` |

Two Postgres functions do the heavy lifting next to the data:

- **`match_chunks(query_embedding, match_count)`** returns the nearest passages with cosine similarity (1 means same direction, 0 means unrelated). An HNSW index backs it.
- **`authorize_request(token_hash)`** allows or denies a class-endpoint request in one round trip, cheapest checks first: kill switch, unknown token, revoked token, monthly spend cap, per-minute limit, per-day limit. Days and months follow class time (Eastern).

## Why limits live in the database

A serverless function keeps no memory between requests; two requests a second apart may run on different machines. The in-memory counter you would write on a laptop silently fails here. So limits are counted from `usage_log` inside Postgres. At class scale that is cheap, and every decision is auditable after the fact.

Two deliberate details: denied requests are logged but don't count toward limits, so retrying doesn't extend a lockout; and the check-then-log sequence is not atomic, so two simultaneous requests at the limit can both pass. Both are acceptable at class scale and worth discussing in Stage 4.

## Security model

- The FastAPI server is the only thing that talks to Supabase, using the secret key. Row-level security is on with no policies, the `anon` and `authenticated` roles have no table grants, and only `service_role` can execute the functions. The publishable key is never used.
- No provider key ever reaches a browser or a student. Students get their own revocable token for the class endpoint.
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

`uv run uvicorn app.main:app --reload` locally, with keys in `.env`. Pushing to `main` deploys to Vercel through the GitHub integration; Vercel's FastAPI preset finds `app` in `app/main.py`. Schema changes go through `supabase migration new`, `scripts/check_migrations.sh`, then `supabase db push`.
