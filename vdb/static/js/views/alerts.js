import { on } from "../api.js";
import { chip, dayKey, dayLabel, el, emptyState, glyph, icon, relTime, SEVERITY, STATUS, timeOf, TYPES, typeInfo, VERIFICATION } from "../dom.js";
import { openAlert } from "../alert-detail.js";
import { alerts, roomName, state, thumbImg, tz } from "../state.js";

export const title = "Alerts";

const FILTERS = [
  { id: "all", label: "All", test: () => true },
  { id: "review", label: "Needs review", test: (a) => a.status === "open" },
  { id: "high", label: "High", test: (a) => a.severity === "high" },
  { id: "aggression", label: "Aggression", test: (a) => a.type === "possible_aggression" },
];

export function mount(root, { params }) {
  let filter = FILTERS.some((f) => f.id === params.filter) ? params.filter : "all";
  let type = "";
  const fresh = new Set();

  const seg = el("div", { class: "segmented", role: "group", "aria-label": "Show" });
  const segButtons = FILTERS.map((f) => {
    const count = el("span", { class: "count" });
    const b = el("button", { type: "button", "aria-pressed": String(f.id === filter), dataset: { id: f.id } }, f.label, count);
    b.__count = count;
    b.addEventListener("click", () => { filter = f.id; segButtons.forEach((x) => x.setAttribute("aria-pressed", String(x === b))); render(); });
    seg.append(b);
    return b;
  });

  const typeSel = el("select", { "aria-label": "Alert type" },
    el("option", { value: "", text: "All types" }),
    ...Object.entries(TYPES).map(([k, v]) => el("option", { value: k, text: v.label })));
  typeSel.addEventListener("change", () => { type = typeSel.value; render(); });

  const listHost = el("div", { "aria-live": "off" });
  const status = el("p", { class: "sr-only", role: "status", "aria-live": "polite" });
  const more = el("div", { class: "load-more" });

  root.replaceChildren(
    el("header", { class: "page-head" },
      el("div", {}, el("div", { class: "eyebrow", text: state.site.name }), el("h1", { class: "t-large", text: "Alerts" }))),
    el("div", { class: "toolbar" }, seg, el("label", { class: "popup large" }, typeSel, icon("chevronDown", { size: 14, stroke: 2.5 }))),
    status, listHost, more,
  );

  function visible() {
    const f = FILTERS.find((x) => x.id === filter);
    return alerts.list().filter((a) => f.test(a) && (!type || a.type === type));
  }

  function updateCounts() {
    const base = alerts.list().filter((a) => !type || a.type === type);
    for (const b of segButtons) {
      const f = FILTERS.find((x) => x.id === b.dataset.id);
      const n = f.id === "all" ? 0 : base.filter(f.test).length;
      b.__count.textContent = n ? String(n) : "";
    }
  }

  function render() {
    updateCounts();
    if (!alerts.loaded) {
      listHost.replaceChildren(skeletonList());
      more.replaceChildren();
      return;
    }
    const focusedId = document.activeElement?.closest?.("[data-alert]")?.dataset.alert;
    const rows = visible();
    if (!rows.length) {
      const msg = filter === "review" ? ["All caught up", "There are no alerts waiting for review."]
        : filter === "aggression" ? ["No aggression flags", "Nothing has been flagged as possible rough handling."]
        : ["No alerts", "Alerts will appear here the moment they are raised."];
      listHost.replaceChildren(el("div", { class: "card" }, emptyState(filter === "review" ? "check" : "alerts", filter === "review" ? "green" : "blue", ...msg)));
    } else {
      const groups = new Map();
      for (const a of rows) {
        const k = dayKey(a.triggered_at, tz());
        if (!groups.has(k)) groups.set(k, []);
        groups.get(k).push(a);
      }
      const nodes = [];
      for (const [k, items] of groups) {
        const hid = `day-${k}`;
        nodes.push(el("section", { "aria-labelledby": hid },
          el("h2", { class: "group-label", id: hid }, dayLabel(k, tz()), el("span", { class: "faint", text: ` · ${items.length}` })),
          el("ul", { class: "list" }, ...items.map((a) => row(a, fresh.has(a.id))))));
      }
      listHost.replaceChildren(...nodes);
      fresh.clear();
    }
    if (focusedId) listHost.querySelector(`[data-alert="${CSS.escape(focusedId)}"] button`)?.focus({ preventScroll: true });
    more.replaceChildren(alerts.exhausted ? el("p", { class: "t-foot faint", text: rows.length ? "No older alerts" : "" }) : loadMoreButton());
  }

  function loadMoreButton() {
    const b = el("button", { class: "btn", type: "button", text: "Show earlier alerts" });
    b.addEventListener("click", async () => {
      b.disabled = true;
      b.textContent = "Loading…";
      try { await alerts.loadOlder(); } catch (e) { b.disabled = false; b.textContent = `Try again — ${e.message}`; }
    });
    return b;
  }

  render();
  const offs = [
    on("alerts:reset", render),
    on("alerts:new", (a) => {
      fresh.add(a.id);
      render();
      const t = typeInfo(a.type);
      status.textContent = `New alert: ${t.label} in ${roomName(a.room_id)}`;
    }),
    on("alerts:update", (a) => {
      const li = listHost.querySelector(`[data-alert="${CSS.escape(a.id)}"]`);
      const f = FILTERS.find((x) => x.id === filter);
      if (li && f.test(a)) {
        const hadFocus = li.contains(document.activeElement);
        const next = row(a, false);
        li.replaceWith(next);
        if (hadFocus) next.querySelector("button")?.focus({ preventScroll: true });
        updateCounts();
      } else render();
    }),
  ];
  // Keep "3 min ago" labels fresh.
  const timer = setInterval(() => {
    for (const n of listHost.querySelectorAll("[data-ts]")) n.firstChild.textContent = relTime(Number(n.dataset.ts), tz());
  }, 30000);

  return () => { offs.forEach((f) => f()); clearInterval(timer); };
}

