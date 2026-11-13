// Family Clinic Assistant demo page.
// Sends one question to POST /api/ask and draws every step of the pipeline's response.
// Model output and handbook text are untrusted, so everything is inserted with textContent.

// The server decides and sends the thresholds it used with every answer (app/thresholds.py).
// The page only draws the lines, so these are replaced by the response's values.
let EMERGENCY_MIN = 0.35;
let IN_SCOPE_MIN = 0.25;
let RELEVANCE_MIN = 0.47;
const MAX_QUESTION_CHARS = 500;

// What to tell the visitor for each error code the API can return.
const ERRORS = {
  invalid_question: {
    title: "That question can't be sent.",
    help: "Questions need between 1 and 500 characters. Edit yours and ask again.",
    retry: false,
  },
  rate_limited: {
    title: "Too many questions in a short time.",
    help: "The demo allows a few questions per minute from each network. Wait about a minute, then try again.",
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
    title: "One of the model providers did not respond.",
    help: "Your question was fine. This usually clears up in a few seconds, so try again.",
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
const CODE_FOR_STATUS = { 422: "invalid_question", 429: "rate_limited", 502: "upstream_error", 503: "kill_switch" };

// Plain-words explanation for each decision_reason.
const REASONS = {
  out_of_scope: (data) =>
    `The in-scope probability, ${score(data.judgments.in_scope)}, is under the ${score(IN_SCOPE_MIN)} line. Code declined without asking the generative model.`,
  low_relevance: (data) =>
    `The best passage scored ${score(bestRelevance(data.passages))} relevance, under the ${score(RELEVANCE_MIN)} line. Nothing relevant enough was found, so code declined without asking the generative model.`,
  not_in_handbook: () =>
    "Both checks passed, but the generative model reported that the passages do not answer the question. Code showed this fixed message instead.",
  refusal: () =>
    "Both checks passed, but the generative model declined to answer. Code showed this fixed message instead.",
  emergency: (data) =>
    `The emergency probability, ${score(data.judgments.is_emergency)}, is at or above the ${score(EMERGENCY_MIN)} line. Code stopped the pipeline and showed a fixed message.`,
};

const $ = (id) => document.getElementById(id);

const form = $("ask-form");
const questionBox = $("question");
const askButton = $("ask-button");
const chips = document.querySelectorAll(".chip");

let isLoading = false;
let lastQuestion = "";
let timer = null;
let pinnedRank = null;

form.addEventListener("submit", (event) => {
  event.preventDefault();
  ask(questionBox.value);
});

questionBox.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    ask(questionBox.value);
  }
});

questionBox.addEventListener("input", () => { updateCharCount(); autosize(questionBox); });

chips.forEach((chip) => {
  chip.addEventListener("click", () => {
    if (isLoading) return;
    questionBox.value = chip.textContent;
    updateCharCount();
    autosize(questionBox);
    ask(chip.textContent);
  });
});

$("retry-button").addEventListener("click", () => ask(lastQuestion));

// Send the question and show the result or an error.
async function ask(rawQuestion) {
  if (isLoading) return;
  const question = rawQuestion.trim();
  if (question.length < 1 || question.length > MAX_QUESTION_CHARS) {
    showError("invalid_question", null);
    questionBox.focus();
    return;
  }

  lastQuestion = question;
  // The examples have done their job once something is asked; the visitor can reopen them
  $("examples").open = false;
  startLoading();

  let response;
  let body = null;
  try {
    response = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
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
  } else if (!body || !body.decision) {
    showError("unknown", response.status);
  } else {
    if (body.thresholds) {
      EMERGENCY_MIN = body.thresholds.emergency_min;
      IN_SCOPE_MIN = body.thresholds.in_scope_min;
      RELEVANCE_MIN = body.thresholds.relevance_min;
    }
    showResult(body);
  }
}

