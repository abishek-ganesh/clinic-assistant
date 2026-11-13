// Token page. Two steps: ask for a link (email + class code), then open the emailed link
// and press a button to see the token once. The code sample is filled in on load.

const $ = (id) => document.getElementById(id);

const ERRORS = {
  invalid_email: "Check the email address",
  invalid_class_code: "Enter the class code",
  wrong_code: "Class code not accepted",
  signups_closed: "Token requests are closed",
  too_many_links: "Too many links for this address",
  rate_limited: "Too many tries",
  spend_cap: "The class budget is used up",
  kill_switch: "The service is paused",
  link_invalid: "This link does not work",
  link_used: "This link was already used",
  link_expired: "This link has expired",
  invalid_link: "The link is incomplete",
};
const NETWORK_MESSAGE = "The request did not reach the server. Check your connection and try again.";

// The emailed link ends in #link=SECRET. Browsers never send that part to a server, and we
// take it out of the address bar straight away so it is not left in the history.
const linkSecret = new URLSearchParams(location.hash.slice(1)).get("link");
if (linkSecret) {
  history.replaceState(null, "", location.pathname);
  $("step-token").hidden = false;
  $("token-note").textContent = "You opened a link from your email. One more press shows the token.";
  $("reveal").hidden = false;
  $("step-token").scrollIntoView({ block: "start" });
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return { ok: response.ok, data: await response.json() };
}

function setBusy(button, busy, label) {
  // aria-disabled, not disabled, so the button keeps keyboard focus while the request runs
  button.setAttribute("aria-disabled", String(busy));
  button.textContent = busy ? "Working" : label;
}

$("sample").textContent = sample();

// Ask for a link
$("token-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("submit-button");
  if (button.getAttribute("aria-disabled") === "true") return;
  setBusy(button, true);
  $("error").hidden = true;
  $("sent").hidden = true;
  try {
    const { ok, data } = await post("/api/token", {
      email: $("email").value,
      class_code: $("class-code").value,
    });
    if (ok) {
      $("sent-text").textContent = `We sent a link to ${data.email}. It works once and expires in ${data.expires_minutes} minutes.`;
      $("sent").hidden = false;
      $("class-code").value = "";
    } else {
      showError("error", data.error);
    }
  } catch {
    showError("error", { code: "network", message: NETWORK_MESSAGE });
  }
  setBusy(button, false, "Email me a link");
});

// Redeem the link: a person presses the button. Opening the page alone never uses the link up, so a
// mail scanner that visits links before you do cannot spend it.
$("reveal-button").addEventListener("click", async () => {
  const button = $("reveal-button");
  if (button.getAttribute("aria-disabled") === "true") return;
  setBusy(button, true);
  $("link-error").hidden = true;
  try {
    const { ok, data } = await post("/api/token/redeem", { link: linkSecret });
    if (ok) {
      showToken(data);
      return;
    }
    showError("link-error", data.error);
    // A link the server refused will never work: hide the button. Other errors can be retried.
    if (data.error.code.startsWith("link_")) $("reveal").hidden = true;
  } catch {
    showError("link-error", { code: "network", message: NETWORK_MESSAGE });
  }
  setBusy(button, false, "Show my token");
});

function showError(id, error) {
  $(`${id}-title`).textContent = ERRORS[error.code] || "Something went wrong";
  $(`${id}-help`).textContent = error.message;
  $(id).hidden = false;
}

function showToken(data) {
  $("reveal").hidden = true;
  $("token-value").textContent = data.token;
  $("token-detail").textContent = data.replaced
    ? `For ${data.email}. Your earlier token has been turned off.`
    : `For ${data.email}. It cannot be shown again, but you can always request a new one.`;
  $("token-note").textContent = "This is the only time the token is shown. The server keeps a hash of it, not the token itself.";
  $("issued").hidden = false;
}

// The sample a student pastes into a notebook: this site's address, the token from the environment
function sample() {
  return `import os
from openai import OpenAI

client = OpenAI(
    base_url="${location.origin}/v1",
    api_key=os.environ["BOOTCAMP_TOKEN"],
)

reply = client.chat.completions.create(
    model="bootcamp",
    messages=[{"role": "user", "content": "Say hello to the class."}],
)
print(reply.choices[0].message.content)`;
}

function copyButton(buttonId, sourceId) {
  const button = $(buttonId);
  const label = button.textContent;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($(sourceId).textContent);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select and copy";
    }
    setTimeout(() => { button.textContent = label; }, 2000);
  });
}

copyButton("copy-token", "token-value");
copyButton("copy-sample", "sample");
