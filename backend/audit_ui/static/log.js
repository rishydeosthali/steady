"use strict";
let filter = "all";
const $ = (id) => document.getElementById(id);
function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function when(iso) {
  try {
    return new Date(iso).toLocaleString("en-US", {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
  } catch {
    return iso;
  }
}
function chips(detail) {
  const wrap = node("div", undefined, "chips");
  for (const [key, value] of Object.entries(detail || {})) {
    if (value === "" || value === null || value === undefined) continue;
    if (key === "text") continue;
    wrap.append(node("span", key.replaceAll("_", " ") + ": " + String(value)));
  }
  return wrap.childNodes.length ? wrap : null;
}
function render(events) {
  $("count").textContent = String(events.length);
  $("fresh").textContent = events.length ? "Updated " + when(events[events.length - 1].at) : "Waiting for the first decision";
  const list = $("timeline");
  list.replaceChildren();
  const shown = events.filter((event) => filter === "all" || event.kind === filter);
  if (!shown.length) {
    list.append(node("p", events.length ? "Nothing in this filter yet." : "The tape is empty. Decisions appear here as Margaret texts Steady.", "empty"));
    return;
  }
  for (const event of [...shown].reverse()) {
    const item = node("li");
    const card = node("article", undefined, "card");
    const head = node("header");
    head.append(node("span", event.kind.replaceAll("_", " "), "kind " + event.kind));
    head.append(node("time", when(event.at)));
    card.append(head);
    card.append(node("p", event.reason || (event.detail && event.detail.text) || "Recorded.", "reason"));
    const extra = chips(event.detail);
    if (extra) card.append(extra);
    item.append(card);
    list.append(item);
  }
}
async function refresh() {
  try {
    const response = await fetch("/audit/events");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "The log could not be read.");
    render(payload.events || []);
    $("error").hidden = true;
  } catch (err) {
    $("error").textContent = err.message;
    $("error").hidden = false;
  }
}
document.querySelectorAll("[data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach((other) => {
      other.classList.toggle("on", other === button);
      other.classList.toggle("secondary", other !== button);
    });
    refresh();
  });
});
refresh();
setInterval(refresh, 2000);
