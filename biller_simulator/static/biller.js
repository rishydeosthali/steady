"use strict";
let token = sessionStorage.getItem("biller-demo-token") || "";
const $ = id => document.getElementById(id);
const money = n => new Intl.NumberFormat("en-US", {style:"currency",currency:"USD"}).format(n / 100);
function node(tag,text) {const el=document.createElement(tag); el.textContent=text; return el;}
async function get(path) {
  const response=await fetch(path,{headers:{Authorization:"Bearer "+token}});
  if(!response.ok) throw new Error("Cannot read the biller. Check its API token and connection.");
  return response.json();
}
async function refresh() {
  if(!token) return;
  try {
    const [invoices,payments]=await Promise.all([get("/v1/invoices?customer_ref=demo-margaret"),get("/v1/payments")]);
    $("data").hidden=false; $("connect").hidden=true; $("error").hidden=true;
    sessionStorage.setItem("biller-demo-token",token);
    $("paid").textContent=invoices.invoices.filter(b=>b.status==="paid").length;
    $("total").textContent=money(payments.payments.reduce((n,p)=>n+p.amount_cents,0));
    $("invoices").replaceChildren();
    for(const invoice of invoices.invoices) {
      const tr=node("tr",""),name=node("td",invoice.payee); name.append(node("small",invoice.id));
      const status=node("td",invoice.status); status.className=invoice.status;
      tr.append(name,node("td",money(invoice.amount_cents)),node("td",invoice.due_date),status); $("invoices").append(tr);
    }
    $("receipts").replaceChildren();
    if(!payments.payments.length) $("receipts").append(node("p","No payments received. A proposal or an approval inside Steady alone does not mark an invoice paid here."));
    for(const payment of payments.payments) {
      const card=node("article",""); card.className="receipt";
      card.append(node("h3",payment.payee+" · "+money(payment.amount_cents)),node("p",payment.id),node("small","Accepted at "+payment.accepted_at+" · simulated date "+payment.simulation_date),node("small","Idempotency key: "+payment.idempotency_key));
      $("receipts").append(card);
    }
  } catch(error) {$("error").textContent=error.message;$("error").hidden=false;}
}
$("connect").addEventListener("submit",event=>{event.preventDefault();token=$("token").value.trim();$("token").value="";refresh();});
refresh();setInterval(refresh,2000);
