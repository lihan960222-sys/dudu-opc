// A raw value from the previous composer has no proof it was never sent. Keep it available
// for explicit recovery, separate from new drafts with a known recipient.
import { workspaceScope } from "./workspace-scope.ts";

type Store = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const MAX_CHARS = 20000;
const stores = new WeakMap<Store, ReturnType<typeof buildDrafts>>();

function defaultStore(): Store | null {
  try { return typeof localStorage === "undefined" ? null : localStorage; } catch { return null; }
}

export function chatDraftKey(threadId: number | null, agentId: number | null) {
  return threadId !== null ? String(threadId) : agentId !== null ? `new:agent:${agentId}` : "new";
}

function buildDrafts(store: Store | null) {
  const listeners = new Set<() => void>();
  const fallback = new Map<string, string>();
  const location = (key: string, old = false) => {
    const scope = workspaceScope();
    return (old ? "suparagent:draft:" : "suparagent:composer:v2:") + (scope ? scope + ":" : "") + key;
  };
  const read = (name: string) => {
    if (fallback.has(name)) return fallback.get(name)!;
    try { return store?.getItem(name) ?? ""; } catch { return ""; }
  };
  const write = (name: string, text: string) => {
    try {
      if (!store) throw new Error("Storage unavailable");
      if (text) store.setItem(name, text.slice(0, MAX_CHARS));
      else store.removeItem(name);
      if (text.length > MAX_CHARS) fallback.set(name, text);
      else fallback.delete(name);
    } catch { fallback.set(name, text); }
  };
  const notify = () => { for (const listener of listeners) listener(); };
  const onStorage = (event: StorageEvent) => {
    if (event.storageArea !== store) return;
    if (event.key === null) fallback.clear();
    else if (/^suparagent:(draft:|composer:v2:)/.test(event.key)) fallback.delete(event.key);
    else return;
    notify();
  };
  const get = (key: string) => read(location(key));
  const pending = (key: string) => location(`sending:${key}`);
  const set = (key: string, text: string) => {
    write(pending(key), "");
    write(location(key), text.trim() ? text : "");
    // The editor now has an explicit choice for this exact destination.
    write(location(key, true), "");
    notify();
  };
  const clearSubmitted = (key: string, submitted: string) => {
    if (get(key) === submitted) set(key, "");
  };
  const legacy = (key: string, lastAccepted?: string) => {
    const old = read(location(key, true));
    return !get(key) && old.trim() && old.trim() !== lastAccepted?.trim() ? old : "";
  };
  return {
    get, set, clearSubmitted, legacy,
    markSending(key: string, submitted: string, message: string, started = Date.now()) {
      if (get(key) !== submitted) return "";
      const record = JSON.stringify({ message, started });
      // Draft text is capped on disk. Long messages can still be acknowledged in this tab.
      if (record.length > MAX_CHARS) return "";
      write(pending(key), record);
      return record;
    },
    confirmSending(key: string, submitted: string, record: string) {
      if (!record || read(pending(key)) === record) clearSubmitted(key, submitted);
    },
    reconcile(key: string, accepted: string, ts?: string) {
      const old = read(location(key, true));
      if (old && old.trim() === accepted.trim()) {
        write(location(key, true), "");
        notify();
      }
      const record = read(pending(key));
      if (!record || !ts) return;
      try {
        const { message, started } = JSON.parse(record);
        if (accepted === message && Date.parse(ts) >= started) set(key, "");
      } catch { /* Incomplete browser storage cannot prove delivery. */ }
    },
    restore(key: string) { const old = legacy(key); if (old) set(key, old); },
    dismiss(key: string) { write(location(key, true), ""); notify(); },
    moveSubmitted(from: string, to: string, submitted: string) {
      if (!get(to)) set(to, submitted);
      clearSubmitted(from, submitted);
    },
    subscribe(listener: () => void) {
      if (!listeners.size && typeof window !== "undefined") window.addEventListener("storage", onStorage);
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
        if (!listeners.size && typeof window !== "undefined") window.removeEventListener("storage", onStorage);
      };
    },
  };
}

export function createChatDrafts(store: Store | null = defaultStore()) {
  if (!store) return buildDrafts(null);
  const existing = stores.get(store);
  if (existing) return existing;
  const drafts = buildDrafts(store);
  stores.set(store, drafts);
  return drafts;
}

export function loadDraft(key: string | null, store: Store | null = defaultStore()) {
  return key ? createChatDrafts(store).get(key) : "";
}

export function saveDraft(key: string | null, text: string, store: Store | null = defaultStore()) {
  if (key) createChatDrafts(store).set(key, text);
}
