"""Real fictional transfers, policy authority, recovery and budget tradeoffs."""
from uuid import uuid4

import pytest

from backend.autonomy.controller import Controller
from backend.autonomy.memory import Memory
from backend.simulation.runtime import SimulationRuntime
from tests.test_app import RecordingTwilio
from tests.test_external_biller import FakeMailer
from tests.test_autonomy import Reasoner, action

TEXT = ("Budget plan: save $22,000 for car, allocate 10% of each new deposit to car; "
        "save $1,000 for emergency, allocate 5% to emergency; cap vacation at $200 monthly; "
        "keep $200 cushion and $100 weekly essentials.")
RAW = dict(targets=[dict(name="car", amount="$22,000", percent="10%"),
                    dict(name="emergency", amount="$1,000", percent="5%")],
           caps=[dict(category="vacation", amount="$200")], buffer="$200", weekly_essentials="$100")


class Model(Reasoner):
    def json_completion(self, system, user, schema_name, schema):
        return RAW if schema_name == "steady_budget_plan" else super().json_completion(system, user, schema_name, schema)


@pytest.fixture
def system(tmp_path, settings):
    transport = RecordingTwilio(settings)
    transport.record_inbound(settings.recipient)
    runtime = SimulationRuntime(settings, transport, tmp_path, mailer=FakeMailer(), today="2026-11-12")
    model = Model()
    controller = Controller(Memory(tmp_path / "goals.sqlite"), model, runtime.bank, transport,
                            settings.recipient, payments=runtime.payments, actions=runtime.budget,
                            lock=runtime.lock, source="local simulation")
    yield runtime, controller, model
    runtime.stop()
    runtime.watcher.ledger.close()
    runtime.audit._db.close()


def change(runtime, operation, **fields):
    runtime.command(operation, dict(run_id=runtime.store.state()["run_id"], **fields), uuid4().hex)


def confirm(runtime, controller):
    response = controller.handle(TEXT, "draft")
    assert "CONFIRM PLAN" in response[0]
    assert runtime.store.state().get("budget_policy") is None
    ref = runtime.store.state()["budget_draft"]["id"]
    controller.handle("CONFIRM PLAN " + ref, "confirm")
    return ref


