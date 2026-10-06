// DOM helpers. Server data only ever reaches the page through textContent / attributes, never innerHTML.

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  setProps(node, props);
  append(node, children);
  return node;
}

function setProps(node, props) {
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.setAttribute("class", v);
    else if (k === "text") node.textContent = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "value" && "value" in node) node.value = v;
    else node.setAttribute(k, v === true ? "" : String(v));
  }
}

function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c === undefined || c === null || c === false) continue;
    node.append(c instanceof Node ? c : String(c));
  }
}

const NS = "http://www.w3.org/2000/svg";

export function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) node.setAttribute(k, String(v));
  for (const c of children.flat()) if (c) node.append(c);
  return node;
}

// Simple stroke icons on a 24-unit grid (drawn for this app; no icon fonts).
const ICONS = {
  logo: ["M12 3.5c-3 0-5.5 1.6-7 4 1.5 2.4 4 4 7 4s5.5-1.6 7-4c-1.5-2.4-4-4-7-4z", "M12 9.5a2 2 0 1 0 0-4 2 2 0 0 0 0 4z", "M12 21s-6.5-3.6-6.5-8", "M12 21s6.5-3.6 6.5-8"],
  overview: ["M4 4h6.5v6.5H4z", "M13.5 4H20v6.5h-6.5z", "M4 13.5h6.5V20H4z", "M13.5 13.5H20V20h-6.5z"],
  live: ["M3 7.5A2.5 2.5 0 0 1 5.5 5h8A2.5 2.5 0 0 1 16 7.5v9a2.5 2.5 0 0 1-2.5 2.5h-8A2.5 2.5 0 0 1 3 16.5z", "M16 10.5l5-3v9l-5-3"],
  alerts: ["M6 9a6 6 0 0 1 12 0c0 6.5 2.5 8.5 2.5 8.5h-17S6 15.5 6 9z", "M10.2 20.5a2 2 0 0 0 3.6 0"],
  reports: ["M14 3H6.5A2.5 2.5 0 0 0 4 5.5v13A2.5 2.5 0 0 0 6.5 21h11a2.5 2.5 0 0 0 2.5-2.5V9z", "M14 3v6h6", "M8 17v-3", "M12 17v-5", "M16 17v-2"],
  activity: ["M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z", "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"],
  userOff: ["M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", "M4.5 21a7.5 7.5 0 0 1 15 0", "M3 3l18 18"],
  users: ["M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z", "M2 20.5a7 7 0 0 1 14 0", "M16 4.3a3.5 3.5 0 0 1 0 6.4", "M18.5 13.8a7 7 0 0 1 3.5 6.7"],
  child: ["M12 9.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z", "M6 21v-3.5A4.5 4.5 0 0 1 10.5 13h3a4.5 4.5 0 0 1 4.5 4.5V21"],
  phone: ["M8 2.5h8A1.5 1.5 0 0 1 17.5 4v16a1.5 1.5 0 0 1-1.5 1.5H8A1.5 1.5 0 0 1 6.5 20V4A1.5 1.5 0 0 1 8 2.5z", "M11 18.5h2"],
  noEntry: ["M12 2.5a9.5 9.5 0 1 0 0 19 9.5 9.5 0 0 0 0-19z", "M5.3 5.3l13.4 13.4"],
  videoOff: ["M3 7.5A2.5 2.5 0 0 1 5.5 5h8A2.5 2.5 0 0 1 16 7.5v9a2.5 2.5 0 0 1-2.5 2.5h-8A2.5 2.5 0 0 1 3 16.5z", "M16 10.5l5-3v9l-5-3", "M2 2l20 20"],
  hand: ["M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4.5", "M12 17.2v.1"],
  fall: ["M12 3v12", "M6.5 10L12 15.5 17.5 10", "M4 20.5h16"],
  soundOn: ["M11 5L6 9H2.5v6H6l5 4z", "M15.5 8.5a5 5 0 0 1 0 7", "M18.5 5.5a9.5 9.5 0 0 1 0 13"],
  soundOff: ["M11 5L6 9H2.5v6H6l5 4z", "M22 9.5l-5 5", "M17 9.5l5 5"],
  sun: ["M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z", "M12 2v2", "M12 20v2", "M4.9 4.9l1.4 1.4", "M17.7 17.7l1.4 1.4", "M2 12h2", "M20 12h2", "M4.9 19.1l1.4-1.4", "M17.7 6.3l1.4-1.4"],
  moon: ["M20.5 13.5A8.5 8.5 0 1 1 10.5 3.5a6.5 6.5 0 0 0 10 10z"],
  auto: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 3v18", "M12 7h4.5", "M12 11h7.5", "M12 15h7", "M12 19h3.5"],
  chevronRight: ["M9.5 6l6 6-6 6"],
  chevronDown: ["M6 9.5l6 6 6-6"],
  close: ["M6 6l12 12", "M18 6L6 18"],
  play: ["M7.5 4.5l12 7.5-12 7.5z"],
  print: ["M6.5 9V3.5h11V9", "M6.5 17.5H4.5A2 2 0 0 1 2.5 15.5v-4.5a2 2 0 0 1 2-2h15a2 2 0 0 1 2 2v4.5a2 2 0 0 1-2 2h-2", "M6.5 14h11v6.5h-11z"],
  refresh: ["M20.5 12a8.5 8.5 0 1 1-2.6-6.1L20.5 8.5", "M20.5 3.5v5h-5"],
  signOut: ["M9 21H5.5A2.5 2.5 0 0 1 3 18.5v-13A2.5 2.5 0 0 1 5.5 3H9", "M16 17l5-5-5-5", "M21 12H9"],
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  shield: ["M12 2.5l8 3v6c0 5-3.4 9.1-8 10.5-4.6-1.4-8-5.5-8-10.5v-6z", "M8.5 12l2.5 2.5 4.5-5"],
  sparkles: ["M11 3l1.9 5.1L18 10l-5.1 1.9L11 17l-1.9-5.1L4 10l5.1-1.9z", "M18.5 15l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"],
  clock: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 7.5V12l3 2"],
  info: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 11v5.5", "M12 7.8v.1"],
  warn: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 7.5V13", "M12 16.2v.1"],
  calendar: ["M5.5 5h13A1.5 1.5 0 0 1 20 6.5v12a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5v-12A1.5 1.5 0 0 1 5.5 5z", "M4 10h16", "M8 3v4", "M16 3v4"],
  expand: ["M14.5 3.5h6v6", "M9.5 20.5h-6v-6", "M20.5 3.5l-7 7", "M3.5 20.5l7-7"],
  lock: ["M6 11h12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1z", "M8 11V7.5a4 4 0 0 1 8 0V11"],
  eye: ["M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z", "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"],
  target: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z", "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z"],
  doc: ["M14 3H6.5A2.5 2.5 0 0 0 4 5.5v13A2.5 2.5 0 0 0 6.5 21h11a2.5 2.5 0 0 0 2.5-2.5V9z", "M14 3v6h6"],
};

