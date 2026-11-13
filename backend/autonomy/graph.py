"""Checkpointed goal orchestration; financial authority stays in existing services.

One thread per household. A failed run resumes its pending node only while its
financial observation is still current. Changed facts supersede the pending run;
the ledger, not a checkpoint, is the source of truth for completed transfers.
"""
import json
import time
import uuid
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from ..nessie.client import NessieError
from ..payments.client import BillerError
from .controller import ACTION_SCHEMA, SYSTEM, TOOLS, digest


class GoalState(TypedDict):
    cycle_id: str
    force: bool
    snapshot: dict
    active: list[dict]
    fingerprint: str
    observed_revision: str
    results: list[dict]
    used: list[list[str]]
    available: list[str]
    action: dict
    steps: int
    stop: bool
    due: bool


def revision(snapshot, active):
    facts = dict(snapshot)
    if snapshot.get("budget_actions", {}).get("adaptive"):
        budget = snapshot["budget_actions"]
        facts["budget_actions"] = dict(
            plan_id=budget["plan_id"], active=budget["active"],
            goals=[{k:g[k] for k in ("id", "target_cents", "saved_cents")} for g in budget["goals"]],
            caps=[{k:c[k] for k in ("category", "original_limit_cents", "spent_cents")} for c in budget["caps"]])
    return digest(dict(snapshot=facts, goals=active))


def initial(force=False):
    return GoalState(cycle_id=uuid.uuid4().hex, force=force, snapshot={}, active=[],
                     fingerprint="", observed_revision="", results=[], used=[], available=[],
                     action={}, steps=0, stop=False, due=False)


