const $ = (id) => document.getElementById(id);
const store = {
  get: (k) => { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } },
  set: (k, v) => { try { sessionStorage.setItem(k, v); } catch {} },
};
$("key").value = store.get("vdb-key");
let key = "", site = "", ws = null, roomTimer = null, me = "", soundOn = false, audio = null;

function beep() {
  if (!soundOn || !audio) return;
  const osc = audio.createOscillator(), gain = audio.createGain();
  osc.frequency.value = 880;
  gain.gain.setValueAtTime(0.2, audio.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + 0.8);
  osc.connect(gain).connect(audio.destination);
  osc.start();
  osc.stop(audio.currentTime + 0.8);
}
const seen = new Set();

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) k === "class" ? (n.className = v) : n.setAttribute(k, v);
  for (const c of children) n.append(c);
  return n;
}

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res;
}

async function blobUrl(path) {
  return URL.createObjectURL(await (await api(path)).blob());
}

function fmtTime(ts) { return new Date(ts * 1000).toLocaleTimeString(); }

function renderAlert(a, prepend) {
  if (seen.has(a.id)) return;
  seen.add(a.id);
  const img = el("img", { alt: "Alert snapshot" });
  if (a.thumbnail) blobUrl(`/api/alerts/${a.id}/thumbnail`).then((u) => (img.src = u)).catch(() => {});
  const status = el("span", { class: "muted" }, a.status && a.status !== "open" ? ` · ${a.status.replace("_", " ")}` : "");
  const body = el("div", {},
    el("div", { class: "title" }, a.type.replace(/_/g, " ")),
    el("div", {}, a.message),
    el("div", { class: "muted" }, `${fmtTime(a.triggered_at)} · ${a.room_id}${a.camera_id ? " · " + a.camera_id : ""}`, status),
  );
  const actions = el("div", { class: "actions" });
  const clipBtn = el("button", {}, "Play clip");
  clipBtn.onclick = async () => {
    try {
      const v = el("video", { controls: "" });
      v.src = await blobUrl(`/api/alerts/${a.id}/clip`);
      clipBtn.replaceWith(v);
      v.play();
    } catch { clipBtn.textContent = "Clip not ready yet"; }
  };
  actions.append(clipBtn);
  for (const [label, value] of [["Confirm", "confirmed"], ["False alarm", "false_alarm"]]) {
    const b = el("button", {}, label);
    b.onclick = async () => {
      try {
        const r = await (await api(`/api/alerts/${a.id}/review`, { method: "POST", body: JSON.stringify({ status: value }) })).json();
        status.textContent = ` · ${value.replace("_", " ")} by ${r.reviewed_by}`;
      } catch (e) { alert(`Review failed: ${e.message}`); }
    };
    actions.append(b);
  }
  body.append(actions);
  const card = el("div", { class: `alert ${a.severity}` }, img, body);
  prepend ? $("alerts").prepend(card) : $("alerts").append(card);
}

async function loadRooms() {
  try {
    const rooms = await (await api(`/api/rooms/live?site_id=${encodeURIComponent(site)}`)).json();
    $("rooms").replaceChildren(...rooms.map((r) => {
      const has = r.ts !== undefined;
      const state = !has ? el("span", { class: "pill medium" }, "no data")
        : r.no_adult ? el("span", { class: "pill high" }, "no adult")
        : !r.ratio_ok ? el("span", { class: "pill medium" }, "over ratio")
        : el("span", { class: "pill ok" }, "ok");
      return el("div", { class: "room" },
        el("div", { class: "name" }, r.name),
        el("div", { class: "counts" }, has ? `${r.adults} adults · ${r.children} children` : "—"),
        state,
        el("div", { class: "muted" }, has ? `updated ${fmtTime(r.ts)} · ${r.cameras_online} camera(s)` : `max ${r.max_children_per_adult} children per adult`),
      );
    }));
  } catch (e) { $("conn").textContent = `error: ${e.message}`; }
}

async function loadAlerts() {
  const alerts = await (await api(`/api/alerts?site_id=${encodeURIComponent(site)}&limit=50`)).json();
  alerts.reverse().forEach((a) => renderAlert(a, true));
}

function openSocket() {
  if (ws) ws.close();
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/alerts`);
  ws.onopen = () => {
    ws.send(JSON.stringify({ key }));
    $("conn").textContent = `live · ${me}`;
    loadAlerts().catch(() => {});  // pick up anything raised while disconnected
  };
  ws.onmessage = (m) => {
    const a = JSON.parse(m.data);
    if (a.site_id !== site) return;
    renderAlert(a, true);
    loadRooms();
    if (a.severity === "high") beep();
  };
  ws.onclose = (e) => {
    $("conn").textContent = e.code === 4401 ? "bad API key" : "reconnecting…";
    if (e.code !== 4401) setTimeout(() => ws && ws.readyState === WebSocket.CLOSED && openSocket(), 3000);
  };
}

async function connect() {
  key = $("key").value.trim();
  store.set("vdb-key", key);
  try {
    me = (await (await api("/api/me")).json()).name;
    const sites = await (await api("/api/sites")).json();
    $("site").replaceChildren(...sites.map((s) => el("option", { value: s.id }, s.name)));
    site = store.get("vdb-site") && sites.some((s) => s.id === store.get("vdb-site")) ? store.get("vdb-site") : sites[0]?.id;
    $("site").value = site;
    await Promise.all([loadRooms(), loadAlerts()]);
    openSocket();
    clearInterval(roomTimer);
    roomTimer = setInterval(loadRooms, 5000);
  } catch (e) { $("conn").textContent = e.message.startsWith("401") ? "bad API key" : `error: ${e.message}`; }
}

$("connect").onclick = connect;
$("key").addEventListener("keydown", (e) => e.key === "Enter" && connect());
$("sound").onclick = () => {
  // Browsers only allow audio after a user gesture, so sound is switched on by a click.
  audio = audio || new AudioContext();
  soundOn = !soundOn;
  $("sound").textContent = `Sound: ${soundOn ? "on" : "off"}`;
  if (soundOn) beep();
};
$("site").onchange = () => {
  site = $("site").value;
  store.set("vdb-site", site);
  $("alerts").replaceChildren();
  seen.clear();
  loadRooms();
  loadAlerts();
};
$("report").onclick = async () => {
  const day = new Date().toLocaleDateString("en-CA");
  try { window.open(await blobUrl(`/api/reports/daily.html?site_id=${encodeURIComponent(site)}&day=${day}`), "_blank"); }
  catch (e) { alert(`Report failed: ${e.message}`); }
};
if (key) connect();