export function icon(name, { size = 20, stroke = 2, label } = {}) {
  const node = svg("svg", {
    viewBox: "0 0 24 24", width: size, height: size, fill: "none", stroke: "currentColor",
    "stroke-width": stroke, "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": label ? undefined : "true", role: label ? "img" : undefined, "aria-label": label,
    focusable: "false",
  });
  for (const d of ICONS[name] || ICONS.info) node.append(svg("path", { d }));
  return node;
}

export function glyph(name, tint, cls = "") {
  return el("span", { class: `glyph tint-${tint} ${cls}`.trim(), "aria-hidden": "true" }, icon(name, { size: 19 }));
}

// ---------- Alert vocabulary ----------
export const TYPES = {
  no_adult: { label: "No adult present", icon: "userOff", tint: "red" },
  ratio_breach: { label: "Ratio breach", icon: "users", tint: "orange" },
  phone_use: { label: "Phone use", icon: "phone", tint: "gray" },
  restricted_zone: { label: "Restricted area", icon: "noEntry", tint: "red" },
  camera_offline: { label: "Camera offline", icon: "videoOff", tint: "gray" },
  possible_aggression: { label: "Possible aggression", icon: "hand", tint: "red" },
  child_fall: { label: "Child fall", icon: "fall", tint: "orange" },
};
export const typeInfo = (t) => TYPES[t] || { label: String(t || "Alert").replace(/_/g, " "), icon: "info", tint: "gray" };

