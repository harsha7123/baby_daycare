import { getJSON, on, q } from "../api.js";
import { chip, dateLong, el, emptyState, icon, nowSec, svg, timeOf, todayKey, dayKey } from "../dom.js";
import { alerts, state, tz } from "../state.js";

const ROOM_REFRESH_MS = 5000;
const TIMELINE_REFRESH_MS = 60000;

export const title = "Overview";

export function mount(root, { navigate }) {
  const site = state.site;
  let rooms = null, timeline = null, stopped = false;

  const tiles = {
    open: tile("alerts", "blue", "Open alerts", "Waiting for review", () => navigate("alerts", { filter: "review" })),
    high: tile("warn", "red", "High severity", "Raised today", () => navigate("alerts", { filter: "high" })),
    kids: tile("child", "teal", "Children present", "Across all rooms"),
    ok: tile("check", "green", "Rooms in ratio", "Adult-to-child ratio met"),
  };
  const roomsGrid = el("div", { class: "rooms", "aria-live": "off" }, ...site.rooms.map(() => roomSkeleton()));
  const updated = el("span", { class: "t-foot muted", "aria-live": "polite" });

  root.replaceChildren(
    el("header", { class: "page-head" },
      el("div", {}, el("div", { class: "eyebrow", text: dateLong(nowSec(), tz()) }), el("h1", { class: "t-large", text: "Overview" })),
      el("div", { class: "actions" }, updated)),
    el("section", { "aria-label": "Summary" }, el("div", { class: "tiles" }, ...Object.values(tiles).map((t) => t.node))),
    el("section", { class: "section", "aria-labelledby": "rooms-h" },
      el("div", { class: "section-head" }, el("h2", { id: "rooms-h", text: "Rooms" }),
        el("span", { class: "t-foot muted", text: site.name })),
      roomsGrid),
  );

  function renderTiles() {
    if (alerts.loaded) {
      const open = alerts.openCount();
      tiles.open.set(open >= 500 ? "500+" : open);
      const today = todayKey(tz());
      tiles.high.set(alerts.list().filter((a) => a.severity === "high" && dayKey(a.triggered_at, tz()) === today).length);
    }
    if (rooms) {
      const withData = rooms.filter((r) => r.ts !== undefined);
      tiles.kids.set(withData.length ? withData.reduce((s, r) => s + (r.children || 0), 0) : "—");
      const ok = withData.filter((r) => !r.no_adult && r.ratio_ok).length;
      tiles.ok.set(withData.length ? ok : "—", withData.length ? ` / ${rooms.length}` : "");
      tiles.ok.setTint(!withData.length ? "gray" : ok === withData.length ? "green" : "orange");
    }
  }

  function renderRooms() {
    if (!rooms) return;
    if (!rooms.length) {
      roomsGrid.replaceChildren(el("div", { class: "card" }, emptyState("overview", "gray", "No rooms", "This site has no rooms configured yet.")));
      return;
    }
    const tl = new Map((timeline?.rooms || []).map((r) => [r.room_id, r.points]));
    roomsGrid.replaceChildren(...rooms.map((r) => roomCard(r, tl.get(r.room_id))));
  }

  async function loadRooms() {
    if (document.visibilityState !== "visible" && rooms) return;
    try {
      const data = await getJSON(`/api/rooms/live?${q({ site_id: site.id })}`);
      if (stopped) return;
      rooms = data;
      renderRooms();
      renderTiles();
      updated.textContent = `Updated ${timeOf(nowSec(), tz())}`;
    } catch (e) {
      if (!stopped && !rooms) roomsGrid.replaceChildren(el("div", { class: "card" }, emptyState("warn", "orange", "Couldn't load rooms", e.message)));
    }
  }

  async function loadTimeline() {
    try {
      const data = await getJSON(`/api/rooms/timeline?${q({ site_id: site.id, day: todayKey(tz()), bucket_minutes: 10 })}`);
      if (stopped) return;
      timeline = data;
      renderRooms();
    } catch { /* sparklines are optional */ }
  }

  loadRooms();
  loadTimeline();
  renderTiles();
  const t1 = setInterval(loadRooms, ROOM_REFRESH_MS);
  const t2 = setInterval(loadTimeline, TIMELINE_REFRESH_MS);
  const offs = [on("alerts:reset", renderTiles), on("alerts:new", renderTiles), on("alerts:update", renderTiles)];
  const onVis = () => { if (document.visibilityState === "visible") loadRooms(); };
  document.addEventListener("visibilitychange", onVis);

  return () => {
    stopped = true;
    clearInterval(t1);
    clearInterval(t2);
    offs.forEach((f) => f());
    document.removeEventListener("visibilitychange", onVis);
  };
}

function tile(iconName, tint, label, foot, onClick) {
  const value = el("span", { class: "tile-value num" }, el("span", { class: "skeleton", style: "display:inline-block;width:56px;height:30px;vertical-align:middle" }));
  const node = el(onClick ? "button" : "div", { class: `card tile tint-${tint}`, type: onClick ? "button" : undefined },
    el("span", { class: "tile-head" }, icon(iconName, { size: 18, stroke: 2.2 }), label),
    value,
    el("span", { class: "tile-foot", text: foot }));
  if (onClick) node.addEventListener("click", onClick);
  return {
    node,
    set(v, suffix = "") {
      value.replaceChildren(String(v), suffix ? el("small", { text: suffix }) : "");
      if (onClick) node.setAttribute("aria-label", `${label}: ${v}${suffix}. ${foot}. Show alerts`);
    },
    setTint(t) { node.className = node.className.replace(/tint-\w+/, `tint-${t}`); },
  };
}

