// Apple-style modal sheet (built on <dialog> for focus trapping and Esc) and notification toasts.
import { el, icon } from "./dom.js";

const dialog = () => document.getElementById("sheet");
let current = null;

export function openSheet({ title, body, wide = false, onClose }) {
  const d = dialog();
  if (current) finish(current, false);
  const returnFocus = document.activeElement;
  const titleEl = el("h2", { id: "sheet-title", text: title });
  const done = el("button", { class: "btn btn-plain bold done", type: "button", text: "Done" });
  const bodyEl = el("div", { class: "sheet-body" }, body);
  d.replaceChildren(
    el("div", { class: "sheet-head" }, el("span", { class: "grabber", "aria-hidden": "true" }), titleEl, done),
    bodyEl,
  );
  d.classList.toggle("wide", wide);
  d.classList.remove("closing");
  const handle = { d, onClose, returnFocus, closed: false, setTitle: (t) => { titleEl.textContent = t; }, body: bodyEl };
  handle.close = () => close(handle);
  current = handle;
  done.addEventListener("click", handle.close);
  d.oncancel = (e) => { e.preventDefault(); handle.close(); };
  d.onclick = (e) => { if (e.target === d) handle.close(); }; // click on the backdrop
  if (!d.open) d.showModal();
  document.documentElement.style.overflow = "hidden";
  bodyEl.scrollTop = 0;
  done.focus({ preventScroll: true });
  return handle;
}

function close(h) {
  if (h.closed) return;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  h.d.classList.add("closing");
  if (reduce) finish(h, true);
  else setTimeout(() => finish(h, true), 230);
}

function finish(h, restoreFocus) {
  if (h.closed) return;
  h.closed = true;
  h.onClose?.();
  if (current === h) {
    current = null;
    h.d.classList.remove("closing");
    if (h.d.open) h.d.close();
    h.d.replaceChildren();
    document.documentElement.style.overflow = "";
    if (restoreFocus && h.returnFocus?.isConnected) h.returnFocus.focus({ preventScroll: true });
  }
}

export const closeSheet = () => current?.close();

// ---------- Toasts ----------
export function toast({ title, text, glyphNode, onClick, timeout = 6000 }) {
  const host = document.getElementById("toasts");
  const node = el(onClick ? "button" : "div", { class: "toast", type: onClick ? "button" : undefined },
    glyphNode || el("span", { class: "glyph sm tint-blue", "aria-hidden": "true" }, icon("info", { size: 17 })),
    el("span", { class: "grow" }, el("strong", { text: title }), text ? el("span", { text }) : null),
  );
  const remove = () => {
    if (!node.isConnected) return;
    node.classList.add("out");
    setTimeout(() => node.remove(), 280);
  };
  if (onClick) node.addEventListener("click", () => { remove(); onClick(); });
  host.prepend(node);
  while (host.children.length > 3) host.lastElementChild.remove();
  let timer = setTimeout(remove, timeout);
  node.addEventListener("mouseenter", () => clearTimeout(timer));
  node.addEventListener("mouseleave", () => { timer = setTimeout(remove, 2500); });
  return remove;
}
