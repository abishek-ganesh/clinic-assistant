# MCP server used from Claude Code

A headless Claude Code run on Nov 11, 2026, connected to the live server at
`https://clinic-assistant-mocha.vercel.app/mcp` with `--mcp-config`. Tool results are shortened.

**Prompt:** Use the family-clinic tools. First: is the clinic open on Saturdays? Second: what is the earliest telehealth slot? Name the tool you used for each answer.

**Connection:** `family-clinic` connected

**Tools listed:** `mcp__family-clinic__check_availability`, `mcp__family-clinic__search_handbook` 

**Tool call:** `mcp__family-clinic__search_handbook` with `{"question": "Is the clinic open on Saturdays?"}`

**Tool call:** `mcp__family-clinic__check_availability` with `{"visit_type": "telehealth"}`

**Tool result (first 300 characters):**

```
{"result":[{"section":"Hours and locations","heading":"Saturday hours","content":"We are open on Saturdays at the Main Street location only, from 9:00 am to 1:00 pm. Riverside is closed on weekends. Saturday visits are for returning patients and are booked the same way as weekday visits. Both locati
```

**Tool result (first 300 characters):**

```
{"result":[{"slot_id":"kim:2026-11-12T09:00","provider":"Dr. Kim","visit_type":"telehealth","starts_at":"2026-11-12T09:00:00-05:00","display":"Thu Nov 12, 9:00 AM with Dr. Kim (video visit)"},{"slot_id":"kim:2026-11-12T09:20","provider":"Dr. Kim","visit_type":"telehealth","starts_at":"2026-11-12T09:
```

**Claude's answer:**

**1. Saturday hours** (tool: `search_handbook`)
Yes, but only at the Main Street location, 9:00 am to 1:00 pm. Riverside is closed on weekends, and both locations are closed on Sundays. Saturday visits are for returning patients. Source: handbook section "Hours and locations."

**2. Earliest telehealth slot** (tool: `check_availability`)
Thu Nov 12, 9:00 AM with Dr. Kim (video visit). Later openings that morning with Dr. Kim are at 9:20, 9:40, and 10:00.

These tools can't book appointments, so to grab that slot you'd book on the clinic's website.

