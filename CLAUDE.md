# Family Clinic Assistant: working rules

Reference app for the Applied AI Engineer Bootcamp. Students read this code in Stage 4 as a production example, so favor clarity over cleverness.

## Rules
- **Plan before building.** Each phase gets a plan with three sections: reusable components (reused vs new, and why), execution strategy (parallel agents vs sequential, and why), verification (run it, don't describe it: Playwright MCP for UI, tests for logic, a live check of the deployed URL).
- **Supabase:** all DDL goes through migration files in `supabase/migrations/`, checked with `scripts/check_migrations.sh`, applied with `supabase db push` (or the Supabase MCP's `apply_migration`). Never ad hoc DDL.
- **The app talks to Supabase over HTTPS only** (supabase-py / PostgREST). No direct Postgres connections from the serverless function.
- **Thresholds live in one file.** Models return probabilities or text; code decides.
- **Model ids live in `app/config.py`.** Never hard-code them elsewhere.
- **Re-verify version-sensitive APIs** (Anthropic, Vercel, Supabase, Voyage, MCP) against current docs before using them.
- **Style:** minimal comments (a brief header per function/class, inline only for non-obvious logic), no unnecessary docstrings, no em dashes in docs or messages.
- **Git:** never add AI attribution to commits or PRs.
- **Synthetic data only.** No real PHI, ever.
- **Keep `DECISIONS.md` current:** every real decision gets options considered and why, and every breakage gets "what broke and how we fixed it".
- If a loop repeats (especially database connection errors), stop and report instead of retrying.

## Commands
`uv run pytest` · `uv run pytest -m integration` · `scripts/check_migrations.sh` · `uv run ruff check .` · `uv run uvicorn app.main:app --reload`
