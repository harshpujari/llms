import { API, icon, FOLDER_ICON } from "./api.js";
import { renderMarkdown } from "./markdown.js";
import { getScope, setScope, onScopeChange } from "./state.js";

// --- Chat ----------------------------------------------------------------
// The conversation and the composer under it: the folder chip, sending, and
// streaming the reply in.

const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("input");
const send = document.getElementById("send");
const scopeBar = document.getElementById("scope");

// Ollama keeps no history, so the full transcript is resent every turn.
const history = [];

// The chip above the composer naming the folder the chat answers from.
function renderChip(next) {
  scopeBar.replaceChildren();
  scopeBar.hidden = !next;
  if (!next) return;

  const chip = document.createElement("span");
  chip.className = "chip";
  const ic = icon(FOLDER_ICON, 14);
  chip.appendChild(ic);
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = next.name;
  chip.appendChild(name);

  const clear = document.createElement("button");
  clear.type = "button";
  clear.setAttribute("aria-label", "Clear folder");
  clear.appendChild(icon(
    `<path d="M3 3l6 6M9 3l-6 6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>`,
    10, 12));
  clear.onclick = () => setScope(null);
  chip.appendChild(clear);

  scopeBar.appendChild(chip);
}

onScopeChange(renderChip);

function addBubble(who, text) {
  const row = document.createElement("div");
  row.className = "row " + who;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  row.appendChild(bubble);
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  return bubble;
}

// Grow the field with the text, up to the CSS max-height.
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = input.scrollHeight + "px";
});

// Enter sends, Shift+Enter makes a newline.
input.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

form.addEventListener("submit", async e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || send.disabled) return;

  addBubble("me", text);
  history.push({ role: "user", content: text });
  input.value = "";
  input.style.height = "auto";
  send.disabled = true;

  const bubble = addBubble("them", "");
  bubble.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>';
  let reply = "";

  // Re-rendering markdown on every token is wasted work at 40 tok/s; coalesce
  // to one paint per frame instead.
  let queued = false;
  const paint = () => {
    bubble.innerHTML = renderMarkdown(reply);
    log.scrollTop = log.scrollHeight;
  };
  const schedulePaint = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; paint(); });
  };

  try {
    const res = await fetch(API + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: history,
        // Ignored by the backend until retrieval lands; sent now so the two
        // sides don't need changing together later.
        folder_id: getScope()?.id ?? null,
      }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    // NDJSON: a chunk can split mid-line, so hold the remainder in the buffer.
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.trim()) continue;
        const msg = JSON.parse(line);
        if (msg.error) throw new Error(msg.error);
        if (msg.token) {
          if (!reply) bubble.classList.add("md"); // first token clears the dots
          reply += msg.token;
          schedulePaint();
        }
      }
    }

    if (reply) {
      paint(); // the last scheduled frame may not have run yet
      history.push({ role: "assistant", content: reply });
    } else {
      bubble.textContent = "(empty response)";
    }
  } catch (err) {
    bubble.classList.remove("md");
    bubble.classList.add("error");
    bubble.textContent = "⚠︎ " + err.message;
    history.pop(); // drop the unanswered turn so the next isn't poisoned
  } finally {
    send.disabled = false;
    input.focus();
  }
});
