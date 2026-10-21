import { api, icon, FOLDER_ICON, DOC_ICON, CHEV_ICON, fileState, fileLabel, isReading } from "./api.js";
import { nudgeIndexing } from "./indexing.js";
import {
  MAX_ATTACHMENTS, attach, detach, isAttached, coveredByFolder, canAttach,
  getAttachments, onAttachmentsChange,
} from "./state.js";

// --- Attach picker -------------------------------------------------------
// The 📎 menu above the composer: upload new files, or choose the folders and
// files the chat answers from. Browsed like the library -- folders, then one
// folder's files -- except every row is a checkbox, all under one limit.

// Where chat uploads land. Mirrors MISC_SLUG in services/library_service.py.
const MISC_SLUG = "miscellaneous";

const UPLOAD_ICON = `<path d="M10 13.5V3.5M6 7.25 10 3.25l4 4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.5 12.5v3a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5v-3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>`;

const attachBtn = document.getElementById("attach");
const picker = document.getElementById("picker");
const pickerTitle = document.getElementById("picker-title");
const pickerBack = document.getElementById("picker-back");
const pickerCount = document.getElementById("picker-count");
const pickerNote = document.getElementById("picker-note");
const pickerBody = document.getElementById("picker-body");
const attachInput = document.getElementById("attach-input");
const messageInput = document.getElementById("input");
const pageHeader = document.querySelector("header");
const footer = picker.parentElement;

// What's on screen, kept so a checkbox can re-render the view without
// refetching it: { folders } for the list, { folder, files, progress } inside.
let shown = null;
let poll = null;
let gen = 0;      // drops a response that a newer view has overtaken
let note = "";    // the last upload's outcome, shown until the picker closes

export function isPickerOpen() {
  return !picker.hidden;
}

// Fits between the page header and the footer. The footer grows with the
// banner and chips -- five chips and a banner on a phone would otherwise push
// the picker's own header up under the page's -- and CSS can't see by how much.
function fit() {
  if (!isPickerOpen()) return;
  const room = footer.getBoundingClientRect().top - pageHeader.getBoundingClientRect().bottom - 16;
  picker.style.maxHeight = Math.max(200, Math.min(520, room)) + "px";
}

new ResizeObserver(fit).observe(footer);
window.addEventListener("resize", fit);

export function setPicker(open, { refocus = false } = {}) {
  picker.hidden = !open;
  attachBtn.setAttribute("aria-expanded", String(open));
  clearTimeout(poll);
  gen++;
  if (open) {
    note = "";
    fit();
    showFolders();
  } else if (refocus) {
    messageInput.focus();
  }
}

attachBtn.onclick = () => setPicker(!isPickerOpen());
document.getElementById("picker-done").onclick = () => setPicker(false, { refocus: true });
pickerBack.onclick = () => showFolders();

// A click anywhere else closes it; the 📎 button toggles on its own.
document.addEventListener("pointerdown", e => {
  if (isPickerOpen() && !picker.contains(e.target) && !attachBtn.contains(e.target)) {
    setPicker(false);
  }
});

