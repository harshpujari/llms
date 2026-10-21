// --- Shared state ------------------------------------------------------------
// What the chat answers from: up to MAX_ATTACHMENTS folders and files, picked
// in the 📎 picker or the library panel, and read by the chat, its chips, and
// the indexing banner. Views that care subscribe, so none of them has to know
// which others exist.

// Mirrors MAX_ATTACHMENTS in backend/schemas/chat_schema.py, which enforces it.
export const MAX_ATTACHMENTS = 5;

// { kind: "folder", id, name } or { kind: "file", id, name, folder_id }
let attachments = [];
const listeners = [];

function changed() {
  for (const fn of listeners) fn(attachments);
}

export function getAttachments() {
  return attachments;
}

export function isAttached(kind, id) {
  return attachments.some(a => a.kind === kind && a.id === id);
}

// A file in an attached folder is already included: the picker shows it as
// "via folder" rather than offering it a second slot.
export function coveredByFolder(file) {
  return isAttached("folder", file.folder_id);
}

export function canAttach() {
  return attachments.length < MAX_ATTACHMENTS;
}

// Attaching a folder absorbs its files attached one by one, which frees their
// slots -- so a full list can still take the folder of a file it holds.
export function canAttachFolder(folderId) {
  return canAttach() || attachments.some(a => a.kind === "file" && a.folder_id === folderId);
}

// False when nothing changed: already attached, covered by its folder, or full.
export function attach(item) {
  if (isAttached(item.kind, item.id)) return false;
  if (item.kind === "file" && coveredByFolder(item)) return false;

  // A folder absorbs its files attached one by one -- they're covered now, so
  // their slots free up. Done before the limit check, so attaching the folder
  // of two attached files still works when the list is full.
  const rest = item.kind === "folder"
    ? attachments.filter(a => !(a.kind === "file" && a.folder_id === item.id))
    : attachments;
  if (rest.length >= MAX_ATTACHMENTS) return false;

  attachments = [...rest, item];
  changed();
  return true;
}

export function detach(kind, id) {
  const rest = attachments.filter(a => !(a.kind === kind && a.id === id));
  if (rest.length === attachments.length) return;
  attachments = rest;
  changed();
}

// A folder was deleted: it goes, and so does any of its files attached on
// their own -- in one change, so the chips redraw once.
export function forgetFolder(folderId) {
  const rest = attachments.filter(a =>
    !(a.kind === "folder" && a.id === folderId) && !(a.kind === "file" && a.folder_id === folderId));
  if (rest.length === attachments.length) return;
  attachments = rest;
  changed();
}

// The shape the chat request and /indexing both take.
export function attachmentIds() {
  return {
    folder_ids: attachments.filter(a => a.kind === "folder").map(a => a.id),
    file_ids: attachments.filter(a => a.kind === "file").map(a => a.id),
  };
}

// Drops what /indexing reports as deleted -- straight away, before SQLite can
// reuse a deleted row's id and the chip quietly points at something new.
export function prune(missing) {
  const gone = new Set([
    ...missing.folder_ids.map(id => "folder:" + id),
    ...missing.file_ids.map(id => "file:" + id),
  ]);
  const rest = attachments.filter(a => !gone.has(a.kind + ":" + a.id));
  if (rest.length === attachments.length) return;
  attachments = rest;
  changed();
}

export function onAttachmentsChange(fn) {
  listeners.push(fn);
}
