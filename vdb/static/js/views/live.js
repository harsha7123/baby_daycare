import { api } from "../api.js";
import { el, emptyState, icon } from "../dom.js";
import { openSheet } from "../sheet.js";
import { roomName, state } from "../state.js";

const REFRESH_MS = 1000;
const STALE_AFTER_MS = 5000; // same frame for this long = feed delayed

export const title = "Live";

export function mount(root) {
  const site = state.site;
  const cams = site.cameras;
  const feeds = new Set(); // feeds currently refreshing (on-screen tiles + the open viewer)

  const grid = el("div", { class: "cams" });
  root.replaceChildren(
    el("header", { class: "page-head" },
      el("div", {}, el("div", { class: "eyebrow", text: site.name }), el("h1", { class: "t-large", text: "Live" })),
      el("div", { class: "actions" }, el("span", { class: "t-foot muted", text: "Annotated frames, about one per second. Viewing is logged." }))),
    cams.length ? grid : el("div", { class: "card" }, emptyState("videoOff", "gray", "No cameras", "No cameras are configured for this site.")),
  );

  const io = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
    for (const e of entries) {
      const f = e.target.__feed;
      if (!f) continue;
      if (e.isIntersecting) { feeds.add(f); if (!viewer) f.refresh(); } else feeds.delete(f);
    }
  }, { threshold: 0.05 }) : null;

  const tileFeeds = cams.map((cam) => {
    const f = feed(cam, false);
    f.node.addEventListener("click", () => openViewer(cam));
    f.node.__feed = f;
    grid.append(f.node);
    if (io) io.observe(f.node); else feeds.add(f);
    return f;
  });

  let viewer = null; // while the enlarged view is open, only it refreshes (the grid is hidden behind the sheet)

  function openViewer(cam) {
    const f = feed(cam, true);
    viewer = f;
    f.refresh();
    openSheet({
      title: `${roomName(cam.room)} · ${cam.id}`,
      wide: true,
      body: el("div", {}, el("div", { class: "live-viewer" }, f.node),
        el("p", { class: "t-foot muted", style: "margin:12px 4px 0", text: "Boxes and labels are drawn by the detector. Refreshes about once a second while this window is visible." })),
      onClose: () => { if (viewer === f) viewer = null; f.dispose(); tick(); },
    });
  }

  const tick = () => {
    if (document.visibilityState !== "visible") return;
    if (viewer) { viewer.refresh(); return; }
    for (const f of feeds) f.refresh();
  };
  const timer = setInterval(tick, REFRESH_MS);

  return () => {
    clearInterval(timer);
    io?.disconnect();
    viewer?.dispose();
    tileFeeds.forEach((f) => f.dispose());
    feeds.clear();
  };
}

function feed(cam, large) {
  const img = el("img", { alt: `Live view of ${roomName(cam.room)}, camera ${cam.id}` });
  const label = el("span", { text: "Connecting" });
  const sub = el("span", { text: cam.id });
  const node = el(large ? "div" : "button", { class: "cam", type: large ? undefined : "button", "data-state": cam.enabled ? "loading" : "disabled",
    "aria-label": large ? undefined : `${roomName(cam.room)}, camera ${cam.id}. Open larger view` },
  img,
  el("span", { class: "nosignal" }, icon(cam.enabled ? "videoOff" : "noEntry", { size: 28, stroke: 1.8 }),
    el("span", { class: "ns-text", text: cam.enabled ? "Connecting…" : "Camera disabled" })),
  el("span", { class: "scrim", "aria-hidden": "true" }),
  el("span", { class: "cam-top" },
    el("span", { class: "live-badge", "aria-live": "off" }, el("span", { class: "dot", "aria-hidden": "true" }), label),
    large ? null : el("span", { class: "expand", "aria-hidden": "true" }, icon("expand", { size: 16 }))),
  el("span", { class: "cam-foot" }, el("span", {}, el("strong", { text: roomName(cam.room) }), sub)));

  const nsText = node.querySelector(".ns-text");
  let url = null, busy = false, ctrl = null, lastFrame = null, lastChange = 0, disposed = false;

  function setState(s) {
    node.dataset.state = s;
    label.textContent = s === "live" ? "Live" : s === "stale" ? "Delayed" : s === "nosignal" ? "Offline" : s === "disabled" ? "Off" : "Connecting";
    if (s === "nosignal") nsText.textContent = "No signal";
  }

  async function refresh() {
    if (disposed || busy || !cam.enabled) return;
    busy = true;
    ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 4000);
    try {
      const res = await api(`/api/cameras/${encodeURIComponent(cam.id)}/preview.jpg`, { signal: ctrl.signal });
      const frameTime = res.headers.get("X-Frame-Time");
      const blob = await res.blob();
      if (disposed) return;
      if (frameTime !== lastFrame || !url) {
        const next = URL.createObjectURL(blob);
        const prev = url;
        url = next;
        img.src = next;
        if (prev) img.decode().catch(() => {}).finally(() => URL.revokeObjectURL(prev));
        lastFrame = frameTime;
        lastChange = Date.now();
      }
      setState(Date.now() - lastChange > STALE_AFTER_MS ? "stale" : "live");
    } catch (e) {
      if (disposed) return;
      if (e.status === 404) {
        setState("nosignal");
        if (url) { URL.revokeObjectURL(url); url = null; img.removeAttribute("src"); }
      } else if (node.dataset.state === "live") setState("stale");
    } finally {
      clearTimeout(timeout);
      busy = false;
    }
  }

  function dispose() {
    disposed = true;
    ctrl?.abort();
    if (url) URL.revokeObjectURL(url);
    url = null;
  }

  return { node, refresh, dispose };
}
