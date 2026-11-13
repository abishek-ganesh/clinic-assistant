// Booking page.
// Each turn goes to POST /api/book and the page draws what came back: the reply, every tool
// call the model made, and any proposal waiting for a person to confirm.
// The server keeps no conversation, so the page holds the text turns and sends them back.
// Model output is untrusted, so everything is inserted with textContent.

const MAX_MESSAGE_CHARS = 500;

// What to tell the visitor for each error code the API can return.
const ERRORS = {
  invalid_request: {
    title: "That could not be sent.",
    help: "Messages need between 1 and 500 characters, and a conversation can only get so long. Edit your message, or start over.",
    retry: false,
  },
  rate_limited: {
    title: "Too many messages in a short time.",
    help: "The demo allows a few requests per minute from each network. Wait about a minute, then try again.",
    retry: true,
  },
  spend_cap: {
    title: "The demo has reached its monthly budget.",
    help: "It is paused until the budget resets or is raised, so retrying will not help. Let the instructor know.",
    retry: false,
  },
  kill_switch: {
    title: "The demo is switched off right now.",
    help: "The instructor has paused it. Try again later, or ask them to switch it back on.",
    retry: false,
  },
  upstream_error: {
    title: "The model or the schedule did not respond.",
    help: "Your message was fine and nothing was booked by this attempt. This usually clears up in a few seconds, so try again.",
    retry: true,
  },
  network: {
    title: "Could not reach the server.",
    help: "Check your internet connection, then try again.",
    retry: true,
  },
  unknown: {
    title: "Something went wrong.",
    help: "The server sent a response this page did not understand. Try again, and tell the instructor if it keeps happening.",
    retry: true,
  },
};

// Used when an error response has no recognizable code.
const CODE_FOR_STATUS = { 422: "invalid_request", 429: "rate_limited", 502: "upstream_error", 503: "kill_switch" };

const VISIT_LABELS = {
  returning: "Returning patient visit, 20 minutes",
  physical: "Annual physical, 40 minutes",
  telehealth: "Telehealth video visit, 20 minutes",
};

// Who wrote the reply, by outcome. A proposal, a booking and the fixed messages are written by code.
const CODE_WRITTEN = Object.fromEntries(
  ["proposed", "booked", "not_booked", "refused", "step_limit"].map((outcome) => [outcome, "Written by code"]),
);

const $ = (id) => document.getElementById(id);

const form = $("book-form");
const messageBox = $("message");
const chips = document.querySelectorAll(".chip");
const buttons = [$("send-button"), $("confirm-button"), $("decline-button"), ...chips];

// The whole conversation state. history is what the server sent back last turn.
let history = [];
let proposal = null;
let turnCount = 0;
let isLoading = false;
let lastPayload = null;
let timer = null;

form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage(messageBox.value);
});

messageBox.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage(messageBox.value);
  }
});

messageBox.addEventListener("input", () => { updateCharCount(); autosize(messageBox); });

chips.forEach((chip) => {
  chip.addEventListener("click", () => sendMessage(chip.textContent));
});

$("confirm-button").addEventListener("click", () => {
  if (isLoading || !proposal) return;
  addBubble("user", `Confirm: ${proposal.slot.display}`, "Button press");
  // The signed confirmation goes back exactly as the server issued it
  send({ action: "confirm", confirmation: proposal.confirmation });
});

$("decline-button").addEventListener("click", () => {
  if (isLoading || !proposal) return;
  addBubble("user", `Decline: ${proposal.slot.display}`, "Button press");
  send({ action: "decline" });
});

$("retry-button").addEventListener("click", () => send(lastPayload));
$("reset-button").addEventListener("click", startOver);
$("start-over").addEventListener("click", startOver);

function sendMessage(rawMessage) {
  if (isLoading) return;
  const message = rawMessage.trim();
  if (message.length < 1 || message.length > MAX_MESSAGE_CHARS) {
    showError("invalid_request", null);
    messageBox.focus();
    return;
  }
  addBubble("user", message);
  messageBox.value = "";
  updateCharCount();
  autosize(messageBox);
  send({ action: "message", message });
}

