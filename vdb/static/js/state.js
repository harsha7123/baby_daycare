// Shared app state: the signed-in user, current site, the alert store, thumbnails, live socket and sound.
import { api, blobOf, emit, getJSON, getKey, q } from "./api.js";

export const state = {
  me: "",
  sites: [],
  site: null, // current site object
  conn: "offline", // live | connecting | offline
  soundOn: false,
};

export const tz = () => state.site?.timezone;
export const roomName = (id) => state.site?.rooms.find((r) => r.id === id)?.name || id;

// ---------- Alerts ----------
const PAGE = 500;
export const alerts = {
  byId: new Map(),
  loaded: false,
  exhausted: false,
  loading: null,

  list() { return [...this.byId.values()].sort((a, b) => b.triggered_at - a.triggered_at); },
  get(id) { return this.byId.get(id); },
  openCount() { let n = 0; for (const a of this.byId.values()) if (a.status === "open") n++; return n; },

  reset() { this.byId.clear(); this.loaded = false; this.exhausted = false; this.loading = null; thumbs.clear(); },

  async load() {
    const siteId = state.site.id;
    const p = (async () => {
      const rows = await getJSON(`/api/alerts?${q({ site_id: siteId, limit: PAGE })}`);
      if (state.site?.id !== siteId) return;
      // Merge so alerts that arrived over the socket meanwhile survive.
      for (const a of rows) this.byId.set(a.id, { ...this.byId.get(a.id), ...a });
      this.loaded = true;
      if (rows.length < PAGE) this.exhausted = true;
      emit("alerts:reset");
    })();
    this.loading = p;
    try { await p; } finally { if (this.loading === p) this.loading = null; }
  },

  async loadOlder() {
    const list = this.list();
    const oldest = list[list.length - 1];
    if (!oldest) return 0;
    const rows = await getJSON(`/api/alerts?${q({ site_id: state.site.id, before: oldest.triggered_at, limit: 200 })}`);
    for (const a of rows) if (!this.byId.has(a.id)) this.byId.set(a.id, a);
    if (rows.length < 200) this.exhausted = true;
    emit("alerts:reset");
    return rows.length;
  },

  add(a) {
    if (a.site_id !== state.site?.id) return;
    const isNew = !this.byId.has(a.id);
    this.byId.set(a.id, { status: "open", ...this.byId.get(a.id), ...a });
    if (isNew) emit("alerts:new", this.byId.get(a.id));
    else emit("alerts:update", this.byId.get(a.id));
  },

  patch(id, fields) {
    const cur = this.byId.get(id);
    if (!cur) return;
    const clean = Object.fromEntries(Object.entries(fields).filter(([k, v]) => k !== "alert_id" && k !== "site_id" && v !== undefined));
    const next = { ...cur, ...clean };
    this.byId.set(id, next);
    if (clean.clip && cur.clip !== clean.clip) emit("alerts:clip", next);
    emit("alerts:update", next);
  },

  async review(id, status, note) {
    const body = { status, ...(note ? { note } : {}) };
    const r = await (await api(`/api/alerts/${encodeURIComponent(id)}/review`, { method: "POST", body })).json();
    this.patch(id, { status: r.status, reviewed_by: r.reviewed_by, reviewed_at: Date.now() / 1000, note: note || null });
    return r;
  },
};

// Thumbnails are fetched with the Authorization header and cached as blob: URLs for the session.
export const thumbs = {
  urls: new Map(),
  pending: new Map(),
  get(alert) {
    if (!alert?.thumbnail) return Promise.reject(new Error("no thumbnail"));
    if (this.urls.has(alert.id)) return Promise.resolve(this.urls.get(alert.id));
    if (this.pending.has(alert.id)) return this.pending.get(alert.id);
    const p = blobOf(`/api/alerts/${encodeURIComponent(alert.id)}/thumbnail`).then((b) => {
      const u = URL.createObjectURL(b);
      this.urls.set(alert.id, u);
      return u;
    }).finally(() => this.pending.delete(alert.id));
    this.pending.set(alert.id, p);
    return p;
  },
  clear() {
    for (const u of this.urls.values()) URL.revokeObjectURL(u);
    this.urls.clear();
    this.pending.clear();
  },
};

// Lazily load thumbnails as they scroll into view.
const thumbObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    thumbObserver.unobserve(e.target);
    e.target.__load?.();
  }
}, { rootMargin: "200px" }) : null;

export function thumbImg(alert, alt) {
  const img = document.createElement("img");
  img.alt = alt;
  img.decoding = "async";
  img.__load = () => thumbs.get(alert).then((u) => {
    img.src = u;
    img.addEventListener("load", () => img.classList.add("ready"), { once: true });
  }).catch(() => {});
  if (thumbs.urls.has(alert.id)) { img.src = thumbs.urls.get(alert.id); img.classList.add("ready"); }
  else if (thumbObserver) thumbObserver.observe(img);
  else img.__load();
  return img;
}

// ---------- Live socket ----------
let ws = null, retry = 0, retryTimer = null, wanted = false, everConnected = false;

function setConn(s) { state.conn = s; emit("conn", s); }

export function connectSocket() {
  wanted = true;
  clearTimeout(retryTimer);
  if (ws && ws.readyState <= 1) return;
  setConn("connecting");
  const sock = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/alerts`);
  ws = sock;
  sock.onopen = () => {
    sock.send(JSON.stringify({ key: getKey() }));
    retry = 0;
    setConn("live");
    // After a drop, catch up on anything raised while disconnected.
    if (everConnected && state.site) alerts.load().catch(() => {});
    everConnected = true;
  };
  sock.onmessage = (m) => {
    let msg;
    try { msg = JSON.parse(m.data); } catch { return; }
    if (!msg || typeof msg !== "object") return;
    if (msg.kind === "alert" && msg.data?.id) alerts.add(msg.data);
    else if (msg.kind === "alert_update" && msg.data?.alert_id) alerts.patch(msg.data.alert_id, msg.data);
  };
  sock.onclose = (e) => {
    if (ws !== sock) return;
    ws = null;
    if (e.code === 4401) { setConn("offline"); emit("auth-lost"); return; }
    if (!wanted) { setConn("offline"); return; }
    setConn("connecting");
    const delay = Math.min(30000, 1000 * 2 ** retry++) * (0.75 + Math.random() * 0.5);
    retryTimer = setTimeout(connectSocket, delay);
  };
}

export function disconnectSocket() {
  wanted = false;
  everConnected = false;
  clearTimeout(retryTimer);
  if (ws) { const s = ws; ws = null; s.close(1000); }
  setConn("offline");
}

// ---------- Sound (Web Audio; browsers require a click before audio can start) ----------
let audio = null;
export function toggleSound() {
  try { audio = audio || new (window.AudioContext || window.webkitAudioContext)(); } catch { return false; }
  if (audio.state === "suspended") audio.resume();
  state.soundOn = !state.soundOn;
  if (state.soundOn) chime();
  emit("sound", state.soundOn);
  return state.soundOn;
}

export function chime() {
  if (!state.soundOn || !audio) return;
  const t = audio.currentTime;
  for (const [i, f] of [[0, 880], [1, 1318.5]].values()) {
    const osc = audio.createOscillator(), gain = audio.createGain();
    osc.type = "sine";
    osc.frequency.value = f;
    const start = t + i * 0.16;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.22, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.7);
    osc.connect(gain).connect(audio.destination);
    osc.start(start);
    osc.stop(start + 0.75);
  }
}
