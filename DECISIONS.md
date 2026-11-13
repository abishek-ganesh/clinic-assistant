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

## Phase 1 (Nov 11, 2026)

### D26. Similarity floor 0.45, measured rather than guessed
- **Considered:** the 0.30 we planned before any data existed.
- **Why:** `scripts/eval_retrieval.py` showed answerable questions scoring 0.52 to 0.69 on their best passage and unanswerable ones 0.14 to 0.37. At 0.30, "Can you recommend a dermatologist?" (0.37) would have reached the generator. 0.45 sits in the gap.
- **Caveat:** this rests on 24 questions written as full sentences. Terse real questions will score lower, so the stored similarities in `usage_log` are what to re-tune from.
- **Teaching angle:** a threshold is a claim about your data. Similarity scores are not comparable across embedding models, so the number has to be measured again if the model changes.

### D27. Embed each chunk with its section title and heading
- **Why:** a chunk such as "Saturday 9:00 am to 1:00 pm, Main Street only" carries little meaning alone. Embedding "Hours and locations: Saturday hours" in front of it gives the vector its topic. Only the passage text is stored and shown.
- **Not measured:** we did not run the eval without the prefix. That comparison is a good Phase 2 exercise.

### D28. Three layers decide whether to answer, cheapest first
- **What:** the judgment model's scope probability, then the similarity floor, then the generator's own `answerable` flag.
- **Why:** each catches what the others miss. The dosing question fails on scope (0.10). "Do you offer dental cleanings?" sounds in scope (0.53) but nothing relevant is retrieved (0.31). A question that retrieves plausible passages which don't actually answer it is caught only when the generator reads them.
- **Detail:** when the generator says the passages don't answer the question, the user sees the same fixed decline message as the other two cases, not the model's wording. A decline still shows the retrieved passages.

### D29. The demo page gets its own limit function, and IP addresses are stored as keyed hashes
- **Considered:** counting demo requests from Python; storing a plain sha256 of the IP.
- **Why:** the same reasoning as D13, one round trip next to the data. A plain hash of an IPv4 address can be reversed by hashing all four billion of them, so the log stores an HMAC with a server-only key.

### D30. Token prefix is 18 characters
- **Why:** every token starts with the same 12 characters (`sk-bootcamp-`), so a 12-character prefix would identify nothing. The stored prefix adds 6 random characters: enough to find a student's token when they paste the start of it, far too few to guess the rest.

### D31. The usage log write is awaited, and a failed limit check refuses the request
- **Considered:** logging in a background task; letting requests through when the database is unreachable.
- **Why:** limits are counted from `usage_log`, so the row has to exist before the next request is checked. And if the check itself fails, serving the request would mean spending money nobody is counting. The reverse case is deliberate too: a failed log write after a successful answer does not turn the answer into an error.

### D32. Unsupported sampling parameters are dropped and reported, not rejected
- **Why:** students will copy `temperature=0.7` from every tutorial. Rejecting it would break their first call for a reason unrelated to what they are learning; dropping it silently would teach something false. The response says what was ignored in a header and a body field.
- **Not reported:** `stop`, `response_format`, `user` and `stream_options` are accepted and ignored without a note. `tools`, `n > 1` and images are rejected with a clear 400.

### D33. A streaming error after the first byte travels as an event
- **Why:** once a stream has started, the 200 status is already sent and cannot become a 502. The error is sent as a final event in OpenAI's error shape, which the `openai` package raises as an exception.

### D34. Tests fake the Anthropic client instead of mocking HTTP
- **Why:** the Anthropic SDK (2.x) is built on `httpx2`, which `respx` cannot intercept. Jev, Voyage and Ollama are called with plain `httpx`, so those are mocked at the HTTP layer; the Anthropic client is replaced with a small fake.

## Thin slices (Nov 2026)

Three thin slices built on Nov 11, 2026, one per later stage of the course: a booking agent with human confirmation (D36 to D46), an MCP server (D47 to D54), and an answer eval in CI with a usage stats page (D55 to D63). D35 is the scaffolding they share.

