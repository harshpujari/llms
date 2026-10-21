import { api } from "./api.js";
import { getScope, onScopeChange } from "./state.js";

// --- Indexing banner -----------------------------------------------------
// A file is searchable by its text seconds after upload, but summarizing it
// takes minutes on CPU. While the chat's folder has files still being read,
// say so above the composer -- what's missing, and that replies may be slow --
// then go quiet once it's all done.

const indexingBar = document.getElementById("indexing");

let indexingPoll = null;
let indexingGen = 0; // drops a response that a newer refresh has overtaken

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function progressLabel(p) {
  if (p.stage === "extracting") return "extracting";
  if (!p.total) return "starting";
  return `${p.done}/${p.total} sections`;
}

function line(...parts) {
  const el = document.createElement("span");
  el.className = "line";
  el.append(...parts);
  return el;
}

function strong(text) {
  const el = document.createElement("strong");
  el.textContent = text;
  return el;
}

function renderIndexing(st) {
  const unfinished = st.files.length;
  indexingBar.hidden = !unfinished;
  if (!unfinished) return;

  // Failed files are excluded: they're never going to be read, and their row
  // already says why.
  const total = st.counts.extracting + st.counts.reading + st.counts.ready;
  const current = st.files.find(p => p.active) || st.files[0];

  const lead = document.createElement("span");
  const text = document.createElement("span");
  text.className = "grow";

  // Ollama unreachable: the worker is waiting to retry, so nothing is moving
  // and "reading" would be a lie.
  indexingBar.classList.toggle("paused", !!st.paused);
  if (st.paused) {
    lead.className = "alert";
    lead.textContent = "⚠︎";
    text.append(
      line(strong("Can't reach the model, so reading is paused"),
           ` · retrying in ${st.paused.retry_in}s`),
      line(`${plural(unfinished, "document")} not fully read yet. Answers may miss what's in them.`),
    );
  } else {
    lead.className = "spin";
    lead.setAttribute("aria-hidden", "true");
    text.append(
      line(strong(`Reading ${unfinished} of ${plural(total, "document")}`),
           ` · ${current.name} · ${progressLabel(current)}`),
      line("Answers may miss what's in them, and replies may be slower until it's done."),
    );
  }
  indexingBar.replaceChildren(lead, text);
}

async function refreshIndexing() {
  clearTimeout(indexingPoll);
  indexingPoll = null;
  const gen = ++indexingGen;
  const scope = getScope();
  if (!scope) {
    indexingBar.hidden = true;
    return;
  }

  let st;
  try {
    st = await api(`/indexing?folder_id=${scope.id}`);
  } catch {
    // Not worth an error of its own: a dead backend already shows up in the
    // header and the chat. Hide, and look again in a while.
    if (gen !== indexingGen) return;
    indexingBar.hidden = true;
    indexingPoll = setTimeout(refreshIndexing, 10000);
    return;
  }
  if (gen !== indexingGen) return; // scope changed, or a newer refresh ran

  renderIndexing(st);
  if (st.files.length) indexingPoll = setTimeout(refreshIndexing, 3000);
}

// For callers that know something may have just changed -- an upload into
// the chat's folder: refresh now, unless a poll is already on its way.
export function nudgeIndexing() {
  if (!indexingPoll) refreshIndexing();
}

onScopeChange(refreshIndexing);
