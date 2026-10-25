// The visible Agent 4 thread is per browser sign-in, not a replay of the
// case's stored transcript. Message rows stay on the server.

const SESSION_KEY = "reconcilo.chatSession";
const BASELINE_PREFIX = "reconcilo.chatBaseline.";
const REVEAL_PREFIX = "reconcilo.chatReveal.";

function storage(): Storage | null {
  try {
    return sessionStorage;
  } catch {
    return null;
  }
}

function viewKey(prefix: string, sessionId: string, caseId: string): string {
  return `${prefix}${sessionId}:${caseId}`;
}

function clearViewKeys(store: Storage) {
  const keys: string[] = [];
  for (let i = 0; i < store.length; i += 1) {
    const key = store.key(i);
    if (key && (key.startsWith(BASELINE_PREFIX) || key.startsWith(REVEAL_PREFIX))) keys.push(key);
  }
  for (const key of keys) store.removeItem(key);
}

function newId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/** A new sign-in. Stored transcripts stay hidden until the user asks for them. */
export function beginChatSession(): string {
  const id = newId();
  const store = storage();
  if (!store) return id;
  clearViewKeys(store);
  store.setItem(SESSION_KEY, id);
  return id;
}

/** Sign-out. The next sign-in must not reuse this view. */
export function endChatSession() {
  const store = storage();
  if (!store) return;
  clearViewKeys(store);
  store.removeItem(SESSION_KEY);
}

export function chatSessionId(): string {
  const store = storage();
  if (!store) return "memory";
  const existing = store.getItem(SESSION_KEY);
  if (existing) return existing;
  const id = newId();
  store.setItem(SESSION_KEY, id);
  return id;
}

/**
 * The next time this case opens in the current sign-in, every message already
 * stored is treated as earlier and stays off screen.
 */
export function forgetChatCase(caseId: string) {
  const store = storage();
  if (!store || !caseId) return;
  const sessionId = chatSessionId();
  store.removeItem(viewKey(BASELINE_PREFIX, sessionId, caseId));
  store.removeItem(viewKey(REVEAL_PREFIX, sessionId, caseId));
}

export function revealEarlierMessages(caseId: string) {
  const store = storage();
  if (!store) return;
  store.setItem(viewKey(REVEAL_PREFIX, chatSessionId(), caseId), "1");
}

/** Message ids that were already stored when this case was first opened this sign-in. */
export function earlierMessageIds(caseId: string, loadedIds: string[]): { earlier: string[]; revealed: boolean } {
  const store = storage();
  if (!store) return { earlier: loadedIds, revealed: false };
  const sessionId = chatSessionId();
  const key = viewKey(BASELINE_PREFIX, sessionId, caseId);
  const raw = store.getItem(key);
  let earlier: string[];
  if (raw == null) {
    earlier = loadedIds;
    store.setItem(key, JSON.stringify(earlier));
  } else {
    try {
      const parsed = JSON.parse(raw);
      earlier = Array.isArray(parsed) ? parsed.filter((id) => typeof id === "string") : loadedIds;
    } catch {
      earlier = loadedIds;
      store.setItem(key, JSON.stringify(earlier));
    }
  }
  return { earlier, revealed: store.getItem(viewKey(REVEAL_PREFIX, sessionId, caseId)) === "1" };
}
