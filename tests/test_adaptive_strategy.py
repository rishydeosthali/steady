"""Multi-goal adaptation, measured outcomes and delegated authority boundaries."""
import copy
import json

import pytest

from backend.agent.llm import LLMError
from tests.test_budget_actions import system, change, TEXT, RAW
from tests.test_autonomy import action


def activate(runtime, controller, model):
    # These goals have no car-specific behavior or names.
    names = {"car":"education", "emergency":"repairs"}
    raw = copy.deepcopy(RAW)
    for g in raw["targets"]:
        g["name"] = names[g["name"]]
    raw["targets"][1]["amount"] = "$100"
    text = TEXT.replace("car", "education").replace("emergency", "repairs").replace("$1,000", "$100")
    original = model.json_completion
    def completion(system, user, schema_name, schema):
        return raw if schema_name == "steady_budget_plan" else original(system, user, schema_name, schema)
    model.json_completion = completion
    reply = controller.handle(text + " Adapt priorities automatically.", "draft-adaptive")[0]
    assert "redistribute up to 15%" in reply
    assert "targets, reserves, and payment approvals stay fixed" in reply
    assert runtime.store.state().get("budget_policy") is None
    ref = runtime.store.state()["budget_draft"]["id"]
    controller.handle("CONFIRM PLAN " + ref, "confirm-adaptive")
    return ref


def decision(education=0, repairs=1500, cap=10000, defer=False):
    return dict(priority_order=["education", "repairs"] if education else ["repairs", "education"],
                allocation_mode="prioritize",
                caps=[dict(category="vacation", limit_cents=cap)], defer_savings=defer,
                reason="Prioritize repairs until funded; restrict optional spending, then reassess.")


def script(model, strategies, tools=()):
    original = model.json_completion
    model.strategy_contexts = []
    def completion(system, user, schema_name, schema):
        if schema_name == "steady_financial_strategy":
            model.strategy_contexts.append(json.loads(user))
            result = strategies.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return original(system, user, schema_name, schema)
    model.json_completion = completion
    model.actions = list(tools)


def test_adapts_across_repair_completion_spending_pressure_and_recovery(system):
    runtime, controller, model = system
    activate(runtime, controller, model)
    change(runtime, "income", amount="1000", description="First new income")
    script(model, [decision(), decision(education=1500, repairs=0, cap=20000)],
           [action("allocate_savings", "repairs"), action("wait")])
    controller.run(force=True)
    assert runtime.store.state()["savings_pots"] == {"education":0, "repairs":10000}
    assert runtime.budget.snapshot()["caps"][0]["limit_cents"] == 10000
    # Completed repair goal causes a different allocation, with no new user command.
    model.actions = [action("allocate_savings", "education"), action("wait")]
    controller.run(force=True)
    assert runtime.store.state()["savings_pots"] == {"education":5000, "repairs":10000}
    assert model.strategy_contexts[-1]["previous_outcome"] == {"education":0, "repairs":10000}
    assert runtime.budget.snapshot()["caps"][0]["limit_cents"] == 20000
    # Revising strategy cannot allocate another 15% from the same source.
    assert runtime.budget.allocate("education")["allocated_cents"] == 0
    receipts = runtime.store.state()["savings_allocations"]
    assert sum(a["amount_cents"] for a in receipts) == 15000
    # Cash disruption, then new income, triggers deferral and independent resumption.
    with runtime.store.tx() as state:
        state["balance_cents"] = 100
    change(runtime, "income", amount="100", description="Small income")
    script(model, [decision(education=1500, repairs=0, defer=True),
                   decision(education=1500, repairs=0, cap=20000)], [action("wait")])
    controller.run(force=True)
    assert runtime.store.state()["savings_pots"]["education"] == 5000
    change(runtime, "income", amount="2000", description="Recovery income")
    model.actions = [action("allocate_savings", "education"), action("wait")]
    controller.run(force=True)
    assert runtime.store.state()["savings_pots"]["education"] == 36500
    state = runtime.store.state()
    assert state["balance_cents"] + sum(state["savings_pots"].values()) == 100 + 15000 + 210000
    # Independent integer conservation checked against mutations, not forecasts.
    assert len(state["strategy_history"]) == 4


@pytest.mark.parametrize("invalid", [
    {**decision(), "priority_order":["education", "education"]},
    {**decision(), "priority_order":["invented", "repairs"]},
    {**decision(), "allocation_mode":"unlimited"},
    {**decision(), "priority_order":[True, "repairs"]}, decision(cap=20001),
    {**decision(), "defer_savings":"false"}, {**decision(), "caps":[]},
])
def test_rejects_invalid_strategy_without_moving_money_or_replacing_strategy(system, invalid):
    runtime, controller, model = system
    activate(runtime, controller, model)
    change(runtime, "income", amount="1000", description="Income")
    script(model, [decision(), invalid])
    runtime.budget.replan(model)
    before = runtime.store.state()
    with pytest.raises(ValueError):
        runtime.budget.replan(model)
    assert runtime.store.state() == before


