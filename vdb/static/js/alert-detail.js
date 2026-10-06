// Alert detail sheet: snapshot, on-demand clip, timeline, AI second opinion and the human review.
import { blobOf, on } from "./api.js";
import { chip, dateLong, duration, el, glyph, icon, SEVERITY, STATUS, timeOf, timeSecOf, typeInfo, VERIFICATION } from "./dom.js";
import { openSheet } from "./sheet.js";
import { alerts, roomName, thumbs, tz } from "./state.js";

export function openAlert(id) {
  const a = alerts.get(id);
  if (!a) return;
  const t = typeInfo(a.type);
  const sensitive = a.type === "possible_aggression";
  let clipUrl = null, clipState = "idle"; // idle | loading | playing | notready | error

  // --- media ---
  const media = el("div", { class: "media" });
  function renderMedia() {
    const cur = alerts.get(id);
    if (clipState === "playing") return;
    const parts = [];
    const img = el("img", { alt: `Snapshot of ${t.label.toLowerCase()} alert in ${roomName(cur.room_id)}` });
    if (thumbs.urls.has(cur.id)) img.src = thumbs.urls.get(cur.id);
    else if (cur.thumbnail) thumbs.get(cur).then((u) => { img.src = u; }).catch(() => {});
    if (cur.thumbnail) parts.push(img);
    else parts.push(el("div", { class: "ph" }, icon("videoOff", { size: 28 }), el("span", { class: "t-foot", text: "No snapshot" })));

    const text = clipState === "loading" ? "Loading clip…"
      : clipState === "notready" ? "The clip is still being saved. It will be ready in a few seconds."
      : clipState === "error" ? "Couldn't load the clip. Tap to try again."
      : cur.clip ? "Play clip" : "Clip is being saved — tap to try";
    const play = el("button", { class: "play", type: "button", "aria-label": clipState === "loading" ? "Loading clip" : `Play the clip of this alert` },
      el("span", { class: "disc", "aria-hidden": "true" }, clipState === "loading" ? el("span", { class: "spinner", style: "border-color:rgba(255,255,255,.35);border-top-color:#fff;width:26px;height:26px" }) : icon("play", { size: 26, stroke: 0 })),
      el("span", { text }));
    play.querySelector("path")?.setAttribute("fill", "currentColor");
    play.disabled = clipState === "loading";
    play.addEventListener("click", playClip);
    parts.push(play);
    media.replaceChildren(...parts);
  }

  async function playClip() {
    clipState = "loading";
    renderMedia();
    try {
      const blob = await blobOf(`/api/alerts/${encodeURIComponent(id)}/clip`);
      if (sheet.closed) return;
      if (clipUrl) URL.revokeObjectURL(clipUrl);
      clipUrl = URL.createObjectURL(blob);
      clipState = "playing";
      const v = el("video", { controls: true, playsinline: true, preload: "auto", "aria-label": "Alert clip" });
      v.src = clipUrl;
      media.replaceChildren(v);
      v.play().catch(() => {});
      v.focus({ preventScroll: true });
    } catch (e) {
      if (sheet.closed) return;
      clipState = e.status === 404 ? "notready" : "error";
      renderMedia();
    }
  }

  // --- header ---
  const statusChip = el("span");
  const verifChip = el("span");
  const head = el("div", { class: "detail-head" }, glyph(t.icon, t.tint, "lg"),
    el("div", { class: "grow", style: "min-width:0" },
      el("h3", { class: "t-title", text: t.label }),
      el("div", { class: "a-meta", style: "display:flex;flex-wrap:wrap;gap:6px;margin-top:6px" },
        chip(`${SEVERITY[a.severity]?.label || a.severity} severity`, SEVERITY[a.severity]?.tint || "gray", { dot: true }),
        statusChip, verifChip)));

  const sensitiveBanner = sensitive ? el("div", { class: "banner tint-purple", role: "note" }, icon("sparkles", { size: 20 }),
    el("div", {}, el("strong", { text: "AI flagged — a person must review the clip" }),
      el("p", { text: "This is an automatic flag, not a confirmed incident. Watch the clip before deciding." }))) : null;

  // --- facts ---
  const facts = el("ul", { class: "list kv" },
    kv("Room", roomName(a.room_id)),
    kv("Camera", a.camera_id || "—"),
    a.track_id != null ? kv("Tracked person", `#${a.track_id}`) : null,
    kv("Date", dateLong(a.triggered_at, tz())));

  const timeline = el("div", { class: "card timeline", role: "list", "aria-label": "Timeline" },
    step("Condition started", timeSecOf(a.started_at, tz()), "orange"),
    step(`Alert raised${a.triggered_at > a.started_at ? ` after ${duration(a.triggered_at - a.started_at)}` : ""}`, timeSecOf(a.triggered_at, tz()), t.tint === "gray" ? "red" : t.tint));

  // --- AI second opinion ---
  const ai = sensitive ? el("div", { class: "card ai-card tint-purple" }) : null;
  function renderAI() {
    if (!ai) return;
    const cur = alerts.get(id);
    const v = VERIFICATION[cur.verification] || (cur.verification ? { label: `AI: ${cur.verification}`, tint: "gray", long: "" } : VERIFICATION.pending);
    ai.replaceChildren(
      el("div", { class: "ai-head" }, glyph("sparkles", "purple", "sm"), el("h4", { class: "t-headline", text: "AI second opinion" }),
        cur.verification && cur.verification !== "pending" ? chip(v.label, v.tint) : el("span", { class: "chip tint-gray" }, el("span", { class: "spinner", style: "width:12px;height:12px;border-width:1.5px" }), "Checking")),
      el("p", { class: "note", style: "margin:0", text: cur.verification_note || v.long }),
      el("p", { class: "disclaimer", style: "margin:0", text: "The second opinion is advice only. It can be wrong in both directions — only a person who has watched the clip should confirm or dismiss this alert." }));
  }

  // --- review ---
  const review = el("div", { class: "card review", role: "group", "aria-labelledby": `rv-${id}` });
  let editing = false;
  function renderReview() {
    const cur = alerts.get(id);
    const done = cur.status && cur.status !== "open";
    if (done && !editing) {
      const s = STATUS[cur.status] || { label: cur.status, tint: "gray" };
      review.replaceChildren(
        el("h4", { class: "t-headline", id: `rv-${id}`, text: "Review" }),
        el("div", { class: "reviewed" }, glyph(cur.status === "confirmed" ? "check" : "close", s.tint, "sm"),
          el("div", { class: "grow" },
            el("div", { class: "t-sub" }, el("strong", { text: s.label }), cur.reviewed_by ? ` by ${cur.reviewed_by}` : "",
              cur.reviewed_at ? el("span", { class: "muted", text: ` · ${timeOf(cur.reviewed_at, tz())}` }) : null),
            cur.note ? el("div", { class: "note t-sub", text: cur.note }) : null),
          el("button", { class: "btn btn-plain", type: "button", text: "Change", onclick: () => { editing = true; renderReview(); } })));
      return;
    }
    const note = el("textarea", { class: "field", id: `note-${id}`, maxlength: 1000, rows: 3, placeholder: "Add a note (optional)", "aria-label": "Review note (optional)" });
    if (cur.note) note.value = cur.note;
    const counter = el("span", { class: "counter", "aria-hidden": "true", text: `${note.value.length} / 1000` });
    note.addEventListener("input", () => { counter.textContent = `${note.value.length} / 1000`; });
    const err = el("p", { class: "err", role: "alert", hidden: true, style: "margin:0" });
    const confirmBtn = el("button", { class: "btn btn-tinted tint-red", type: "button" }, icon("check", { size: 18, stroke: 2.4 }), sensitive ? "Confirm incident" : "Confirm");
    const falseBtn = el("button", { class: "btn btn-tinted tint-gray", type: "button" }, icon("close", { size: 18, stroke: 2.4 }), "False alarm");
    const submit = async (status, btn) => {
      confirmBtn.disabled = falseBtn.disabled = true;
      const label = btn.lastChild.textContent;
      btn.lastChild.textContent = "Saving…";
      err.hidden = true;
      try {
        await alerts.review(id, status, note.value.trim());
        editing = false;
        renderReview();
        review.querySelector("button")?.focus({ preventScroll: true });
      } catch (e) {
        err.textContent = `Couldn't save the review. ${e.message}`;
        err.hidden = false;
        btn.lastChild.textContent = label;
        confirmBtn.disabled = falseBtn.disabled = false;
      }
    };
    confirmBtn.addEventListener("click", () => submit("confirmed", confirmBtn));
    falseBtn.addEventListener("click", () => submit("false_alarm", falseBtn));
    review.replaceChildren(
      el("h4", { class: "t-headline", id: `rv-${id}`, text: done ? "Change review" : "Your review" }),
      el("p", { class: "t-foot muted", style: "margin:-6px 0 0", text: sensitive ? "Decide only after watching the clip. Your name is recorded with the decision." : "Your name is recorded with the decision." }),
      note, counter,
      el("div", { class: "choices" }, confirmBtn, falseBtn),
      err,
      ...(done ? [el("button", { class: "btn btn-plain", type: "button", style: "justify-self:center", text: "Cancel", onclick: () => { editing = false; renderReview(); } })] : []));
  }

  function renderChips() {
    const cur = alerts.get(id);
    const s = STATUS[cur.status] || STATUS.open;
    statusChip.replaceChildren(chip(s.label, s.tint));
    if (sensitive) {
      const v = VERIFICATION[cur.verification] || VERIFICATION.pending;
      verifChip.replaceChildren(chip(v.label, v.tint, { iconName: "sparkles" }));
    }
  }

  const body = el("div", { class: "stack" },
    sensitiveBanner, head, media,
    el("p", { class: "t-body", style: "margin:16px 4px 0", text: a.message }),
    ai, review,
    el("div", { class: "group-label", style: "margin:24px 16px 8px", text: "Details" }), facts,
    el("div", { class: "group-label", style: "margin:24px 16px 8px", text: "Timeline" }), timeline,
    el("p", { class: "group-foot", text: "Opening the snapshot or clip is recorded in the access log." }));

  renderMedia();
  renderChips();
  renderAI();
  renderReview();

  const off = on("alerts:update", (u) => {
    if (u.id !== id) return;
    renderChips();
    renderAI();
    if (!editing && !review.contains(document.activeElement)) renderReview();
    if (clipState === "notready" || clipState === "idle") renderMedia();
  });
  const offClip = on("alerts:clip", (u) => { if (u.id === id && clipState === "notready") { clipState = "idle"; renderMedia(); } });

  const sheet = openSheet({
    title: sensitive ? "Needs human review" : "Alert",
    body,
    onClose: () => {
      off();
      offClip();
      media.querySelector("video")?.pause();
      if (clipUrl) URL.revokeObjectURL(clipUrl);
      clipUrl = null;
    },
  });
}

function kv(k, v) {
  return el("li", {}, el("div", { class: "row" }, el("span", { class: "k", text: k }), el("span", { class: "v", text: v })));
}

function step(label, when, tint) {
  return el("div", { class: `tl-step tint-${tint}`, role: "listitem" }, el("span", { class: "node", "aria-hidden": "true" }),
    el("span", { text: label }), el("span", { class: "when", text: when }));
}
