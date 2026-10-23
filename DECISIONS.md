# Decisions

A running log: what we decided, what else we considered, and why. Then what broke and how we fixed it. Newest decisions are appended at the end of each phase.

## Phase 0 (Oct 23, 2026)

### D1. Call it "Family Clinic"
- **Considered:** branded fictional names (Northwind, Bluebird, Juniper Street).
- **Why:** a generic name is obviously fictional, can't collide with a real clinic, and keeps attention on the engineering. A bagel shop called Bagel Shop.

### D2. Its own public repo
- **Considered:** a folder inside the course planning repo; private until Stage 4.
- **Why:** students read this code as a production example. Planning notes are private and stay separate. Public from day one means the history students see is the real one.

### D3. FastAPI on Vercel, deployed by pushing to GitHub
- **Considered:** Render, Railway, Fly, Modal; Vercel CLI deploys.
- **Why:** Vercel auto-detects FastAPI from `app/main.py` and `pyproject.toml`, has a free tier, and push-to-deploy is the first deployment story students should see.
- **Teaching angle:** serverless means no memory between requests, which shapes D13.

### D4. Supabase over HTTPS, never a direct Postgres connection
- **Considered:** psycopg/asyncpg with a connection pooler.
- **Why:** a serverless function starts and stops constantly; direct connections exhaust the pool and produce connection-error loops. PostgREST over HTTPS is stateless. Heavy logic runs in Postgres functions called as RPCs.

### D5. Schema changes only through migration files
- **Considered:** clicking in the dashboard; running SQL by hand.
- **Why:** migrations are reviewable, repeatable and testable (see D23). The dashboard is for looking, not changing.

### D6. Three models, one job each
- **Considered:** one LLM doing everything (deciding, retrieving, writing).
- **Why:** a judgment model returns calibrated probabilities you can threshold; a generative model writes fluent text but its stated confidence isn't calibrated. Splitting the jobs makes each one testable and makes the decision logic plain code.

### D7. Voyage AI `voyage-4-lite` for embeddings, 512 dimensions
- **Considered:** Supabase's built-in `gte-small`; OpenAI `text-embedding-3-small`.
- **Why:** Anthropic has no embeddings API and points to Voyage. `gte-small` runs only inside Supabase Edge Functions (Deno/TypeScript), which would add a second language and an extra network hop per question, and it truncates at 512 tokens. Voyage costs $0.02 per million tokens after 200 million free ones, so this app never pays. Voyage also sells the rerankers Phase 2 needs. The cost is one more vendor, which is itself a governance teaching point.
- **512 dimensions:** a quarter the size of the default 1024 with negligible quality loss at handbook scale.

### D8. Record which model made each vector
- **Why:** vectors from two embedding models live in different spaces and can't be compared. Each row stores `embedding_model`; changing models means re-embedding everything. This is also why there is no local fallback for embeddings.

### D9. Claude Haiku 5.5 with low effort and structured output
- **Why:** cheap (about $0.0003 per answer), fast, good at grounded answers. Effort defaults to `medium` on Haiku 5.5, so it is set to `low` explicitly. Structured output returns `{answer, citations, answerable}` as validated JSON.
- **Gotcha worth teaching:** since Oct 21, 2026 the Claude API rejects `temperature`, `top_p` and `top_k`. The class endpoint accepts them from students and reports that they were ignored.

### D10. Jev over plain HTTP
- **Why:** there is no Python SDK, and an HTTP contract with the base URL in an env var lets a self-hosted compatible server replace Jev later. Request shape and retry policy are ported from a production client, not guessed.

### D11. Emergency threshold 0.35, not 0.5
- **Why:** the costs are asymmetric. A false positive shows a safety message to someone who didn't need it. A false negative is the failure the guardrail exists to prevent. Raw probabilities are stored so the number can be tuned from real traffic.

### D12. A fallback ladder to local Ollama
- **Why:** mirrors the course's model ladder. Missing Anthropic key: generation via local Ollama. Missing TypeSafe key: judgments via Ollama, labeled uncalibrated. Production never falls back silently; `/health` reports missing keys.

### D13. Rate limits and spend cap counted in Postgres
- **Considered:** an in-memory counter; Redis.
- **Why:** serverless keeps no memory (D3), and Redis would be another service. Counting `usage_log` rows in one Postgres function is cheap at class scale and leaves an audit trail.

### D14. Limits: 20/min and 300/day per token, 10/min per IP on the demo page, $10/month cap, kill switch
- **Why:** worst case with 30 students at the daily cap is about $3/day; realistic use is under $5/month.

### D15. The demo page is open
- **Considered:** a token in the URL.
- **Why:** shareable with anyone; the per-IP limit, the spend cap and the kill switch bound the downside.

### D16. A `bootcamp` model alias
- **Why:** students write `model="bootcamp"` once; the backing model can change without anyone editing a notebook.

### D17. Streaming in Phase 1, after the non-streaming path
- **Why:** students will try `stream=True`. Getting non-streaming right first keeps the translation layer testable.

### D18. uv, Pydantic v2, pytest, respx, ruff
- **Why:** current Python practice. `uv` replaces pip, venv and pyenv; `respx` mocks HTTP so unit tests need no network or keys.

### D19. Separate keys for this app
- **Why:** spend is visible on its own, and a leaked key is revoked without touching anything else.

### D20. Synthetic data only, documented like a real health app
- **Why:** no real patient information ever enters the system, and the subprocessor list in ARCHITECTURE.md is written anyway. That is the Stage 4 governance lesson.

### D21. Python 3.13 instead of 3.12
- **Why:** Vercel supports 3.12, 3.13 and 3.14, and the dev machine already had a native 3.13. Nothing in the stack needs 3.12.

### D22. Voyage over plain HTTP, not its SDK
- **Considered:** the `voyageai` package.
- **Why:** the SDK resolves to about 55 packages (numpy, tokenizers, huggingface-hub, langchain-core, pillow) for what is one POST request. In a serverless function every dependency is bundle size and cold-start time. `embed()` is about 40 lines with retries.
- **Teaching angle:** check what a dependency drags in before adding it. `uv pip compile` shows the tree in a second.

### D23. Test migrations in a throwaway Postgres, not the full local Supabase stack
- **Considered:** `supabase start` (about a dozen containers).
- **Why:** one pgvector container plus a few lines that recreate Supabase's roles is enough to apply the migration and assert every outcome of `authorize_request`. It takes seconds and runs anywhere Docker runs.

### D24. Denied requests don't count toward limits; check-then-log is not atomic
- **Why:** if denials counted, a student retrying in a loop would never get unlocked. The race between the check and the log write lets two simultaneous requests pass at the limit; at class scale that's harmless, and making it atomic is a Stage 4 discussion (advisory locks, or counting inside one transaction).

### D25. Days and months follow Eastern time
- **Why:** class runs 8-11pm ET. With UTC days, the daily cap would reset at 7 or 8pm Eastern, in the middle of a session.

## What broke and how we fixed it

### Phase 0
- **The wrong `uv` ran.** `which uv` found an old Intel build inside Anaconda, which ran under Rosetta and offered only Intel Pythons. The native Homebrew `uv` was installed but shadowed on the PATH. Fix: call `/opt/homebrew/bin/uv` and move Homebrew ahead of Anaconda on the PATH. Lesson: when tooling behaves oddly, check `which` and `file` before anything else.
