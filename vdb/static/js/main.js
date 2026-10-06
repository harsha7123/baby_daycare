// Vision Day Baby — staff dashboard entry point: theme, sign-in, app shell and hash router.
import { ApiError, emit, getJSON, on, prefs, session, setKey } from "./api.js";
import { el, glyph, icon, initials, typeInfo } from "./dom.js";
import { openAlert } from "./alert-detail.js";
import { closeSheet, openSheet, toast } from "./sheet.js";
import { alerts, chime, connectSocket, disconnectSocket, roomName, state, toggleSound } from "./state.js";
import * as overview from "./views/overview.js";
import * as live from "./views/live.js";
import * as alertsView from "./views/alerts.js";
import * as reports from "./views/reports.js";
import * as activity from "./views/activity.js";

const ROUTES = {
  overview: { view: overview, label: "Overview", icon: "overview", tint: "blue" },
  live: { view: live, label: "Live", icon: "live", tint: "red" },
  alerts: { view: alertsView, label: "Alerts", icon: "alerts", tint: "orange" },
  reports: { view: reports, label: "Reports", icon: "reports", tint: "teal" },
  activity: { view: activity, label: "Activity", icon: "activity", tint: "indigo" },
};
const KEY = "vdb-key", SITE = "vdb-site", THEME = "vdb-theme";
const app = document.getElementById("app");

// ---------- Theme: follows the system, with a manual override ----------
const THEMES = ["system", "light", "dark"];
const THEME_LABEL = { system: "Automatic", light: "Light", dark: "Dark" };
const THEME_ICON = { system: "auto", light: "sun", dark: "moon" };
let theme = THEMES.includes(prefs.get(THEME)) ? prefs.get(THEME) : "system";
function applyTheme() {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = theme;
}
function setTheme(t) {
  theme = t;
  prefs.set(THEME, theme);
  applyTheme();
  renderControls();
  emit("theme", theme);
}
applyTheme();

// ---------- Sign in ----------
function showSignIn(message) {
  teardownShell();
  const input = el("input", { class: "field", id: "key", type: "password", name: "key", autocomplete: "current-password",
    required: true, spellcheck: "false", autocapitalize: "off", placeholder: "Paste your staff key", "aria-describedby": "key-help" });
  const err = el("div", { class: "error", role: "alert", hidden: !message }, icon("warn", { size: 18 }), el("span", { text: message || "" }));
  const submit = el("button", { class: "btn btn-primary btn-large", type: "submit", text: "Continue" });
  const form = el("form", { novalidate: true },
    el("label", { for: "key", text: "API key" }), input,
    el("p", { id: "key-help", class: "t-foot muted", style: "margin:-4px 4px 4px", text: "Your director gives each staff member their own key." }),
    err, submit);
  const showErr = (m) => {
    err.hidden = false;
    err.lastChild.textContent = m;
    err.style.animation = "none";
    void err.offsetWidth;
    err.style.animation = "";
  };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = input.value.trim();
    if (!key) { showErr("Enter your API key."); input.focus(); return; }
    submit.disabled = true;
    submit.textContent = "Signing in…";
    try {
      await signIn(key);
    } catch (ex) {
      showErr(ex instanceof ApiError ? ex.message : "Something went wrong. Try again.");
      submit.disabled = false;
      submit.textContent = "Continue";
      input.select();
    }
  });
  app.replaceChildren(el("main", { class: "signin", id: "content" },
    el("div", { class: "signin-card" },
      el("div", { class: "app-icon", "aria-hidden": "true" }, icon("logo", { size: 40, stroke: 1.8 })),
      el("h1", { class: "t-title", text: "Vision Day Baby" }),
      el("p", { class: "t-sub muted", style: "margin:0", text: "Safety monitoring for your daycare" }),
      form,
      el("div", { class: "privacy t-foot" }, icon("shield", { size: 18 }),
        el("span", { text: "Footage of children is private. Your key stays in this browser tab only, and every view of a snapshot, clip or report is logged under your name." })))));
  app.setAttribute("aria-busy", "false");
  document.title = "Sign in · Vision Day Baby";
  input.focus();
}

