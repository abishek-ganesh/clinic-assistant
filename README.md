# Family Clinic Assistant

> **Fictional clinic. Synthetic data. Not medical advice.**

The reference app for the Applied AI Engineer Bootcamp. It starts as a question-answering assistant over a fictional clinic's patient handbook and grows with the course: retrieval-augmented generation (RAG) first, then agents and MCP, then evals, guardrails and production operations.

**Live:** https://clinic-assistant-mocha.vercel.app. Ask the fictional clinic a question and watch every step: the judgments, the retrieved passages, the answer and the exact prompt.

Three more pieces sit on the same app:

| Piece | Where | What it shows |
|---|---|---|
| Booking agent | https://clinic-assistant-mocha.vercel.app/book | an agent that can look up open times and propose one, but only code books, and only after you press Confirm |
| MCP server | `https://clinic-assistant-mocha.vercel.app/mcp` | the handbook search and the open-slot lookup as tools for any MCP client. [docs/mcp.md](docs/mcp.md) shows how to connect |
| Class tokens | https://clinic-assistant-mocha.vercel.app/token | students enter their email and the class code, get a one-time link by email, and open it to see their own token for the class endpoint |
| Evals and usage stats | https://clinic-assistant-mocha.vercel.app/stats | a public page of usage numbers from the request log. The answer eval behind it runs in CI on every push to `main` |

See [ARCHITECTURE.md](ARCHITECTURE.md) for how it works, [DECISIONS.md](DECISIONS.md) for why it works that way and [DEMO.md](DEMO.md) for how to show it.

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

## The handbook

```bash
uv run python scripts/ingest.py           # chunk, embed and load handbook/*.md
uv run python scripts/eval_retrieval.py   # does the right section rank first for each question?
uv run python scripts/eval_answers.py     # is the final answer right? ten questions, real models, needs .env
```

CI (`.github/workflows/ci.yml`) runs the lint and unit tests on every push and pull request, and `eval_answers.py` on pushes to `main`.

## The class endpoint

Students call the app with the `openai` package and a personal token:

```python
from openai import OpenAI

client = OpenAI(base_url="https://clinic-assistant-mocha.vercel.app/v1", api_key="sk-bootcamp-...")
reply = client.chat.completions.create(
    model="bootcamp", messages=[{"role": "user", "content": "hi"}]
)
```

```bash
uv run python scripts/tokens.py mint --label "Ada Lovelace" --email ada@example.com
uv run python scripts/tokens.py mint --roster roster.csv > tokens.csv   # keep tokens.csv out of the repo
uv run python scripts/tokens.py list
uv run python scripts/tokens.py revoke <prefix or label>
uv run python scripts/tokens.py class-code new     # a new code for the /token page; share it in Slack
uv run python scripts/tokens.py class-code close   # stop issuing tokens

BOOTCAMP_TOKEN=sk-bootcamp-... uv run python scripts/student_smoke.py <base url>   # both surfaces
uv run python scripts/access_checks.py <base url>   # bad token, rate limit, spend cap, revoke
```

## Deploy

Pushing to `main` deploys to Vercel. Environment variables live in the Vercel project settings, never in the repo.

## Layout

```
app/            FastAPI app (main.py is the Vercel entry point); app/static/ holds the four pages
handbook/       the fictional clinic's patient handbook, one Markdown file per topic
eval/           questions with the section that should answer each, and with the facts a right answer needs
tests/          pytest; HTTP is mocked with respx, integration tests are marked
supabase/       migrations and SQL checks
scripts/        ingest, evals, tokens and end-to-end checks
docs/           the MCP guide, recorded MCP runs and screenshots
.github/        the CI workflow
```
