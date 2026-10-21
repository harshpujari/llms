import { api, formatBytes, icon, FOLDER_ICON, DOC_ICON, CHEV_ICON, fileState, fileLabel, isReading } from "./api.js";
import { nudgeIndexing } from "./indexing.js";
import { getScope, setScope } from "./state.js";
import { openViewer } from "./viewer.js";

// --- Library -------------------------------------------------------------
// Two views in one panel, iOS navigation-stack style: the folder list, and one
// folder's files. The scope (state.js) is the folder the next message will be
// answered from -- tracked now, consumed once retrieval is wired up.

const panel = document.getElementById("panel");
const backdrop = document.getElementById("backdrop");
const panelBody = document.getElementById("panel-body");
const panelFoot = document.getElementById("panel-foot");
const panelTitle = document.getElementById("panel-title");
const panelBack = document.getElementById("panel-back");
const fileInput = document.getElementById("file-input");

let openFolder = null;
let folderPoll = null;

export function isPanelOpen() {
  return panel.classList.contains("open");
}

export function setPanel(open) {
  panel.classList.toggle("open", open);
  backdrop.classList.toggle("open", open);
  panel.setAttribute("aria-hidden", String(!open));
  if (open) showFolders();
}

document.getElementById("library-btn").onclick = () => setPanel(true);
document.getElementById("panel-close").onclick = () => setPanel(false);
backdrop.onclick = () => setPanel(false);
panelBack.onclick = () => showFolders();

function message(text) {
  const p = document.createElement("p");
  p.className = "empty";
  p.textContent = text;
  return p;
}

// Every name below goes in via textContent: filenames are user input, and this
// panel would otherwise be a clean injection path into the page.
function row({ iconPaths, name, meta, onOpen, onDelete, active, chevron, state }) {
  const el = document.createElement(onOpen ? "button" : "div");
  el.className = "item" + (active ? " active" : "") + (state ? " is-" + state : "");
  if (onOpen) {
    el.type = "button";
    el.onclick = onOpen;
  }

  const ic = icon(iconPaths);
  ic.classList.add("icon");
  el.appendChild(ic);

  const grow = document.createElement("span");
  grow.className = "grow";
  const n = document.createElement("span");
  n.className = "name";
  n.textContent = name;
  grow.appendChild(n);
  if (meta) {
    const m = document.createElement("span");
    m.className = "meta";
    m.textContent = meta;
    grow.appendChild(m);
  }
  el.appendChild(grow);

  if (onDelete) {
    const del = document.createElement("button");
    del.className = "del";
    del.type = "button";
    del.textContent = "Delete";
    del.onclick = e => { e.stopPropagation(); onDelete(); };
    el.appendChild(del);
  }
  if (chevron) {
    const c = icon(CHEV_ICON);
    c.classList.add("chev");
    el.appendChild(c);
  }
  return el;
}

async function showFolders() {
  openFolder = null;
  clearTimeout(folderPoll);
  panelTitle.textContent = "Library";
  panelBack.hidden = true;
  panelBody.replaceChildren(message("Loading…"));
  panelFoot.replaceChildren();

  let folders;
  try {
    folders = await api("/folders");
  } catch (err) {
    panelBody.replaceChildren(message("Couldn't load folders: " + err.message));
    return;
  }

  panelBody.replaceChildren();
  if (!folders.length) {
    panelBody.appendChild(message("No folders yet. Create one to group related files."));
  }
  for (const f of folders) {
    panelBody.appendChild(row({
      iconPaths: FOLDER_ICON,
      name: f.name,
      meta: `${f.file_count} file${f.file_count === 1 ? "" : "s"} · ${formatBytes(f.total_bytes)}`,
      active: getScope()?.id === f.id,
      chevron: true,
      onOpen: () => showFolder(f),
      onDelete: async () => {
        if (!confirm(`Delete "${f.name}" and its ${f.file_count} file(s)?`)) return;
        await api(`/folders/${f.id}`, { method: "DELETE" });
        if (getScope()?.id === f.id) setScope(null);
        showFolders();
      },
    }));
  }

  const add = document.createElement("button");
  add.className = "action";
  add.type = "button";
  add.textContent = "New Folder";
  add.onclick = async () => {
    const name = prompt("Folder name");
    if (!name || !name.trim()) return;
    try {
      await api("/folders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim() }),
      });
      showFolders();
    } catch (err) {
      alert(err.message);
    }
  };
  panelFoot.replaceChildren(add);
}

