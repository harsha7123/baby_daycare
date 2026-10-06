import { getJSON, q } from "../api.js";
import { dateTime, el, emptyState, glyph, icon, initials, typeInfo } from "../dom.js";
import { openAlert } from "../alert-detail.js";
import { alerts, state, tz } from "../state.js";
import { precision } from "./reports.js";

export const title = "Activity";

const KIND = {
  thumbnail: { label: "Viewed snapshot", icon: "eye", tint: "blue" },
  clip: { label: "Watched clip", icon: "play", tint: "indigo" },
  report: { label: "Opened report", icon: "doc", tint: "teal" },
  live: { label: "Watched live", icon: "live", tint: "red" },
};
const DAYS = [7, 30, 90];

export function mount(root) {
  const site = state.site;
  let days = 30, stopped = false;

  const accHost = el("div", { "aria-busy": "true" }, skeletonCard(4));
  const logHost = el("div", { "aria-busy": "true" }, skeletonCard(6));
  const refresh = el("button", { class: "btn", type: "button" }, icon("refresh", { size: 18 }), "Refresh");
  refresh.addEventListener("click", () => { loadAcc(); loadLog(); });

  const seg = el("div", { class: "segmented", role: "group", "aria-label": "Period" });
  for (const d of DAYS) {
    const b = el("button", { type: "button", "aria-pressed": String(d === days), text: `${d} days` });
    b.addEventListener("click", () => {
      days = d;
      for (const x of seg.children) x.setAttribute("aria-pressed", String(x === b));
      loadAcc();
    });
    seg.append(b);
  }

  root.replaceChildren(
    el("header", { class: "page-head" },
      el("div", {}, el("div", { class: "eyebrow", text: site.name }), el("h1", { class: "t-large", text: "Activity" })),
      el("div", { class: "actions" }, refresh)),
    el("div", { class: "two-col" },
      el("section", { "aria-labelledby": "acc-h" },
        el("div", { class: "section-head" }, el("h2", { id: "acc-h", text: "Detection accuracy" }), seg),
        accHost,
        el("p", { class: "group-foot", text: "Measured from staff reviews: of the alerts someone confirmed or dismissed, the share that were real." })),
      el("section", { "aria-labelledby": "log-h" },
        el("div", { class: "section-head" }, el("h2", { id: "log-h", text: "Who viewed what" })),
        logHost,
        el("p", { class: "group-foot", text: "Every snapshot, clip, report and live view is recorded with the viewer's name and address." }))),
  );

  async function loadAcc() {
    accHost.setAttribute("aria-busy", "true");
    try {
      const acc = await getJSON(`/api/accuracy?${q({ site_id: site.id, days })}`);
      if (stopped) return;
      const rows = Object.entries(acc).sort((a, b) => typeInfo(a[0]).label.localeCompare(typeInfo(b[0]).label));
      accHost.replaceChildren(rows.length ? el("ul", { class: "list", style: "--inset:56px" }, ...rows.map(([type, v]) => {
        const t = typeInfo(type);
        const reviewed = (v.confirmed || 0) + (v.false_alarm || 0);
        return el("li", {}, el("div", { class: "row" },
          glyph(t.icon, t.tint, "sm"),
          el("div", { class: "grow" }, el("div", { style: "font-weight:600", text: t.label }),
            el("div", { class: "t-foot muted", text: `${v.confirmed || 0} confirmed · ${v.false_alarm || 0} false · ${v.open || 0} open` })),
          el("div", { style: "width:clamp(104px,32%,180px);flex:none", "aria-label": reviewed ? `Precision ${Math.round((v.precision || 0) * 100)}%` : "Not enough reviews" }, precision(v.precision))));
      })) : el("div", { class: "card" }, emptyState("target", "gray", "No alerts yet", `Nothing was raised in the last ${days} days.`)));
    } catch (e) {
      if (!stopped) accHost.replaceChildren(el("div", { class: "card" }, emptyState("warn", "orange", "Couldn't load accuracy", e.message)));
    } finally { accHost.setAttribute("aria-busy", "false"); }
  }

  async function loadLog() {
    logHost.setAttribute("aria-busy", "true");
    try {
      const log = await getJSON(`/api/access-log?${q({ site_id: site.id, limit: 200 })}`);
      if (stopped) return;
      logHost.replaceChildren(log.length ? el("ul", { class: "list", style: "--inset:60px" }, ...log.map((r) => {
        const k = KIND[r.kind] || { label: r.kind, icon: "eye", tint: "gray" };
        const a = r.alert_id ? alerts.get(r.alert_id) : null;
        const what = a ? `${k.label} · ${typeInfo(a.type).label}` : r.alert_id ? `${k.label} · alert ${r.alert_id.slice(0, 6)}` : k.label;
        const inner = [
          el("span", { class: "avatar", "aria-hidden": "true", text: initials(r.user) }),
          el("span", { class: "grow" },
            el("span", {}, el("strong", { text: r.user }), " ", el("span", { class: "muted", text: what })),
            el("span", { class: "t-foot faint num", text: `${dateTime(r.ts, tz())} · ${r.ip}` })),
          el("span", { class: `glyph sm tint-${k.tint}`, "aria-hidden": "true" }, icon(k.icon, { size: 16 })),
        ];
        return el("li", {}, a
          ? el("button", { class: "row", type: "button", "aria-label": `${r.user}: ${what}, ${dateTime(r.ts, tz())}. Open alert`, onclick: () => openAlert(a.id) }, ...inner)
          : el("div", { class: "row" }, ...inner));
      })) : el("div", { class: "card" }, emptyState("eye", "gray", "No views yet", "Footage views will be listed here.")));
    } catch (e) {
      if (!stopped) logHost.replaceChildren(el("div", { class: "card" }, emptyState("warn", "orange", "Couldn't load the access log", e.message)));
    } finally { logHost.setAttribute("aria-busy", "false"); }
  }

  loadAcc();
  loadLog();
  return () => { stopped = true; };
}

function skeletonCard(n) {
  return el("ul", { class: "list", "aria-hidden": "true" }, ...Array.from({ length: n }, () => el("li", {},
    el("div", { class: "row" }, el("div", { class: "skeleton", style: "width:32px;height:32px;border-radius:50%" }),
      el("div", { class: "grow" }, el("div", { class: "skeleton line", style: "width:55%" }), el("div", { class: "skeleton line", style: "width:35%" }))))));
}
