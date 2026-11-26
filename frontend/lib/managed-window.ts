import type { SessionInfo } from "./session";

const STORAGE_KEY = "dudu:managed-window";
const ID = /^[0-9a-f]{32}$/;
const MARKER_TIME_MS = 15_000;
type Pairing = { id: string; ready: Promise<void>; requesting: boolean };
const pairings = new WeakMap<Window, Pairing>();

function localPage(page: Window) {
  return page.location.protocol === "http:" && page.location.hostname === "127.0.0.1";
}

export function captureManagedWindow(page: Window): void {
  if (!localPage(page)) return;
  let id = /^#dudu-managed=([0-9a-f]{32})$/.exec(page.location.hash)?.[1];
  if (id) page.history.replaceState(page.history.state, "", page.location.pathname + page.location.search);
  if (pairings.has(page)) return;
  try {
    if (id) page.sessionStorage.setItem(STORAGE_KEY, id);
    else id = page.sessionStorage.getItem(STORAGE_KEY) ?? undefined;
  } catch { /* the original launch fragment also works when storage is unavailable */ }
  if (!id || !ID.test(id)) return;
  const ready = new Promise<void>(resolve => markTitle(page, `Dudu [${id}]`, resolve));
  pairings.set(page, { id, ready, requesting: false });
}

export function managedWindowId(page: Window, session: SessionInfo | null): string | null {
  if (!localPage(page) || session?.kind !== "browser" || session.role !== "owner"
      || session.person_id == null || !session.scopes.includes("admin")) return null;
  return pairings.get(page)?.id ?? null;
}

// The Windows host matches the Edge process and its window handle. These temporary
// titles can only ask that host to show its own dialog. No shutdown request leaves the page.
function markTitle(page: Window, marker: string, finish: () => void) {
  let previous = page.document.title;
  page.document.title = marker;
  // Next can update the current route's title while the host is locating its window.
  const observer = typeof MutationObserver !== "undefined" && page.document.head
    ? new MutationObserver(() => {
        if (page.document.title !== marker) {
          previous = page.document.title;
          page.document.title = marker;
        }
      })
    : null;
  if (observer) observer.observe(page.document.head, { childList: true, subtree: true, characterData: true });
  page.setTimeout(() => {
    observer?.disconnect();
    if (page.document.title === marker) page.document.title = previous;
    finish();
  }, MARKER_TIME_MS);
}

export async function requestManagedWindowExit(
  page: Window, session: SessionInfo | null, readSession: () => Promise<SessionInfo | null>,
): Promise<void> {
  const state = pairings.get(page);
  if (!state || state.requesting || managedWindowId(page, session) !== state.id) return;
  state.requesting = true;
  await state.ready;
  let current: SessionInfo | null;
  try {
    current = await readSession();
  } catch {
    current = null;
  }
  if (managedWindowId(page, current) !== state.id) {
    state.requesting = false;
    return;
  }
  markTitle(page, `Dudu close [${state.id}]`, () => { state.requesting = false; });
}