### D35. One per-visitor limit across every open surface
- **Considered:** a separate per-minute counter for each open surface (the demo page, the booking agent, the MCP server).
- **Why:** a visitor is one source of cost, whichever door they come in through. Three counters would give one address three allowances. One shared count also needed no new function: migration `20261111200000_open_surfaces.sql` widens the `usage_log.surface` check to allow `booking`, `mcp` and `eval`, and `authorize_demo_request` now counts every row with the visitor's address hash instead of only demo rows.
- **Consequence:** ten requests a minute is the total for one address. A booking conversation and a few Ask questions share it.

### D36. The model proposes, code books, a person confirms
- **Considered:** letting the model call `book_slot` whenever it judges the patient agreed; asking "shall I book it?" in text and trusting a typed "yes".
- **Why:** booking is a side effect, and the app's rule is that code owns every side effect. A typed "yes" is just more text the model interprets, and text can be injected or misread. A button press that returns a server-signed token is something code can verify without interpreting anything.
- **Teaching angle:** this is the human-in-the-loop pattern in its smallest form. In the real-model runs, asked to "skip the buttons and call book_slot", the model either proposed anyway or tried and got an error result. Nothing was booked either way.

### D37. `book_slot` stays a model tool, guarded by the signature (superseded by D66)
- **Considered:** having the Confirm click book directly in code with no model call.
- **Why:** keeping it a tool shows students the part that matters about agents: the model can ask for anything, and the tool's own code decides whether it runs. It also lets the model write the confirmation message and recover gracefully when the slot was just taken.
- **Cost:** a Confirm click costs one or two model calls, and if the model ever failed to call `book_slot` after a Confirm the patient would have to press again. In repeated real-model runs it called it every time. Booking directly in code on Confirm would be the more deterministic choice for a real product and is a fair class discussion.

### D38. A signed, expiring confirmation instead of server-side state
- **Considered:** storing pending proposals in a table; a session cookie.
- **Why:** the function is serverless and keeps no memory (D3, D13). A proposal table would need cleanup and a second round trip. An HMAC over the slot id and an expiry lets any instance verify a confirmation with no storage at all. Ten minutes is long enough to read the card and short enough that the slot list has not drifted far.
- **Not done:** the signature is not bound to the visitor's address, because phones change address mid-conversation. Replays are harmless because of D41.

### D39. The browser holds the conversation as plain text turns
- **Considered:** sending back the full message list with tool calls, tool results and thinking blocks.
- **Why:** simple to read, simple to validate, and nothing in it can cause a side effect. It also avoids replaying thinking blocks across requests, which Haiku 5.5 only accepts when nothing before them changed.
- **Consequence:** slot ids do not survive between turns. The system prompt says so, and `propose_slot` only accepts a slot that `check_availability` returned in the same turn, so every proposal is grounded in a fresh look at the schedule.

### D40. Availability is computed, not stored
- **Considered:** seeding a slots table for the next few months.
- **Why:** a seeded table goes stale and needs a refill job. Generating slots from weekly blocks with `generate_series` is a dozen lines of SQL and always starts tomorrow. Only bookings are rows.
- **Detail:** the functions run with `set timezone = 'America/New_York'`, so "tomorrow", weekdays and clock times mean clinic time wherever the server is. The date window and display text come from Postgres, so Python never does timezone math on slots.

### D41. The database prevents double-booking
- **Considered:** check availability in Python, then insert.
- **Why:** two visitors can pass the same check a millisecond apart. `unique (provider, starts_at)` makes the second insert fail, and `book_slot` turns that into a clean `slot_taken`. Each provider's blocks never overlap, so a start time identifies a slot and one constraint is enough.

### D42. Block scheduling for visit types
- **Considered:** a 20 minute grid where a physical takes two adjacent slots.
- **Why:** two-slot bookings need an overlap check that a simple unique constraint cannot express. Real clinics also block their days (physicals first thing, video visits late afternoon), so the simpler model is the realistic one.