async function signIn(key) {
  setKey(key);
  let me, sites;
  try {
    [me, sites] = await Promise.all([getJSON("/api/me", { quietAuth: true }), getJSON("/api/sites", { quietAuth: true })]);
  } catch (e) {
    setKey("");
    throw e;
  }
  if (!sites.length) { setKey(""); throw new ApiError(404, "This key isn't linked to any site yet. Ask your director."); }
  session.set(KEY, key);
  state.me = me.name;
  state.sites = sites;
  const saved = session.get(SITE);
  state.site = sites.find((s) => s.id === saved) || sites[0];
  buildShell();
  alerts.reset();
  alerts.load().catch((e) => toast({ title: "Couldn't load alerts", text: e.message }));
  connectSocket();
  route();
}

function signOut(message) {
  disconnectSocket();
  closeSheet();
  setKey("");
  session.remove(KEY);
  alerts.reset();
  state.me = "";
  state.site = null;
  showSignIn(message);
}

// ---------- Shell ----------
let shell = null; // { content, title, nav links, badges, conn, ... }
let unmount = null, currentRoute = null, offs = [];

function buildShell() {
  teardownShell();
  const navLinks = {}, badges = [];
  const navList = el("ul", { class: "nav" });
  const tabbar = el("nav", { class: "tabbar", "aria-label": "Sections" });
  for (const [id, r] of Object.entries(ROUTES)) {
    const badge = id === "alerts" ? el("span", { class: "badge-count", hidden: true }) : null;
    const a = el("a", { href: `#/${id}` }, glyph(r.icon, r.tint, "sm"), r.label, badge);
    const t = el("a", { href: `#/${id}` }, icon(r.icon, { size: 24, stroke: 1.9 }), el("span", { text: r.label }),
      id === "alerts" ? el("span", { class: "badge-count", hidden: true }) : null);
    if (badge) badges.push(badge, t.lastChild);
    navLinks[id] = [a, t];
    navList.append(el("li", {}, a));
    tabbar.append(t);
  }

  const siteSel = el("select", { "aria-label": "Site" }, ...state.sites.map((s) => el("option", { value: s.id, text: s.name })));
  siteSel.value = state.site.id;
  siteSel.addEventListener("change", () => switchSite(siteSel.value));
  const sitePopup = el("label", { class: "popup" }, siteSel, icon("chevronDown", { size: 14, stroke: 2.5 }));
  if (state.sites.length < 2) siteSel.disabled = true;

  const conn = el("span", { class: "conn", role: "status", "data-state": state.conn },
    el("span", { class: "dot", "aria-hidden": "true" }), el("span", { class: "label" }));
  const sound = el("button", { class: "icon-btn", type: "button", "aria-pressed": String(state.soundOn) });
  const themeBtn = el("button", { class: "icon-btn theme-btn", type: "button" });
  const accountBtn = el("button", { class: "icon-btn account-btn", type: "button", "aria-label": `Account: ${state.me}`, title: state.me },
    el("span", { class: "avatar", "aria-hidden": "true", text: initials(state.me) }));
  sound.addEventListener("click", () => toggleSound());
  themeBtn.addEventListener("click", () => {
    setTheme(THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]);
    toast({ title: `Appearance: ${THEME_LABEL[theme]}`, glyphNode: glyph(THEME_ICON[theme], "indigo", "sm"), timeout: 2000 });
  });
  accountBtn.addEventListener("click", openAccount);

  const barTitle = el("span", { class: "bar-title" });
  const topbar = el("header", { class: "topbar" },
    el("span", { class: "mobile-brand app-icon", style: "width:30px;height:30px;margin:0 8px 0 0;border-radius:8px;box-shadow:none", "aria-hidden": "true" }, icon("logo", { size: 19, stroke: 1.8 })),
    barTitle, el("span", { class: "spacer" }), sitePopup, conn, sound, themeBtn, accountBtn);

  const sidebar = el("aside", { class: "sidebar", "aria-label": "Sidebar" },
    el("div", { class: "brand" }, el("span", { class: "app-icon", "aria-hidden": "true" }, icon("logo", { size: 20, stroke: 1.8 })), el("strong", { text: "Vision Day Baby" })),
    el("nav", { "aria-label": "Sections" }, navList),
    el("div", { class: "sidebar-foot" },
      el("div", { class: "user-row" }, el("span", { class: "avatar", "aria-hidden": "true", text: initials(state.me) }),
        el("div", { style: "min-width:0;flex:1" }, el("div", { class: "name", text: state.me }), el("div", { class: "t-foot muted", text: "Signed in" })))));

  const content = el("main", { class: "content", id: "content", tabindex: "-1" });
  app.replaceChildren(el("div", { class: "shell" }, sidebar, el("div", { class: "main" }, topbar, content)), tabbar);
  app.setAttribute("aria-busy", "false");

  shell = { content, barTitle, topbar, navLinks, badges, conn, sound, themeBtn };
  renderControls();
  renderConn(state.conn);
  renderBadge();

  const onScroll = () => topbar.classList.toggle("scrolled", window.scrollY > 24);
  window.addEventListener("scroll", onScroll, { passive: true });
  offs.push(() => window.removeEventListener("scroll", onScroll));
  offs.push(on("conn", renderConn));
  offs.push(on("sound", renderControls));
  offs.push(on("alerts:reset", renderBadge), on("alerts:update", renderBadge));
  offs.push(on("alerts:new", (a) => { renderBadge(); notify(a); }));
}

