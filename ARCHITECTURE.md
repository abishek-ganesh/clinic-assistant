# Architecture

*As of the thin slices (built Nov 11, 2026): the demo page and the class endpoint from Phase 1, plus a booking agent with human confirmation, an MCP server, and an answer eval in CI with a public usage page.*

## The idea in one picture

A patient asks the fictional Family Clinic a question. The app finds the handbook passages that answer it, checks whether it should answer at all, and then has a model write a short answer that cites those passages. Every step is visible on the demo page, because the point is to make RAG something students can see.

Three kinds of models, each with one job:

| Kind | Job | Model | Analogy |
|---|---|---|---|
| Embedding | represents text as vectors so similar meanings sit close together | Voyage `voyage-4-lite` | the librarian's card catalog |
| Judgment | answers a typed question with a calibrated probability | TypeSafe Jev | the triage nurse |
| Generative | writes the answer from the passages it is handed | Claude Haiku 5.5 | the doctor who explains |

**Code owns every threshold and every side effect.** Jev never writes text. Haiku never decides whether to answer. The embedding model only measures similarity.

The thin slices add three things on top of that pipeline, one per later stage of the course. Each has its own section below:

- **The booking agent** (`/book`): the model chooses which tool to call, but only code books, and only after a person presses Confirm.
- **The MCP server** (`/mcp`): the handbook search and the open-slot lookup as tools any MCP client can call.
- **Evals, CI and the stats page** (`/stats`): ten graded questions on every push to `main`, and a public page of usage numbers.

## System diagram

```
Browser                          Student notebook             MCP client
(Ask, Book, Usage stats pages)   (openai package)             (Claude Code, claude.ai, your agent)
      |                                |                            |
      v                                v                            v
 GET /        POST /api/ask      POST /v1/chat/completions     POST /mcp
 GET /book    POST /api/book     GET  /v1/models
 GET /stats   GET  /api/stats
      |                                |                            |
      +--------------------------------+----------------------------+
                                       v
              FastAPI on Vercel (Python 3.13, one serverless function)
                                       |
     +-----------------+---------------+--+-------------------+
     v                 v                  v                   v
  Voyage AI       TypeSafe Jev      Anthropic API        Supabase
  embeddings      judgments         Claude Haiku 5.5     Postgres + pgvector, over HTTPS
```

Which route uses which provider:

| Route | Voyage | Jev | Anthropic | Supabase |
|---|---|---|---|---|
| `POST /api/ask` | yes | yes | yes | yes |
| `POST /v1/chat/completions` | | | yes | yes |
| `POST /api/book` | | | yes | yes |
| `POST /mcp` (`search_handbook`) | yes | | | yes |
| `POST /mcp` (`check_availability`) | | | | yes |
| `GET /api/stats` | | | | yes |

GitHub Actions sits beside all of this: on every push to `main` it runs the answer eval against the same four providers (see "Evals, CI and the stats page").

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

Each module has one job and a small surface. The data shapes are in `app/models.py`; the numbers are in `app/thresholds.py`. The leaf modules (`retrieval`, `chunking`, `judge`, `generate`, `openai_compat`, `access`, `schedule`, `stats`) never import each other sideways. Only the composers do: `app/pipeline.py` for a question, `app/agent.py` for a booking turn, `app/mcp_server.py` for a tool call, and the route files.