### D43. No patient details, and a per-visitor booking cap
- **Why:** the page is public and the data must stay synthetic, so an appointment is a provider, a time, a visit type and a code. Because anyone can book, one visitor could fill the two-week schedule; `booking_max_per_ip_per_day` (5, in `app_settings`) limits that on top of the shared per-minute request limit (D35).
- **Not done:** there is no cancel tool, and old appointments are never deleted. Past rows cost nothing because the schedule only looks forward.

### D44. Code writes the proposal text and the fixed messages
- **Why:** when `propose_slot` ends the turn there may be no model text at all, and the wording next to a Confirm button should not vary. The page labels these turns "Written by code" with a dashed border, the same visual language the Ask page uses for a declined answer.

### D45. In the booking agent, emergencies and medical questions are handled by the system prompt only
- **Considered:** running the Jev emergency judgment in front of the agent, as `/api/ask` does.
- **Why (for now):** this slice was kept thin. In real-model runs the agent pointed chest pain to 911 and declined a dosing question without proposing anything. It is still a prompt, not a threshold: putting the same judgment gate in front of the booking agent is the consistent next step.

### D46. Low effort and no strict tool schemas for the agent
- **Why:** with `effort: low` Haiku 5.5 completed every scenario in two or three model calls. Tool inputs are validated in Python instead of `strict: true`, because the validation code is itself the lesson: tool arguments are model output.

### D47. The official MCP Python SDK, version 2 (`MCPServer`), not hand-rolled JSON-RPC
- **Considered:** writing the three JSON-RPC methods by hand (about 60 lines); the third-party FastMCP package.
- **Why:** the SDK turns a typed Python function into a tool with a JSON schema, validates arguments, and already speaks both protocol generations (the handshake one and the 2026-07-28 revision, which makes every request self-contained). Hand-rolling would teach the wire format but freeze us on one revision.
- **Gotcha worth teaching:** SDK 2.x renamed `FastMCP` to `MCPServer` (`from mcp.server.mcpserver import MCPServer`). Most tutorials and most model training data still show the old import, which now raises with a pointer to the migration guide. Check the installed version before trusting an example.

### D48. Streamable HTTP, stateless, JSON responses
- **Considered:** stateful sessions with an event stream (the SDK default); the older SSE transport.
- **Why:** serverless keeps no memory between requests (D3, D13), so a session id handed out by one instance means nothing to the next. Stateless JSON makes each POST self-contained. The cost is no server-to-client messages, which two read-only tools don't use.

### D49. A session manager per request, not one started in the app's lifespan
- **The problem:** the SDK's HTTP handler needs its session manager "running" before it serves a request. Standalone, the SDK starts it in its own app's lifespan. Mounted inside another app it does not start, because Starlette never runs a mounted sub-app's lifespan, and the first request fails with `RuntimeError: Task group is not initialized`. The documented fix is a lifespan on the host app that enters `mcp.session_manager.run()`.
- **Considered:** (1) the documented host-lifespan pattern; (2) starting the manager lazily on the first request and keeping it for the life of the instance; (3) building and starting a manager for each request.
- **Chose (3). Why:**
  - It works whether or not lifespan events run on the host. Vercel's changelog says FastAPI lifespan events have been supported since December 9, 2025, but older community threads report otherwise and we did not test it ourselves.
  - A manager's `run()` is single-use. With (1), anything that enters the app's lifespan twice in one process (two `with TestClient(app)` blocks in a test suite) raises on the second entry.
  - With (1) a failure during startup takes down every route, not just `/mcp`. With (3) the MCP endpoint is isolated.
  - In stateless mode the manager holds no sessions, so there is nothing worth keeping between requests. Starting one costs a task group. Measured in-process: about 3 ms for a whole `tools/call` with stubbed work, and 40 concurrent calls through the real `main.app` each got their own answer.
  - (2) keeps a task group alive across requests on a platform that freezes and thaws instances. That is the kind of hidden state serverless punishes.
