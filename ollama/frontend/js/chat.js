import { API, icon, FOLDER_ICON, DOC_ICON } from "./api.js";
import { onIndexingStatus } from "./indexing.js";
import { renderMarkdown } from "./markdown.js";
import { attachmentIds, detach, getAttachments, onAttachmentsChange } from "./state.js";

// --- Chat ----------------------------------------------------------------
// The conversation and the composer under it: the attachment chips, sending,
// and streaming the reply in.

const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("input");
const send = document.getElementById("send");
const chipBar = document.getElementById("scope");

// Ollama keeps no history, so the full transcript is resent every turn.
const history = [];

// The latest /indexing response, which each chip reads its progress from.
let progress = null;

// What a chip says after its name, or null once there's nothing to say.
// Folders report only progress: a folder that merely holds an unreadable file
// would otherwise wear a warning forever, and that file's row already says so.
function chipStatus(item) {
  if (!progress) return null;

  if (item.kind === "file") {
    const bad = progress.failed.find(f => f.id === item.id);
    if (bad) return { kind: "failed", text: "can't read", title: bad.error };
    const p = progress.files.find(f => f.id === item.id);
    if (!p) return null;
    // Ollama unreachable: nothing is moving, so no spinner -- the banner says why.
    if (progress.paused) return { kind: "paused", text: "paused" };
    if (p.stage === "extracting") return { kind: "busy", text: "extracting…" };
    return { kind: "busy", text: p.total ? `reading ${p.done}/${p.total}` : "reading…" };
  }

  const busy = progress.files.filter(f => f.folder_id === item.id).length;
  if (!busy) return null;
  if (progress.paused) return { kind: "paused", text: "paused" };
  return { kind: "busy", text: `${busy} in progress` };
}

function chip(item) {
  const st = chipStatus(item);
  const el = document.createElement("span");
  el.className = "chip" + (st ? " is-" + st.kind : "");
  el.appendChild(icon(item.kind === "folder" ? FOLDER_ICON : DOC_ICON, 14));

  const name = document.createElement("span");
  name.className = "name";
  name.textContent = item.name;
  el.appendChild(name);

  if (st) {
    const label = document.createElement("span");
    label.className = "status";
    if (st.kind === "busy") {
      const spin = document.createElement("span");
      spin.className = "spin";
      spin.setAttribute("aria-hidden", "true");
      label.appendChild(spin);
    }
    label.append((st.kind === "failed" ? "⚠︎ " : "") + st.text);
    if (st.title) el.title = st.title;
    el.appendChild(label);
  }

  const remove = document.createElement("button");
  remove.type = "button";
  remove.setAttribute("aria-label", `Remove ${item.name}`);
  remove.appendChild(icon(
    `<path d="M3 3l6 6M9 3l-6 6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>`,
    10, 12));
  remove.onclick = () => detach(item.kind, item.id);
  el.appendChild(remove);
  return el;
}

// One chip per attachment, above the composer.
function renderChips() {
  const items = getAttachments();
  chipBar.hidden = !items.length;
  chipBar.replaceChildren(...items.map(chip));
}

onAttachmentsChange(renderChips);
onIndexingStatus(st => {
  progress = st;
  renderChips();
});

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
        ...attachmentIds(),
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