def test_strategy_failure_does_not_execute_old_strategy_on_new_facts(system):
    runtime, controller, model = system
    activate(runtime, controller, model)
    change(runtime, "income", amount="1000", description="Income")
    script(model, [decision(), LLMError("Unavailable")], [action("wait")])
    controller.run(force=True)
    before = runtime.store.state()["savings_pots"]
    change(runtime, "expense", merchant="Surprise bill", amount="100", category="other")
    model.actions = [action("allocate_savings", "repairs")]
    controller.run(force=True)
    assert runtime.store.state()["savings_pots"] == before
    assert any(e["kind"] == "run_unavailable" for e in controller.memory.read()["events"])


def test_adaptive_plan_cannot_allocate_before_assessment_or_while_paused(system):
    runtime, controller, model = system
    ref = activate(runtime, controller, model)
    change(runtime, "income", amount="1000", description="Income")
    assert runtime.budget.allocate("repairs")["allocated_cents"] == 0
    script(model, [decision()])
    runtime.budget.replan(model)
    controller.handle("PAUSE PLAN " + ref, "pause")
    assert runtime.budget.replan(model) is None
    with pytest.raises(ValueError):
        runtime.budget.allocate("repairs")
    assert runtime.store.state()["savings_pots"] == {"education":0, "repairs":0}


def test_unchanged_finances_do_not_trigger_repeated_strategy_calls(system):
    runtime, controller, model = system
    activate(runtime, controller, model)
    script(model, [decision(defer=True)], [action("wait")])
    controller.run()
    controller.run()
    assert len(model.strategy_contexts) == 1


def test_strategy_and_measured_progress_survive_controller_restart(system):
    from backend.autonomy.controller import Controller
    from backend.autonomy.memory import Memory
    runtime, controller, model = system
    activate(runtime, controller, model)
    change(runtime, "income", amount="1000", description="Income")
    script(model, [decision(), decision(education=1500, repairs=0)],
           [action("allocate_savings", "repairs"), action("wait")])
    controller.run(force=True)
    restarted = Controller(Memory(controller.memory.path), model, runtime.bank, runtime.twilio,
                           controller.recipient, payments=runtime.payments, actions=runtime.budget,
                           lock=runtime.lock, source="local simulation")
    model.actions = [action("allocate_savings", "education"), action("wait")]
    restarted.run(force=True)
    assert model.strategy_contexts[-1]["previous_outcome"]["repairs"] == 10000
    assert runtime.store.state()["savings_pots"] == {"education":5000, "repairs":10000}


def test_completed_goals_may_be_omitted_and_balanced_mode_redistributes_their_share(system):
    runtime, controller, model = system
    activate(runtime, controller, model)
    change(runtime, "income", amount="1000", description="Income")
    raw = {**decision(), "allocation_mode":"balanced"}
    next_strategy = {**raw, "priority_order":["education"]}
    script(model, [raw, next_strategy])
    runtime.budget.replan(model)
    assert runtime.budget.snapshot()["strategy"]["shares"] == {"education":1000, "repairs":500}
    with runtime.store.tx() as state:
        state["savings_pots"]["repairs"] = 10000
    runtime.budget.replan(model)
    assert runtime.budget.snapshot()["strategy"]["shares"] == {"education":1500, "repairs":0}


def test_model_cannot_override_cash_protection_by_choosing_to_continue_saving(system):
    runtime, controller, model = system
    activate(runtime, controller, model)
    with runtime.store.tx() as state:
        state["balance_cents"] = 0
    change(runtime, "income", amount="100", description="Small income")
    script(model, [decision(defer=False)], [action("allocate_savings", "repairs"), action("wait")])
    controller.run(force=True)
    assert runtime.store.state()["savings_pots"] == {"education":0, "repairs":0}


def test_strategy_queries_report_decision_without_proposing_new_policy(system):
    runtime, controller, model = system
    ref = activate(runtime, controller, model)
    script(model, [decision()])
    runtime.budget.replan(model)
    reply = controller.handle("Why is this your financial strategy?", "strategy-query")[0]
    assert "Priority order: repairs, education" in reply
    assert runtime.store.state()["budget_draft"] is None
    assert runtime.store.state()["budget_policy"]["id"] == ref