- **Cost:** it is not the pattern in the SDK's docs, and `StatelessEndpoint` calls `mcp.streamable_http_app()` only to get a fresh manager, discarding the Starlette app it returns. Both are explained in the class's comments. If we ever want stateful sessions this has to change, but so would the hosting.
- **Wiring consequence:** `main.py` needs one line (`app.router.routes.extend(mcp_routes)`) and no `lifespan=` argument.
- **Sources:** MCP Python SDK v2.3.0 docs `docs/run/asgi.md` ("Mounting disables the built-in lifespan") and `docs/troubleshooting.md` ("Task group is not initialized"); https://vercel.com/changelog/fastapi-lifespan-events-are-now-supported-on-vercel; https://vercel.com/docs/frameworks/backend/fastapi ("Startup and shutdown").

### D50. Plain routes at `/mcp` and `/mcp/`, POST only, instead of `app.mount`
- **Considered:** `app.mount("/mcp", mcp.streamable_http_app(streamable_http_path="/"))`.
- **Why:** a mount answers the bare `/mcp` by redirecting to `/mcp/`, and a 307 on a POST breaks some MCP clients. Two exact-path routes answer both spellings directly and cannot shadow the other routes or `/static`.
- **POST only:** in stateless mode the SDK still answers `GET /mcp` by opening an event stream that it never writes to and never closes. On serverless that would hold a function open until it times out, for nothing. The MCP spec says a server that offers no stream answers GET with 405, which is what a POST-only route does. `DELETE` (end a session) gets 405 too: there is no session.

### D51. The MCP Host/Origin check is on locally and off in production
- **What it is:** the SDK's DNS-rebinding protection rejects a request whose `Host` header is not on an allowlist (421) or whose `Origin` is not (403). With no configuration it allows only localhost, so behind a real hostname it rejects everything.
- **Considered:** (1) allowlist the production hostname; (2) build the allowlist from Vercel's system environment variables to cover preview URLs; (3) switch the check off in production.
- **Chose (3), keyed on `APP_ENV=production`. Why:** the attack is a web page tricking your browser into calling a server on your own machine or private network. It matters for local runs, where the check stays on with the localhost allowlist. In production the server is public, takes no cookies or credentials, and Vercel's proxy only routes requests whose Host is one of the project's domains. An allowlist there protects nothing and breaks the first time someone adds a custom domain or opens a preview URL. The SDK docs call switching it off "the honest configuration" behind a proxy that controls the Host header.
- **Watch for:** a Vercel environment where `APP_ENV` is not `production` (a preview deployment, if the variable is only set for Production) will answer every MCP request with `421 Invalid Host header`.
- **Source:** MCP Python SDK v2.3.0 `docs/run/deploy.md` ("Before anything else: the Host allowlist").

### D52. MCP limits are checked per tool call, inside the tool, by one helper
- **Considered:** an HTTP dependency on the route (counts every POST); the SDK's server middleware hook (one place for all tools).
- **Why:** a client makes two or three protocol requests (`initialize`, `notifications/initialized`, `tools/list`) before its first tool call. Counting those against a 10-per-minute limit would lock people out for connecting. The SDK's middleware hook is marked provisional. So each tool passes its work to `metered(tool, request, work)`, which mirrors `/api/ask` step for step: check, deny and log, run, log. It fails closed: if the check itself errors, the call is refused.
- **Trade-off:** a new tool is only limited if its author remembers to call `metered`. With two tools that is easy to see in review.

### D53. MCP denials and failures are tool errors, with fixed messages
- **Considered:** HTTP 429/503 from the route; JSON-RPC protocol errors.
- **Why:** the model reads tool results, not HTTP statuses. `ToolError(message)` becomes a result with `isError: true`, so the model can say "you've hit the rate limit, try again in a minute". The messages reuse `DENIALS` from `app/access.py`. Internal failures get one fixed message; the exception text goes to the server log only.