def test_car_and_emergency_receive_only_authorized_new_income_once(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    # Historical seed deposits are already reflected in checking and excluded.
    assert not any(g["eligible"] for g in runtime.budget.snapshot()["goals"])
    change(runtime, "income", amount="1000", description="Payday")
    model.actions = [action("allocate_savings", "car"), action("allocate_savings", "emergency"),
                     action("review_spending"), action("wait")]
    controller.run()
    state = runtime.store.state()
    assert state["savings_pots"] == {"car":10000, "emergency":5000}
    assert state["balance_cents"] == 221300 + 100000 - 15000
    assert sum(state["savings_pots"].values()) + state["balance_cents"] == 321300
    assert len(state["savings_allocations"]) == 2
    runtime.budget.allocate("car")
    runtime.budget.allocate("emergency")
    assert len(runtime.store.state()["savings_allocations"]) == 2
    assert model.contexts[1]["results"][0]["result"]["allocated_cents"] == 10000
    controller.flush()
    assert any("actually allocated" in text for _, text in runtime.twilio.sent)


@pytest.mark.parametrize("command", ["PAUSE PLAN", "RESUME PLAN", "CONFIRM PLAN", "PAUSE PLAN invalid"])
def test_incomplete_plan_commands_request_reference_without_changing_policy(system, command):
    runtime, controller, _ = system
    confirm(runtime, controller)
    before = runtime.store.state()["budget_policy"]
    response = controller.handle(command, "incomplete-command")
    assert "eight-character plan reference" in response[0]
    assert "No plan or money changed" in response[0]
    assert runtime.store.state()["budget_policy"] == before


def test_copied_percentage_with_deposit_qualifier_is_validated(system):
    import copy
    runtime, controller, model = system
    raw = copy.deepcopy(RAW)
    raw["targets"][0]["percent"] = "10% of each new deposit"
    raw["targets"][1]["percent"] = "5% to emergency"
    model.json_completion = lambda *args:raw
    # Extra arbitrary text is still rejected, even if copied from the message.
    assert "CONFIRM PLAN" not in controller.handle(TEXT, "bad-qualifier")[0]
    raw["targets"][1]["percent"] = "5%"
    assert "CONFIRM PLAN" in controller.handle(TEXT, "valid-qualifier")[0]
    assert runtime.store.state()["budget_draft"]["targets"][0]["basis_points"] == 1000


def test_unsafe_allocations_wait_then_resume_without_duplicate_credits(system):
    runtime, controller, _ = system
    confirm(runtime, controller)
    with runtime.store.tx() as state:
        state["balance_cents"] = 10000
    change(runtime, "income", amount="100", description="Small deposit")
    assert runtime.budget.allocate("car")["allocated_cents"] == 0
    assert runtime.budget.snapshot()["goals"][0]["eligible"]
    change(runtime, "income", amount="1000", description="Recovery deposit")
    assert runtime.budget.allocate("car")["allocated_cents"] == 11000
    assert runtime.budget.allocate("car")["allocated_cents"] == 0
    assert runtime.store.state()["savings_pots"]["car"] == 11000


def test_caps_track_posted_truth_and_month_boundary(system):
    runtime, controller, _ = system
    confirm(runtime, controller)
    change(runtime, "expense", merchant="Flight", amount="150", category="vacation")
    cap = runtime.budget.snapshot()["caps"][0]
    assert cap["remaining_cents"] == 5000
    change(runtime, "expense", merchant="Hotel", amount="100", category="vacation")
    assert runtime.budget.snapshot()["caps"][0]["over_cents"] == 5000
    assert "Pause further spending" in runtime.budget.review()["text"]
    with runtime.store.tx() as state:
        state["today"] = "2026-12-01"
    assert runtime.budget.snapshot()["caps"][0]["spent_cents"] == 0


def test_pausing_blocks_allocation_and_policy_changes_exclude_old_income(system):
    runtime, controller, _ = system
    ref = confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Payday")
    controller.handle("PAUSE PLAN " + ref, "pause")
    with pytest.raises(ValueError):
        runtime.budget.allocate("car")
    controller.handle("RESUME PLAN " + ref, "resume")
    runtime.budget.allocate("car")
    assert runtime.store.state()["savings_pots"]["car"] == 10000
    controller.handle(TEXT, "new-draft")
    newref = runtime.store.state()["budget_draft"]["id"]
    controller.handle("CONFIRM PLAN " + newref, "new-confirm")
    assert runtime.budget.allocate("car")["allocated_cents"] == 0


def test_model_cannot_select_unknown_pot_or_invent_a_policy(system):
    runtime, controller, _ = system
    with pytest.raises(ValueError):
        runtime.budget.allocate("car")
    confirm(runtime, controller)
    with pytest.raises(ValueError):
        runtime.budget.allocate("attacker-account")
    raw_model = Model()
    response = runtime.budget.handle(TEXT.replace("$22,000", "$25,000"), "bad-grounding", raw_model)
    assert "Example:" in response[0]
    assert runtime.store.state()["budget_policy"]["targets"][0]["target_cents"] == 2200000


def test_target_completion_never_overfunds_and_survives_restart(system, tmp_path):
    runtime, controller, _ = system
    confirm(runtime, controller)
    with runtime.store.tx() as state:
        state["savings_pots"]["car"] = 2199999
    change(runtime, "income", amount="1000", description="Payday")
    assert runtime.budget.allocate("car")["allocated_cents"] == 1
    assert runtime.budget.snapshot()["goals"][0]["complete"]
    assert runtime.budget.allocate("car")["allocated_cents"] == 0
    controller.observe()
    assert controller.memory.read()["goals"]["allocation:car"]["status"] == "complete"
    from backend.simulation.store import Store
    reopened = Store(tmp_path / "account.sqlite3", "2026-11-12", "Margaret")
    assert reopened.state()["savings_pots"]["car"] == 2200000


def test_reconciling_payment_blocks_allocations(system):
    runtime, controller, _ = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Payday")
    with runtime.payments._tx() as (_, state):
        next(iter(state["jobs"].values()))["status"] = "reconciling"
    assert runtime.budget.allocate("car")["allocated_cents"] == 0


def test_busy_week_and_retried_income_are_atomic(system):
    runtime, controller, _ = system
    confirm(runtime, controller)
    run_id = runtime.store.state()["run_id"]
    income = dict(run_id=run_id, amount="1000", description="Payday")
    runtime.command("income", income, "same-income-key")
    runtime.command("income", income, "same-income-key")
    rows = [dict(merchant="Groceries", amount="80", category="groceries"),
            dict(merchant="Restaurant", amount="55", category="dining"),
            dict(merchant="Bus", amount="30", category="transport"),
            dict(merchant="Trip", amount="120", category="vacation")]
    body = dict(run_id=run_id, expenses=rows)
    runtime.command("expense-batch", body, "same-expense-batch")
    runtime.command("expense-batch", body, "same-expense-batch")
    assert runtime.store.state()["balance_cents"] == 221300 + 100000 - 28500
    assert runtime.budget.snapshot()["caps"][0]["spent_cents"] == 12000
    before = runtime.store.state()["balance_cents"]
    with pytest.raises(ValueError):
        change(runtime, "expense-batch", expenses=rows + [dict(merchant="Bad", amount="NaN")])
    assert runtime.store.state()["balance_cents"] == before


def test_many_bill_payments_and_income_allocations_conserve_every_cent(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Payday")
    for day in range(8):
        if day:
            change(runtime, "advance")
        runtime.tick()
        for proposal in list(runtime.payments.state()["jobs"].values()):
            if proposal["status"] != "awaiting_user":
                continue
            runtime.payments.handle("YES " + proposal["id"], "yes", "local-consent-" + proposal["id"])
            runtime.tick()
            runtime.payments.caregiver_decide(runtime.payments.mailer.token, "approve")
            runtime.tick()
        model.actions = [action("allocate_savings", "car"), action("allocate_savings", "emergency"),
                         action("review_spending"), action("wait")]
        controller.run(force=True)
    state = runtime.store.state()
    assert len(state["receipts"]) == 3
    assert all(b["status"] == "paid" for b in state["invoices"])
    assert state["savings_pots"] == {"car":28740, "emergency":14370}
    paid = sum(r["amount_cents"] for r in state["receipts"].values())
    # Opening cash + new payday + previously pending income = checking + pots + paid bills.
    assert runtime.state()["balance_cents"] + sum(state["savings_pots"].values()) + paid == 221300 + 100000 + 187400
    assert paid == 25524
    for b in state["invoices"]:
        assert controller.memory.read()["goals"]["bill:" + b["id"]]["status"] == "complete"


def test_overcommitted_percentages_are_not_authorized(system):
    from copy import deepcopy
    runtime, _, _ = system
    raw = deepcopy(RAW)
    raw["targets"][0]["percent"] = "80%"
    raw["targets"][1]["percent"] = "30%"
    class BadModel:
        def json_completion(self, *args):
            return raw
    response = runtime.budget.handle(TEXT.replace("10%", "80%").replace("5%", "30%"), "overcommit", BadModel())
    assert "at most 100%" in response[0]
    assert not runtime.store.state().get("budget_draft")
    assert not runtime.store.state().get("budget_policy")


def test_concurrent_retries_allocate_each_source_once(system):
    from concurrent.futures import ThreadPoolExecutor
    runtime, controller, _ = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Payday")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _:runtime.budget.allocate("car"), range(12)))
    assert sum(r["allocated_cents"] for r in results) == 10000
    assert len(runtime.store.state()["savings_allocations"]) == 1


def test_plain_budget_questions_recall_named_plan_without_starting_new_intake(system):
    runtime, controller, _ = system
    confirm(runtime, controller)
    assert "$22,000.00" in controller.handle("How is my budget?", "status-how")[0]
    assert "$22,000.00" in controller.handle("What is my car goal?", "status-car")[0]
    assert not controller.memory.read().get("intake")
    assert runtime.budget.snapshot()["active"]


def test_spending_question_respects_vacation_cap_without_mutation(system):
    runtime, controller, _ = system
    confirm(runtime, controller)
    change(runtime, "expense", merchant="Hotel", amount="150", category="vacation")
    before = runtime.store.state()
    result = controller.handle("Can I spend $100 on vacation?", "preview-cap")
    assert result and "$50.00" in result[0] and "cap" in result[0]
    assert "No expense or transfer was made" in result[0]
    after = runtime.store.state()
    for key in ("balance_cents", "purchases", "savings_pots", "budget_policy"):
        assert after[key] == before[key]


def test_spending_question_uses_cash_reserves_and_rejects_ambiguous_amounts(system):
    runtime, controller, _ = system
    confirm(runtime, controller)
    with runtime.store.tx() as state:
        state["balance_cents"] = 50000
    result = controller.handle("What if I spend $20 on dining?", "preview-cash")[0]
    assert "$0.00" in result and "bills" in result
    result = controller.handle("Can I spend $20 or $50 on vacation?", "preview-many")[0]
    assert "one" in result.lower()
    assert not runtime.store.state().get("savings_allocations")


def test_successful_allocation_is_reported_even_if_next_model_call_fails(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Payday")
    model.actions = [action("allocate_savings", "car")]
    controller.run()
    controller.flush()
    assert runtime.store.state()["savings_pots"]["car"] == 10000
    assert any("car: $100.00 actually allocated" in text for _, text in runtime.twilio.sent)
    # Recovery must allocate only the still-unfunded goal, not repeat car.
    model.actions = [action("allocate_savings", "emergency"), action("wait")]
    controller.run(force=True)
    assert runtime.store.state()["savings_pots"] == {"car":10000, "emergency":5000}
    assert len(runtime.store.state()["savings_allocations"]) == 2


def test_partial_allocation_completes_exactly_after_safe_cash_returns(system):
    runtime, controller, _ = system
    confirm(runtime, controller)
    # Known bills + cushion + rounded-up 30-day essentials = $883.94.
    with runtime.store.tx() as state:
        state["balance_cents"] = 0
    change(runtime, "income", amount="900", description="Restricted payday")
    first = runtime.budget.allocate("car")
    assert first["allocated_cents"] == 1606
    assert runtime.budget.allocate("emergency")["allocated_cents"] == 0
    change(runtime, "income", amount="1000", description="Recovery payday")
    runtime.budget.allocate("car")
    runtime.budget.allocate("emergency")
    assert runtime.store.state()["savings_pots"] == {"car":19000, "emergency":9500}
    assert runtime.budget.allocate("car")["allocated_cents"] == 0


def test_future_income_and_duplicate_confirmation_never_credit_a_pot(system):
    runtime, controller, _ = system
    ref = confirm(runtime, controller)
    change(runtime, "income", amount="500", description="Future payday", date="2026-11-13")
    assert runtime.budget.allocate("car")["allocated_cents"] == 0
    controller.handle("CONFIRM PLAN " + ref, "duplicate-confirm-new-message")
    change(runtime, "advance")
    assert runtime.budget.allocate("car")["allocated_cents"] == 5000
    controller.handle("CONFIRM PLAN " + ref, "duplicate-confirm-after-transfer")
    assert runtime.budget.allocate("car")["allocated_cents"] == 0
    assert runtime.store.state()["savings_pots"]["car"] == 5000


def test_expired_policy_draft_cannot_authorize_allocations(system):
    runtime, controller, _ = system
    controller.handle(TEXT, "expired-draft")
    ref = runtime.store.state()["budget_draft"]["id"]
    with runtime.store.tx() as state:
        state["budget_draft"]["created_at"] -= 86401
    result = controller.handle("CONFIRM PLAN " + ref, "expired-confirm")
    assert "expired" in result[0]
    assert not runtime.store.state().get("budget_policy")
    with pytest.raises(ValueError):
        runtime.budget.allocate("car")
