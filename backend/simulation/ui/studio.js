"use strict";
const $ = (id) => document.getElementById(id);
let state = null, busy = false, uncertain = null;
const money = (cents) => new Intl.NumberFormat("en-US", {style:"currency",currency:"USD"}).format(cents / 100);
const date = (iso) => iso ? new Date(iso.slice(0,10) + "T12:00:00").toLocaleDateString("en-US", {month:"short",day:"numeric",year:"numeric"}) : "—";
function el(tag, text, cls) { const n = document.createElement(tag); if(text != null) n.textContent=text; if(cls)n.className=cls; return n; }
function error(text) { $("error").textContent=text; $("error").hidden=!text; }
function notify(text) { $("success").textContent=text; $("success").hidden=!text; }
function disabled(value) {
  document.body.classList.toggle("busy", value);
  document.querySelectorAll("#workspace button, #workspace input, #workspace select, dialog button").forEach(n=>n.disabled=value);
}
async function api(operation, body, key) {
  let response;
  try {
    response=await fetch("/simulation/api/"+operation, {method:body?"POST":"GET",cache:"no-store",
      headers:body?{"Content-Type":"application/json","Idempotency-Key":key}:{},
      ...(body?{body:JSON.stringify(body)}:{})});
  } catch (_) { const e=new Error("Connection lost. Retry the same request to confirm its result without applying it twice."); e.network=true; throw e; }
  let result;
  try { result=await response.json(); }
  catch (_) { const e=new Error("The server did not return a result. Retry to confirm what happened."); e.network=true; throw e; }
  if(!response.ok) {
    const e=new Error(result.error||"This change could not be completed.");
    e.status=response.status;
    if(response.status>=500)e.network=true;
    throw e;
  }
  return result;
}
async function refresh() {
  if(busy || uncertain)return;
  try { render(await api("state")); $("loading").hidden=true; $("workspace").hidden=false; error(""); }
  catch(e) { error(e.message); }
}
async function act(operation, data={}, retry=false) {
  if(!state || busy || (!retry && uncertain))return;
  busy=true;disabled(true);error("");notify("");
  const command=retry?uncertain:{operation,body:{...data,run_id:state.run_id},key:crypto.randomUUID()};
  try {
    const result=await api(command.operation,command.body,command.key);
    uncertain=null;$("retry").hidden=true;render(result);
    notify(command.operation==="save"?"Scenario saved. Load it whenever you need a fresh run.":"Simulation updated. Steady has checked the new account state.");
  } catch(e) {
    if(e.network){uncertain=command;$("retry").hidden=false;}
    else {uncertain=null;$("retry").hidden=true;}
    error(e.message);
    if(e.status===409){try{render(await api("state"));}catch(_) {}}
  } finally {busy=false;disabled(Boolean(uncertain));}
}
function render(next) {
  const previous=state;state=next;
  $("today").textContent=date(state.today);
  $("balance").textContent=money(state.balance_cents);
  $("buffer").textContent=money(state.buffer_cents);
  const unpaid=state.invoices.filter(b=>b.status==="unpaid");
  $("upcoming").textContent=money(unpaid.reduce((n,b)=>n+b.amount_cents,0));
  $("bills-count").textContent=unpaid.length+" unpaid bill"+(unpaid.length===1?"":"s");
  $("session-label").textContent=state.session_open?"Ready to receive":"Waiting for your HI";
  $("session-note").textContent=state.channel==="slack"?"Send a direct message to Steady in Slack":state.session_open?"Reply on your phone":"Text HI on WhatsApp to open the session";
  $("conversation-note").textContent=state.channel==="slack"?"Slack direct messages":state.session_open?"WhatsApp session is open":"Messages wait until you text HI";
  $("channel-label").textContent=state.channel==="slack"?"SLACK":"WHATSAPP";
  $("worker-status").textContent=state.last_error?"Last check needs attention: "+state.last_error:
    state.last_check?"Last checked "+new Date(state.last_check).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})+" · Background checks every 10 seconds":"Ready · background checks run on the hosted server";
  if(!previous || previous.run_id!==state.run_id || $("expense-date").value===previous.today)$("expense-date").value=state.today;
  if(!$("income-date").value || $("income-date").value===previous?.today)$("income-date").value=state.today;
  $("expense-date").max=state.today;
  if(!$("bill-date").value || previous?.run_id!==state.run_id)$("bill-date").value=unpaid[0]?.due_date||state.today;
  $("bill-date").min=state.today;
  const depositId=$("deposit").value, savedId=$("saved").value;
  $("deposit").replaceChildren();
  for(const d of state.deposits.filter(d=>d.status==="pending")){
    const n=el("option", d.description+" · "+money(Math.round(Number(d.amount)*100))+" · "+date(d.transaction_date));n.value=d._id;$("deposit").append(n);
  }
  if([...$("deposit").options].some(n=>n.value===depositId))$("deposit").value=depositId;
  if(!$("deposit").options.length){const n=el("option","No pending deposits");n.value="";$("deposit").append(n);}
  $("saved").replaceChildren(el("option","Saved scenarios"));$("saved").firstChild.value="";
  for(const s of state.saved){const n=el("option",s.name);n.value=s.id;$("saved").append(n);}
  $("saved").value=savedId;
  renderPresets();renderMessages();renderAccount();renderActivity();renderBudget();
}
function renderPresets(){
  $("presets").replaceChildren();
  for(const p of state.presets){
    const row=el("div",null,"preset"),text=el("div");text.append(el("b",p.name),el("p",p.description));
    const button=el("button","Run");button.type="button";button.setAttribute("aria-label","Run "+p.name);button.disabled=busy||Boolean(uncertain);button.onclick=()=>preset(p.id);row.append(text,button);$("presets").append(row);
  }
}
function preset(id){
  if(id==="payday")return act("income",{description:"Demo payday",amount:"2500.00",date:state.today});
  if(id==="vacation")return act("expense",{merchant:"Vacation booking",amount:"350.00",date:state.today,category:"vacation"});
  if(id==="busy_week")return act("expense-batch",{expenses:[
    {merchant:"Groceries",amount:"80",category:"groceries"},{merchant:"Bus pass",amount:"30",category:"transport"},
    {merchant:"Restaurant",amount:"55",category:"dining"},{merchant:"Vacation deposit",amount:"120",category:"vacation"}]});
  if(id==="shortfall")return act("expense",{merchant:"Dentist",amount:"1900.00",date:state.today});
  if(id==="duplicate")return act("expense",{merchant:"Dentist",amount:"120.00",date:state.today});
  if(id==="subscription")return act("expense",{merchant:"StreamBox",amount:"14.99",date:state.today});
  if(id==="bill_increase")return act("bill",{payee:"Austin Energy",amount:"180.00",due_date:state.invoices.find(b=>b.payee==="Austin Energy"&&b.status==="unpaid")?.due_date||state.today});
  if(id==="delay_income"){
    const d=state.deposits.find(d=>d.status==="pending");
    if(!d){error("There is no pending deposit to delay.");return;}
    return act("delay-income",{deposit_id:d._id,days:7});
  }
}
function renderMessages(){
  const list=$("messages"), nearBottom=list.scrollHeight-list.scrollTop-list.clientHeight<60;
  const delivered=state.channel==="slack"?"Sent to Slack":"Sent to WhatsApp";
  const all=[...state.messages.map(m=>({...m,status:m.status==="sent"?delivered:m.status==="failed"?"Send failed":m.status==="superseded"?"Replaced by a newer plan":"Waiting to send"})),
    ...state.alerts.filter(a=>a.text&&!a.resolved).map(a=>({text:a.text,status:a.delivered?delivered:"Waiting to send"}))];
  const seen=new Set();list.replaceChildren();
  for(const m of all){if(seen.has(m.text)||m.status==="Replaced by a newer plan")continue;seen.add(m.text);
    const card=el("article",null,"message"+(m.status==="Waiting to send"?" waiting":""));card.append(el("small",m.status),el("p",m.text));list.append(card);
  }
  if(!list.childNodes.length){const empty=el("div",null,"empty");empty.append(el("strong","No messages yet."),el("p","Add an expense or run a preset to see Steady’s response. Text HI on WhatsApp to receive messages on your phone."));list.append(empty);}
  if(nearBottom)list.scrollTop=list.scrollHeight;
}
function renderAccount(){
  $("bills").replaceChildren();
  for(const b of [...state.invoices].sort((a,b)=>a.due_date.localeCompare(b.due_date))){
    const job=[...state.jobs].reverse().find(j=>j.invoice_id===b.id),status=b.status==="paid"?"paid":job?.status||"unpaid";
    const tr=el("tr");for(const text of [b.payee,money(b.amount_cents),date(b.due_date),job?.scheduled_for?date(job.scheduled_for):"—"])tr.append(el("td",text));
    const td=el("td");td.append(el("span",status.replaceAll("_"," "),"status-tag "+status));tr.append(td);$("bills").append(tr);
  }
  records("deposits",[...state.deposits].reverse(),d=>[d.description,money(Math.round(Number(d.amount)*100)),date(d.transaction_date),d.status]);
  records("purchases",[...state.purchases].reverse(),p=>[p.description,money(Math.round(Number(p.amount)*100)),date(p.purchase_date),"Posted"]);
  records("receipts",state.receipts,r=>[r.payee,money(r.amount_cents),r.id,date(r.simulation_date)]);
}
function records(id,rows,format){
  $(id).replaceChildren();
  if(!rows.length){$(id).append(el("p","Nothing recorded yet.","subtle"));return;}
  for(const r of rows){const [title,value,detail,status]=format(r),row=el("div",null,"record"),a=el("div"),b=el("div");a.append(el("span",title),el("small",detail));b.append(el("span",value),el("small",status));row.append(a,b);$(id).append(row);}
}
function renderActivity(){
  $("timeline").replaceChildren();
  const rows=[...state.events.map(e=>({date:e.date,text:e.text})),...state.audit.map(e=>({date:e.at,text:e.reason||e.detail?.text||e.kind}))];
  for(const r of rows.reverse()){const li=el("li");li.append(el("small",r.date),el("p",r.text));$("timeline").append(li);}
  if(!rows.length)$("timeline").append(el("li","Changes and decisions will appear here."));
}
function renderBudget(){
  const budget=state.budget||{goals:[],caps:[],allocations:[]};
  $("budget-status").textContent=budget.plan_id?"Plan "+budget.plan_id+" · "+(budget.active?"Active":"Paused"):"No allocation plan yet. Confirm one with Steady to authorize fictional transfers.";
    if(budget.adaptive)$("budget-status").textContent+=" · Adaptive: "+(budget.strategy.reason||"Waiting for financial assessment")+(budget.strategy.defer_savings?" Savings temporarily deferred.":"");
  $("savings-goals").replaceChildren();$("spending-caps").replaceChildren();$("savings-transfers").replaceChildren();
  for(const goal of budget.goals){const row=el("article",null,"record");
    row.append(el("strong",goal.id),el("p",money(goal.saved_cents)+" saved in simulation / "+money(goal.target_cents)+" target"),
      el("p",(goal.basis_points/100)+"% of new received income · "+money(goal.remaining_cents)+" remaining"));
    const progress=document.createElement("progress");progress.max=goal.target_cents;progress.value=Math.min(goal.saved_cents,goal.target_cents);progress.setAttribute("aria-label",goal.id+" savings progress");row.append(progress);$("savings-goals").append(row);}
  for(const cap of budget.caps){const row=el("article",null,"record");row.append(el("strong",cap.category),
    el("p",money(cap.spent_cents)+" spent / "+money(cap.limit_cents)+" monthly cap"),
    el("p",cap.over_cents?money(cap.over_cents)+" over cap. Pause new spending here.":money(cap.remaining_cents)+" available this month."));$("spending-caps").append(row);}
  for(const transfer of [...budget.allocations].reverse()){const row=el("article",null,"record");row.append(el("strong",money(transfer.amount_cents)+" → "+transfer.goal_id),el("p",date(transfer.date)+" · simulated checking transfer"));$("savings-transfers").append(row);}
  if(!budget.goals.length)$("savings-goals").append(el("p","Your confirmed named goals will appear here."));
  if(!budget.allocations.length)$("savings-transfers").append(el("p","No simulated transfers recorded yet."));
}
document.querySelectorAll("[data-tab]").forEach(b=>b.onclick=()=>{document.querySelectorAll("[data-tab]").forEach(n=>n.classList.toggle("active",n===b));document.querySelectorAll("[data-view]").forEach(n=>n.hidden=n.dataset.view!==b.dataset.tab);});
document.querySelectorAll("[data-form]").forEach(b=>b.onclick=()=>{document.querySelectorAll("[data-form]").forEach(n=>n.classList.toggle("selected",n===b));document.querySelectorAll(".change-form").forEach(n=>n.hidden=n.id!==b.dataset.form+"-form");});
$("expense-form").onsubmit=e=>{e.preventDefault();act("expense",Object.fromEntries(new FormData(e.target)));};
$("bill-form").onsubmit=e=>{e.preventDefault();act("bill",Object.fromEntries(new FormData(e.target)));};
$("new-income-form").onsubmit=e=>{e.preventDefault();act("income",Object.fromEntries(new FormData(e.target)));};
$("income-form").onsubmit=e=>{e.preventDefault();act("delay-income",{deposit_id:$("deposit").value,days:Number($("delay-days").value)});};
$("settle").onclick=()=>act("settle-income",{deposit_id:$("deposit").value});
$("advance").onclick=()=>act("advance");$("check").onclick=()=>act("check");$("replan").onclick=()=>act("replan");
$("retry").onclick=()=>act(null,{},true);
$("reset-open").onclick=()=>$("reset-dialog").showModal();
$("reset-confirm").onclick=()=>{const template=$("starting-account").value;$("reset-dialog").close();act("reset",{template});};
$("save-open").onclick=()=>$("save-dialog").showModal();$("save-cancel").onclick=()=>$("save-dialog").close();
$("save-form").onsubmit=e=>{e.preventDefault();const name=$("scenario-name").value;$("save-dialog").close();act("save",{name});};
$("load").onclick=()=>{if(!$("saved").value){error("Choose a saved scenario first.");return;}if(confirm("Load this scenario as a new run? Current approvals and pending messages will be cleared."))act("load",{saved_id:$("saved").value});};
refresh();
setInterval(refresh,5000);
