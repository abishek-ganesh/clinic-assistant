# MCP server: local end-to-end transcript

Recorded Nov 11, 2026 on a laptop, before the first deploy of the MCP server.

**Setup.** A scratch FastAPI app (kept outside the repo) mounted `/static`, a `/health` route and the MCP routes exactly as `app/main.py` does (`app.router.routes.extend(mcp_routes)`), served by `uvicorn` on port 8022. The client was the MCP Python SDK's own client (`mcp.Client` over `streamable_http_client`, SDK 2.3.0), run twice: once forcing the older handshake protocol (`mode="legacy"`, what most clients speak today) and once in the default `auto` mode, which negotiated the newer 2026-07-28 protocol.

**What was real and what was stubbed.**

- `search_handbook` was real: the question was embedded by Voyage and matched in Supabase, using the keys in `.env`.
- The access check (`authorize_demo`) was real and allowed every call.
- `check_availability` was **stubbed**: `app/schedule.py` did not exist yet, so the scratch app registered a stand-in module that returns one fixed slot. The slot below is not real schedule data.
- The `usage_log` writes **failed** during this run: the database still had the old `surface` check constraint, which does not allow `mcp` (the migration `20261111200000_open_surfaces.sql` had not been applied). `log_usage` swallows a failed insert by design, so the tool calls still answered. See "What broke and how we fixed it" in `DECISIONS.md`.

## Client output