function renderControls() {
  if (!shell) return;
  const { sound, themeBtn } = shell;
  sound.replaceChildren(icon(state.soundOn ? "soundOn" : "soundOff"));
  sound.setAttribute("aria-pressed", String(state.soundOn));
  sound.setAttribute("aria-label", state.soundOn ? "Alert sound on" : "Alert sound off");
  sound.title = state.soundOn ? "Sound on for high-severity alerts" : "Turn on sound for high-severity alerts";
  const label = `Appearance: ${THEME_LABEL[theme]}`;
  themeBtn.replaceChildren(icon(THEME_ICON[theme]));
  themeBtn.setAttribute("aria-label", `${label}. Change appearance`);
  themeBtn.title = label;
}

// Account sheet: who is signed in, appearance, alert sound and sign out (Settings-style).
function openAccount() {
  const seg = el("div", { class: "segmented", role: "group", "aria-label": "Appearance", style: "width:100%" });
  const segBtns = THEMES.map((t) => {
    const b = el("button", { type: "button", "aria-pressed": String(t === theme), text: THEME_LABEL[t] });
    b.addEventListener("click", () => { setTheme(t); segBtns.forEach((x) => x.setAttribute("aria-pressed", String(x === b))); });
    seg.append(b);
    return b;
  });
  const sw = el("button", { class: "switch", type: "button", role: "switch", "aria-checked": String(state.soundOn), "aria-labelledby": "snd-l" });
  sw.addEventListener("click", () => toggleSound());
  const offSound = on("sound", (v) => sw.setAttribute("aria-checked", String(v)));
  const site = state.site;
  openSheet({
    title: "Account",
    onClose: offSound,
    body: el("div", {},
      el("div", { class: "card", style: "display:flex;align-items:center;gap:14px;padding:16px" },
        el("span", { class: "avatar", style: "width:56px;height:56px;font-size:22px", "aria-hidden": "true", text: initials(state.me) }),
        el("div", {}, el("div", { class: "t-title3", text: state.me }), el("div", { class: "t-foot muted", text: `${state.sites.length} site${state.sites.length === 1 ? "" : "s"} · viewing ${site.name}` }))),
      el("h3", { class: "group-label", text: "Appearance" }),
      el("div", { class: "card", style: "padding:12px" }, seg),
      el("h3", { class: "group-label", text: "Notifications" }),
      el("ul", { class: "list", style: "--inset:60px" },
        el("li", {}, el("div", { class: "row" }, glyph("soundOn", "red", "sm"),
          el("div", { class: "grow" }, el("div", { id: "snd-l", text: "Alert sound" }), el("div", { class: "t-foot muted", text: "Chime for high-severity alerts while this tab is open" })), sw))),
      el("ul", { class: "list", style: "margin-top:32px" },
        el("li", {}, el("button", { class: "row", type: "button", style: "justify-content:center;color:var(--red-text);font-weight:600", onclick: () => signOut() }, "Sign out"))),
      el("p", { class: "group-foot", text: "Signing out removes your key from this browser tab." })),
  });
}

