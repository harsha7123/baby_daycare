import { blobOf, getJSON, q } from "../api.js";
import { chip, el, emptyState, glyph, icon, minutes, pct, STATUS, todayKey, typeInfo, SEVERITY } from "../dom.js";
import { openAlert } from "../alert-detail.js";
import { alerts, state, tz } from "../state.js";

export const title = "Reports";

export function mount(root) {
  const site = state.site;
  const today = todayKey(tz());
  let day = today, seq = 0, stopped = false;

  const date = el("input", { type: "date", value: day, max: today, "aria-label": "Report date", required: true });
  date.addEventListener("change", () => { if (date.value) { day = date.value; load(); } });
  const printBtn = el("button", { class: "btn btn-primary", type: "button" }, icon("print", { size: 18 }), "Print / PDF");
  printBtn.addEventListener("click", printReport);
  const printErr = el("span", { class: "t-foot", role: "alert", style: "color:var(--red-text)" });
  const host = el("div", { "aria-live": "polite", "aria-busy": "true" });

  root.replaceChildren(
    el("header", { class: "page-head" },
      el("div", {}, el("div", { class: "eyebrow", text: site.name }), el("h1", { class: "t-large", text: "Daily report" })),
      el("div", { class: "actions" }, el("label", { class: "date-field" }, el("span", { class: "sr-only", text: "Date" }), date), printBtn, printErr)),
    host,
  );

  async function load() {
    const my = ++seq;
    host.setAttribute("aria-busy", "true");
    host.replaceChildren(skeleton());
    try {
      const r = await getJSON(`/api/reports/daily?${q({ site_id: site.id, day })}`);
      if (stopped || my !== seq) return;
      host.replaceChildren(...render(r).filter(Boolean));
    } catch (e) {
      if (stopped || my !== seq) return;
      host.replaceChildren(el("div", { class: "card" }, emptyState("warn", "orange", "Couldn't load the report", e.message)));
    } finally {
      if (my === seq) host.setAttribute("aria-busy", "false");
    }
  }

  async function printReport() {
    printErr.replaceChildren();
    // Open the tab synchronously (popup blockers), then fill it with the report as a blob: URL.
    const win = window.open("", "_blank");
    printBtn.disabled = true;
    try {
      const blob = await blobOf(`/api/reports/daily.html?${q({ site_id: site.id, day })}`);
      const url = URL.createObjectURL(new Blob([blob], { type: "text/html" }));
      setTimeout(() => URL.revokeObjectURL(url), 5 * 60000);
      if (win) win.location.href = url;
      else {
        // Pop-ups blocked: offer a normal link instead.
        printErr.append(el("a", { href: url, target: "_blank", rel: "noopener", class: "btn", style: "min-height:36px", text: "Open printable report" }));
      }
    } catch (e) {
      win?.close();
      printErr.textContent = `Couldn't open the report. ${e.message}`;
    } finally {
      printBtn.disabled = false;
    }
  }

  load();
  return () => { stopped = true; };
}