// One turn: send the earlier turns plus this one, then draw the response or an error.
async function send(payload) {
  if (isLoading || !payload) return;
  lastPayload = payload;
  startLoading();

  let response;
  let body = null;
  try {
    response = await fetch("/api/book", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history, ...payload }),
    });
    body = await response.json().catch(() => null);
  } catch {
    stopLoading();
    showError("network", null);
    return;
  }

  stopLoading();
  if (!response.ok) {
    const code = body?.error?.code ?? CODE_FOR_STATUS[response.status] ?? "unknown";
    showError(code, response.status);
  } else if (!body || !body.outcome || !Array.isArray(body.history)) {
    showError("unknown", response.status);
  } else {
    showTurn(body);
  }
}

// Draw one /api/book response.
function showTurn(data) {
  history = data.history;
  turnCount += 1;

  addBubble("agent", data.reply, CODE_WRITTEN[data.outcome] ?? "Agent", Boolean(CODE_WRITTEN[data.outcome]));
  addTraceTurn(data);
  showProposal(data.proposal);
  const bookedCard = data.booking ? showBooking(data.booking) : null;

  $("prompt").hidden = false;
  $("prompt-system").textContent = data.prompt.system;
  $("prompt-tools").textContent = JSON.stringify(data.prompt.tools, null, 2);

  const calls = data.model_calls === 1 ? "1 model call" : `${data.model_calls} model calls`;
  $("meta").textContent = data.model_calls
    ? `${data.model} · ${calls} · ${data.input_tokens} in + ${data.output_tokens} out tokens · ${data.latency_ms} ms`
    : `No model call · code only · ${data.latency_ms} ms`;
  $("start-over").hidden = false;

  // Tell screen readers what happened. The reply itself is announced by the chat log.
  const status = $("status");
  status.classList.add("sr-only");
  status.hidden = false;
  $("status-text").textContent = data.proposal
    ? "The agent suggested a time. Confirm and Decline buttons are at the end of the conversation."
    : data.booking
      ? `Booked. Confirmation code ${data.booking.confirmation_code}.`
      : "The agent replied.";

  if (bookedCard) scrollIntoViewIfBelow(bookedCard);
  else if (data.proposal) scrollIntoViewIfBelow($("proposal"));
}

// A turn in the conversation column. label names who wrote it when that is worth saying.
function addBubble(who, text, label, dashed) {
  const log = $("chat-log");
  log.querySelector(".chat-empty")?.remove();
  const item = el("li", `bubble bubble-${who}${dashed ? " bubble-code" : ""}`);
  if (label) item.append(el("span", "eyebrow", label));
  item.append(text);
  log.append(item);
  log.scrollTop = log.scrollHeight;
}

// The tool calls of one turn, in the order the model made them.
function addTraceTurn(data) {
  const log = $("trace-log");
  log.querySelector(".chat-empty")?.remove();

  const turn = el("li", "trace-turn");
  const seconds = (data.latency_ms / 1000).toFixed(1);
  // A booking step is code acting on the Confirm press, not a tool the model called
  const toolCalls = data.steps.filter((step) => step.by !== "code").length;
  const calls = `${toolCalls} tool ${toolCalls === 1 ? "call" : "calls"}`;
  const byCode = toolCalls < data.steps.length ? " · booked by code" : "";
  const summary = data.model_calls === 0 ? "No model call" : calls;
  turn.append(el("p", "eyebrow", `Turn ${turnCount} · ${summary}${byCode} · ${seconds} s`));
  if (data.steps.length === 0) {
    turn.append(el("p", "trace-none", "No tool calls. The model replied from the conversation alone."));
  }
  for (const step of data.steps) turn.append(toolCall(step));

  log.append(turn);
  // Start of the newest turn, not the very bottom, so its first tool call is readable
  log.scrollTop = turn.offsetTop - log.offsetTop - 12;
}

// One tool call: its name, what code did with it, the arguments and the result.
function toolCall(step) {
  const [kind, tag] = describeStep(step);
  const card = el("div", "tool-call");
  card.dataset.kind = kind;
  card.dataset.by = step.by ?? "model";

  const head = el("div", "tool-head");
  head.append(el("span", "tool-name", step.tool), el("span", "tool-tag eyebrow", tag));

  const io = el("div", "tool-io");
  io.append(
    el("span", "eyebrow", "Input"),
    el("pre", "", JSON.stringify(step.input, null, 2)),
    el("span", "eyebrow", "Result"),
    el("pre", "", JSON.stringify(step.result, null, 2)),
  );
  io.querySelectorAll("pre").forEach((pre) => { pre.tabIndex = 0; });

  card.append(head, io);
  return card;
}