// Loading: the page cannot see which stage the server is in, so it shows elapsed time only.
function startLoading() {
  isLoading = true;
  setControlsDisabled(true);
  $("error").hidden = true;
  $("intro").hidden = true;
  $("results").hidden = true;
  $("hood").hidden = true;
  $("hood-waiting").hidden = false;

  const status = $("status");
  status.classList.remove("sr-only");
  status.hidden = false;
  $("status-text").textContent = "Judging the question and finding passages";
  const started = performance.now();
  const tick = () => {
    $("status-time").textContent = `${((performance.now() - started) / 1000).toFixed(1)} s`;
  };
  tick();
  timer = setInterval(tick, 100);
}

function stopLoading() {
  isLoading = false;
  clearInterval(timer);
  setControlsDisabled(false);
  $("status").hidden = true;
  $("status-time").textContent = "";
  $("hood-waiting").hidden = true;
}

// aria-disabled (not disabled) so a chip keeps keyboard focus while its question runs.
function setControlsDisabled(disabled) {
  [askButton, ...chips].forEach((button) => button.setAttribute("aria-disabled", String(disabled)));
}

function showError(code, status) {
  const error = ERRORS[code] ?? ERRORS.unknown;
  $("error-title").textContent = error.title;
  $("error-help").textContent = error.help;
  $("retry-button").hidden = !error.retry;
  $("error-code").textContent = status ? `${status} ${code}` : code;
  $("error").hidden = false;
  if ($("results").hidden) $("intro").hidden = false;
}

// Draw one /api/ask response.
function showResult(data) {
  const isEmergency = data.decision === "emergency";
  pinnedRank = null;

  $("results").hidden = false;
  $("results").dataset.decision = data.decision;
  $("intro").hidden = true;
  $("hood").hidden = false;

  showJudgments(data);
  showDecision(data);

  // An emergency stops the pipeline before retrieval, so those stages have nothing to show
  $("emergency").hidden = !isEmergency;
  $("answer").hidden = isEmergency;
  $("hood-evidence").hidden = isEmergency;
  $("hood-prompt").hidden = isEmergency;

  if (isEmergency) {
    $("emergency-text").textContent = data.answer.text;
  } else {
    showPassages(data);
    showAnswer(data);
    showPrompt(data.prompt);
  }

  const seconds = (data.latency_ms / 1000).toFixed(1);
  const model = data.model ?? "no generative model call";
  $("meta").textContent = `${model} · ${data.latency_ms.toLocaleString()} ms · judged by ${data.judgments.provider}`;
  $("answer-time").textContent = `${seconds} s`;

  // Tell screen readers the outcome. The emergency message announces itself (role="alert").
  const status = $("status");
  status.classList.add("sr-only");
  status.hidden = false;
  $("status-text").textContent = isEmergency ? "" : `${capitalize(data.decision)} in ${seconds} seconds. Results are below the question.`;

  if (isEmergency) scrollToResults($("emergency"));
  else scrollAnswerIntoView();
}

function showJudgments(data) {
  const { is_emergency, in_scope, calibrated } = data.judgments;

  const emergencyTripped = is_emergency >= EMERGENCY_MIN;
  drawMeter($("meter-emergency"), is_emergency, EMERGENCY_MIN, emergencyTripped, `alarm at ${score(EMERGENCY_MIN)}`,
    emergencyTripped ? "At or above the line: treated as an emergency." : "Below the line: not an emergency.");

  const scopeTripped = in_scope < IN_SCOPE_MIN;
  drawMeter($("meter-scope"), in_scope, IN_SCOPE_MIN, scopeTripped, `needs ≥ ${score(IN_SCOPE_MIN)}`,
    scopeTripped ? "Below the line: outside what the handbook covers." : "At or above the line: in scope.");

  document.querySelectorAll(".meter .tag").forEach((tag) => { tag.hidden = calibrated; });
  $("uncalibrated-note").hidden = calibrated;
}

// One probability as a bar from 0 to 1 with the threshold drawn as a line across it.
function drawMeter(meter, value, threshold, tripped, lineLabel, verdict) {
  meter.classList.toggle("is-tripped", tripped);
  meter.querySelector(".meter-value").textContent = score(value);
  meter.querySelector(".meter-fill").style.width = `${clamp01(value) * 100}%`;
  meter.querySelector(".meter-line").style.left = `${threshold * 100}%`;
  meter.querySelector(".meter-line-label").textContent = `· ${lineLabel}`;
  meter.querySelector(".meter-verdict").textContent = verdict;
}