```text
===== client mode: legacy =====
protocol_version: 2025-11-25
server_info: name='family-clinic' title='Family Clinic Assistant' version='' description=None website_url=None icons=None

--- tool: search_handbook
title: Search the clinic handbook
annotations: {'read_only_hint': True, 'idempotent_hint': True, 'open_world_hint': False}
description: Search the Family Clinic patient handbook: hours, appointments, refills, billing,
insurance and other clinic policies.

Returns the few passages closest in meaning to the question, best first, each with its
section, heading, text and a similarity score from 0 to 1. A best score under about 0.45
means the handbook probably does not cover the question: say so instead of guessing.
Answer only from the returned text and name the section you used. Read-only.

input_schema: {"properties": {"question": {"description": "A patient's question in plain language, e.g. 'How long do refills take?'", "maxLength": 500, "minLength": 1, "title": "Question", "type": "string"}}, "required": ["question"], "type": "object", "title": "search_handbookArguments"}

--- tool: check_availability
title: Check open appointment times
annotations: {'read_only_hint': True, 'idempotent_hint': True, 'open_world_hint': False}
description: List the next open appointment times at the Family Clinic, soonest first.

Each result has the provider, the visit type, the start time (ISO 8601 with the clinic's
UTC offset) and a ready-to-show label. An empty list means nothing is open for that
filter. Read-only: this tool cannot book, hold or cancel an appointment.

input_schema: {"properties": {"visit_type": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "Kind of visit, e.g. 'annual physical'. Omit for any kind.", "title": "Visit Type"}, "on_or_after": {"anyOf": [{"format": "date", "type": "string"}, {"type": "null"}], "default": null, "description": "Earliest acceptable date as YYYY-MM-DD. Omit for the next openings.", "title": "On Or After"}}, "type": "object", "title": "check_availabilityArguments"}

--- call search_handbook
is_error: False
text: {
  "section": "Hours and locations",
  "heading": "Saturday hours",
  "content": "We are open on Saturdays at the Main Street location only, from 9:00 am to 1:00 pm. Riverside is closed on weekends. Saturday visits are for returning patients and are booked the same way as weekday visits. Both locations are closed on Sundays.",
  "similarity": 0.566
}
text: {
  "section": "Hours and locations",
  "heading": "Main Street location",
  "content": "The Main Street clinic is at 100 Main Street, Suite 200. It is our larger location and the only one open on Saturdays. The lab draw station and the vaccine room are both here.\n\nMain Street hours:\n\n- Monday to Friday: 8:00 am to 6:00 pm\n- Saturday: 9:00 am to 1:00 pm\n- Sunday: closed",
  "similarity": 0.486
}
text: {
  "section": "Hours and locations",
  "heading": "Holiday closures",
  "content": "Both locations are closed on these holidays:\n\n- New Year's Day\n- Memorial Day\n- Independence Day\n- Labor Day\n- Thanksgiving Day and the Friday after\n- Christmas Day\n\nOn Christmas Eve and New Year's Eve both locations close early, at 12:00 pm. When a holiday falls on a weekend, the closure moves to the nearest weekday. The after-hours nurse line stays open on every holiday.",
  "similarity": 0.43
}
text: {
  "section": "Hours and locations",
  "heading": "Phone line hours",
  "content": "Our main phone number for both locations is (555) 555-0100. The phone line is staffed:\n\n- Monday to Friday: 7:30 am to 6:00 pm\n- Saturday: 9:00 am to 1:00 pm\n\nOutside those times, the same number offers the option to reach the after-hours nurse line or leave a message. Messages are returned the next business day. The phone line is busiest between 8:00 am and 10:00 am, so call later in the day for shorter waits on routine questions.",
  "similarity": 0.418
}
structured: {
  "result": [
    {
      "section": "Hours and locations",
      "heading": "Saturday hours",
      "content": "We are open on Saturdays at the Main Street location only, from 9:00 am to 1:00 pm. Riverside is closed on weekends. Saturday visits are for returning patients and are booked the same way as weekday visits. Both locations are closed on Sundays.",
      "similarity": 0.566
    },
    {
      "section": "Hours and locations",
      "heading": "Main Street location",
      "content": "The Main Street clinic is at 100 Main Street, Suite 200. It is our larger location and the only one open on Saturdays. The lab draw station and the vaccine room are both here.\n\nMain Street hours:\n\n- Monday to Friday: 8:00 am to 6:00 pm\n- Saturday: 9:00 am to 1:00 pm\n- Sunday: closed",
      "similarity": 0.486
    },
    {
      "section": "Hours and locations",
      "heading": "Holiday closures",
      "content": "Both locations are closed on these holidays:\n\n- New Year's Day\n- Memorial Day\n- Independence Day\n- Labor Day\n- Thanksgiving Day and the Friday after\n- Christmas Day\n\nOn Christmas Eve and New Year's Eve both locations close early, at 12:00 pm. When a holiday falls on a weekend, the closure moves to the nearest weekday. The after-hours nurse line stays open on every holiday.",
      "similarity": 0.43
    },
    {
      "section": "Hours and locations",
      "heading": "Phone line hours",
      "content": "Our main phone number for both locations is (555) 555-0100. The phone line is staffed:\n\n- Monday to Friday: 7:30 am to 6:00 pm\n- Saturday: 9:00 am to 1:00 pm\n\nOutside those times, the same number offers the option to reach the after-hours nurse line or leave a message. Messages are returned the next business day. The phone line is busiest between 8:00 am and 10:00 am, so call later in the day for shorter waits on routine questions.",
      "similarity": 0.418
    }
  ]
}

--- call check_availability
is_error: False
text: {
  "slot_id": "stub-1",
  "provider": "Dr. Patel",
  "visit_type": "annual physical",
  "starts_at": "2026-12-01T09:30:00-05:00",
  "display": "Tue Dec 1, 9:30 AM with Dr. Patel"
}
structured: {
  "result": [
    {
      "slot_id": "stub-1",
      "provider": "Dr. Patel",
      "visit_type": "annual physical",
      "starts_at": "2026-12-01T09:30:00-05:00",
      "display": "Tue Dec 1, 9:30 AM with Dr. Patel"
    }
  ]
}

--- call search_handbook with an empty question (expect a tool error)
is_error: True
text: Error executing tool search_handbook: 1 validation error for search_handbookArguments
question
  String should have at least 1 character [type=string_too_short, input_value='', input_type=str]
    For further information visit https://errors.pydantic.dev/2.14/v/string_too_short

--- HTTP requests made
POST /mcp  rpc=initialize  mcp-protocol-version=-  -> 200 application/json
POST /mcp  rpc=notifications/initialized  mcp-protocol-version=2025-11-25  -> 202 application/json
POST /mcp  rpc=tools/list  mcp-protocol-version=2025-11-25  -> 200 application/json
POST /mcp  rpc=tools/call  mcp-protocol-version=2025-11-25  -> 200 application/json
POST /mcp  rpc=tools/call  mcp-protocol-version=2025-11-25  -> 200 application/json
POST /mcp  rpc=tools/call  mcp-protocol-version=2025-11-25  -> 200 application/json

===== client mode: auto =====
protocol_version: 2026-07-28
server_info: name='family-clinic' title='Family Clinic Assistant' version='' description=None website_url=None icons=None

--- tool: search_handbook
title: Search the clinic handbook
annotations: {'read_only_hint': True, 'idempotent_hint': True, 'open_world_hint': False}
description: Search the Family Clinic patient handbook: hours, appointments, refills, billing,
insurance and other clinic policies.

Returns the few passages closest in meaning to the question, best first, each with its
section, heading, text and a similarity score from 0 to 1. A best score under about 0.45
means the handbook probably does not cover the question: say so instead of guessing.
Answer only from the returned text and name the section you used. Read-only.

input_schema: {"type": "object", "properties": {"question": {"description": "A patient's question in plain language, e.g. 'How long do refills take?'", "maxLength": 500, "minLength": 1, "title": "Question", "type": "string"}}, "required": ["question"], "title": "search_handbookArguments"}

--- tool: check_availability
title: Check open appointment times
annotations: {'read_only_hint': True, 'idempotent_hint': True, 'open_world_hint': False}
description: List the next open appointment times at the Family Clinic, soonest first.

Each result has the provider, the visit type, the start time (ISO 8601 with the clinic's
UTC offset) and a ready-to-show label. An empty list means nothing is open for that
filter. Read-only: this tool cannot book, hold or cancel an appointment.

input_schema: {"type": "object", "properties": {"visit_type": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "Kind of visit, e.g. 'annual physical'. Omit for any kind.", "title": "Visit Type"}, "on_or_after": {"anyOf": [{"format": "date", "type": "string"}, {"type": "null"}], "default": null, "description": "Earliest acceptable date as YYYY-MM-DD. Omit for the next openings.", "title": "On Or After"}}, "title": "check_availabilityArguments"}

--- call search_handbook
is_error: False
text: {
  "section": "Prescription refills",
  "heading": "Turnaround time",
  "content": "Refill requests take up to 3 business days to process. Business days are Monday to Friday, not counting holidays. A request sent on a Friday afternoon may not be ready until the following Wednesday. Once a refill is approved, we send it directly to your pharmacy and you will see the status change in the portal. Your pharmacy may need extra time to fill it.",
  "similarity": 0.572
}
text: {
  "section": "Prescription refills",
  "heading": null,
  "content": "This section covers how to request a refill of a medication that a Family Clinic provider already prescribes for you, and how long it takes.",
  "similarity": 0.449
}
text: {
  "section": "Prescription refills",
  "heading": "Controlled substances",
  "content": "Controlled substances cannot be refilled through a portal or pharmacy request. They require a visit with your provider at least every 3 months, and the prescription is renewed during that visit. These prescriptions cannot be called in by phone and are not eligible for 90-day supplies. Lost or stolen controlled prescriptions are not replaced early.",
  "similarity": 0.431
}
text: {
  "section": "Prescription refills",
  "heading": "When a visit is needed first",
  "content": "A provider can only keep refilling a medication for patients they see regularly. If it has been more than 12 months since your last visit, we will approve one 30-day refill and ask you to book an appointment before the next one. For many medications this refill visit can be a telehealth visit, so you do not need to come in.",
  "similarity": 0.424
}
structured: {
  "result": [
    {
      "section": "Prescription refills",
      "heading": "Turnaround time",
      "content": "Refill requests take up to 3 business days to process. Business days are Monday to Friday, not counting holidays. A request sent on a Friday afternoon may not be ready until the following Wednesday. Once a refill is approved, we send it directly to your pharmacy and you will see the status change in the portal. Your pharmacy may need extra time to fill it.",
      "similarity": 0.572
    },
    {
      "section": "Prescription refills",
      "heading": null,
      "content": "This section covers how to request a refill of a medication that a Family Clinic provider already prescribes for you, and how long it takes.",
      "similarity": 0.449
    },
    {
      "section": "Prescription refills",
      "heading": "Controlled substances",
      "content": "Controlled substances cannot be refilled through a portal or pharmacy request. They require a visit with your provider at least every 3 months, and the prescription is renewed during that visit. These prescriptions cannot be called in by phone and are not eligible for 90-day supplies. Lost or stolen controlled prescriptions are not replaced early.",
      "similarity": 0.431
    },
    {
      "section": "Prescription refills",
      "heading": "When a visit is needed first",
      "content": "A provider can only keep refilling a medication for patients they see regularly. If it has been more than 12 months since your last visit, we will approve one 30-day refill and ask you to book an appointment before the next one. For many medications this refill visit can be a telehealth visit, so you do not need to come in.",
      "similarity": 0.424
    }
  ]
}

--- HTTP requests made
POST /mcp  rpc=server/discover  mcp-protocol-version=2026-07-28  -> 200 application/json
POST /mcp  rpc=tools/list  mcp-protocol-version=2026-07-28  -> 200 application/json
POST /mcp  rpc=tools/call  mcp-protocol-version=2026-07-28  -> 200 application/json
```