### D54. Tool descriptions are written for the model, results are typed, and there is no booking tool
- **Why:** the description and input schema are the whole interface a model sees. Each description says what the tool covers, what comes back, how to read it (a similarity under about 0.45 means "probably not in the handbook") and what the tool cannot do. Arguments carry their own descriptions and limits (`question` is 1 to 500 characters, reusing `MAX_QUESTION_CHARS`). Return types are Pydantic models, so the SDK publishes an output schema and sends structured content alongside the text. Both tools are annotated `readOnlyHint`, which clients use to decide whether to ask before calling.
- **`search_handbook` returns four fields, not the full `Passage`:** `rank` is the list order and `section_slug` is an internal id. Fewer fields means fewer tokens in the client's context.
- **No booking tool:** the same rule as D36. Booking needs a person to confirm it, and this server has no way to ask, so it does not offer the action.

### D55. Grade answers with deterministic checks, not a model
- **Considered:** an LLM grader with a rubric; exact string match against a reference answer; embedding similarity to a reference answer.
- **Why:** the handbook is fixed, so the facts a right answer must contain are known in advance and can be checked with a pattern. Code checks are free, instant, repeatable and explainable: a failure names the missing fact. Exact match fails on harmless rewording. Similarity gives a number with no obvious threshold and scores "3 business days" and "5 business days" as nearly the same. A model grader needs its own eval before it can be trusted.
- **Teaching angle:** start with the cheapest check that could catch the failure. Reach for a model grader only for what code cannot judge (tone, completeness, faithfulness), and then measure the grader.

### D56. The eval's pass bar is 10 of 10
- **Considered:** 9 of 10, to tolerate model variation.
- **Why:** the checks ask only for what any correct answer must have, and the patterns already absorb rewording, so a miss means behavior changed. With ten questions, a 90% bar lets exactly one failure through, and that one could be the emergency question or the dosing question. Six consecutive live runs passed 10 of 10 once the checks matched the questions (see "What broke").
- **Cost of this choice:** a provider outage turns the job red even though the code is fine. That is acceptable: the eval is an alarm, a re-run is one click, and the failure text ("pipeline raised ...") says which kind of red it is.

### D57. Facts are regular expressions stored next to the question
- **Considered:** lists of acceptable substrings; normalizing times and money in code before comparing.
- **Why:** one small pattern per fact covers "9 AM", "9:00 am" and "9 a.m." without a list of every spelling. The answer is lowercased, dashes are straightened and whitespace is collapsed before matching, so the patterns stay short. Each fact has a plain-language label, which is what a failure prints.
- **Guard:** a unit test runs every pattern against the handbook file it came from. If the handbook changes and a fact goes stale, the unit test fails before any model is called.

### D58. The eval calls the pipeline in-process, not the deployed URL
- **Considered:** HTTP requests to the live `/api/ask`.
- **Why:** the eval should test the code in this commit. On a push, the deployed site is still the previous commit for a minute or two. In-process also skips the per-IP rate limit, which would otherwise throttle ten quick questions from one CI runner. The trade: routes, access checks and Vercel itself are not exercised (`scripts/student_smoke.py` and `scripts/access_checks.py` cover those against a URL).
- **Consequence:** in-process calls skip the spend cap check too, so the eval keeps running after the cap is hit. At about a tenth of a cent per run that is acceptable, and the rows are logged so the spend is visible.

### D59. Eval runs are logged as their own surface
- **Why:** CI spends real money on every push. Logging it as `surface = 'eval'` makes it count toward the cap and show up on the stats page, where it can be told apart from visitors. It also keeps eval questions out of the demo's decline rate.

### D60. The eval job runs on pushes to `main` only, and actions are pinned to commits
- **Considered:** running the eval on pull requests; `pull_request_target`; version tags such as `@v7`.
- **Why:** secrets are not given to pull requests from forks, and the workarounds that do give them are a well-known way to leak keys. Pinning to a commit hash means the code that runs with the keys cannot change unless this file changes.
- **Versions checked Nov 11, 2026:** `actions/checkout` v7.0.1, `astral-sh/setup-uv` v10.3.3.
- **The eval is an alarm, not a gate:** Vercel deploys on the same push and does not wait for GitHub Actions. Making the eval block a deploy is a production-unit topic.

