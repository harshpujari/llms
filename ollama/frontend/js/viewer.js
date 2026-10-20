import { API, api, fileState } from "./api.js";
import { renderMarkdown } from "./markdown.js";

// --- File viewer ---------------------------------------------------------

const viewer = document.getElementById("viewer");
const viewerTitle = document.getElementById("viewer-title");
const viewerDoc = document.getElementById("viewer-doc");
const viewerText = document.getElementById("viewer-text");
const viewerChars = document.getElementById("viewer-chars");
const viewerDownload = document.getElementById("viewer-download");
const viewerSeg = document.getElementById("viewer-seg");

// What the browser will render in a frame. Everything else gets a card with a
// link -- an iframe of a .docx just downloads it or shows nothing.
const INLINE_VIEWABLE = new Set([
  ".pdf", ".txt", ".md", ".markdown", ".rst", ".csv", ".json", ".xml", ".html", ".htm",
]);

function extensionOf(name) {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i).toLowerCase();
}

function showPane(which) {
  for (const pane of viewer.querySelectorAll(".pane")) {
    pane.classList.toggle("showing", pane.dataset.pane === which);
  }
  for (const btn of viewerSeg.children) {
    btn.classList.toggle("on", btn.dataset.pane === which);
  }
}

viewerSeg.onclick = e => {
  const btn = e.target.closest("button");
  if (btn) showPane(btn.dataset.pane);
};

export function closeViewer() {
  viewer.classList.remove("open");
  viewer.setAttribute("aria-hidden", "true");
  // Drop the frame so the PDF stops holding memory and can't keep loading.
  viewerDoc.replaceChildren();
}

export function isViewerOpen() {
  return viewer.classList.contains("open");
}

document.getElementById("viewer-close").onclick = closeViewer;

export async function openViewer(f) {
  const ext = extensionOf(f.name);
  viewerTitle.textContent = f.name;
  // ?download=1 makes the server send Content-Disposition: attachment, so this
  // saves the file whatever its type -- rather than opening a PDF in a tab it
  // is already showing in the pane below.
  viewerDownload.href = `${API}/files/${f.id}/raw?download=1`;
  viewerDownload.setAttribute("aria-label", `Download ${f.name}`);
  viewerDownload.title = `Download the original ${extensionOf(f.name) || "file"}`;
  viewer.classList.add("open");
  viewer.setAttribute("aria-hidden", "false");
  showPane("doc");

  // --- original -----------------------------------------------------------
  if (INLINE_VIEWABLE.has(ext)) {
    const frame = document.createElement("iframe");
    frame.src = `${API}/files/${f.id}/raw`;
    frame.title = f.name;
    viewerDoc.replaceChildren(frame);
  } else {
    const card = document.createElement("div");
    card.className = "no-preview";
    const title = document.createElement("strong");
    title.textContent = `No preview for ${ext || "this format"}`;
    const note = document.createElement("span");
    note.textContent =
      "The browser can't render it. Use Open to download the original — " +
      "the extracted text beside it is what retrieval will use anyway.";
    card.append(title, note);
    viewerDoc.replaceChildren(card);
  }

  // --- extracted ----------------------------------------------------------
  const state = fileState(f);
  viewerChars.textContent = "";

  if (state === "queued") {
    viewerText.className = "";
    viewerText.textContent = "Still extracting…";
    return;
  }
  if (state === "failed") {
    viewerText.className = "";
    viewerText.textContent = "⚠︎ " + f.extract_error;
    return;
  }

  viewerText.className = "";
  viewerText.textContent = "Loading…";
  try {
    const doc = await api(`/files/${f.id}/text`);
    viewerText.className = "md";
    viewerText.innerHTML = renderMarkdown(doc.text_content || "");
    viewerChars.textContent = `· ${(doc.text_content || "").length.toLocaleString()} chars`;
  } catch (err) {
    viewerText.className = "";
    viewerText.textContent = "Couldn't load the extracted text: " + err.message;
  }
}