function message(text) {
  const p = document.createElement("p");
  p.className = "empty";
  p.textContent = text;
  return p;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function renderHead() {
  const n = getAttachments().length;
  pickerCount.textContent = `${n} of ${MAX_ATTACHMENTS}`;
  pickerNote.textContent =
    note || (n >= MAX_ATTACHMENTS ? `Up to ${MAX_ATTACHMENTS} — remove one to add another.` : "");
  pickerNote.hidden = !pickerNote.textContent;
}

// One row: a checkbox (or, for a file that can't be read, nothing to tick),
// the name, and optionally a › to open it. Names go in via textContent --
// they're user input.
function pickRow({ iconPaths, name, meta, checked, disabled, onToggle, onOpen, state }) {
  const el = document.createElement("div");
  el.className = "item pick" + (state ? " is-" + state : "") + (disabled ? " is-disabled" : "");

  const main = document.createElement("label");
  main.className = "pick-main";
  if (onToggle) {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = checked;
    box.disabled = disabled;
    box.onchange = () => onToggle(box.checked);
    main.appendChild(box);
  } else {
    const gap = document.createElement("span");
    gap.className = "pick-gap";
    main.appendChild(gap);
  }

  const ic = icon(iconPaths);
  ic.classList.add("icon");
  main.appendChild(ic);

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
  main.appendChild(grow);
  el.appendChild(main);

  if (onOpen) {
    const open = document.createElement("button");
    open.type = "button";
    open.className = "pick-open";
    open.setAttribute("aria-label", `Open ${name}`);
    open.appendChild(icon(CHEV_ICON));
    open.onclick = onOpen;
    el.appendChild(open);
  }
  return el;
}

function uploadRow() {
  const el = document.createElement("button");
  el.type = "button";
  el.className = "item pick-upload";
  const ic = icon(UPLOAD_ICON);
  ic.classList.add("icon");
  const grow = document.createElement("span");
  grow.className = "grow";
  const n = document.createElement("span");
  n.className = "name";
  n.textContent = "Upload files…";
  const m = document.createElement("span");
  m.className = "meta";
  m.textContent = "Saved to Miscellaneous and attached";
  grow.append(n, m);
  el.append(ic, grow);
  el.onclick = () => attachInput.click();
  return el;
}

// Attaching a folder absorbs its files attached one by one, which frees their
// slots -- so a full picker can still take the folder of a file it holds.
function canTakeFolder(folderId) {
  return canAttach() || getAttachments().some(a => a.kind === "file" && a.folder_id === folderId);
}

function folderToggle(folder) {
  return on => on
    ? attach({ kind: "folder", id: folder.id, name: folder.name })
    : detach("folder", folder.id);
}

// --- the folder list -------------------------------------------------------

async function showFolders() {
  clearTimeout(poll);
  const g = ++gen;
  pickerTitle.textContent = "Attach";
  pickerBack.hidden = true;
  if (!shown || shown.folder) pickerBody.replaceChildren(message("Loading…"));
  renderHead();

  let folders;
  try {
    folders = await api("/folders");
  } catch (err) {
    if (g !== gen) return;
    pickerBody.replaceChildren(uploadRow(), message("Couldn't load folders: " + err.message));
    return;
  }
  // Closed while loading (an upload finishing after Done, say): nothing to draw.
  if (g !== gen || !isPickerOpen()) return;

  // Miscellaneous first: it's where uploads land, so it's what you'll want
  // next. The sort is stable, so the rest stay alphabetical.
  folders.sort((a, b) => (b.slug === MISC_SLUG) - (a.slug === MISC_SLUG));
  shown = { folders };
  renderFolders();
}

function renderFolders() {
  const rows = shown.folders.map(f => {
    const on = isAttached("folder", f.id);
    return pickRow({
      iconPaths: FOLDER_ICON,
      name: f.name,
      meta: plural(f.file_count, "file"),
      checked: on,
      disabled: !on && !canTakeFolder(f.id),
      onToggle: folderToggle(f),
      onOpen: () => showFolder(f),
    });
  });
  if (!rows.length) rows.push(message("No folders yet. Uploads go into Miscellaneous."));
  pickerBody.replaceChildren(uploadRow(), ...rows);
  renderHead();
}

// --- one folder's files ----------------------------------------------------

async function showFolder(folder) {
  clearTimeout(poll);
  const g = ++gen;
  pickerTitle.textContent = folder.name;
  pickerBack.hidden = false;
  if (!shown || shown.folder?.id !== folder.id) pickerBody.replaceChildren(message("Loading…"));
  renderHead();

  let files;
  const progress = new Map();
  try {
    const [list, st] = await Promise.all([
      api(`/folders/${folder.id}/files`),
      api(`/indexing?folder_id=${folder.id}`).catch(() => null),
    ]);
    files = list;
    if (st) for (const p of st.files) progress.set(p.id, p);
  } catch (err) {
    if (g !== gen) return;
    pickerBody.replaceChildren(message("Couldn't load files: " + err.message));
    return;
  }
  if (g !== gen || !isPickerOpen()) return;

  shown = { folder, files, progress };
  renderFolder();

  // Files still extracting or being read move along while you watch.
  if (files.some(f => fileState(f) === "queued" || isReading(f, progress.get(f.id)))) {
    poll = setTimeout(() => showFolder(folder), 3000);
  }
}

function renderFolder() {
  const { folder, files, progress } = shown;
  const whole = isAttached("folder", folder.id);
  const rows = [
    pickRow({
      iconPaths: FOLDER_ICON,
      name: `All of ${folder.name}`,
      meta: plural(files.length, "file"),
      checked: whole,
      disabled: !whole && !canTakeFolder(folder.id),
      onToggle: folderToggle(folder),
    }),
  ];

  for (const f of files) {
    const p = progress.get(f.id);
    if (fileState(f) === "failed") {
      // Nothing to tick: there's no text to answer from.
      rows.push(pickRow({ iconPaths: DOC_ICON, name: f.name, meta: fileLabel(f, p), state: "failed" }));
      continue;
    }
    const item = { kind: "file", id: f.id, name: f.name, folder_id: f.folder_id };
    const viaFolder = coveredByFolder(item);
    const on = viaFolder || isAttached("file", f.id);
    rows.push(pickRow({
      iconPaths: DOC_ICON,
      name: f.name,
      meta: (viaFolder ? "via folder · " : "") + fileLabel(f, p),
      checked: on,
      disabled: viaFolder || (!on && !canAttach()),
      onToggle: tick => tick ? attach(item) : detach("file", f.id),
      state: isReading(f, p) ? "reading" : fileState(f),
    }));
  }
  if (!files.length) rows.push(message("No files here yet."));
  pickerBody.replaceChildren(...rows);
  renderHead();
}

// A checkbox ticked here, a chip removed below, or the library changing the
// list: whichever view is showing redraws from what it already has.
onAttachmentsChange(() => {
  if (!isPickerOpen() || !shown) return;
  if (shown.folder) renderFolder();
  else renderFolders();
});

// --- uploading -------------------------------------------------------------

attachInput.onchange = async () => {
  if (!attachInput.files.length) return;
  const body = new FormData();
  for (const file of attachInput.files) body.append("files", file);
  attachInput.value = "";  // so re-picking the same file fires change again

  clearTimeout(poll);
  gen++;
  pickerBody.replaceChildren(message("Uploading…"));

  let res;
  try {
    res = await api("/files", { method: "POST", body });
  } catch (err) {
    note = "Upload failed: " + err.message;
    showFolders();
    return;
  }

  // Attach what fits -- the limit wins over the upload. A file already covered
  // by an attached Miscellaneous counts as attached.
  let over = 0;
  for (const f of res.saved) {
    const item = { kind: "file", id: f.id, name: f.name, folder_id: f.folder_id };
    if (!coveredByFolder(item) && !attach(item)) over++;
  }

  const lines = res.failed.map(f => `${f.name}: ${f.error}`);
  if (over) {
    lines.push(`Attached ${res.saved.length - over} — the limit is ${MAX_ATTACHMENTS}. ` +
               `The rest are in Miscellaneous.`);
  }
  note = lines.join("\n");

  // Show where they went, extracting as you watch.
  nudgeIndexing();
  showFolder(res.folder);
};

// Filters the OS file dialog to what the backend accepts, which checks the
// same list again regardless.
api("/formats")
  .then(f => { attachInput.accept = f.groups.flatMap(g => g.extensions).join(","); })
  .catch(() => {});
