"""Specialist proposals, coordinated execution, stale evidence and recovery."""

import json

import pytest

from backend.autonomy.graph import GoalGraph
from tests.test_autonomy import action
from tests.test_budget_actions import system, confirm, change
from tests.test_goal_graph import checkpoint, pause_at


def specialize(model, *, bills=None, coordination=None, reviews=None):
    """Script domain decisions independently from tool choices."""
    original = model.json_completion
    calls = []
    reviews = list(reviews or [])
    def completion(system, user, schema_name, schema):
        context = json.loads(user)
        calls.append((schema_name, context))
        if schema_name == "steady_bill_strategy" and bills:
            return bills(context)
        if schema_name == "steady_coordinate_strategy" and coordination:
            return coordination(context)
        if schema_name == "steady_review_progress" and reviews:
            return dict(next=reviews.pop(0), reason="Reassess the observed change in goal progress.")
        return original(system, user, schema_name, schema)
    model.json_completion = completion
    return calls


def later_dates(context):
    bills = context["options"]["bills"]
    return dict(priority_order=[b["invoice_id"] for b in reversed(bills)],
                schedule=[dict(invoice_id=b["invoice_id"], date=b["candidate_dates"][-1])
                          for b in bills if b["candidate_dates"]], needs_user_decision=False,
                reason="Keep liquidity until the listed deadlines while reserving every obligation.")


def test_specialist_dates_and_order_drive_real_proposals_without_granting_consent(system):
    runtime, controller, model = system
    # Make every invoice eligible for outreach so the model's order matters.
    with runtime.store.tx() as state:
        for bill in state["invoices"]:
            bill["due_date"] = "2026-11-14"
    model.actions = [action("wait")]
    calls = specialize(model, bills=later_dates)
    controller.run(force=True)
    strategy = controller.memory.read()["bill_strategy"]
    assert strategy["priority_order"] == [b["id"] for b in reversed(runtime.store.state()["invoices"])]
    runtime.payments.tick()
    proposed = list(runtime.payments.state()["jobs"].values())[0]
    assert proposed["invoice_id"] == strategy["priority_order"][0]
    assert proposed["scheduled_for"] == "2026-11-14"  # code's old default was today
    assert not proposed["user_approved"] and not proposed["caregiver_approved"]
    assert runtime.store.state()["receipts"] == {}
    context = next(ctx for name, ctx in calls if name == "steady_next_action")
    assert context["bill_strategy"]["schedule"] == strategy["schedule"]


def test_bill_dates_wait_for_future_income_and_never_treat_it_as_cleared_cash(system):
    runtime, controller, model = system
    with runtime.store.tx() as state:
        state["balance_cents"] = 30000
    options = runtime.payments.bill_options()
    electric = next(b for b in options["bills"] if b["payee"] == "Austin Energy")
    assert "2026-11-12" not in electric["candidate_dates"]
    assert electric["candidate_dates"] and all(day >= "2026-11-14" for day in electric["candidate_dates"])
    model.actions = [action("wait")]
    specialize(model, bills=later_dates)
    controller.run(force=True)
    runtime.payments.tick()
    proposal = list(runtime.payments.state()["jobs"].values())[0]
    assert proposal["scheduled_for"] > "2026-11-12"
    assert not runtime.store.state()["receipts"]


@pytest.mark.parametrize("corrupt", [
    lambda p: p["priority_order"].append(p["priority_order"][0]),
    lambda p: p["priority_order"].__setitem__(0, "invented-bill"),
    lambda p: p["schedule"][0].__setitem__("date", "2099-01-01"),
    lambda p: p["schedule"].clear(),
    lambda p: p.__setitem__("needs_user_decision", "false"),
])
def test_invalid_bill_proposals_never_become_executable_strategy(system, corrupt):
    runtime, controller, model = system
    def invalid(context):
        proposal = later_dates(context)
        corrupt(proposal)
        return proposal
    specialize(model, bills=invalid)
    model.actions = [action("wait")]
    controller.run(force=True)
    assert "bill_strategy" not in controller.memory.read()
    assert not runtime.store.state()["receipts"]
    assert not model.contexts  # execution selection is never reached


def test_individually_safe_dates_are_rejected_if_the_combined_schedule_is_unsafe(system):
    runtime, controller, model = system
    with runtime.store.tx() as state:
        state["balance_cents"] = 40000
    # Each bill alone fits before income, but all three together breach the cushion.
    options = runtime.payments.bill_options()
    assert all("2026-11-12" in bill["candidate_dates"] for bill in options["bills"])
    def all_today(context):
        proposal = later_dates(context)
        for entry in proposal["schedule"]:
            entry["date"] = "2026-11-12"
        return proposal
    specialize(model, bills=all_today)
    model.actions = [action("wait")]
    controller.run(force=True)
    assert "bill_strategy" not in controller.memory.read()
    assert not runtime.payments.state()["jobs"]
    assert not runtime.store.state()["receipts"]