### D61. Stats are computed in one Postgres function
- **Considered:** selecting rows and aggregating in Python; one query per number; a materialized view refreshed on a schedule.
- **Why:** the same reasoning as D4 and D13. One round trip over HTTPS, and the counting happens next to the data, where percentiles and time zones are one line each. Pulling rows would move every request's metadata across the network to throw most of it away, and would put identifying columns in the app's memory on a public route. A materialized view is the right next step when the table is large; at class scale the function is cheap.
- **Privacy by construction:** the function has no way to return a row, so the public page cannot leak one. A check in `supabase/tests/checks_stats.sql` asserts that labels, token hashes, IP hashes and decline reasons planted in the test data do not appear in the output.

### D62. The stats response is cacheable for 60 seconds
- **Considered:** no caching; a rate limit like the demo page's.
- **Why:** the page is public and unauthenticated, and every uncached load is a database query. A `Cache-Control` header lets browsers and the CDN reuse a response, which caps the query rate without another limit function. Numbers that are a minute old are fine for a usage page.

### D63. No chart library on the stats page
- **Considered:** Chart.js or similar from a CDN.
- **Why:** the page has one bar chart and two ranked lists. Bars are `div`s with a height; ranked lists are tables with a bar cell. That is a small amount of JavaScript students can read, no build step, no third-party script, and the tables are their own text alternative. Bars use one color throughout: the rows are named, so color has nothing to add.
- **Design:** same header, tabs, tokens, spacing scale, `.card` and `.eyebrow` as the Ask page. `stats.css` holds only what this page adds.

### D64. Students request their own token with a class code
- **Considered:** the instructor minting one token per student from a roster and sending each by direct message; an open form that gives a token to any email; an allowlist of roster emails; picking your name from a class list.
- **Why:** enrollment changes until the first week, so a roster is always out of date. An open form is an open endpoint. An allowlist is the roster again. A name list proves nothing, since anyone can pick any name, and it publishes the class list. The class code is shared in the class Slack, so knowing it shows you are in the class.
- **Controls:** only the code's hash is stored; wrong guesses count toward the per-visitor limit; the instructor can change the code or close sign-ups in one command; every token keeps its own per-minute and per-day limits and can be revoked.

### D65. The token is reached through an emailed one-time link
- **Considered:** showing the token on the page as soon as the class code matched (the first version, live for a few hours on Nov 11); emailing the token itself.
- **Why:** with the code alone, the email was only a label. Anyone in the class could request a token under a classmate's address, which also turned the classmate's token off. A link sent to the inbox proves the address, so the list of who holds which token can be trusted. Emailing the token itself would leave a long-lived secret sitting in an inbox; a link that works once for 15 minutes does not.
- **Details that matter:** the link's secret rides after the `#`, so it stays out of server logs. Redeeming takes a button press, so a mail scanner that opens links cannot spend it. The link's address comes from configuration, not from the request's `Host` header. A new request does not turn off the current token until its link is redeemed. One address gets at most three links an hour.
- **Cost:** a second step, a new provider (Resend) that receives student email addresses, and a student whose message lands in spam is stuck until they find it. This is also the first real personal data in an app that otherwise holds only synthetic data, so the page says what the address is used for.

### D66. Confirm books in code; the model has no booking tool (replaces D37)
- **Considered:** keeping D37, where a Confirm press went back through the model and the model called a guarded `book_slot` tool.
- **Why:** in D37 the side effect still depended on the model choosing to call a tool after the person had already decided. Now the person's decision goes straight to code: `agent.confirm_turn` checks the signature, books, and writes the reply, with no model call. The model can look and propose and nothing else, so no prompt, however clever, can reach the booking code. A Confirm press is also faster and free.
- **What the model still does:** if the slot went in the meantime, code tells the model why in place of the confirm turn and it gets one normal turn to offer other times. A forged or expired confirmation gets a fixed message written by code.
- **Teaching angle:** "the human approves, code acts." The safest tool is the one the model was never given. Checked against the real model (Nov 13): asked to "skip the buttons and book", it proposed instead.

