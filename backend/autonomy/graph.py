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
from .planning import bill_strategy, coordinate, REVIEW_PROMPT, REVIEW_SCHEMA, explanation


class GoalState(TypedDict):
    architecture_version: int
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
    bill_strategy: dict
    budget_strategy: dict
    coordination: dict
    valid_action: bool
    review: str
    replans: int
    needs_replan: bool


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
    return GoalState(architecture_version=2, cycle_id=uuid.uuid4().hex, force=force, snapshot={}, active=[],
                     fingerprint="", observed_revision="", results=[], used=[], available=[],
                     action={}, steps=0, stop=False, due=False, bill_strategy={}, budget_strategy={},
                     coordination={}, valid_action=False, review="continue", replans=0, needs_replan=False)


class GoalGraph:
    def __init__(self, controller):
        self.controller = controller
        self.preobserved = None
        self.path = str(Path(controller.memory.path).with_suffix(".checkpoints.sqlite"))
        self.thread_id = "goals:" + digest(dict(customer=controller.customer,
                          recipient=controller.recipient, source=controller.source))[:24]
        self.config = dict(configurable=dict(thread_id=self.thread_id), recursion_limit=120)

    def build(self, checkpointer, **compile_options):
        graph = StateGraph(GoalState)
        for name in ("observe", "assess_due", "plan_bills", "plan_strategy", "coordinate_strategy", "choose_action",
                     "validate_action", "execute_action", "evaluate_result", "review_progress", "report"):
            def guarded(state, method=getattr(self, name)):
                self.controller.memory.renew_lease()
                result = method(state)
                self.controller.memory.renew_lease()
                return result
            graph.add_node(name, guarded)
        graph.add_edge(START, "observe")
        graph.add_edge("observe", "assess_due")
        graph.add_conditional_edges("assess_due", lambda s: "plan_bills" if s["due"] else END,
                                    {"plan_bills":"plan_bills", END:END})
        graph.add_edge("plan_bills", "plan_strategy")
        graph.add_edge("plan_strategy", "coordinate_strategy")
        graph.add_edge("coordinate_strategy", "choose_action")
        graph.add_conditional_edges("choose_action", lambda s: "report" if s["stop"] else "validate_action",
                                    {"report":"report", "validate_action":"validate_action"})
        graph.add_conditional_edges("validate_action", lambda s: "execute_action" if s["valid_action"] else "evaluate_result",
                                    {"execute_action":"execute_action", "evaluate_result":"evaluate_result"})
        graph.add_edge("execute_action", "evaluate_result")
        graph.add_conditional_edges("evaluate_result", lambda s: "report" if s["stop"] else "review_progress",
                                    {"report":"report", "review_progress":"review_progress"})
        graph.add_conditional_edges("review_progress", lambda s: {"replan":"plan_bills", "continue":"choose_action",
                                                                 "report":"report"}[s["review"]],
                                    {"plan_bills":"plan_bills", "choose_action":"choose_action", "report":"report"})
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
                unchanged = (checkpoint.values.get("architecture_version") == 2
                             and revision(fresh, active) == checkpoint.values.get("observed_revision"))
                failed = any(task.error for task in checkpoint.tasks)
                if unchanged and failed and not force and time.time() < memory["next_check"]:
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

    def plan_bills(self, state):
        c = self.controller
        options = c.payments.bill_options() if c.payments is not None else {"bills": []}
        proposal = bill_strategy(c, state["snapshot"], options, c.memory.read().get("bill_strategy"))
        with c.memory.transaction() as memory:
            memory["bill_proposal"] = proposal
        c.memory.event("bill_strategy", decision=proposal)
        return dict(bill_strategy=proposal, needs_replan=False)

    def plan_strategy(self, state):
        """Budget-domain planning. Kept named for existing checkpoint compatibility."""
        c = self.controller
        snapshot = state["snapshot"]
        budget = snapshot.get("budget_actions", {})
        strategy = {}
        if c.actions is not None and budget.get("adaptive") and budget["active"]:
            strategy = c.actions.replan(c.llm, bill_strategy=state["bill_strategy"])
            c.memory.event("financial_strategy", decision=strategy)
            snapshot = c.observe()
            c._tool("review_spending", "", {}, snapshot, [])
        elif budget.get("active"):
            strategy = dict(mode="confirmed_fixed_shares", goals=budget["goals"], caps=budget.get("caps", []))
        elif any(g["kind"] == "budget" for g in state["active"]):
            strategy = {g["id"]: c._tool("budget", g["id"], {}, snapshot, [])
                        for g in state["active"] if g["kind"] == "budget"}
        return dict(snapshot=snapshot, budget_strategy=strategy,
                    observed_revision=revision(snapshot, state["active"]))

    def coordinate_strategy(self, state):
        c = self.controller
        combined = coordinate(c, state["snapshot"], state["bill_strategy"], state["budget_strategy"])
        with c.memory.transaction() as memory:
            memory["coordination"] = combined
            memory["bill_strategy"] = state["bill_strategy"]
        c.memory.event("coordinated_strategy", decision=combined)
        if combined["blockers"]:
            c.memory.queue("coordination:review:" + digest(combined),
                           "I need to review a bill shortfall, missing details, or an uncertain payment. "
                           "I am holding new savings allocations while this is unresolved. "
                           "I will keep your existing goals and payment approvals unchanged.")
        return dict(coordination=combined)

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
        if pending and state["coordination"]["savings_allowed"]:
            available.append("allocate_savings")
        if not state["coordination"]["savings_allowed"]:
            pending = []
        memory = c.memory.read()
        context = dict(goals=state["active"], facts=snapshot, reports=memory["reports"],
                       bill_strategy=state["bill_strategy"], budget_strategy=state["budget_strategy"],
                       coordination=state["coordination"],
                       replannable_invoice_ids=replannable, available_tools=available,
                       pending_allocation_ids=pending, recent_decisions=memory["events"][-8:], results=state["results"])
        schema = {**ACTION_SCHEMA, "properties": {**ACTION_SCHEMA["properties"],
                  "tool": {"type":"string", "enum":available}}}
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

    def validate_action(self, state):
        """Re-observe before executing a model choice, even after checkpoint resume."""
        c = self.controller
        snapshot = c.observe()
        active = [g for g in c.memory.read()["goals"].values() if g["status"] == "active"]
        stale = revision(snapshot, active) != state["observed_revision"]
        action = state["action"]
        valid = not stale and action["tool"] in state["available"]
        if action["tool"] in {"inspect_bill", "resolve_bill", "report_bill"}:
            valid = valid and any(b["id"] == action["id"] for b in snapshot["invoices"])
        if action["tool"] == "allocate_savings":
            valid = valid and state["coordination"]["savings_allowed"] and any(
                g["id"] == action["id"] and g["eligible"] for g in snapshot.get("budget_actions", {}).get("goals", []))
        update = dict(snapshot=snapshot, active=active, valid_action=valid,
                      observed_revision=revision(snapshot, active), needs_replan=stale)
        if not valid:
            result = dict(error="StaleFacts" if stale else "ActionNotAllowed", action_required="Reassess before acting.")
            c.memory.event("action_rejected", tool=action["tool"], id=action["id"], result=result)
            update.update(results=state["results"] + [dict(tool=action["tool"], id=action["id"], result=result)],
                          used=state["used"] + [[action["tool"], action["id"]]], steps=state["steps"] + 1)
        return update

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

    def review_progress(self, state):
        """Assess committed outcomes, rather than merely counting tool calls."""
        c = self.controller
        last = state["results"][-1]
        route = "continue"
        reason = "Observation recorded; choose the next useful action."
        if state["needs_replan"]:
            route, reason = "replan", "Financial facts or permissions changed before execution."
        elif last["tool"] in {"allocate_savings", "resolve_bill"} or last["result"].get("error"):
            context = dict(goals=state["active"], facts=state["snapshot"],
                           bill_strategy=state["bill_strategy"], budget_strategy=state["budget_strategy"],
                           last_action=last, results=state["results"], remaining_steps=c.max_steps-state["steps"])
            decision = c.llm.json_completion(REVIEW_PROMPT, json.dumps(context), "steady_review_progress", REVIEW_SCHEMA)
            route, reason = decision.get("next"), explanation(decision.get("reason"))
            if route not in {"continue", "replan", "report"}:
                raise ValueError("Invalid outcome review")
        if route == "replan" and state["replans"] >= 2:
            route, reason = "report", "Replanning limit reached; preserve state for the next check."
        c.memory.event("progress_review", next=route, reason=reason)
        return dict(review=route, replans=state["replans"] + (route == "replan"))

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
