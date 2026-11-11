// Token page: send the email and class code, show the token once, fill in the code sample.

const $ = (id) => document.getElementById(id);

const ERRORS = {
  invalid_email: "Check the email address",
  invalid_class_code: "Enter the class code",
  wrong_code: "Class code not accepted",
  signups_closed: "Token requests are closed",
  rate_limited: "Too many tries",
  spend_cap: "The class budget is used up",
  kill_switch: "The service is paused",
};

let busy = false;

$("token-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  setBusy(true);
  $("error").hidden = true;
  try {
    const response = await fetch("/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("email").value, class_code: $("class-code").value }),
    });
    const data = await response.json();
    if (response.ok) showToken(data);
    else showError(data.error.code, data.error.message);
  } catch {
    showError("network", "The request did not reach the server. Check your connection and try again.");
  }
  setBusy(false);
});

function setBusy(value) {
  busy = value;
  // aria-disabled, not disabled, so the button keeps keyboard focus while the request runs
  $("submit-button").setAttribute("aria-disabled", String(value));
  $("submit-button").textContent = value ? "Working" : "Get my token";
}

function showError(code, message) {
  $("error-title").textContent = ERRORS[code] || "Something went wrong";
  $("error-help").textContent = message;
  $("error").hidden = false;
}

function showToken(data) {
  $("token-value").textContent = data.token;
  $("token-detail").textContent = data.replaced
    ? `For ${data.email}. Your earlier token has been turned off.`
    : `For ${data.email}. It cannot be shown again, but you can always request a new one.`;
  $("sample").textContent = sample(data.token);
  $("token-note").textContent = "This is the only time the token is shown. The server keeps a hash of it, not the token itself.";
  $("step-token").classList.remove("step-ghost");
  $("issued").hidden = false;
  $("class-code").value = "";
  $("step-token").scrollIntoView({ behavior: "smooth", block: "start" });
}

// The sample a student pastes into a notebook, with this site's address and their token
function sample(token) {
  return `from openai import OpenAI

client = OpenAI(
    base_url="${location.origin}/v1",
    api_key="${token}",
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
