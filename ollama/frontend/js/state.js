// --- Shared state ------------------------------------------------------------
// The folder the chat answers from: chosen in the library panel, read by the
// chat and the indexing banner. Views that care subscribe, so none of them has
// to know which others exist.

let scope = null; // { id, name } or null
const listeners = [];

export function getScope() {
  return scope;
}

export function setScope(next) {
  scope = next;
  for (const fn of listeners) fn(scope);
}

export function onScopeChange(fn) {
  listeners.push(fn);
}