| Module | Surface | Notes |
|---|---|---|
| `app/retrieval.py` | `embed(texts, input_type) -> list[list[float]]`, `retrieve(question, k=TOP_K) -> list[Passage]`, `check_corpus_model() -> None` | `check_corpus_model` raises if any stored chunk was embedded by a different model than the one configured; the pipeline runs it once per warm instance |
| `app/chunking.py` | `chunk_markdown(text, slug) -> list[Chunk]` | pure function: split on headings, cap at `CHUNK_MAX_TOKENS`, one-sentence overlap |
| `app/judge.py` | `judge(question) -> Judgments` | Jev over HTTP; Ollama fallback in development, labeled `calibrated=False` |
| `app/generate.py` | `build_prompt(question, passages) -> Prompt`, `generate(question, passages) -> Generation`, `chat(request) -> ChatResult`, `chat_stream(request)` yielding text deltas then one `ChatResult`, `get_client()` | owns the one Anthropic client (and the Ollama fallback for generation). `app/agent.py` is the only other module that calls Anthropic, and it does so through `get_client()` |
| `app/openai_compat.py` | `translate_request(body) -> ChatRequest` (raises `UnsupportedRequest`), `completion_body(...)`, `Stream` + `stream_chunk(...)`, `error_body(...)`, `models_body()` | pure functions, no network: OpenAI shapes in, Anthropic arguments out, and back |
| `app/access.py` | `mint_token()`, `hash_token(token)`, `hash_ip(ip)`, `client_ip(request)`, `authorize(token) -> AccessDecision`, `authorize_demo(ip) -> AccessDecision`, `log_usage(...)`, `estimate_cost(model, input_tokens, output_tokens)`, `DENIALS` | `DENIALS` maps each denial reason to an HTTP status, an OpenAI error type and a message. `client_ip` reads the first hop of `x-forwarded-for`, which is the caller behind Vercel's proxy; every open surface uses it |
| `app/pipeline.py` | `gate(judgments, passages) -> reason or None`, `ask(question) -> AskResult` | judge and retrieve in parallel, gate, generate; `AskResult.body` is the `/api/ask` response |
| `app/schedule.py` | `check_availability(visit_type=None, on_or_after=None, limit=5) -> list[Slot]`, `book(slot_id, ip_hash) -> Confirmation` (raises `BookingError` with `reason`), `parse_slot_id(slot_id)`, `VISIT_TYPES` | the only module that touches the schedule tables: two RPC calls and the slot id format |
| `app/agent.py` | `run_turn(messages) -> Turn`, `confirm_turn(messages, confirmation, ip_hash) -> Turn`, `sign_confirmation(slot_id)`, `confirmation_allows(confirmation, slot_id)`, `confirm_message(slot_id)`, `DECLINE_MESSAGE`, `TOOLS`, `SYSTEM_PROMPT` | the tool loop; composes `generate.get_client()` and `schedule` the way `pipeline.py` composes the Ask modules |
| `app/booking_routes.py` | `router` with `GET /book` and `POST /api/book` | validate, check access, one call to `run_turn` (or `confirm_turn` for a Confirm press), log usage |
| `app/mcp_server.py` | `mcp_routes` (`/mcp` and `/mcp/`, POST only), `mcp` (the `MCPServer`), `metered(tool, request, work)`, `find_passages(question)`, `find_slots(visit_type, on_or_after)` | composes `retrieval`, `schedule` and `access`; every tool runs its work through `metered` |
| `app/stats.py` | `get_stats(days=30) -> dict`, `clamp_days(raw) -> int` | one RPC call; the window is clamped to 1 to 90 days here and again inside the Postgres function |
| `app/stats_routes.py` | `router` with `GET /stats` and `GET /api/stats?days=` | 502 with `{"error": {"code": "upstream_error", ...}}` when the database call fails; successful responses are cacheable for 60 seconds |
| `app/main.py` | the Ask and class-endpoint routes; wires in the rest | checks access, calls one function, logs usage, shapes the response. Includes `booking_routes.router` and `stats_routes.router`, and adds `mcp_routes` as plain routes, so the app needs no startup hook |

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

The contract for `POST /api/book` is in "The booking agent" below.

## The handbook and how it is searched

The handbook is eight Markdown files in `handbook/`, one per topic. `scripts/ingest.py` turns them into searchable rows:

1. **Chunk.** `app/chunking.py` splits each file at its headings, so a chunk is one topic a patient might ask about ("Saturday hours", "No-show fee"). A section longer than about 350 tokens is split further with one sentence of overlap. The current handbook makes 60 chunks.
2. **Embed.** Each chunk is embedded together with its section title and heading, so a short passage still carries its topic. Only the passage text is stored and shown.
3. **Store.** Rows go into `handbook_chunks` with the name of the embedding model. Ingest inserts the new rows before deleting the old ones, so search never sees an empty table.

`scripts/eval_retrieval.py` checks the result: for every question in `eval/questions.jsonl` the expected section must rank first. It also prints the best similarity for questions that have no answer in the handbook, which is how the similarity threshold was chosen (see DECISIONS D26).

## The booking agent

The Ask page shows a pipeline where code decides and the model writes. The Book page shows the next step up: an agent, where the model chooses which tool to call next. The app's rule still holds. **The model can look and propose. Only code books, and only after a person pressed Confirm.**

Analogy: the model is a receptionist who can read the calendar and pencil in a suggestion. The receptionist has no pen for the appointment book at all. When the patient signs the suggestion, the clinic's system writes it in, and the receptionist is not involved.

The model has two tools, and neither can book:

| Tool | What code does when the model calls it |
|---|---|
| `check_availability(visit_type?, on_or_after?, limit?)` | reads open slots from the schedule and remembers which slot ids it returned this turn |
| `propose_slot(slot_id)` | accepts only a slot returned this turn, signs a confirmation for it, and ends the turn |

Booking happens in `agent.confirm_turn`, which runs only for a Confirm press and calls no model on the happy path.

### One booking, end to end