Things to notice:

- Every request is a `POST /mcp` answered with one `application/json` body. There is no session id and no event stream.
- The legacy client makes three requests before its first tool call (`initialize`, `notifications/initialized`, `tools/list`). None of them is counted against the rate limit: only `tools/call` is.
- The 2026-07-28 client skips the handshake: `server/discover`, then straight to work. The same endpoint serves both.
- The empty question was rejected by the tool's input schema (`minLength: 1`) before the access check ran, and came back as a normal result with `is_error: True`, which is how a model sees tool failures.

## Path, method and header checks

Run with `curl` against the same scratch app:

```text
POST /mcp/ (trailing slash) -> 200
GET /mcp -> 405
DELETE /mcp -> 405
POST /mcp with Host: evil.example -> Invalid Host header (421)
POST /mcp with Origin: https://evil.example -> Invalid Origin header (403)
GET /static/styles.css -> 200
GET /health -> 200
```

- `/mcp/` answers directly instead of redirecting to `/mcp` (a 307 on a POST breaks some clients).
- `GET` and `DELETE` get 405: this server offers no event stream and has no session to delete.
- The Host and Origin checks are the SDK's DNS-rebinding protection, which is on for local runs and off in production (see D51 in `DECISIONS.md`).
- The existing routes and `/static` are untouched.