def test_lost_exclusive_lease_prevents_further_graph_execution(system):
    runtime, controller, model = system
    import sqlite3
    with controller.memory.lease() as acquired:
        assert acquired
        with sqlite3.connect(controller.memory.path) as db:
            db.execute("UPDATE lease SET owner='replacement' WHERE id=1")
        with pytest.raises(ValueError, match="lease lost"):
            controller.memory.renew_lease()


def test_approved_and_user_cancelled_bills_have_no_strategy_date_options(system):
    runtime, controller, model = system
    runtime.payments.tick()
    first = list(runtime.payments.state()["jobs"].values())[0]
    with runtime.payments._tx() as (_, state):
        state["jobs"][first["id"]]["status"] = "cancelled"
    options = runtime.payments.bill_options()
    entry = next(b for b in options["bills"] if b["invoice_id"] == first["invoice_id"])
    assert entry["candidate_dates"] == [] and entry["blocked_by"] == "existing_decision"
    model.actions = [action("wait")]
    specialize(model, bills=later_dates)
    controller.run(force=True)
    strategy = controller.memory.read()["bill_strategy"]
    assert all(b["invoice_id"] != first["invoice_id"] for b in strategy["schedule"])
    assert runtime.payments.state()["jobs"][first["id"]]["status"] == "cancelled"


def test_coordinator_cannot_allow_savings_despite_uncovered_bills(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    with runtime.store.tx() as state:
        state["balance_cents"] = 100
    specialize(model, coordination=lambda context: dict(
        focus="budget", defer_savings=False, reason="Try to advance the savings plan."))
    model.actions = [action("allocate_savings", "car"), action("wait")]
    controller.run(force=True)
    coordination = controller.memory.read()["coordination"]
    assert not coordination["savings_allowed"] and "bill_review_required" in coordination["blockers"]
    assert "allocate_savings" not in model.contexts[0]["available_tools"]
    assert runtime.store.state()["savings_pots"] == {"car": 0, "emergency": 0}
    assert any(e["kind"] == "action_rejected" for e in controller.memory.read()["events"])


def test_available_income_no_longer_forces_savings_ahead_of_bill_investigation(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Income")
    bill = runtime.store.state()["invoices"][0]
    specialize(model, coordination=lambda context: dict(
        focus="bills", defer_savings=True, reason="Inspect the invoice first, then revisit optional savings."))
    model.actions = [action("inspect_bill", bill["id"]), action("wait")]
    controller.run(force=True)
    assert model.contexts[0]["coordination"]["focus"] == "bills"
    assert model.contexts[1]["results"][0]["tool"] == "inspect_bill"
    assert not runtime.store.state().get("savings_allocations")


def test_outcome_review_can_reenter_specialist_planning_with_actual_progress(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Income")
    calls = specialize(model, reviews=["replan", "continue"])
    model.actions = [action("allocate_savings", "car"), action("allocate_savings", "emergency"), action("wait")]
    controller.run(force=True)
    assert sum(name == "steady_bill_strategy" for name, _ in calls) == 2
    reviewed = [ctx for name, ctx in calls if name == "steady_review_progress"]
    assert reviewed[0]["last_action"]["result"]["allocated_cents"] == 10000
    assert runtime.store.state()["savings_pots"] == {"car": 10000, "emergency": 5000}
    assert checkpoint(controller).values["replans"] == 1


def test_replanning_is_bounded_even_if_model_keeps_requesting_it(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    calls = specialize(model, reviews=["replan"] * 6)
    # Invalid arguments are rejected before any tool runs; each attempt consumes a step.
    model.actions = [action("inspect_bill", "invented-" + str(i)) for i in range(6)]
    controller.run(force=True)
    saved = checkpoint(controller)
    assert saved.values["replans"] == 2
    assert saved.values["steps"] <= controller.max_steps
    assert not runtime.store.state()["receipts"]


def test_changed_facts_between_selection_and_execution_trigger_replan(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Income")
    model.actions = [action("allocate_savings", "car")]
    saved = pause_at(controller, before=["validate_action"])
    assert saved.next == ("validate_action",)
    change(runtime, "expense", amount="3000", merchant="Unexpected expense", category="other")
    model.actions = [action("wait")]
    controller.run()
    assert runtime.store.state()["savings_pots"] == {"car": 0, "emergency": 0}
    assert any(e["kind"] == "graph_superseded" for e in controller.memory.read()["events"])


def test_old_graph_checkpoint_is_superseded_without_replaying_a_pending_action(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    model.actions = [action("wait")]
    pause_at(controller, before=["execute_action"])
    runner = GoalGraph(controller)
    from langgraph.checkpoint.sqlite import SqliteSaver
    with SqliteSaver.from_conn_string(runner.path) as saver:
        runner.build(saver).update_state(runner.config, {"architecture_version": 1})
    model.actions = [action("wait")]
    controller.run()
    assert any(e["kind"] == "graph_superseded" for e in controller.memory.read()["events"])
    assert checkpoint(controller).values["architecture_version"] == 2