```
Turn 1   browser: {history: [], message: "Earliest returning visit, please"}
           |
         POST /api/book: validate, authorize_demo(ip), then agent.run_turn()
           |
         model call 1 -> tool_use check_availability({visit_type: "returning"})
           code: schedule.check_availability() -> RPC available_slots -> 5 open slots
         model call 2 -> tool_use propose_slot({slot_id: "patel:2026-12-01T09:40"})
           code: slot was returned this turn? yes -> sign (slot_id, expiry) with HMAC
           THE TURN ENDS HERE. The model is not called again.
           |
         response: outcome "proposed", a reply written by code, the proposal with its
                   signed confirmation, every tool call and result
           |
         page: proposal card with Confirm and Decline

Turn 2   browser: {history: [...], action: "confirm", confirmation: {slot_id, expires_at, signature}}
           |
         POST /api/book: validate, authorize_demo(ip), then agent.confirm_turn()
           code: signature valid, not expired, for this exact slot? yes
                 -> schedule.book() -> RPC book_slot -> one row in appointments
           NO MODEL CALL.
           |
         response: outcome "booked", booking {confirmation_code, ...}, a reply written by code,
                   one step {"tool": "book_slot", "by": "code"}
```

The other ways a Confirm press can go:

- **Bad confirmation** (forged, expired, or a signature for a different slot): nothing is written, no model is called, and the reply is a fixed message written by code (outcome `not_booked`).
- **The slot went in the meantime** (`slot_taken`, or the visitor hit the daily `booking_limit`): code replaces the confirm turn with "[...could not be booked: reason] What else is open?" and runs one normal agent turn, so the model explains and offers other times. It still has no tool that books.

Typing "I confirm" is just text and reaches the model like any other message. The model can only propose. If it asks for a tool called `book_slot` anyway, it gets an "Unknown tool" error result. Decline books nothing: the server writes a fixed "not that one" user turn and the agent offers other times.

### The loop in `agent.run_turn`

1. Call Haiku with the system prompt, the two tools and the conversation (`effort: low`, no sampling parameters, no forced tool choice).
2. If the model refused (`stop_reason == "refusal"`), return a fixed message. Any other stop that is not `tool_use` ends the turn with the model's text.
3. Otherwise append the response's whole content list to the conversation unchanged (thinking blocks carry a signature the API checks), run every `tool_use` block in order, and send all the results back in one user message.
4. A valid `propose_slot` ends the turn immediately: the next move belongs to the patient, not the model.
5. After `AGENT_MAX_MODEL_CALLS` (6) model calls the turn ends with a fixed message (`step_limit`). A normal turn takes two or three.

Tool arguments are model output, so they are validated like any other input. The loop has no side effects, so a failed model call is simply a 502 and nothing needs to be reported or undone.

The numbers live in `app/thresholds.py`: `AGENT_MAX_MODEL_CALLS = 6`, `AVAILABILITY_MAX_SLOTS = 10`, `PROPOSAL_TTL_SECONDS = 600`, `MAX_BOOKING_MESSAGE_CHARS = 500`, `MAX_BOOKING_HISTORY = 30`, `MAX_BOOKING_TURN_CHARS = 2000`.

### `POST /api/book`

Request:

```json
{ "history": [ {"role": "user", "content": "..."}, {"role": "assistant", "content": "..."} ],
  "action": "message",
  "message": "Do you have anything Thursday?",
  "confirmation": null }
```

- `history`: the `history` array from the previous response, unchanged (empty on the first turn). Text turns only, alternating user then assistant, at most 30 turns of at most 2000 characters each.
- `action`: `message` (the default, needs `message` of 1 to 500 characters), `confirm` (needs `confirmation` exactly as the proposal carried it) or `decline`.

Response, always 200 when the agent ran:

```json
{ "outcome": "proposed",
  "reply": "I can book Tue Dec 1, 9:40 AM with Dr. Patel (Main Street), a returning patient visit (20 minutes). Press Confirm to book it, or Decline to see other times.",
  "steps": [ { "tool": "check_availability", "input": {"visit_type": "returning"},
               "result": {"slots": [ {"slot_id": "patel:2026-12-01T09:40", "visit_type": "returning",
                                      "when": "Tue Dec 1, 9:40 AM with Dr. Patel (Main Street)"} ]},
               "is_error": false },
             { "tool": "propose_slot", "input": {"slot_id": "patel:2026-12-01T09:40"},
               "result": {"proposed": true, "waiting_for": "the patient to press Confirm or Decline"},
               "is_error": false } ],
  "proposal": { "slot": { "slot_id": "patel:2026-12-01T09:40", "provider": "Dr. Patel",
                          "visit_type": "returning", "starts_at": "2026-12-01T09:40:00-05:00",
                          "display": "Tue Dec 1, 9:40 AM with Dr. Patel (Main Street)" },
                "confirmation": { "slot_id": "patel:2026-12-01T09:40", "expires_at": 1796049600,
                                  "signature": "9f2c..." } },
  "booking": null,
  "history": [ "...the request's history plus this turn's user and assistant text..." ],
  "prompt": { "system": "...", "tools": [ "...the two tool definitions..." ] },
  "model": "claude-haiku-5-5", "model_calls": 2,
  "input_tokens": 3111, "output_tokens": 192, "latency_ms": 2140 }
```