function showDecision(data) {
  const words = { answered: "Answer", declined: "Decline", emergency: "Stop" };
  $("decision").dataset.decision = data.decision;
  $("decision-word").textContent = words[data.decision] ?? data.decision;
  $("decision-why").textContent = data.decision === "answered"
    ? "Not an emergency, in scope, and retrieval found a relevant enough passage, so code asked the generative model to write."
    : explainReason(data);
}

// One row per passage: rank, source and the reranker's relevance against the floor. Opening it shows the text.
function showPassages(data) {
  const cited = new Set(data.answer.citations ?? []);
  $("evidence-note").textContent = data.decision === "answered"
    ? "Vector search found the 10 nearest passages, and a reranker kept the 4 that best answer the question. Bars show that relevance score; the line is the floor. The answer may only use these."
    : "What retrieval found, scored by the reranker against the floor. Code declined, so none of it was used.";

  const items = data.passages.map((passage) => {
    const classes = ["passage"];
    if (cited.has(passage.rank)) classes.push("is-cited");
    if (passage.relevance >= RELEVANCE_MIN) classes.push("is-above");
    const item = el("li", classes.join(" "));
    item.id = `passage-${passage.rank}`;

    const details = el("details", "");
    const summary = el("summary", "");
    const source = el("span", "passage-source", passage.section);
    if (passage.heading) source.append(el("span", "passage-heading", passage.heading));

    const bar = el("span", "bar-track passage-bar");
    bar.setAttribute("aria-hidden", "true");
    const fill = el("span", "bar-fill");
    fill.style.display = "block";
    fill.style.width = `${clamp01(passage.relevance) * 100}%`;
    const marker = el("span", "bar-marker");
    marker.style.left = `${RELEVANCE_MIN * 100}%`;
    bar.append(fill, marker);

    summary.append(
      el("span", "passage-rank", String(passage.rank)),
      source,
      el("span", "passage-sim", score(passage.relevance)),
      bar,
    );
    summary.setAttribute("aria-label",
      `Passage ${passage.rank}: ${passage.section}${passage.heading ? `, ${passage.heading}` : ""}. Relevance ${score(passage.relevance)}, similarity ${score(passage.similarity)}${cited.has(passage.rank) ? ", cited" : ""}.`);
    details.append(summary, handbookText(passage.content));
    item.append(details);
    return item;
  });

  $("passages").replaceChildren(...items);
  if (items.length === 0) $("evidence-note").textContent = "Retrieval returned no passages.";
}

function showAnswer(data) {
  const answered = data.decision === "answered";
  $("answer").dataset.decision = data.decision;
  $("answer-kicker").textContent = answered
    ? "Answered from the handbook"
    : "Declined: a fixed message written by code";
  $("answer-why").textContent = answered ? "" : explainReason(data);
  $("answer-why").hidden = answered;

  const text = $("answer-text");
  text.replaceChildren();
  $("answer-sources").replaceChildren();
  if (!answered) {
    text.textContent = data.answer.text;
    return;
  }

  // The cited passages again as labelled pills, so a patient sees where the answer came from
  const byRank = new Map(data.passages.map((passage) => [passage.rank, passage]));
  for (const rank of data.answer.citations ?? []) {
    const passage = byRank.get(rank);
    if (passage) $("answer-sources").append(citationButton(rank, passage.heading ?? passage.section));
  }

  // Split "…three business days [1]." into text and citation buttons.
  const ranks = new Set(data.passages.map((passage) => passage.rank));
  const parts = data.answer.text.split(/(\[\d+\])/);
  for (const part of parts) {
    const match = part.match(/^\[(\d+)\]$/);
    const rank = match ? Number(match[1]) : null;
    if (rank !== null && ranks.has(rank)) {
      text.append(citationButton(rank));
    } else {
      text.append(part);
    }
  }
}

