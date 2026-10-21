// --- Backend access, and the small helpers every view shares --------------

export const API = "http://localhost:8000";

export async function api(path, opts = {}) {
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    // FastAPI puts the message in `detail`; fall back if the body isn't JSON.
    let msg = "HTTP " + res.status;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

export function formatBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

export function icon(paths, size = 18, box = 20) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("viewBox", `0 0 ${box} ${box}`);
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = paths;
  return svg;
}

export const FOLDER_ICON = `<path d="M2.5 5.5a1.5 1.5 0 0 1 1.5-1.5h3.2l1.6 1.8h7.7a1.5 1.5 0 0 1 1.5 1.5v7.2a1.5 1.5 0 0 1-1.5 1.5H4a1.5 1.5 0 0 1-1.5-1.5z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>`;
export const DOC_ICON = `<path d="M5 2.5h6l4 4v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-14a1 1 0 0 1 1-1z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M11 2.5v4.5h4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>`;
export const CHEV_ICON = `<path d="M7.5 4.5 13 10l-5.5 5.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`;

// The three states are derived server-side from two nullable columns:
// no text and no error means it's still in the queue.
export function fileState(f) {
  if (f.extract_error) return "failed";
  if (f.text_chars == null) return "queued";
  return "done";
}

function extractionLabel(f) {
  const state = fileState(f);
  if (state === "failed") return "⚠︎ " + f.extract_error;
  if (state === "queued") return "extracting…";
  return `${f.text_chars.toLocaleString()} chars extracted`;
}

// `p` is the file's entry from /indexing, present only while it's unfinished.
export function isReading(f, p) {
  return fileState(f) === "done" && p && p.stage === "reading";
}

export function fileLabel(f, p) {
  const base = extractionLabel(f);
  if (!isReading(f, p)) return base;
  return `${base} · reading ${p.total ? `${p.done}/${p.total}` : "…"}`;
}