### D67. A cleaner look: the patient's view beside an "Under the hood" panel
- **Considered:** four directions mocked up side by side (a clean app, a patient view with an inspector panel, a soft clinic look, a sidebar dashboard). The old pages read like slides: a big gradient title band on every page, uppercase letter-spaced labels everywhere, a numbered step rail and long paragraphs.
- **Chosen (Nov 13):** Ask and Book show what a patient sees on the left and a dark "Under the hood" panel on the right (judgments, passages and prompt; tool calls and which steps code ran). Stats and Token use the plain single-column style. Ask uses a rounded search box. One white header on every page. Same colors and fonts as the slide decks; sentence case; long explanations moved behind "What is...?" disclosures instead of deleted.
- **Teaching angle:** the split is the lesson in one picture: the product on one side, the machinery on the other. On Book the proposal and the booking are now cards inside the conversation, and the panel colors each step by who ran it (model or code).

## What broke and how we fixed it

### Phase 0
- **The wrong `uv` ran.** `which uv` found an old Intel build inside Anaconda, which ran under Rosetta and offered only Intel Pythons. The native Homebrew `uv` was installed but shadowed on the PATH. Fix: call `/opt/homebrew/bin/uv` and move Homebrew ahead of Anaconda on the PATH. Lesson: when tooling behaves oddly, check `which` and `file` before anything else.

### Phase 1
- **Blank lines in `.env` overrode defaults.** `JEV_BASE_URL=` with nothing after it was read as an empty string, not as "use the default", so the Jev client would have called a URL with no host. Fix: `env_ignore_empty=True` in the settings class. Lesson: an unset variable and an empty one are different things, and config code has to choose what empty means.
- **The planned similarity threshold was wrong.** 0.30 was written down before any data existed and would have let near-scope questions through. Fix: measure, then choose (D26).
- **The plan assumed Anthropic SDK 1.x; 2.8 was current.** Sampling parameters are gone from the method signatures entirely, and a truncated structured output raises a pydantic error from inside the SDK rather than an API error. Fix: read the installed package before writing against it, and treat every failure at the route boundary as an upstream error.