// A [n] in the answer, or a source pill under it when label is given. Hover or focus previews
// passage n in the inspector; click pins the highlight, opens the passage and scrolls to it.
function citationButton(rank, label) {
  const button = el("button", label ? "source-pill cite-target" : "cite cite-target", label ? "" : `[${rank}]`);
  if (label) button.append(el("span", "rank", String(rank)), label);
  button.type = "button";
  button.dataset.rank = rank;
  button.setAttribute("aria-label", `Source ${rank}${label ? `, ${label}` : ""}: show passage ${rank}`);
  button.setAttribute("aria-pressed", "false");

  button.addEventListener("mouseenter", () => highlight(rank, true));
  button.addEventListener("focus", () => highlight(rank, true));
  button.addEventListener("mouseleave", () => highlight(rank, pinnedRank === rank));
  button.addEventListener("blur", () => highlight(rank, pinnedRank === rank));
  button.addEventListener("click", () => {
    const previous = pinnedRank;
    pinnedRank = previous === rank ? null : rank;
    if (previous !== null && previous !== rank) highlight(previous, false);
    highlight(rank, true);
    document.querySelectorAll(".cite-target").forEach((cite) => {
      cite.setAttribute("aria-pressed", String(Number(cite.dataset.rank) === pinnedRank));
    });
    if (pinnedRank === rank) {
      window.showHood?.();
      const passage = $(`passage-${rank}`);
      passage.querySelector("details").open = true;
      passage.scrollIntoView({ block: "nearest", behavior: scrollBehavior() });
    }
  });
  return button;
}

function highlight(rank, on) {
  $(`passage-${rank}`)?.classList.toggle("is-highlighted", on);
}

function showPrompt(prompt) {
  $("prompt").open = false;
  $("prompt").hidden = !prompt;
  $("no-prompt").hidden = Boolean(prompt);
  $("prompt-system").textContent = prompt?.system ?? "";
  $("prompt-user").textContent = prompt?.user ?? "";
}

function explainReason(data) {
  const explain = REASONS[data.decision_reason];
  return explain ? explain(data) : "Code declined to answer this question.";
}

// Bring the results into view when they would otherwise land below the fold (phones mostly).
function scrollToResults(target) {
  if (target.getBoundingClientRect().top > window.innerHeight * 0.6) {
    target.scrollIntoView({ block: "start", behavior: scrollBehavior() });
  }
}

// Scroll just far enough that the whole answer card is on screen, keeping the steps above it
// in view where they fit. On a shared screen the answer is what the room is waiting for.
function scrollAnswerIntoView() {
  const answer = $("answer");
  const box = answer.getBoundingClientRect();
  if (box.top >= 0 && box.bottom <= window.innerHeight) return;
  answer.scrollIntoView({ block: box.height > window.innerHeight ? "start" : "nearest", behavior: scrollBehavior() });
}

function scrollBehavior() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

// Grow the one-line box with what is typed, up to about five lines.
function autosize(box) {
  box.style.height = "auto";
  box.style.height = `${Math.min(box.scrollHeight, 160)}px`;
}

function updateCharCount() {
  $("char-count").textContent = questionBox.value.length;
}

function bestRelevance(passages) {
  return Math.max(0, ...passages.map((passage) => passage.relevance));
}

// Handbook passages are Markdown. This draws the two things they use, lists and **bold**,
// by building elements. Everything else stays plain text, so no HTML is ever parsed.
function handbookText(markdown) {
  const box = el("div", "passage-content");
  for (const block of markdown.split(/\n\s*\n/)) {
    const lines = block.split("\n").filter((line) => line.trim());
    if (lines.length === 0) continue;
    if (lines.every((line) => line.startsWith("- "))) {
      const list = el("ul", "passage-list");
      for (const line of lines) list.append(withBold(el("li", ""), line.slice(2)));
      box.append(list);
    } else {
      box.append(withBold(el("p", ""), lines.join(" ")));
    }
  }
  return box;
}

// Append text to a node, turning **words** into <strong> elements.
function withBold(node, text) {
  text.split(/\*\*(.+?)\*\*/).forEach((part, index) => {
    if (part) node.append(index % 2 ? el("strong", "", part) : part);
  });
  return node;
}

// Create an element with a class and plain text. Never parses HTML.
function el(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function score(number) {
  return number.toFixed(2);
}

function clamp01(number) {
  return Math.min(1, Math.max(0, number));
}

function capitalize(word) {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

updateCharCount();
