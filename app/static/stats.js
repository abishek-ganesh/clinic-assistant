// Usage stats page.
// Fetches GET /api/stats?days=N (aggregates computed in Postgres) and fills in the page.
// Everything is inserted with textContent; the page never builds HTML from data.

const WINDOWS = [7, 30, 90];
const DEFAULT_WINDOW = 30;

// Plain names for the values usage_log stores. An unknown value is shown as it is stored.
const SURFACES = {
  demo: "Demo page",
  endpoint: "Class endpoint",
  booking: "Booking agent",
  mcp: "MCP server",
  eval: "CI eval",
};

const OUTCOMES = {
  answered: ["Answered", "the handbook covered it"],
  declined: ["Declined", "out of scope, or nothing relevant found"],
  emergency: ["Emergency", "fixed call-911 message, nothing generated"],
  denied: ["Denied", "over a rate limit or the spend cap"],
  error: ["Error", "a provider or the database failed"],
};

const $ = (id) => document.getElementById(id);
const buttons = document.querySelectorAll(".range-button");

let currentDays = DEFAULT_WINDOW;
let hasRendered = false;

buttons.forEach((button) => {
  button.addEventListener("click", () => load(Number(button.dataset.days)));
});
$("retry-button").addEventListener("click", () => load(currentDays));

const asked = Number(new URLSearchParams(location.search).get("days"));
load(WINDOWS.includes(asked) ? asked : DEFAULT_WINDOW);

// Fetch one window and show the numbers, the empty state or an error.
async function load(days) {
  currentDays = days;
  buttons.forEach((button) => {
    button.setAttribute("aria-pressed", String(Number(button.dataset.days) === days));
  });
  history.replaceState(null, "", days === DEFAULT_WINDOW ? location.pathname : `?days=${days}`);

  $("error").hidden = true;
  $("empty").hidden = true;
  // After the first render, keep the old numbers on screen (dimmed) until the new ones arrive
  if (hasRendered && !$("stats").hidden) $("stats").classList.add("is-stale");
  else $("status").hidden = false;

  let response;
  let body = null;
  try {
    response = await fetch(`/api/stats?days=${days}`);
    body = await response.json().catch(() => null);
  } catch {
    return showError("Check your internet connection, then try again.", "network");
  }
  if (!response.ok || !body || !body.totals) {
    const code = body?.error?.code ?? "unknown";
    return showError(
      "The database did not answer. This usually clears up in a few seconds, so try again.",
      `${response.status} ${code}`,
    );
  }

  $("status").hidden = true;
  $("stats").classList.remove("is-stale");
  if (body.totals.requests === 0) {
    $("stats").hidden = true;
    $("empty-title").textContent = `No requests in the last ${body.days} days.`;
    $("empty").hidden = false;
    return;
  }
  render(body);
  $("stats").hidden = false;
  hasRendered = true;
}

function showError(help, code) {
  $("status").hidden = true;
  $("stats").hidden = true;
  $("stats").classList.remove("is-stale");
  $("error-help").textContent = help;
  $("error-code").textContent = code;
  $("error").hidden = false;
}

function render(data) {
  const span = `${shortDate(data.first_day)} to ${shortDate(data.last_day)}`;
  $("window-note").textContent =
    `${span}: the last ${data.days} days, in Eastern time. Cost is the exception. It is counted for the calendar month, because the cap is monthly.`;
  renderTiles(data);
  renderDaily(data, span);
  renderSurfaces(data);
  renderOutcomes(data);
  renderLatency(data);
}

// The four headline numbers. A number that cannot be computed yet says so instead of showing 0.
function renderTiles(data) {
  const { totals, month, demo } = data;
  const perDay = totals.requests / data.days;
  setValue("tile-requests", integer(totals.requests));
  $("tile-requests-detail").textContent =
    perDay >= 1 ? `about ${integer(Math.round(perDay))} a day` : "fewer than 1 a day";

  setValue("tile-cost", money(month.spend_usd));
  const fill = $("tile-cost-fill");
  if (month.cap_usd) {
    const share = month.spend_usd / month.cap_usd;
    $("tile-cost-detail").textContent = `${percent(share)} of the ${money(month.cap_usd)} cap`;
    fill.style.width = `${Math.min(share, 1) * 100}%`;
    fill.classList.toggle("is-over", share >= 1);
  } else {
    $("tile-cost-detail").textContent = "No monthly cap is set";
    fill.style.width = "0";
  }

  const speed = data.latency.demo_answered;
  setValue("tile-latency", speed.count ? duration(speed.p50_ms) : null);
  $("tile-latency-detail").textContent = speed.count
    ? `p95 ${duration(speed.p95_ms)}, from ${plural(speed.count, "answer")}`
    : "No answered demo questions in this window";

  const declined = count(demo.outcomes, "declined");
  setValue("tile-decline", demo.completed ? percent(demo.decline_rate) : null);
  $("tile-decline-detail").textContent = demo.completed
    ? `${integer(declined)} of ${plural(demo.completed, "demo question")}`
    : "No demo questions in this window";
}