export function row(a, isNew) {
  const t = typeInfo(a.type);
  const sev = SEVERITY[a.severity] || SEVERITY.low;
  const st = STATUS[a.status] || STATUS.open;
  const sensitive = a.type === "possible_aggression";
  const v = sensitive ? (VERIFICATION[a.verification] || VERIFICATION.pending) : null;
  const label = `${t.label}, ${sev.label} severity, ${roomName(a.room_id)}, ${timeOf(a.triggered_at, tz())}. ${st.label}${v ? `. ${v.label}` : ""}`;

  return el("li", { class: `alert-row${sensitive ? " sensitive" : ""}${isNew ? " enter" : ""}`, dataset: { alert: a.id } },
    a.status === "open" ? el("span", { class: "unread-dot", "aria-hidden": "true" }) : null,
    el("button", { class: "row", type: "button", "aria-label": label, onclick: () => openAlert(a.id) },
      glyph(t.icon, t.tint),
      el("span", { class: "grow" },
        el("span", { class: "a-title" }, el("span", { class: `sev tint-${sev.tint}`, style: "background:var(--tint)", "aria-hidden": "true" }), t.label),
        el("span", { class: "a-msg", text: a.message }),
        el("span", { class: "a-meta" },
          el("span", { text: `${roomName(a.room_id)}${a.camera_id ? ` · ${a.camera_id}` : ""}` }),
          chip(st.label, st.tint),
          v ? chip(v.label, v.tint, { iconName: "sparkles" }) : null,
          sensitive && a.status === "open" ? chip("Person must review", "purple") : null)),
      el("span", { class: "a-right" },
        el("span", { class: "a-time", dataset: { ts: String(a.triggered_at) } }, relTime(a.triggered_at, tz()), icon("chevronRight", { size: 14, stroke: 2.5 })),
        a.thumbnail ? el("span", { class: "thumb" }, thumbImg(a, "")) : null)));
}

function skeletonList() {
  return el("div", { "aria-hidden": "true" },
    el("div", { class: "skeleton", style: "height:14px;width:90px;margin:24px 16px 8px" }),
    el("ul", { class: "list" }, ...Array.from({ length: 5 }, () => el("li", {},
      el("div", { class: "row" }, el("div", { class: "skeleton", style: "width:32px;height:32px;border-radius:8px" }),
        el("div", { class: "grow" }, el("div", { class: "skeleton line", style: "width:40%" }), el("div", { class: "skeleton line", style: "width:75%" })),
        el("div", { class: "skeleton", style: "width:96px;height:54px" }))))));
}
