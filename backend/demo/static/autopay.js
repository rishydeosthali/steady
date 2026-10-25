"use strict";
let token = sessionStorage.getItem("steady-demo-token") || "";
let current = null;
const $ = (id) => document.getElementById(id);
const money = (cents) => new Intl.NumberFormat("en-US", {style:"currency", currency:"USD"}).format(cents / 100);
function node(tag, text, cls) { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; }
function error(text) { $("error").textContent = text; $("error").hidden = !text; }
async function api(path, data) {
  const response = await fetch(path, {method:data === undefined ? "GET" : "POST", headers:{"Authorization":"Bearer " + token, "Content-Type":"application/json"}, ...(data === undefined ? {} : {body:JSON.stringify(data)})});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "The demo could not complete that action.");
  return value;
}
function active(actor) {
  if (!current) return null;
  return Object.values(current.requests).reverse().find(r => actor === "family" ? r.status === "awaiting_family" : ["awaiting_user","awaiting_family","scheduled"].includes(r.status));
}
function render(state) {
  current = state;
  $("access").hidden = true; $("workspace").hidden = false;
  $("today").textContent = new Date(state.today + "T12:00:00").toLocaleDateString("en-US", {month:"long",day:"numeric",year:"numeric"});
  $("balance").textContent = money(state.balance_cents); $("buffer").textContent = money(state.snapshot.context.buffer_cents);
  const ledger = Object.values(state.ledger);
  $("paid").textContent = ledger.filter(e => e.kind === "payment").length;
  $("channel").textContent = state.channel === "whatsapp" ? "Real WhatsApp · simulated money" : "Local phone simulator";
  $("senior-name").textContent = state.snapshot.customer.first_name;
  $("family-name").textContent = state.snapshot.context.trusted_person;
  $("scenario").textContent = state.snapshot.title;
  $("clock-label").textContent = state.seconds_per_day ? `One demo day every ${state.seconds_per_day} seconds · checks run automatically` : "Dates paused · bill checks continue in the background";
  $("auto").textContent = state.seconds_per_day ? "Pause dates" : "Auto-play";
  if (state.worker_error) error("The background worker needs attention: " + state.worker_error);
  for (const actor of ["senior", "family"]) {
    const list = $(actor + "-messages"), messages = state.messages.filter(m => m.actor === actor);
    const scroll = list.dataset.count !== String(messages.length);
    list.replaceChildren();
    if (!messages.length) list.append(node("p", actor === "family" ? "Only payments that need a second look appear here. The account balance stays private." : "Steady will start the conversation when a bill is within three days.", "empty"));
    for (const msg of messages) {
      const bubble = node("div", msg.body, "message " + msg.direction);
      bubble.append(node("small", msg.date + (msg.direction === "out" ? " · " + msg.delivery.replaceAll("_", " ") : " · you")));
      list.append(bubble);
    }
    list.dataset.count = String(messages.length); if (scroll) list.scrollTop = list.scrollHeight;
    document.querySelectorAll(`[data-actor="${actor}"]`).forEach(button => {button.disabled = state.channel === "whatsapp" || !active(actor);});
    const form = document.querySelector(`[data-form-actor="${actor}"]`);
    form.querySelectorAll("input,button").forEach(el => {el.disabled = state.channel === "whatsapp";});
    if (state.channel === "whatsapp") form.querySelector("input").placeholder = "Reply on the configured WhatsApp phone";
  }
  const requests = Object.values(state.requests);
  $("requests-count").textContent = requests.length + " request" + (requests.length === 1 ? "" : "s");
  $("requests").replaceChildren();
  for (const r of requests) {
    const tr = node("tr"), name = node("td", r.payee); name.append(node("small", r.id)); tr.append(name);
    tr.append(node("td", r.amount_cents === null ? "Unknown" : money(r.amount_cents)), node("td", r.due_date), node("td", r.payment_date || "No safe date"));
    const status = node("td"); status.append(node("span", r.status.replaceAll("_", " "), "status " + r.status)); tr.append(status); $("requests").append(tr);
  }
  $("events").replaceChildren();
  if (!state.events.length) $("events").append(node("p", "The tape is empty until Steady proposes a bill.", "empty"));
  for (const event of [...state.events].reverse()) {
    const li = node("li"), card = node("article", undefined, "tape-card"), head = node("header");
    head.append(node("span", event.kind.replaceAll("_", " "), "kind " + event.kind));
    head.append(node("time", event.date));
    card.append(head, node("p", event.reason || event.text));
    li.append(card); $("events").append(li);
  }
  $("ledger").replaceChildren();
  if (!ledger.length) $("ledger").append(node("p", "No simulated money has moved. A proposal alone never creates a payment.", "empty"));
  for (const entry of ledger.reverse()) {
    const li = node("li"), card = node("article", undefined, "tape-card"), head = node("header");
    head.append(node("span", entry.kind, "kind " + entry.kind));
    head.append(node("time", entry.date));
    card.append(head, node("p", (entry.kind === "deposit" ? "+ " : "− ") + money(entry.amount_cents) + (entry.payee ? " · " + entry.payee : " · synthetic income")));
    li.append(card); $("ledger").append(li);
  }
}
async function refresh() { if (!token) return; try {render(await api("/api/state"));} catch (err) {error(err.message);} }
$("access-form").addEventListener("submit", async (event) => {event.preventDefault(); token = $("token").value.trim(); try {render(await api("/api/state")); sessionStorage.setItem("steady-demo-token",token); $("token").value=""; error("");} catch (err) {error(err.message);} });
async function send(actor,text) { const req = active(actor); try {render(await api("/api/message", {actor,text,message_id:crypto.randomUUID(),request_id:req?.id})); error("");} catch(err) {error(err.message);} }
document.querySelectorAll("[data-actor]").forEach(button => button.addEventListener("click", () => send(button.dataset.actor,button.dataset.text)));
document.querySelectorAll("[data-form-actor]").forEach(form => form.addEventListener("submit", event => {event.preventDefault(); const input = form.elements.message; const text=input.value.trim(); if(text) {send(form.dataset.formActor,text); input.value="";}}));
document.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", async () => {button.disabled = true; const action=button.dataset.action; const data=action === "clock" ? {} : action === "auto" ? {seconds_per_day:current.seconds_per_day ? 0 : 20} : ["advance","delay-income"].includes(action) ? {days:action === "advance" ? 1 : 7} : {}; try {render(await api("/api/" + (action === "auto" ? "clock" : action),data)); error("");} catch(err) {error(err.message);} finally {button.disabled=false;} }));
refresh(); setInterval(refresh,2000);