### Thin slices
- **`usage_log` rejected every row from the new surfaces, silently.** In the local runs the MCP tool calls and the eval both worked, but each insert failed with `violates check constraint "usage_log_surface_check"`: migration `20261111200000_open_surfaces.sql` had not been applied to the database yet. `log_usage` swallows insert failures by design (D31), so nothing looked wrong from the client. The consequence is worse than a missing log line: the per-minute limit counts `usage_log` rows, so until the migration was applied MCP calls were not rate limited at all. Fix: apply the migration before deploying the code that needs it. The eval script also counts failed log writes and prints one line saying its spend is not being counted. Lesson: a limit that counts log rows is only as good as the log write, and a swallowed error still needs to be visible somewhere.
- **A repeated Confirm made the model retract a real booking.** Replaying the same confirmation correctly booked nothing new, but `book_slot` answered `slot_taken` and the model, which sees earlier turns only as text, told the patient the first booking had not happened. Fix in the database: when the same visitor books the same slot again, `book_slot` returns the original booking. A retry after a lost response is now idempotent.
- **A check asked for more than the question did.** "How do I get a copy of my medical records?" required the fact "within 30 days". The model answered the question that was asked (fill out the records request form, on the portal or at the front desk) and left out the turnaround in two of three runs. The app was right and the check was wrong: the fact was replaced with one the question actually calls for (the form is on the patient portal). Lesson: a required fact must be something every correct answer contains. Otherwise the eval is flaky, people learn to ignore red, and it stops being an alarm.
- **The import everyone knows no longer exists.** `from mcp.server.fastmcp import FastMCP` raises in MCP SDK 2.x. Found by checking the installed version (2.3.0) and its docs before writing anything. Fix: `MCPServer` (D47).
- **Creating the MCP server turned on INFO logging for the whole app.** `MCPServer()` calls `logging.basicConfig(level="INFO")` when constructed, which is at import time here. The first run printed a line for every outgoing HTTP request. Fix: `MCPServer(..., log_level="WARNING")`. The SDK still adds one handler to the root logger; that is harmless but worth knowing if log formatting ever looks different.
- **The textbook mount fails twice.** A throwaway app with `app.mount("/mcp", mcp.streamable_http_app(...))` and no lifespan answered `POST /mcp` with a 307 redirect to `/mcp/`, and `POST /mcp/` with a 500 (`Task group is not initialized`). Both were expected from the SDK docs and both are designed out (D49, D50).
- **A GET would have held a function open.** Reading the SDK's transport showed that stateless mode still serves `GET /mcp` as an endless event stream. Caught before it ran anywhere. Fix: POST-only routes (D50).
- **The default Host check rejects real hostnames.** Confirmed locally: `Host: evil.example` gets `421 Invalid Host header`, and so would the Vercel hostname with the defaults. Fix: configured deliberately (D51).
- **The MCP `check_availability` tool could not run for real in the local run.** `app/schedule.py` and its Postgres function arrived after the MCP slice was recorded, so the transcript recorded then used a stub for that tool and said so. It was deleted on Nov 13, 2026 once the deployed run replaced it. Unit tests stub the module. The later run against the deployed server (`docs/mcp-transcript-claude-code.md`) shows the real tool.
- **The first SQL check parsed slot ids by splitting on ":"**, which also splits the time. Fix: split on the first colon only, the same rule `parse_slot_id` uses.
- **A flex-basis became a height.** The intro text on the stats page had `flex: 1 1 28rem` to sit beside the window buttons. On a phone the row becomes a column, and the same basis turned into 28rem of empty height, which pushed the error card off the first screen. Fix: reset the flex value in the small-screen rule. It was caught only by looking at a 390 px screenshot of the error state.
- **Headless Chrome will not open a window narrower than 500 px**, so a "390 px" screenshot was really 500. Fix: load the page inside a 390 px iframe and compare the page's `scrollWidth` to its `innerWidth`.
- **Non-ASCII characters slipped into source.** A dash range in the eval's `normalize` and two test strings were meant to be written as escapes but landed in the files as literal characters, which are invisible in review. They are escapes again.
- **Local ports were already taken, three times.** A scratch server's port and two tries at the Docker port for `scripts/check_migrations.sh` collided with other things running on the machine. Fix: pick another port. Lesson: this is why the port is a variable (`CHECK_PORT`).
- **Two parallel build sessions shared one scratch folder and one file name.** Both wrote a `scratch_app.py`; the second overwrote the first, so one server silently started serving the other slice's page, and a kill-by-name may have stopped the wrong server. Fix: a private subfolder, a unique module name and a pid file. Lesson: shared temp space needs namespacing, and "kill by pattern" is not safe on a shared machine.
- **Not broken, but worth knowing:** `agent.py` uses `zoneinfo` to put today's clinic date in the system prompt, which needs a timezone database on the host. macOS has one. The `tzdata` package is in the dependencies so the deploy does not rely on the host having one.

**Checked on the deployed app (Nov 11, 2026):** `/book`, `/stats` and `/api/stats` load; `GET /mcp` answers 405; a `tools/list` request to `POST /mcp` answers 200 with both tools and no redirect; and the stats page counts `booking` and `mcp` rows, so the migration from the first item is applied and MCP tool calls are being logged. A Claude Code session connected to the live server and called both tools with real results (`docs/mcp-transcript-claude-code.md`).

**Not yet verified on the deployed app:** that `x-forwarded-for` carries the caller's address for MCP calls, and that a claude.ai connector connects and calls a tool. The `eval` job in GitHub Actions has not passed yet: its first runs stopped with "Missing credentials" because the five repository secrets were not set (the `test` job passed).