- `outcome` is `replied`, `proposed`, `booked`, `not_booked` (a Confirm press with a bad confirmation; fixed message), `refused` (the model declined; fixed message) or `step_limit` (the loop cap; fixed message).
- `steps` is every tool call in the turn with its arguments and the result code sent back, plus the booking itself on a Confirm turn, marked `"by": "code"`. The page shows them under "What the agent did".
- `booking` is `{"confirmation_code", "slot_id", "visit_type", "display"}` when this turn booked, otherwise `null`.
- `input_tokens` and `output_tokens` are summed over every model call in the turn, and that sum is what `usage_log` records (`surface = "booking"`, with the outcome above, or `denied` or `error`). `detail` holds the confirmation code, or why a confirmed slot was not booked.
- Errors use the same shape as `/api/ask`: 422 `invalid_request`, 429 `rate_limited` or `spend_cap`, 503 `kill_switch`, 502 `upstream_error`.

### Why it is built this way

- **A button, not a typed "yes".** A typed "yes" is just more text the model interprets, and text can be injected or misread. A button press that returns a server-signed token is something code can verify without interpreting anything (D36).
- **A signature instead of stored state.** The function is serverless and keeps no memory, so there is no "pending proposal" to look up. The confirmation is `HMAC-SHA256(key, "<slot_id>|<expires_at>")`, valid for ten minutes, and any instance can verify it with no storage (D38).
- **The browser holds the conversation, as plain text.** The server keeps nothing between turns. The page sends earlier turns back, and the route rebuilds each one from `role` and `content` alone. Forging history can change what the model says, never what gets booked. One consequence: slot ids do not survive between turns, so every proposal starts from a fresh look at the schedule (D39).
- **The database is the last line.** `book_slot` in Postgres only accepts slots the schedule really offers, a unique constraint settles races between two visitors, and one visitor can book at most 5 slots in 24 hours (D41, D43).
- **Emergencies are handled by the system prompt only.** The Jev emergency judgment that guards `/api/ask` does not run in front of the booking agent yet. That is a prompt, not a threshold, and putting the same gate here is the consistent next step (D45).

### The schedule

Open slots are computed on every call from each provider's weekly blocks, never stored, so there is nothing to seed and nothing goes stale. Only bookings are rows. A slot id is `<provider slug>:<clinic-local start>`, for example `patel:2026-12-01T09:40`.

The schedule sits inside the handbook's hours: three fictional providers (Dr. Patel and Dr. Lee at Main Street, Dr. Kim at Riverside), Saturday mornings at Main Street for returning patients only, closed Sundays and holidays, a noon close on Christmas Eve and New Year's Eve, 20 minute returning and telehealth visits, 40 minute physicals. New patient visits and same-day visits are not offered online because the handbook says those are booked by phone, which is also why booking opens tomorrow and covers the next 14 days.

## The MCP server

The Model Context Protocol (MCP) is a standard way for an AI application to discover and call tools on someone else's server. `app/mcp_server.py` exposes two of this app's abilities that way, at `/mcp`, so Claude Code, claude.ai or a student's own agent can call them. [docs/mcp.md](docs/mcp.md) shows how to connect.

| Tool | Calls | Returns |
|---|---|---|
| `search_handbook(question)` | `app.retrieval.retrieve` | passages, best first: `section`, `heading`, `content`, `similarity` |
| `check_availability(visit_type?, on_or_after?)` | `app.schedule.check_availability` | open slots, soonest first: `slot_id`, `provider`, `visit_type`, `starts_at`, `display` |

Both only read. There is no booking tool: booking is a side effect, a side effect needs a person to confirm it, and this server has no way to ask.

Notice which half of RAG is exposed. The server retrieves; the model in the client writes the answer. The judgment gate and the generator from the Ask pipeline are not involved, so an MCP call costs no model tokens on our side (only a Voyage embedding for `search_handbook`).

`check_availability` passes `visit_type` straight to the schedule, which matches `returning`, `physical` or `telehealth` exactly. Any other text returns an empty list.

### One tool call, end to end

```
MCP client
   |  POST /mcp   {"jsonrpc":"2.0","id":7,"method":"tools/call",
   |               "params":{"name":"search_handbook","arguments":{"question":"..."}}}
   v
route /mcp (POST only)
   |
StatelessEndpoint: build a session manager for this one request, start it
   |
MCP SDK: check Host/Origin (local runs only), parse JSON-RPC,
         validate the arguments against the tool's input schema
   |
search_handbook(question, ctx)
   |
metered("search_handbook", request, work)
   |-- client_ip(request) -> authorize_demo(ip)
   |       denied          -> usage_log row (outcome "denied")  -> ToolError(denial message)
   |       check errors    -> no row, call refused              -> ToolError(fixed message)
   |-- work(): retrieve(question) -> Voyage embedding -> match_chunks() in Postgres
   |       raises          -> usage_log row (outcome "error")   -> ToolError(fixed message)
   |-- usage_log row (surface "mcp", outcome "tool_call", detail = tool name, latency_ms)
   v
one JSON body: {"result": {"content": [...], "structuredContent": {...}, "isError": false}}
```

