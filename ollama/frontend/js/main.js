// --- Entry point -------------------------------------------------------------
// Loads every view and wires up the little that spans more than one. Each
// module sets itself up when imported:
//
//   api.js       backend access, and helpers every view shares
//   state.js     the folder the chat answers from
//   markdown.js  markdown -> HTML, for replies and extracted text
//   chat.js      the conversation, the composer, and the folder chip
//   library.js   the folders panel: folders, files, uploads
//   viewer.js    the file viewer
//   indexing.js  the "still reading" banner above the composer
//   picker.js    the 📎 menu: upload, or choose folders and files to attach
//
// No bundler: the browser loads these as native modules, straight from nginx.

import { API } from "./api.js";
import "./chat.js";
import { setPanel, isPanelOpen } from "./library.js";
import { setPicker, isPickerOpen } from "./picker.js";
import { closeViewer, isViewerOpen } from "./viewer.js";

const status = document.getElementById("status");

document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  // Topmost first: the viewer covers everything, and the picker is a popover
  // over the chat, so neither leaves the other open behind it.
  if (isViewerOpen()) closeViewer();
  else if (isPickerOpen()) setPicker(false, { refocus: true });
  else if (isPanelOpen()) setPanel(false);
});

fetch(API + "/health")
  .then(r => r.json())
  .then(d => { status.textContent = d.ok ? d.model : "Ollama unreachable"; })
  .catch(() => { status.textContent = "backend unreachable"; });