function describeStep(step) {
  if (step.by === "code") return step.is_error ? ["refused", "Code could not book"] : ["ran", "Booked by code, no model"];
  if (step.is_error) return ["refused", "Rejected by code"];
  if (step.tool === "propose_slot") return ["proposed", "Ended the turn"];
  return ["ran", "Ran"];
}

// The pending proposal: a card at the end of the conversation with Confirm and Decline.
// One card element is reused; it goes back to its holder when there is no proposal.
function showProposal(next) {
  proposal = next;
  const card = $("proposal");
  $("signed").hidden = !proposal;
  if (!proposal) {
    card.hidden = true;
    $("proposal-home").append(card);
    return;
  }
  $("proposal-slot").textContent = proposal.slot.display;
  $("proposal-type").textContent = VISIT_LABELS[proposal.slot.visit_type] ?? proposal.slot.visit_type;
  $("proposal-signed").textContent = JSON.stringify(proposal.confirmation, null, 2);
  const log = $("chat-log");
  log.append(card);
  card.hidden = false;
  log.scrollTop = log.scrollHeight;
}

// A finished booking, added to the conversation with the code the database returned.
function showBooking(booking) {
  const card = el("li", "chat-card booked");
  const what = el("div", "");
  what.append(el("p", "booked-label", "Booked"), el("p", "booked-slot", booking.display));
  const code = el("div", "booked-code-box");
  code.append(el("p", "booked-label", "Confirmation code"), el("p", "booked-code", booking.confirmation_code));
  card.append(what, code, el("p", "booked-note", "Booked by code after your confirmation. This appointment exists only in the demo's made-up schedule."));
  const log = $("chat-log");
  log.append(card);
  log.scrollTop = log.scrollHeight;
  return card;
}

function startOver() {
  if (isLoading) return;
  history = [];
  turnCount = 0;
  lastPayload = null;
  showProposal(null);
  $("chat-log").replaceChildren(el("li", "chat-empty", "Say what kind of visit you want and roughly when, or pick an example below."));
  $("trace-log").replaceChildren(el("li", "chat-empty", "Each tool the model calls shows up here with its arguments and the result code sent back."));
  $("error").hidden = true;
  $("prompt").hidden = true;
  $("status").hidden = true;
  $("start-over").hidden = true;
  $("meta").textContent = "";
  messageBox.focus();
}

// Loading: the page cannot see which tool the server is running, so it shows elapsed time only.
function startLoading() {
  isLoading = true;
  setControlsDisabled(true);
  $("error").hidden = true;

  const status = $("status");
  status.classList.remove("sr-only");
  status.hidden = false;
  const started = performance.now();
  const tick = () => {
    const seconds = ((performance.now() - started) / 1000).toFixed(1);
    $("status-text").textContent = `The agent is working: calling the model and running its tools. ${seconds} s`;
  };
  tick();
  timer = setInterval(tick, 100);
}

function stopLoading() {
  isLoading = false;
  clearInterval(timer);
  setControlsDisabled(false);
  $("status").hidden = true;
}

// aria-disabled (not disabled) so a button keeps keyboard focus while its turn runs.
function setControlsDisabled(disabled) {
  buttons.forEach((button) => button.setAttribute("aria-disabled", String(disabled)));
}

function showError(code, status) {
  const error = ERRORS[code] ?? ERRORS.unknown;
  $("error-title").textContent = error.title;
  $("error-help").textContent = error.help;
  $("retry-button").hidden = !error.retry || !lastPayload;
  $("reset-button").hidden = history.length === 0;
  $("error-code").textContent = status ? `${status} ${code}` : code;
  $("error").hidden = false;
}

// Bring a new card into view when it landed below the fold (phones mostly).
function scrollIntoViewIfBelow(target) {
  if (target.getBoundingClientRect().top > window.innerHeight * 0.6) {
    const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ block: "start", behavior: smooth ? "smooth" : "auto" });
  }
}

// Grow the one-line box with what is typed, up to about five lines.
function autosize(box) {
  box.style.height = "auto";
  box.style.height = `${Math.min(box.scrollHeight, 160)}px`;
}

function updateCharCount() {
  $("char-count").textContent = messageBox.value.length;
}

// Create an element with a class and plain text. Never parses HTML.
function el(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

updateCharCount();
