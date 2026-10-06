// Network layer. The API key lives in memory and sessionStorage only, and is only ever sent in a header
// (or as the first WebSocket message) — never in a URL.

export const session = {
  get(k) { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* storage blocked */ } },
  remove(k) { try { sessionStorage.removeItem(k); } catch { /* storage blocked */ } },
};
export const prefs = {
  get(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage blocked */ } },
};

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

let key = "";
export const setKey = (k) => { key = k; };
export const getKey = () => key;

export const events = new EventTarget();
export const emit = (name, detail) => events.dispatchEvent(new CustomEvent(name, { detail }));
export const on = (name, fn) => {
  const h = (e) => fn(e.detail);
  events.addEventListener(name, h);
  return () => events.removeEventListener(name, h);
};

export async function api(path, { method = "GET", body, signal, quietAuth = false } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method, signal, cache: "no-store", credentials: "omit",
      headers: { Authorization: `Bearer ${key}`, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(0, "Can't reach the server. Check your connection.");
  }
  if (!res.ok) {
    if (res.status === 401 && !quietAuth) emit("auth-lost");
    throw new ApiError(res.status, messageFor(res.status));
  }
  return res;
}

function messageFor(status) {
  if (status === 401) return "That key wasn't recognised.";
  if (status === 429) return "Too many failed attempts. Please wait a few minutes and try again.";
  if (status === 404) return "Not found.";
  if (status >= 500) return "The server had a problem. Try again in a moment.";
  return `Request failed (${status}).`;
}

export const getJSON = async (path, opts) => (await api(path, opts)).json();
export const blobOf = async (path, opts) => (await api(path, opts)).blob();
export const q = (params) => new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