Connecting and listing the tools never reach `metered`, so they are free. Arguments that fail the input schema (an empty question, a malformed date) are rejected by the SDK before the tool runs, so they are free too. Only tool calls count against the limit.

A `ToolError` is not an HTTP error. The client gets HTTP 200 and a normal result with `isError: true` and the message as text. That is the right channel: the model reads tool results, so "Too many requests in the last minute. Wait 60 seconds and try again." reaches the model, which can tell the user. An HTTP 429 would reach only the client's transport code.

### Why stateless

MCP's original HTTP transport is a session: the client sends `initialize`, the server answers with a session id, and the server keeps per-session state in memory. On one long-lived process that works. On serverless it does not: the next request may land on a different instance that has never heard of the session.

So the server runs with `stateless_http=True` and `json_response=True`: every POST is one complete exchange, answered with one JSON body, and nothing is remembered. What we give up is everything that needs a channel from server to client (progress notifications, sampling, elicitation). Two read-only lookups need none of it.

Three details follow from that choice:

- **A session manager per request.** The SDK expects its session manager to be started once in the host app's startup hook. `StatelessEndpoint` builds and starts one for each request instead, so `/mcp` does not depend on startup hooks running and shares nothing between requests. That is why `app/main.py` wires it in with one line and no `lifespan` (D49).
- **POST only, at `/mcp` and `/mcp/`.** A GET would open an event stream that a stateless server never writes to, holding a serverless function open for nothing. Other methods get 405. Both spellings of the path are plain routes, so neither is answered with a redirect, which breaks some MCP clients on a POST (D50).
- **The Host/Origin check is on locally and off in production.** The SDK's DNS-rebinding protection matters for a server on your own machine. In production the server is public, takes no cookies or credentials, and sits behind Vercel's proxy. The switch is `APP_ENV=production` (D51).

## Evals, CI and the stats page

### The answer eval

`scripts/eval_retrieval.py` asks "did the right passage come back?". `scripts/eval_answers.py` asks the next question: "was the final answer right?". It runs ten questions from `eval/answers.jsonl` through `pipeline.ask`, the same function the demo page calls, with the real models and the real database.

The ten: seven the handbook answers (one of them needs two sections), a medical-advice question and an off-topic question that must be declined, and one emergency.

Each answer is graded by plain code, in this order:

1. **Decision.** `answered`, `declined` or `emergency` must match. A wrong decision is reported alone, because nothing after it means anything.
2. **Citations.** An answered question cites at least one passage, and only passages it was actually shown.
3. **Section.** Every expected handbook section is among the cited passages. Citation `[n]` is the passage ranked `n`, so this is a lookup, not a guess.
4. **Facts.** Each required fact is a small case-insensitive pattern, so "9 AM to 1 PM" and "9:00 a.m. to 1:00 p.m." both pass. Every fact is copied from the handbook text, and a unit test checks that each pattern matches the handbook file it came from.

The script prints a table, the estimated cost and the time taken, exits non-zero when the bar (10 of 10) is missed, and writes the same table to the GitHub Actions job summary. Each question also writes a `usage_log` row with `surface = 'eval'`, so CI spend counts toward the monthly cap and shows on the stats page.

**What it cannot catch:**

- **Wrong extra content.** The checks confirm required facts are present. They do not notice an invented sentence next to them, or a correct fact attached to the wrong citation.
- **Tone, length and clarity.** Nothing here reads the answer the way a patient would.
- **Anything outside ten questions.** Ten is a smoke test, not coverage. It will catch a broken prompt, a wrong threshold, an empty table or a dead API key. It will not catch a subtle regression on a question nobody wrote down.
- **Declines for the wrong reason.** A declined question passes whichever of the three layers declined it.
- **Rewording the patterns did not foresee.** A correct answer phrased in a new way fails. That is a false alarm, and the fix is to widen the pattern, never to delete the fact.

A graded rubric (a model scoring answers against written criteria, checked against human labels) is left out on purpose here: a grader model is a second system that needs its own eval.

### CI

`.github/workflows/ci.yml` has two jobs.

| Job | Runs on | Secrets | What it does |
|---|---|---|---|
| `test` | every push and every pull request | none | `uv sync --locked`, `ruff check`, `ruff format --check`, `pytest` |
| `eval` | pushes to `main` only, after `test` passes | five provider keys | `scripts/eval_answers.py` against the real providers |

The split is the lesson. Unit tests fake every provider, so they are free, fast and safe to run on code from anyone. The eval spends money and holds keys, so it never runs for pull requests: a pull request from a fork must not be able to reach the secrets.

Other details: the workflow's token can only read the repository; a newer push to the same branch cancels the run in progress; each job stops after 10 minutes; both actions are pinned to a commit hash; the keys are given to the one step that needs them, not to the whole job; and `APP_ENV=production` means a missing key is an error, never a silent fallback to a local model.

