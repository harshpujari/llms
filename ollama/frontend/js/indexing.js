import { api } from "./api.js";
import { attachmentIds, getAttachments, onAttachmentsChange, prune } from "./state.js";

// --- Indexing banner -----------------------------------------------------
// A file is searchable by its text seconds after upload, but summarizing it
// takes minutes on CPU. While anything attached to the chat is still being
// read, say so above the composer -- what's missing, and that replies may be
// slow -- then go quiet once it's all done. Each status is also passed on, so
// the attachment chips can show their own progress.

const indexingBar = document.getElementById("indexing");

let indexingPoll = null;
let indexingGen = 0; // drops a response that a newer refresh has overtaken
const listeners = [];

// Every /indexing response, as it lands; null when nothing is attached or the
// backend can't be reached.
export function onIndexingStatus(fn) {
  listeners.push(fn);
}

function publish(st) {
  for (const fn of listeners) fn(st);
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function progressLabel(p) {
  if (p.stage === "extracting") return "extracting";
  if (!p.searchable) return "indexing";
  if (!p.total) return "starting summaries";
  return `${p.done} of ${p.total} sections`;
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
  const retrying = st.files.filter(p => p.retrying);
  const moving = st.files.filter(p => !p.retrying);
  const current = st.files.find(p => p.active) || moving[0] || st.files[0];
  // Every unfinished file already searchable: only summaries are left, and
  // answers can use these documents now.
  const searchable = st.files.every(p => p.searchable);

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
    // A spinner only while something is actually moving.
    lead.className = moving.length ? "spin" : "alert";
    if (moving.length) lead.setAttribute("aria-hidden", "true");
    else lead.textContent = "⚠︎";
    if (moving.length) {
      text.append(line(
        strong(`${searchable ? "Summarizing" : "Reading"} ${moving.length} of ${plural(total, "document")}`),
        ` · ${current.name} · ${progressLabel(current)}`,
      ));
    }
    if (retrying.length) {
      const next = Math.min(...retrying.map(p => p.retrying.retry_in));
      const when = next ? `retrying in ${next}s` : "retrying now";
      text.append(line(
        strong(retrying.length === 1 ? `${retrying[0].name} hit an error` : `${retrying.length} documents hit errors`),
        ` · ${when}`,
      ));
    }
    text.append(line(searchable
      ? "They can already be searched; summaries add detail. Replies may be slower until it's done."
      : "Answers may miss what's in them, and replies may be slower until it's done."));
  }
  indexingBar.replaceChildren(lead, text);
}

async function refreshIndexing() {
  clearTimeout(indexingPoll);
  indexingPoll = null;
  const gen = ++indexingGen;
  const { folder_ids, file_ids } = attachmentIds();
  if (!folder_ids.length && !file_ids.length) {
    indexingBar.hidden = true;
    publish(null);
    return;
  }

  const query = [
    ...folder_ids.map(id => "folder_id=" + id),
    ...file_ids.map(id => "file_id=" + id),
  ].join("&");

  let st;
  try {
    st = await api(`/indexing?${query}`);
  } catch {
    // Not worth an error of its own: a dead backend already shows up in the
    // header and the chat. Hide, and look again in a while.
    if (gen !== indexingGen) return;
    indexingBar.hidden = true;
    publish(null);
    indexingPoll = setTimeout(refreshIndexing, 10000);
    return;
  }
  if (gen !== indexingGen) return; // attachments changed, or a newer refresh ran

  // Deleted since being attached. Pruning notifies, which starts a fresh
  // refresh for what's left -- this response still describes the old list.
  const { missing } = st;
  if (missing && (missing.folder_ids.length || missing.file_ids.length)) {
    const before = getAttachments();
    prune(missing);
    if (getAttachments() !== before) return;
  }

  renderIndexing(st);
  publish(st);
  if (st.files.length) indexingPoll = setTimeout(refreshIndexing, 3000);
}

// For callers that know something may have just changed -- an upload into
// an attached folder: refresh now, unless a poll is already on its way.
export function nudgeIndexing() {
  if (!indexingPoll) refreshIndexing();
}

onAttachmentsChange(refreshIndexing);