function roomSkeleton() {
  return el("div", { class: "card room", "aria-hidden": "true" },
    el("div", { class: "skeleton title" }),
    el("div", { class: "room-body" }, el("div", { class: "skeleton", style: "width:84px;height:84px;border-radius:50%" }),
      el("div", {}, el("div", { class: "skeleton line" }), el("div", { class: "skeleton line", style: "width:60%" }))),
    el("div", { class: "skeleton", style: "height:44px" }));
}

function roomStatus(r) {
  if (r.ts === undefined) return { label: "No data", tint: "gray" };
  if (r.no_adult) return { label: "No adult", tint: "red" };
  if (!r.ratio_ok) return { label: "Over ratio", tint: "orange" };
  return { label: "OK", tint: "green" };
}

function roomCard(r, points) {
  const st = roomStatus(r);
  const has = r.ts !== undefined;
  const max = r.max_children_per_adult;
  const perAdult = has && r.adults > 0 ? r.children / r.adults : null;
  const load = !has ? 0 : r.adults > 0 ? Math.min(1, perAdult / max) : r.children > 0 ? 1 : 0;
  const ratioText = !has ? "—" : r.adults > 0 ? (Math.round(perAdult * 10) / 10).toString() : r.children > 0 ? "!" : "0";
  const age = has ? nowSec() - r.ts : null;
  const ringLabel = !has ? "No data" : r.adults > 0 ? `${ratioText} children per adult, maximum ${max}` : `${r.children} children and no adult`;

  return el("article", { class: `card room tint-${st.tint}`, "aria-label": `${r.name}: ${st.label}` },
    el("div", { class: "room-head" },
      el("h3", { class: "t-headline", text: r.name }),
      chip(st.label, st.tint, { dot: true })),
    el("div", { class: "room-body" },
      ring(load, ratioText, "per adult", ringLabel),
      el("div", { class: "stats" },
        stat("Children", has ? r.children : "—"),
        stat("Adults", has ? r.adults : "—"),
        stat("Max ratio", `1 : ${max}`),
        stat("Cameras", has ? r.cameras_online : "—", has ? " online" : ""))),
    el("div", { class: "spark" },
      el("div", { class: "spark-legend" },
        el("span", {}, el("i", { style: "background:var(--blue)" }), "Children"),
        el("span", {}, el("i", { style: "background:var(--green)" }), "Adults"),
        el("span", { class: "when", text: has ? (age < 90 ? "Live" : `As of ${timeOf(r.ts, tz())}`) : "Today" })),
      sparkline(points)));
}

function stat(k, v, suffix = "") {
  return el("div", { class: "stat" }, el("div", { class: "k", text: k }), el("div", { class: "v" }, String(v), suffix ? el("small", { text: suffix }) : null));
}

function ring(frac, big, small, label) {
  const R = 40, C = 2 * Math.PI * R;
  return el("div", { class: "ring", role: "img", "aria-label": label },
    svg("svg", { viewBox: "0 0 96 96", "aria-hidden": "true" },
      svg("circle", { class: "track", cx: 48, cy: 48, r: R, fill: "none", "stroke-width": 9 }),
      svg("circle", { class: "arc", cx: 48, cy: 48, r: R, fill: "none", "stroke-width": 9, "stroke-linecap": "round",
        "stroke-dasharray": C, "stroke-dashoffset": C * (1 - Math.max(frac, frac > 0 ? 0.02 : 0)) })),
    el("div", { class: "center", "aria-hidden": "true" }, el("b", { text: big }), el("span", { text: small })));
}

function sparkline(points) {
  const W = 300, H = 44;
  if (!points || points.length < 2) {
    return el("div", { class: "t-foot faint", style: "height:44px;display:grid;align-items:center", text: points ? "Not enough data yet today" : "Loading today’s trend…" });
  }
  const t0 = points[0].t, t1 = points[points.length - 1].t || t0 + 1;
  const maxV = Math.max(1, ...points.map((p) => Math.max(p.children, p.adults)));
  const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * W;
  const y = (v) => H - 2 - (v / maxV) * (H - 6);
  const line = (k) => points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p[k]).toFixed(1)}`).join("");
  const area = `${line("children")}L${x(t1).toFixed(1)},${H}L0,${H}Z`;
  const step = points.length > 1 ? x(points[1].t) - x(points[0].t) : 4;
  const peak = Math.max(...points.map((p) => p.children));
  const noAdult = points.filter((p) => p.no_adult).map((p) => svg("rect", { class: "noadult", x: x(p.t).toFixed(1), y: 0, width: Math.max(2, step).toFixed(1), height: H }));
  return svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img",
    "aria-label": `Today: peak of ${peak} children${noAdult.length ? `, ${noAdult.length} periods without an adult` : ""}` },
  ...noAdult,
  svg("path", { class: "a-children", d: area, stroke: "none" }),
  svg("path", { class: "l-children", d: line("children"), fill: "none", "stroke-width": 2, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round" }),
  svg("path", { class: "l-adults", d: line("adults"), fill: "none", "stroke-width": 2, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round" }));
}
