# The Family Clinic MCP server

The Model Context Protocol (MCP) is a standard way for an AI application to discover and call tools that live on someone else's server. Write a tool once as an MCP server and any MCP client (Claude Code, Claude Desktop, claude.ai, your own agent) can use it without custom glue code.

This app exposes two of its abilities that way:

```
https://clinic-assistant-mocha.vercel.app/mcp
```

Everything behind it is synthetic: the clinic is fictional and there is no patient data.

## The two tools

| Tool | Arguments | Returns |
|---|---|---|
| `search_handbook` | `question` (text, 1 to 500 characters) | the handbook passages closest in meaning to the question, best first: `section`, `heading`, `content`, `similarity` (0 to 1) |
| `check_availability` | `visit_type` (optional text), `on_or_after` (optional date, `YYYY-MM-DD`) | the next open appointment times, soonest first: `slot_id`, `provider`, `visit_type`, `starts_at`, `display` |

Both tools only read. There is no booking tool on purpose: booking changes something in the world, and an action like that needs a person to confirm it first. This server has no way to ask, so it does not offer the action.

Notice what `search_handbook` does **not** do: it does not write an answer. It hands back passages and the model in your client writes the answer. The retrieval half of RAG lives on our server; the generation half is whatever model you connected.

The code is in `app/mcp_server.py`. The tool descriptions there are worth reading closely, because they are the only thing a model sees when it decides whether and how to call a tool.

## Connect from Claude Code

```bash
claude mcp add --transport http family-clinic https://clinic-assistant-mocha.vercel.app/mcp
```

Check that it connected:

```bash
claude mcp list
```

Inside a Claude Code session, `/mcp` shows the server and its two tools. To remove it later: `claude mcp remove family-clinic`.

## Connect from claude.ai or Claude Desktop

Add it as a custom connector (the menu names change from time to time; these are current as of November 2026):

1. Open **Customize > Connectors**.
2. Click **+ Add**, then **Add custom connector**.
3. Name it `Family Clinic` and paste the URL above. Click **Continue**.
4. Under **Authentication** choose **No sign in**, then click **Add**.
5. In a chat, click **+** at the lower left, choose **Connectors**, and switch Family Clinic on.

A connector added on claude.ai also shows up in Claude Desktop and the mobile apps.

## Try it

One question per tool:

- **`search_handbook`:** "Using the Family Clinic tools, how long does a prescription refill take, and which handbook section says so?"
- **`check_availability`:** "What are the next open appointment times at the Family Clinic on or after December 1?"

Then try one the handbook cannot answer ("What is the clinic's policy on bringing pets?") and watch what the model does with low similarity scores. Finally, ask it to book one of the open times. It should tell you it can't, because no tool lets it.

## Limits

The server needs no sign-in, so it is rate limited per network address: 10 tool calls a minute, shared with the demo page. Listing the tools is free; only tool calls count. When you hit the limit the tool returns an error the model can read ("Too many requests in the last minute. Wait 60 seconds and try again."), and it will usually tell you exactly that.

One thing to know: claude.ai and Claude Desktop connectors call the server from Anthropic's cloud, not from your laptop, so everyone using a connector shares a few addresses and one allowance. Claude Code calls from your own machine.
