// Fetch wrapper: bearer token in memory + sessionStorage, backend `detail`
// surfaced as the error message, and 401 sends the user back to sign-in.

const KEY = "ghostledger.token";
let token: string | null = safeGet();
let onUnauthorized: () => void = () => {};

function safeGet(): string | null {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setToken(value: string | null) {
  token = value;
  try {
    if (value) sessionStorage.setItem(KEY, value);
    else sessionStorage.removeItem(KEY);
  } catch {
    // Storage blocked: the in-memory token still works for this tab.
  }
}

export const getToken = () => token;
export const setUnauthorizedHandler = (fn: () => void) => (onUnauthorized = fn);

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Turn FastAPI's `detail` (string or validation list) into one plain sentence. */
export function detailText(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return `The request was not valid: ${detail[0].msg}.`;
  if (status === 413) return "That is too large to send.";
  if (status >= 500) return "The server ran into a problem. Please try again.";
  return `The request failed (${status}).`;
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  let response: Response;
  try {
    response = await fetch(path, { ...init, headers, body });
  } catch {
    throw new ApiError(0, "Can't reach the GhostLedger server. Is the backend running?");
  }
  const parsed = await response.json().catch(() => null);
  if (response.status === 401 && token) {
    setToken(null);
    onUnauthorized();
  }
  if (!response.ok) throw new ApiError(response.status, detailText(response.status, parsed));
  return parsed as T;
}