**The eval is an alarm, not a gate.** Vercel deploys on the same push, in parallel, and does not wait for GitHub Actions. A red eval tells you the deployed app is wrong; it does not stop the deploy. Making it a gate is a production-unit topic.

The eval job needs five secrets in the GitHub repository (Settings, Secrets and variables, Actions): `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY`, `VOYAGE_API_KEY`.

### The stats page

```
Browser  GET /stats        -> app/static/stats.html + stats.css + stats.js
         GET /api/stats    -> app/stats_routes.py -> app/stats.py get_stats(days)
                                -> one RPC: usage_stats(p_days)   (Postgres)
                                     -> counts, sums, percentiles over usage_log
```

Every request to the app already writes one `usage_log` row. The Postgres function `usage_stats(p_days)` turns those rows into one JSON object: totals (requests, estimated cost, tokens in and out), requests and cost by surface, demo outcomes with the decline and emergency rates, median and p95 latency for answered demo questions and for class endpoint completions, one entry per day (Eastern time, quiet days included as zero), and month-to-date spend next to the cap.

The app never pulls raw rows. The function returns aggregates only: no token ids or labels, no IP hashes, no decline reasons, no per-request rows. A public page cannot leak a row it has no way to read.

Two definitions worth knowing when reading the page:

- **Decline rate** is declined questions divided by questions the pipeline finished (answered, declined or emergency). Denied and error rows are not answers, so they are not in the denominator.
- **Estimated cost** is generative-model tokens times the price table in `app/access.py`. Embedding and judgment calls are not priced in.

The page (its header reads "Observability") is the course's first look at that topic, so each number has one sentence saying what it means and why an engineer watches it. Charts are plain HTML and CSS with no chart library: the daily chart is a row of `div` bars with a table of the same numbers behind a toggle, and the two breakdowns are tables with a bar in one column.

## Self-service class tokens

The class endpoint needs a token per student. Students get their own on the `/token` page, in two steps. The class code shows they are in the class. A link sent to their inbox shows they own the address.

```
Step 1   POST /api/token  {"email": "...", "class_code": "..."}
   |-- validate the email shape and the code's length
   |-- authorize_demo(ip)          the shared per-visitor limit, kill switch, spend cap
   |-- make a random link secret, hash it, hash the class code
   |-- start_token_request() in Postgres:
   |       no class code set          -> signups_closed (403)
   |       hash does not match        -> wrong_code (403)
   |       3 links this hour already  -> too_many_links (429)
   |       otherwise                  -> store a pending request: email, link hash, expiry
   |-- send the link through Resend:  https://<site>/token#link=<secret>
   v
{"sent": true, "email": "...", "expires_minutes": 15}        no token yet

Step 2   the student opens the link and presses "Show my token"
         POST /api/token/redeem  {"link": "<secret>"}
   |-- authorize_demo(ip)
   |-- mint a token in Python
   |-- redeem_token_request() in Postgres, all or nothing:
   |       unknown, used or expired link -> link_invalid, link_used, link_expired (410)
   |       otherwise -> mark the request used, turn off this email's earlier
   |                    self-service token, store the new token's hash
   v
{"token": "sk-bootcamp-...", "email": "...", "replaced": false}   shown once, never cached
```

- **Nothing secret is stored.** The database holds a hash of the class code, a hash of each link and a hash of each token. The token never travels by email: the inbox only ever holds a link that works once, for 15 minutes.
- **The link's secret sits after the `#`.** Browsers do not send that part of an address to a server, so it never appears in request logs, and the page removes it from the address bar on load.
- **A person has to press the button.** Some mail systems open every link in a message to scan it. Opening the page spends nothing; only the button press redeems the link.
- **The link's address comes from configuration**, never from the request's `Host` header. Otherwise a forged header could make the app mail someone a link that points at another site.
- **Asking again is safe.** A new request does not touch the current token. The old token is turned off only when the new link is redeemed, and only if it was a self-service token: one the instructor minted by hand (`self_service = false`) is never replaced by the form.
- **Guessing and flooding are both capped.** Wrong codes and bad links are logged with an outcome that counts toward the visitor's ten requests a minute, and one address can be sent three links an hour.
- The instructor changes the class code or closes sign-ups with `scripts/tokens.py class-code`, and sees who holds which token with `scripts/tokens.py list`.

## What exists today