// One bar per day, a three-tick axis, and a readout line that follows the pointer.
function renderDaily(data, span) {
  const days = data.daily;
  const busiest = days.reduce((top, day) => (day.requests > top.requests ? day : top), days[0]);
  const top = axisTop(busiest.requests);
  const summary = `Busiest day: ${longDate(busiest.day)}, ${plural(busiest.requests, "request")}`;

  $("chart-axis").replaceChildren(...[top, top / 2, 0].map((tick) => el("span", integer(tick))));
  const middle = days[Math.floor((days.length - 1) / 2)];
  const labels = days.length > 2 ? [days[0], middle, days.at(-1)] : days;
  $("chart-dates").replaceChildren(...labels.map((day) => el("span", shortDate(day.day))));

  const readout = $("chart-readout");
  readout.textContent = summary;
  const bars = $("chart-bars");
  bars.setAttribute(
    "aria-label",
    `Bar chart of requests per day, ${span}. ${integer(data.totals.requests)} in total. ${summary}. The table below has every day.`,
  );
  bars.replaceChildren(
    ...days.map((day) => {
      const column = el("div");
      column.className = "day";
      const bar = el("div");
      bar.className = "day-bar";
      // A day with any requests always shows at least a sliver
      bar.style.height = day.requests ? `max(2px, ${(day.requests / top) * 100}%)` : "0";
      column.append(bar);
      column.addEventListener("pointerenter", () => {
        readout.textContent =
          `${longDate(day.day)}: ${plural(day.requests, "request")}, ${money(day.cost_usd, 4)} est. cost`;
      });
      return column;
    }),
  );
  bars.onpointerleave = () => { readout.textContent = summary; };

  // Newest first in the table: the most recent days are the ones people look for
  $("daily-rows").replaceChildren(
    ...[...days].reverse().map((day) =>
      row(longDate(day.day), [integer(day.requests), money(day.cost_usd, 4)]),
    ),
  );
}

function renderSurfaces(data) {
  const total = data.totals.requests;
  $("surface-rows").replaceChildren(
    ...data.by_surface.map((item) =>
      row(SURFACES[item.surface] ?? item.surface, [integer(item.requests), money(item.cost_usd, 4)], {
        share: item.requests / total,
      }),
    ),
  );
  $("token-note").textContent =
    `Tokens in this window: ${compact(data.totals.input_tokens)} in, ${compact(data.totals.output_tokens)} out.`;
}

function renderOutcomes(data) {
  const demo = data.demo;
  $("outcome-rows").replaceChildren(
    ...demo.outcomes.map((item) => {
      const [name, note] = OUTCOMES[item.outcome] ?? [item.outcome, ""];
      const share = item.requests / demo.requests;
      return row(name, [integer(item.requests), percent(share)], { share, note });
    }),
  );
  if (!demo.requests) {
    $("outcome-rows").replaceChildren(row("No demo questions in this window", ["", ""], { share: 0 }));
  }
  $("outcome-why").textContent = demo.completed
    ? `Rates count only the ${plural(demo.completed, "question")} the pipeline finished, so denied and error rows are left out: decline rate ${percent(demo.decline_rate)}, emergency rate ${percent(demo.emergency_rate)}. An emergency rate far above what a clinic expects points at a threshold set too low.`
    : "Decline and emergency rates appear once the demo page has answered a question in this window.";
}

function renderLatency(data) {
  const kinds = [
    ["Demo answers", "question in to cited answer out", data.latency.demo_answered],
    ["Class endpoint completions", "one model call", data.latency.endpoint_completions],
  ];
  $("latency-rows").replaceChildren(
    ...kinds.map(([name, note, speed]) =>
      row(
        name,
        speed.count
          ? [integer(speed.count), duration(speed.p50_ms), duration(speed.p95_ms)]
          : ["0", "No data", "No data"],
        { note },
      ),
    ),
  );
}

// One table row: a header cell, an optional bar cell, then right-aligned number cells.
function row(name, numbers, { share = null, note = "" } = {}) {
  const tr = el("tr");
  const head = el("th", name);
  head.scope = "row";
  if (note) {
    const small = el("span", note);
    small.className = "row-note";
    head.append(small);
  }
  tr.append(head);
  if (share !== null) {
    const cell = el("td");
    cell.className = "bar-col";
    if (share > 0) {
      const bar = el("div");
      bar.className = "bar";
      bar.style.width = `${share * 100}%`;
      cell.append(bar);
    }
    tr.append(cell);
  }
  numbers.forEach((text) => {
    const cell = el("td", text);
    cell.className = "num";
    tr.append(cell);
  });
  return tr;
}

function el(tag, text = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function setValue(id, text) {
  $(id).textContent = text ?? "No data";
  $(id).classList.toggle("is-missing", text === null);
}

function count(outcomes, name) {
  return outcomes.find((item) => item.outcome === name)?.requests ?? 0;
}

// The top of the y-axis: a round number at or above the busiest day, with a whole-number middle.
function axisTop(value) {
  if (value <= 10) return Math.max(4, Math.ceil(value / 2) * 2);
  const power = 10 ** Math.floor(Math.log10(value));
  return [1, 2, 4, 6, 8, 10].map((step) => step * power).find((step) => value <= step);
}

function integer(number) {
  return Number(number).toLocaleString("en-US");
}

function plural(number, noun) {
  return `${integer(number)} ${noun}${Number(number) === 1 ? "" : "s"}`;
}

function compact(number) {
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(number);
}

// Amounts under a cent need four places to show anything at all.
function money(amount, places = null) {
  const value = Number(amount);
  const digits = places ?? (value > 0 && value < 0.01 ? 4 : 2);
  return `$${value.toFixed(digits)}`;
}

function percent(share) {
  const value = Number(share) * 100;
  return `${value > 0 && value < 10 ? value.toFixed(1) : Math.round(value)}%`;
}

function duration(ms) {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

// "2026-11-04" is a calendar day, not an instant: build it in local time so it never shifts.
function parseDay(text) {
  const [year, month, day] = text.split("-").map(Number);
  return new Date(year, month - 1, day);
}

function shortDate(text) {
  return parseDay(text).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function longDate(text) {
  return parseDay(text).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}