function renderConn(s) {
  if (!shell) return;
  shell.conn.dataset.state = s;
  const text = s === "live" ? "Live" : s === "connecting" ? "Reconnecting…" : "Offline";
  shell.conn.lastChild.textContent = text;
  shell.conn.setAttribute("aria-label", `Connection: ${text}`);
  shell.conn.title = s === "live" ? "Receiving alerts in real time" : s === "connecting" ? "Trying to reconnect for live alerts" : "Not receiving live alerts";
}

function renderBadge() {
  if (!shell) return;
  const n = alerts.openCount();
  for (const b of shell.badges) {
    b.hidden = !n;
    b.textContent = n > 99 ? "99+" : String(n);
  }
  for (const a of shell.navLinks.alerts) a.setAttribute("aria-label", n ? `Alerts, ${n} need review` : "Alerts");
}

function notify(a) {
  if (a.severity === "high") chime();
  if (currentRoute === "alerts") return;
  const t = typeInfo(a.type);
  toast({
    title: a.type === "possible_aggression" ? `${t.label} — needs review` : t.label,
    text: `${roomName(a.room_id)} · ${a.message}`,
    glyphNode: glyph(t.icon, t.tint, "sm"),
    onClick: () => openAlert(a.id),
    timeout: a.severity === "high" ? 9000 : 6000,
  });
}

function teardownShell() {
  unmount?.();
  unmount = null;
  currentRoute = null;
  offs.forEach((f) => f());
  offs = [];
  shell = null;
}

async function switchSite(id) {
  const site = state.sites.find((s) => s.id === id);
  if (!site || site.id === state.site.id) return;
  state.site = site;
  session.set(SITE, site.id);
  closeSheet();
  alerts.reset();
  renderBadge();
  alerts.load().catch((e) => toast({ title: "Couldn't load alerts", text: e.message }));
  route(true);
}

// ---------- Router ----------
function parseHash() {
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const id = ROUTES[path] ? path : "overview";
  return { id, params: Object.fromEntries(new URLSearchParams(query)) };
}

function navigate(id, params = {}) {
  const qs = new URLSearchParams(params).toString();
  const hash = `#/${id}${qs ? `?${qs}` : ""}`;
  if (location.hash === hash) route(true); else location.hash = hash;
}

function route(force = false) {
  if (!shell) return;
  const { id, params } = parseHash();
  if (!force && id === currentRoute && !Object.keys(params).length) return;
  unmount?.();
  currentRoute = id;
  const r = ROUTES[id];
  for (const [rid, links] of Object.entries(shell.navLinks)) {
    for (const a of links) {
      if (rid === id) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    }
  }
  shell.barTitle.textContent = r.label;
  document.title = `${r.label} · ${state.site.name} · Vision Day Baby`;
  const host = el("div", { class: "view" });
  shell.content.replaceChildren(host);
  unmount = r.view.mount(host, { params, navigate }) || null;
  window.scrollTo(0, 0);
  shell.topbar.classList.remove("scrolled");
  shell.content.focus({ preventScroll: true });
}

window.addEventListener("hashchange", () => route());
on("auth-lost", () => { if (state.me) signOut("Your session ended. Please sign in again."); });

// The skip link targets the main region without touching the router's hash.
document.querySelector(".skip-link")?.addEventListener("click", (e) => {
  e.preventDefault();
  document.getElementById("content")?.focus();
});

// ---------- Boot ----------
const savedKey = session.get(KEY);
if (savedKey) {
  app.replaceChildren(el("div", { class: "signin" }, el("div", { class: "spinner", role: "status", "aria-label": "Signing in" })));
  signIn(savedKey).catch((e) => {
    session.remove(KEY);
    showSignIn(e instanceof ApiError && e.status !== 401 ? e.message : "");
  });
} else {
  showSignIn();
}