async function showFolder(folder) {
  openFolder = folder;
  panelTitle.textContent = folder.name;
  panelBack.hidden = false;
  panelBody.replaceChildren(message("Loading…"));

  // Any pending refresh belongs to the view we're leaving.
  clearTimeout(folderPoll);

  let files;
  const progress = new Map();
  try {
    const [list, st] = await Promise.all([
      api(`/folders/${folder.id}/files`),
      // Progress is a nicety: without it the rows just lose their "reading".
      api(`/indexing?folder_id=${folder.id}`).catch(() => null),
    ]);
    files = list;
    if (st) for (const p of st.files) progress.set(p.id, p);
  } catch (err) {
    panelBody.replaceChildren(message("Couldn't load files: " + err.message));
    return;
  }

  // Extraction runs on a background worker, so rows land as "extracting…" and
  // fill in one at a time. Poll only while something is actually unfinished:
  // briskly while extracting, slower while reading, which takes minutes.
  const extracting = files.some(f => fileState(f) === "queued");
  const reading = files.some(f => isReading(f, progress.get(f.id)));
  if (extracting || reading) {
    folderPoll = setTimeout(() => {
      if (openFolder && openFolder.id === folder.id) showFolder(folder);
    }, extracting ? 1200 : 3000);
  }

  // An upload into the chat's folder should bring the banner up now, not on
  // its next poll -- which, if it had nothing to show, isn't coming.
  if (getScope()?.id === folder.id) nudgeIndexing();

  panelBody.replaceChildren();
  if (!files.length) {
    panelBody.appendChild(message("Empty. Add files to chat about them."));
  }
  for (const f of files) {
    panelBody.appendChild(row({
      iconPaths: DOC_ICON,
      name: f.name,
      // text_chars is what retrieval will actually see, which is the number
      // worth surfacing -- a 4 MB PDF that extracted to 200 characters is a
      // problem you want to notice here, not after embedding.
      meta: `${formatBytes(f.bytes)} · ${fileLabel(f, progress.get(f.id))}`,
      state: isReading(f, progress.get(f.id)) ? "reading" : fileState(f),
      chevron: true,
      onOpen: () => openViewer(f),
      onDelete: async () => {
        await api(`/files/${f.id}`, { method: "DELETE" });
        showFolder(folder);
      },
    }));
  }

  const upload = document.createElement("button");
  upload.className = "action";
  upload.type = "button";
  upload.textContent = "Add Files";
  upload.onclick = () => fileInput.click();

  const info = document.createElement("button");
  info.className = "info-btn";
  info.type = "button";
  info.setAttribute("aria-label", "Supported formats");
  info.setAttribute("aria-expanded", "false");
  info.appendChild(icon(
    `<circle cx="10" cy="10" r="7.75" fill="none" stroke="currentColor" stroke-width="1.6"/>
     <path d="M10 9v5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
     <circle cx="10" cy="6.2" r="1" fill="currentColor"/>`));

  const notice = await formatsNotice();
  notice.hidden = true;

  // Filters the OS picker to the allowlist. A convenience only -- the browser
  // lets you override it, which is why the server checks the same list.
  if (formatsCache) {
    fileInput.accept = formatsCache.groups.flatMap(g => g.extensions).join(",");
  }
  info.onclick = () => {
    notice.hidden = !notice.hidden;
    info.setAttribute("aria-expanded", String(!notice.hidden));
  };

  const actions = document.createElement("div");
  actions.className = "action-row";
  actions.append(upload, info);

  const selected = getScope()?.id === folder.id;
  const use = document.createElement("button");
  use.className = "action secondary";
  use.type = "button";
  use.textContent = selected ? "Stop using in chat" : "Chat with this folder";
  use.onclick = () => {
    setScope(selected ? null : { id: folder.id, name: folder.name });
    showFolder(folder);
    if (!selected) setPanel(false);
  };

  panelFoot.replaceChildren(notice, actions, use);
}

// Fetched once from /formats so the allowlist has a single definition -- the
// backend rejects on the same list this renders.
let formatsCache = null;

async function formatsNotice() {
  const box = document.createElement("div");
  box.className = "notice";

  if (!formatsCache) {
    try {
      formatsCache = await api("/formats");
    } catch {
      box.textContent = "Couldn't load the list of supported formats.";
      return box;
    }
  }

  const dl = document.createElement("dl");
  for (const group of formatsCache.groups) {
    const dt = document.createElement("dt");
    dt.textContent = group.label;
    const dd = document.createElement("dd");
    dd.textContent = group.extensions.join("  ");
    dl.append(dt, dd);
  }
  box.appendChild(dl);

  // Any group carrying a note gets it called out -- today that's the PDF one.
  for (const group of formatsCache.groups) {
    if (!group.note) continue;
    const warn = document.createElement("strong");
    warn.className = "warn";
    warn.textContent = group.note;
    box.appendChild(warn);
  }

  const limit = document.createElement("span");
  limit.className = "limit";
  limit.textContent = `Up to ${formatsCache.max_mb} MB per file. Text is extracted on upload; anything we can't read is rejected.`;
  box.appendChild(limit);

  return box;
}

fileInput.onchange = async () => {
  if (!openFolder || !fileInput.files.length) return;
  const folder = openFolder;
  const body = new FormData();
  for (const file of fileInput.files) body.append("files", file);
  fileInput.value = "";  // so re-picking the same file fires change again

  // The request returns once the bytes are stored; extraction happens on the
  // worker afterwards, and the rows show their own progress.
  panelBody.replaceChildren(message("Uploading…"));
  try {
    const res = await api(`/folders/${folder.id}/files`, { method: "POST", body });
    if (res.failed.length) {
      alert(res.failed.map(f => `${f.name}\n${f.error}`).join("\n\n"));
    }
  } catch (err) {
    alert(err.message);
  }
  showFolder(folder);
};