function render(r) {
  const rooms = r.rooms || [];
  const totalAlerts = Object.values(r.alert_totals || {}).reduce((s, n) => s + n, 0);
  const noAdult = rooms.reduce((s, x) => s + (x.no_adult_minutes || 0), 0);
  const comp = rooms.map((x) => x.ratio_compliance_pct).filter((x) => x != null);
  const avgComp = comp.length ? comp.reduce((s, x) => s + x, 0) / comp.length : null;
  const peak = rooms.reduce((m, x) => Math.max(m, x.peak_children || 0), 0);
  const reviewed = Object.values(r.review || {}).reduce((s, x) => s + (x.confirmed || 0) + (x.false_alarm || 0), 0);

  const metrics = el("div", { class: "metric-grid" },
    metric("shield", avgComp == null ? "gray" : avgComp >= 95 ? "green" : avgComp >= 80 ? "orange" : "red", "Ratio compliance", avgComp == null ? "—" : `${Math.round(avgComp)}%`, "Average across rooms"),
    metric("userOff", noAdult > 0 ? "red" : "green", "No adult", minutes(noAdult), "Total, all rooms"),
    metric("child", "teal", "Peak children", String(peak), "Highest in one room"),
    metric("alerts", "blue", "Alerts", String(totalAlerts), `${reviewed} reviewed`));

  const roomTable = rooms.length ? el("div", { class: "card table-card" }, el("table", { class: "data stack-sm" },
    el("caption", { class: "sr-only", text: "Rooms" }),
    el("thead", {}, el("tr", {}, ...["Room", "Monitored", "No adult", "Ratio met", "Peak", "Alerts"].map((h, i) => el("th", { scope: "col", class: i ? "r" : undefined, text: h })))),
    el("tbody", {}, ...rooms.map((x) => {
      const n = Object.values(x.alerts || {}).reduce((s, v) => s + v, 0);
      const c = x.ratio_compliance_pct;
      const tint = c == null ? "gray" : c >= 95 ? "green" : c >= 80 ? "orange" : "red";
      return el("tr", {},
        el("td", { style: "font-weight:600" }, x.name),
        el("td", { class: "r", "data-label": "Monitored", text: minutes(x.monitored_minutes) }),
        el("td", { class: "r", "data-label": "No adult", text: minutes(x.no_adult_minutes) }),
        el("td", { class: "r", "data-label": "Ratio met" }, c == null ? "—" : chip(`${Math.round(c)}%`, tint)),
        el("td", { class: "r", "data-label": "Peak", text: String(x.peak_children ?? "—") }),
        el("td", { class: "r", "data-label": "Alerts", text: String(n) }));
    })))) : el("div", { class: "card" }, emptyState("overview", "gray", "No room data", "Nothing was monitored on this day."));

  const reviewRows = Object.entries(r.review || {});
  const precisionTable = reviewRows.length ? el("div", { class: "card table-card" }, el("table", { class: "data stack-sm" },
    el("caption", { class: "sr-only", text: "Alert review and precision" }),
    el("thead", {}, el("tr", {}, ...["Type", "Open", "Confirmed", "False alarm", "Precision"].map((h, i) => el("th", { scope: "col", class: i ? "r" : undefined, text: h })))),
    el("tbody", {}, ...reviewRows.map(([type, v]) => {
      const t = typeInfo(type);
      return el("tr", {},
        el("td", {}, el("span", { class: "cell-type" }, glyph(t.icon, t.tint, "sm"), t.label)),
        el("td", { class: "r", "data-label": "Open", text: String(v.open || 0) }),
        el("td", { class: "r", "data-label": "Confirmed", text: String(v.confirmed || 0) }),
        el("td", { class: "r", "data-label": "False alarm", text: String(v.false_alarm || 0) }),
        el("td", { class: "r", "data-label": "Precision", style: "min-width:120px" }, precision(v.precision)));
    })))) : el("div", { class: "card" }, emptyState("check", "green", "No alerts", "No alerts were raised on this day."));

  const list = (r.alerts || []);
  const alertList = list.length ? el("ul", { class: "list" }, ...list.map((a) => {
    const t = typeInfo(a.type);
    const st = STATUS[a.status] || STATUS.open;
    const inner = [glyph(t.icon, t.tint, "sm"),
      el("span", { class: "grow" }, el("span", { style: "font-weight:600", text: t.label }), el("span", { class: "t-foot muted", text: `${a.room} · ${a.message}` })),
      el("span", { style: "display:grid;justify-items:end;gap:4px" }, el("span", { class: "t-foot muted num", text: a.time }), chip(st.label, st.tint))];
    const known = alerts.get(a.id);
    return el("li", { style: "--inset:56px" }, known
      ? el("button", { class: "row", type: "button", "aria-label": `${t.label}, ${a.room}, ${a.time}, ${SEVERITY[a.severity]?.label || ""} severity. Open`, onclick: () => openAlert(a.id) }, ...inner)
      : el("div", { class: "row" }, ...inner));
  })) : null;

  return [
    metrics,
    el("section", { class: "section", "aria-labelledby": "rep-rooms" }, el("div", { class: "section-head" }, el("h2", { id: "rep-rooms", text: "Rooms" })), roomTable,
      el("p", { class: "group-foot", text: "Ratio met: share of occupied minutes where each adult had no more children than the room's limit." })),
    el("section", { class: "section", "aria-labelledby": "rep-review" }, el("div", { class: "section-head" }, el("h2", { id: "rep-review", text: "Alert review" })), precisionTable,
      el("p", { class: "group-foot", text: "Precision = confirmed ÷ (confirmed + false alarms). Open alerts are not counted." })),
    alertList ? el("section", { class: "section", "aria-labelledby": "rep-alerts" }, el("div", { class: "section-head" }, el("h2", { id: "rep-alerts", text: `Alerts (${list.length})` })), alertList) : null,
  ];
}

export function precision(p) {
  const tint = p == null ? "gray" : p >= 0.8 ? "green" : p >= 0.5 ? "orange" : "red";
  return el("div", { class: `prec tint-${tint}` },
    el("div", { class: "bar", "aria-hidden": "true" }, el("i", { style: `width:${p == null ? 0 : Math.round(p * 100)}%` })),
    el("b", { text: pct(p) }));
}

function metric(iconName, tint, label, value, foot) {
  return el("div", { class: `card tile tint-${tint}` },
    el("div", { class: "tile-head" }, icon(iconName, { size: 18, stroke: 2.2 }), label),
    el("div", { class: "tile-value num", text: value }),
    el("div", { class: "tile-foot", text: foot }));
}

function skeleton() {
  return el("div", { "aria-hidden": "true" },
    el("div", { class: "metric-grid" }, ...Array.from({ length: 4 }, () => el("div", { class: "card tile" }, el("div", { class: "skeleton line", style: "width:50%" }), el("div", { class: "skeleton", style: "height:34px;width:40%" })))),
    el("div", { class: "card pad section" }, ...Array.from({ length: 4 }, (_, i) => el("div", { class: "skeleton line", style: `width:${95 - i * 10}%;margin:14px 0` }))));
}