| Piece | File | What it does |
|---|---|---|
| Entry point | `app/main.py` | The Ask and class-endpoint routes (`/`, `/api/ask`, `/v1/chat/completions`, `/v1/models`, `/health`), and the wiring for the booking, stats and MCP routes |
| Pipeline | `app/pipeline.py` | One question end to end; the gate that decides in code |
| Settings | `app/config.py` | Every env var and model id in one place; `/health` reports which providers have keys, never the keys |
| Thresholds | `app/thresholds.py` | Every number the code decides with, one comment each |
| Data shapes | `app/models.py` | What passes between modules |
| Database client | `app/db.py` | One Supabase client per warm serverless instance |
| Embeddings + search | `app/retrieval.py`, `app/chunking.py` | Voyage over HTTP with retries; vector search in Postgres; the chunker |
| Judgments | `app/judge.py` | Two questions to Jev in one request, with retries |
| Generation | `app/generate.py` | The grounded answer with structured output; plain chat for the class endpoint; the shared Anthropic client |
| OpenAI compatibility | `app/openai_compat.py` | Translation between OpenAI's shapes and Anthropic's |
| Access | `app/access.py` | Tokens, limits, the usage log, denial messages, the caller's address |
| Schedule | `app/schedule.py` | Open slots and bookings, through two Postgres functions |
| Booking agent | `app/agent.py`, `app/booking_routes.py` | The tool loop with the signed confirmation; `/book` and `/api/book` |
| MCP server | `app/mcp_server.py` | Two read-only tools at `/mcp`, stateless |
| Usage stats | `app/stats.py`, `app/stats_routes.py` | `/stats` and `/api/stats`: aggregates from one Postgres function |
| Pages | `app/static/` | Plain HTML, CSS and JavaScript: Ask the handbook (`index.html`), Book an appointment (`book.html`), Usage stats (`stats.html`). They share `styles.css` |
| Schema | `supabase/migrations/` | Tables, indexes, the functions below, row-level security |
| Scripts | `scripts/` | `ingest.py`, `eval_retrieval.py`, `eval_answers.py`, `tokens.py`, `student_smoke.py`, `access_checks.py`, `check_migrations.sh` |
| CI | `.github/workflows/ci.yml` | Lint and unit tests on every push and pull request; the answer eval on pushes to `main` |
| Guides | `docs/` | `mcp.md` (connect a client to the MCP server), `mcp-transcript-claude-code.md` (Claude Code using the live server), screenshots |

## Data model

| Table | Holds | Notes |
|---|---|---|
| `handbook_chunks` | handbook passages and their 512-dim vectors | `embedding_model` is recorded per row: vectors from two models are not comparable |
| `student_tokens` | one row per student API token | only the sha256 hash is stored; the token is shown once when minted. `self_service` marks tokens requested on the `/token` page |
| `usage_log` | one row per request on any surface | `surface` is `endpoint`, `demo`, `booking`, `mcp`, `eval` or `token`. Tokens, latency, status, outcome, ignored params, raw judgments, estimated cost |
| `token_requests` | one row per emailed token link | the email, a hash of the link, when it expires and when it was used |
| `app_settings` | key/value switches | `kill_switch`, `spend_cap_usd` (monthly), `demo_rpm_per_ip`, `booking_max_per_ip_per_day` (5), `class_code_hash` (null closes token sign-ups) |
| `providers` | three fictional providers: Dr. Patel and Dr. Lee (Main Street), Dr. Kim (Riverside) | |
| `provider_blocks` | the weekly schedule: provider, weekday, visit type, start, end, slot length | blocks never overlap for a provider, so slots cannot overlap |
| `clinic_closures` | holiday closures and early closes from the handbook | listed through November 2027; add rows to extend |
| `appointments` | one row per booked slot | `unique (provider, starts_at)`; no patient name or detail, only a keyed hash of the visitor's address and a short confirmation code |

What each surface writes to `usage_log`:

| Surface | One row per | Outcomes |
|---|---|---|
| `endpoint` | class endpoint request | `answered`, `invalid_request`, `denied`, `error` |
| `demo` | question on the Ask page | `answered`, `declined`, `emergency`, `denied`, `error` |
| `booking` | agent turn (tokens summed over its model calls) | `replied`, `proposed`, `booked`, `refused`, `step_limit`, `denied`, `error` |
| `mcp` | tool call (the tool name is in `detail`) | `tool_call`, `denied`, `error` |
| `token` | link request or link redeemed (the reason is in `detail`) | `link_sent`, `issued`, `refused`, `denied`, `error` |
| `eval` | eval question | the pipeline's decision, or `error` |

Nine Postgres functions do the heavy lifting next to the data (the last two, `start_token_request` and `redeem_token_request`, are described under "Self-service class tokens"):