class GoalGraph:
    def __init__(self, controller):
        self.controller = controller
        self.preobserved = None
        self.path = str(Path(controller.memory.path).with_suffix(".checkpoints.sqlite"))
        self.thread_id = "goals:" + digest(dict(customer=controller.customer,
                          recipient=controller.recipient, source=controller.source))[:24]
        self.config = dict(configurable=dict(thread_id=self.thread_id), recursion_limit=40)

    def build(self, checkpointer, **compile_options):
        graph = StateGraph(GoalState)
        for name in ("observe", "assess_due", "plan_strategy", "choose_action",
                     "execute_action", "evaluate_result", "report"):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "observe")
        graph.add_edge("observe", "assess_due")
        graph.add_conditional_edges("assess_due", lambda s: "plan_strategy" if s["due"] else END)
        graph.add_edge("plan_strategy", "choose_action")
        graph.add_conditional_edges("choose_action", lambda s: "report" if s["stop"] else "execute_action")
        graph.add_edge("execute_action", "evaluate_result")
        graph.add_conditional_edges("evaluate_result", lambda s: "report" if s["stop"] else "choose_action")
        graph.add_edge("report", END)
        return graph.compile(checkpointer=checkpointer, **compile_options)

    def run(self, force=False):
        c = self.controller
        with SqliteSaver.from_conn_string(self.path) as saver:
            graph = self.build(saver)
            checkpoint = graph.get_state(self.config)
            if checkpoint.next:
                # No stale allocation or bill decision may survive a changed
                # policy, simulation reset, account, date, approval or balance.
                fresh = c.observe()
                self.preobserved = fresh
                memory = c.memory.read()
                active = [g for g in memory["goals"].values() if g["status"] == "active"]
                unchanged = revision(fresh, active) == checkpoint.values.get("observed_revision")
                if unchanged and not force and time.time() < memory["next_check"]:
                    return
                if unchanged:
                    c.memory.event("graph_resumed", next_nodes=list(checkpoint.next))
                    graph.invoke(None, self.config, durability="sync")
                    return
                c.memory.event("graph_superseded", reason="Financial facts or goal authority changed.")
                graph.update_state(self.config, {"stop":True}, as_node="report")
            graph.invoke(initial(force), self.config, durability="sync")

    def observe(self, state):
        snapshot = self.preobserved if self.preobserved is not None else self.controller.observe()
        self.preobserved = None
        active = [g for g in self.controller.memory.read()["goals"].values() if g["status"] == "active"]
        return dict(snapshot=snapshot, active=active, observed_revision=revision(snapshot, active))

    def assess_due(self, state):
        c = self.controller
        memory = c.memory.read()
        fingerprint = state["observed_revision"]
        due = bool(state["active"]) and (state["force"] or fingerprint != memory["fingerprint"]
                                        or time.time() >= memory["next_check"])
        if not due:
            return dict(due=False)
        for goal in state["active"]:
            if goal["kind"] == "budget" and goal["deadline"] < state["snapshot"]["today"]:
                c.memory.queue("deadline:" + goal["id"], "Your savings deadline has passed. "
                               "I will not change your goal automatically. Send a revised goal if you want a new plan.")
                with c.memory.transaction() as data:
                    data["goals"][goal["id"]]["status"] = "needs_review"
        active = [g for g in c.memory.read()["goals"].values() if g["status"] == "active"]
        if active:
            with c.memory.transaction() as data:
                data["fingerprint"] = fingerprint
                data["next_check"] = time.time() + 600
        return dict(due=bool(active), active=active, fingerprint=fingerprint,
                    observed_revision=revision(state["snapshot"], active))

    def plan_strategy(self, state):
        c = self.controller
        snapshot = state["snapshot"]
        budget = snapshot.get("budget_actions", {})
        if c.actions is not None and budget.get("adaptive") and budget["active"]:
            strategy = c.actions.replan(c.llm)
            c.memory.event("financial_strategy", decision=strategy)
            snapshot = c.observe()
            c._tool("review_spending", "", {}, snapshot, [])
        return dict(snapshot=snapshot, observed_revision=revision(snapshot, state["active"]))

    def choose_action(self, state):
        c = self.controller
        snapshot, used = state["snapshot"], state["used"]
        replannable = [j["invoice_id"] for j in snapshot["jobs"]
                       if j["status"] == "held" and j.get("hold_reason") == "funds"]
        available = ["refresh", "inspect_bill", "report_bill", "wait"]
        if any(g["kind"] == "budget" for g in state["active"]):
            available += ["budget", "report_budget"]
        if replannable:
            available.append("resolve_bill")
        budget = snapshot.get("budget_actions", {})
        pending = [g["id"] for g in budget.get("goals", []) if budget.get("active")
                   and g["eligible"] and not g["complete"] and ["allocate_savings", g["id"]] not in used]
        if budget.get("active"):
            available.append("review_spending")
        if pending:
            available = ["allocate_savings"] + (["review_spending"] if ["review_spending", ""] not in used else [])
        memory = c.memory.read()
        context = dict(goals=state["active"], facts=snapshot, reports=memory["reports"],
                       replannable_invoice_ids=replannable, available_tools=available,
                       pending_allocation_ids=pending, recent_decisions=memory["events"][-8:], results=state["results"])
        schema = {**ACTION_SCHEMA, "properties": {**ACTION_SCHEMA["properties"],
                  "tool": {"type":"string", "enum":available}}}
        if pending:
            schema["properties"]["id"] = {"type":"string", "enum":[""] + pending}
        action = c.llm.json_completion(SYSTEM, json.dumps(context), "steady_next_action", schema)
        tool, key = action.get("tool"), action.get("id", "")
        if tool not in TOOLS or not isinstance(key, str):
            raise ValueError("Invalid tool")
        if tool == "allocate_savings":
            key = key.removeprefix("allocation:").casefold()
        if [tool, key] in used:
            c.memory.event("no_progress", tool=tool, id=key)
            return dict(stop=True)
        return dict(action={**action, "id":key}, available=available)

    def execute_action(self, state):
        c = self.controller
        action, snapshot = state["action"], state["snapshot"]
        tool, key = action["tool"], action["id"]
        try:
            if tool not in state["available"]:
                raise ValueError("Tool is unavailable for the current goals")
            result = c._tool(tool, key, action, snapshot, state["results"])
            if tool == "refresh":
                snapshot = result
            elif tool in {"resolve_bill", "allocate_savings"}:
                snapshot = c.observe()
        except (ValueError, KeyError, BillerError, NessieError) as exc:
            result = dict(error=type(exc).__name__, action_required="Inspect facts or wait; no authority changed.")
        c.memory.event("tool", tool=tool, id=key, reason=str(action.get("reason", ""))[:180], result=result)
        active = [g for g in c.memory.read()["goals"].values() if g["status"] == "active"]
        return dict(snapshot=snapshot, results=state["results"] + [dict(tool=tool, id=key, result=result)],
                    used=state["used"] + [[tool, key]], steps=state["steps"] + 1,
                    active=active, observed_revision=revision(snapshot, active),
                    stop=tool == "wait" and tool in state["available"])

    def evaluate_result(self, state):
        if state["stop"]:
            return {}
        if state["steps"] >= self.controller.max_steps:
            self.controller.memory.event("step_limit")
            return dict(stop=True)
        return {}

    def report(self, state):
        c = self.controller
        for goal in state["active"]:
            if goal["kind"] == "budget":
                c._tool("report_budget", goal["id"], {}, state["snapshot"], state["results"])
        if c.actions is not None and state["snapshot"].get("budget_actions", {}).get("active"):
            c._tool("review_spending", "", {}, state["snapshot"], state["results"])
        with c.memory.transaction() as data:
            data["fingerprint"] = state["fingerprint"]
            if data["next_check"] <= time.time():
                data["next_check"] = time.time() + 600
        return dict(stop=True)