export const SEVERITY = { high: { label: "High", tint: "red" }, medium: { label: "Medium", tint: "orange" }, low: { label: "Low", tint: "gray" } };

export const STATUS = {
  open: { label: "Needs review", tint: "blue" },
  confirmed: { label: "Confirmed", tint: "red" },
  false_alarm: { label: "False alarm", tint: "gray" },
};

export const VERIFICATION = {
  pending: { label: "AI checking…", tint: "gray", long: "The AI is reviewing the clip. This takes a few seconds." },
  likely: { label: "AI: likely", tint: "orange", long: "The AI second opinion thinks rough handling is likely." },
  unlikely: { label: "AI: unlikely", tint: "gray", long: "The AI second opinion thinks rough handling is unlikely — this does not clear the alert; a person must still review it." },
  unclear: { label: "AI: unclear", tint: "gray", long: "The AI second opinion could not tell from the clip." },
};

export function chip(text, tint, { dot = false, iconName } = {}) {
  return el("span", { class: `chip tint-${tint}` }, dot ? el("span", { class: "dot", "aria-hidden": "true" }) : null,
    iconName ? icon(iconName, { size: 13, stroke: 2.4 }) : null, text);
}

export function skeletonLines(n = 3) {
  return Array.from({ length: n }, (_, i) => el("div", { class: "skeleton line", style: `width:${90 - i * 18}%` }));
}

export function emptyState(iconName, tint, title, body) {
  return el("div", { class: "empty" }, glyph(iconName, tint, "lg"), el("h3", { text: title }), body ? el("p", { text: body }) : null);
}

// ---------- Time formatting (in the site's timezone) ----------
function safeTz(tz) {
  try { new Intl.DateTimeFormat("en-US", { timeZone: tz }); return tz; } catch { return undefined; }
}
const fmtCache = new Map();
function fmt(tz, opts) {
  const k = `${tz}|${JSON.stringify(opts)}`;
  if (!fmtCache.has(k)) fmtCache.set(k, new Intl.DateTimeFormat(undefined, { timeZone: safeTz(tz), ...opts }));
  return fmtCache.get(k);
}
export function dayKey(ts, tz) {
  // YYYY-MM-DD in the site timezone.
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: safeTz(tz), year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(ts * 1000));
  const get = (t) => parts.find((p) => p.type === t)?.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}
export const nowSec = () => Date.now() / 1000;
export const todayKey = (tz) => dayKey(nowSec(), tz);
export const timeOf = (ts, tz) => fmt(tz, { hour: "numeric", minute: "2-digit" }).format(new Date(ts * 1000));
export const timeSecOf = (ts, tz) => fmt(tz, { hour: "numeric", minute: "2-digit", second: "2-digit" }).format(new Date(ts * 1000));
export const dateLong = (ts, tz) => fmt(tz, { weekday: "long", day: "numeric", month: "long" }).format(new Date(ts * 1000));
export const dateTime = (ts, tz) => fmt(tz, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }).format(new Date(ts * 1000));

export function dayLabel(key, tz) {
  const today = todayKey(tz);
  if (key === today) return "Today";
  if (key === dayKey(nowSec() - 86400, tz)) return "Yesterday";
  const [y, m, d] = key.split("-").map(Number);
  return new Intl.DateTimeFormat(undefined, { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" }).format(Date.UTC(y, m - 1, d, 12));
}

export function relTime(ts, tz) {
  const s = Math.max(0, nowSec() - ts);
  if (s < 45) return "Just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 6 * 3600) return `${Math.floor(s / 3600)} h ago`;
  return timeOf(ts, tz);
}

export function duration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} s`;
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return s ? `${m} min ${s} s` : `${m} min`;
  const h = Math.floor(m / 60);
  return `${h} h ${m % 60} min`;
}

export function minutes(mins) {
  if (mins == null) return "—";
  const m = Math.round(mins);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60 ? `${m % 60} min` : ""}`.trim();
}

export const pct = (x) => (x == null || Number.isNaN(x) ? "—" : `${Math.round(x * 100)}%`);
export const initials = (name) => String(name || "?").trim().split(/\s+/).slice(0, 2).map((p) => p[0]?.toUpperCase() || "").join("") || "?";
