# Family Clinic Assistant

> **Fictional clinic. Synthetic data. Not medical advice.**

The reference app for the Applied AI Engineer Bootcamp. It starts as a question-answering assistant over a fictional clinic's patient handbook and grows with the course: retrieval-augmented generation (RAG) first, then agents and MCP, then evals, guardrails and production operations.

**Status:** Phase 0 complete (Nov 10, 2026): database migrated, deployed at https://clinic-assistant-mocha.vercel.app ([/health](https://clinic-assistant-mocha.vercel.app/health)). See [ARCHITECTURE.md](ARCHITECTURE.md) for how it works and [DECISIONS.md](DECISIONS.md) for why it works that way.

## Three models, one job each

| Kind | Job | Model |
|---|---|---|
| Embedding | turns text into vectors so we can find relevant passages | Voyage AI `voyage-4-lite` |
| Judgment | makes a yes/no decision with a calibrated probability | TypeSafe Jev |
| Generative | writes the answer from the passages it is given | Claude Haiku 5.5 |

Code owns every threshold and every decision. Models answer questions; they don't decide what the app does.

## Run it locally

Prerequisites: [uv](https://docs.astral.sh/uv/) and Python 3.13 (`uv` installs it if missing).

```bash
cp .env.example .env        # then fill in the keys
uv sync
uv run uvicorn app.main:app --reload
open http://127.0.0.1:8000/health
```

## Test it

```bash
uv run pytest                    # unit tests, no network, no keys needed
uv run pytest -m integration     # real Supabase + Voyage round trip, needs .env
scripts/check_migrations.sh      # applies migrations to a throwaway Postgres in Docker
uv run ruff check . && uv run ruff format --check .
```

## Database

Schema changes are migration files in `supabase/migrations/`, never ad hoc SQL.

```bash
supabase link --project-ref <project-ref>
supabase migration new <name>    # write the SQL, then:
scripts/check_migrations.sh
supabase db push
```

## Deploy

Pushing to `main` deploys to Vercel. Environment variables live in the Vercel project settings, never in the repo.

## Layout

```
app/            FastAPI app (main.py is the Vercel entry point)
tests/          pytest; HTTP is mocked with respx, integration tests are marked
supabase/       migrations and SQL checks
scripts/        operational scripts
docs/           screenshots and notes
```