- **`match_chunks(query_embedding, match_count)`** returns the nearest passages with cosine similarity (1 means same direction, 0 means unrelated). An HNSW index backs it.
- **`authorize_request(token_hash)`** allows or denies a class-endpoint request in one round trip, cheapest checks first: kill switch, unknown token, revoked token, monthly spend cap, per-minute limit, per-day limit. Days and months follow class time (Eastern).
- **`authorize_demo_request(ip_hash)`** does the same for the open surfaces, per visitor instead of per token: kill switch, monthly spend cap, then requests in the last minute from that address. The count covers every row with that address, so one visitor gets one allowance (`demo_rpm_per_ip`, 10 a minute) across the Ask page, the booking agent and the MCP server (D35).
- **`schedule_slots(p_from, p_to)`** returns every slot the clinic offers between two dates, booked or not: the weekly blocks cut into slots, minus closures. It runs in `America/New_York`, so dates, weekdays and clock times mean clinic time wherever the server is.
- **`available_slots(p_visit_type, p_from, p_limit)`** is `schedule_slots` from tomorrow through 14 days ahead, minus rows in `appointments`, soonest first, at most 20.
- **`book_slot(p_provider, p_starts_local, p_ip_hash)`** checks the slot against `schedule_slots`, checks the per-visitor daily limit, then inserts. It returns `{"booked": true, confirmation_code, slot_id, visit_type, display}` or `{"booked": false, "reason": "invalid_slot" | "slot_taken" | "booking_limit"}`. The same visitor booking the same slot again gets the first booking back, so a retry is safe.
- **`usage_stats(p_days)`** returns the aggregates behind the stats page as one JSON object. It has no way to return a row.

## Why limits live in the database

A serverless function keeps no memory between requests; two requests a second apart may run on different machines. The in-memory counter you would write on a laptop silently fails here. So limits are counted from `usage_log` inside Postgres. At class scale that is cheap, and every decision is auditable after the fact.

Two deliberate details: denied requests are logged but don't count toward limits, so retrying doesn't extend a lockout; and the check-then-log sequence is not atomic, so two simultaneous requests at the limit can both pass. Both are acceptable at class scale and worth discussing in Stage 4.

One consequence to keep in mind: a limit that counts log rows is only as good as the log write. If a surface's rows cannot be written, that surface is not rate limited (see "What broke" in DECISIONS for the time this happened).

## Security model

- The FastAPI server is the only thing that talks to Supabase, using the secret key. Row-level security is on with no policies, the `anon` and `authenticated` roles have no table grants, and only `service_role` can execute the functions. The schedule tables and the new functions follow the same rule. The publishable key is never used.
- No provider key ever reaches a browser or a student. Students get their own revocable token for the class endpoint.
- The open surfaces (Ask page, booking agent, MCP server) need no sign-in. Each request passes the same check (kill switch, monthly spend cap, one shared per-visitor limit) and writes a `usage_log` row.
- That limit is per IP address, but addresses are never stored. The log holds a keyed hash (HMAC) of each one: there are few enough IPv4 addresses that a plain hash could be reversed by hashing them all.
- Student tokens are hashed with sha256. A fast hash is fine for a long random token; passwords need a slow hash because humans choose them.
- **Booking.** Everything the browser sends back is untrusted. The one thing that authorizes a booking is the signed confirmation, `HMAC-SHA256(key, "<slot_id>|<expires_at>")`, compared with `hmac.compare_digest`. The key is derived from the Supabase secret with its own label, the way `hash_ip` derives its key, so one secret has one use per key. A confirmation can be replayed until it expires; that is safe because the database, not the token, prevents the second booking.
- **MCP.** The server offers nothing that changes data. Connectors on claude.ai and Claude Desktop call from Anthropic's cloud, so all their users share a few addresses and one per-visitor allowance; Claude Code calls from the user's machine. The SDK's Host/Origin check is on for local runs and off in production (D51).
- **Stats.** The public page gets aggregates only, from a function that cannot return a row.
- **CI.** The provider keys are GitHub Actions secrets, given to one step of one job that never runs for pull requests.
- Secrets live in `.env` locally (gitignored), in Vercel's environment settings in production, and in GitHub Actions secrets for the eval.

## Data and subprocessors

All data is synthetic. The clinic is fictional and no real patient information exists anywhere in the system. The booking agent asks for no personal detail (its system prompt tells the model not to), and an appointment is a provider, a time, a visit type and a code. Even so, the app is documented the way a real health app would be:

| Subprocessor | Receives | Purpose |
|---|---|---|
| Anthropic | the question and retrieved handbook passages; class endpoint messages; booking conversations and the tool results in them | answer generation, class completions, the booking agent |
| TypeSafe | the question text | emergency and scope judgments |
| Voyage AI | the question text (from the Ask page and from MCP `search_handbook` calls) and handbook passages | embeddings |
| Resend | the email address of a student requesting a token, and the one-time link | delivering the token link |
| Supabase | everything stored in the tables above | database |
| Vercel | request traffic | hosting |

What an MCP client does with the passages it gets back (which model reads them, and where) is up to whoever connected the client, not this app.

## Local loop and deploy

`uv run uvicorn app.main:app --reload` locally, with keys in `.env`. With no Anthropic or TypeSafe key, development falls back to a local Ollama model for generation and judgments (labeled uncalibrated); production never falls back. The booking agent has no Ollama path: it needs an Anthropic key. Pushing to `main` deploys to Vercel through the GitHub integration; Vercel's FastAPI preset finds `app` in `app/main.py`. The same push starts CI. Schema changes go through `supabase migration new`, `scripts/check_migrations.sh`, then `supabase db push`.
