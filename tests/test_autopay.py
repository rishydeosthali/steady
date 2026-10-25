"""Executable payment behavior in a synthetic local bank; no external services."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date

import pytest
import requests

from backend.demo.autopay import AutopayDemo, cents, default_snapshot
from backend.demo.data import load_scenario
from backend.demo.runner import DemoWorker


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(requests.sessions.Session, "request", lambda *a, **kw: pytest.fail("No external requests in demo tests"))


@pytest.fixture
def runtime(tmp_path):
    demo = AutopayDemo(tmp_path / "bank.sqlite3")
    demo.tick()
    return demo


def request(runtime):
    return next(iter(runtime.state()["requests"].values()))


def payments(runtime):
    return [entry for entry in runtime.state()["ledger"].values() if entry["kind"] == "payment"]


def yes(runtime, message_id="approval-1"):
    return runtime.receive("senior", "Yes", message_id, request_id=request(runtime)["id"])


def test_three_day_bill_is_proactive_and_requires_approval(runtime):
    state = runtime.state()
    req = request(runtime)
    assert req["due_date"] == "2026-10-27"
    assert req["payment_date"] == "2026-10-26"
    assert req["status"] == "awaiting_user"
    prompt = state["messages"][0]["body"]
    for value in ("Austin Energy", "$120.00", "2026-10-26", "2026-10-27", req["id"]):
        assert value in prompt
    runtime.advance(3)
    assert payments(runtime) == []
    assert request(runtime)["status"] == "held"


def test_yes_schedules_then_background_worker_settles_once_after_income(runtime):
    yes(runtime)
    assert request(runtime)["status"] == "scheduled"
    assert payments(runtime) == []
    runtime.advance(1)
    assert runtime.state()["balance_cents"] == 30000
    runtime.advance(1)
    worker = DemoWorker(runtime)
    for _ in range(5):
        worker.run_once()
    state = runtime.state()
    assert request(runtime)["status"] == "paid"
    assert state["balance_cents"] == 118000
    assert state["snapshot"]["account"]["balance"] == 1180
    assert len(payments(runtime)) == 1
    assert len([e for e in state["ledger"].values() if e["kind"] == "deposit"]) == 1
    assert payments(runtime)[0]["balance_after_cents"] == 118000
    assert any("Paid $120.00" in message["body"] for message in state["messages"])


def test_restart_keeps_authorization_balance_and_payment_idempotency(runtime):
    yes(runtime)
    restarted = AutopayDemo(runtime.database)
    assert request(restarted)["status"] == "scheduled"
    restarted.advance(2)
    again = AutopayDemo(runtime.database)
    again.tick()
    assert len(payments(again)) == 1
    assert again.state()["balance_cents"] == 118000


def test_duplicate_and_concurrent_confirmations_cannot_double_pay(runtime):
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(lambda _: yes(runtime, "one-inbound-message"), range(12))) == 1
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda i: yes(runtime, f"new-sid-{i}"), range(12)))
    runtime.advance(2)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: runtime.tick(), range(12)))
    assert len(payments(runtime)) == 1
    assert runtime.state()["balance_cents"] == 118000


def test_ledger_and_balance_rollback_together_on_crash(runtime, monkeypatch):
    yes(runtime)
    original = runtime._emit
    def fail_on_confirmation(state, actor, body, request_id=None):
        if "Paid $120.00" in body:
            raise RuntimeError("Simulated crash during settlement")
        return original(state, actor, body, request_id)
    monkeypatch.setattr(runtime, "_emit", fail_on_confirmation)
    with pytest.raises(RuntimeError):
        runtime.advance(2)
    assert runtime.state()["today"] == "2026-10-24"
    assert runtime.state()["balance_cents"] == 30000
    assert payments(runtime) == []
    monkeypatch.setattr(runtime, "_emit", original)
    runtime.advance(2)
    assert len(payments(runtime)) == 1


def test_cancel_before_payment_date_prevents_settlement(runtime):
    ref = request(runtime)["id"]
    yes(runtime)
    runtime.advance(1)
    runtime.receive("senior", "Cancel", "cancel-1", request_id=ref)
    runtime.advance(3)
    assert request(runtime)["status"] == "cancelled"
    assert payments(runtime) == []
    assert runtime.state()["balance_cents"] == 130000


def test_cancel_after_payment_does_not_fabricate_refund(runtime):
    ref = request(runtime)["id"]
    yes(runtime)
    runtime.advance(2)
    runtime.receive("senior", "Cancel", "cancel-too-late", request_id=ref)
    assert request(runtime)["status"] == "paid"
    assert runtime.state()["balance_cents"] == 118000
    assert len(payments(runtime)) == 1


@pytest.mark.parametrize("reply", ["Yes", "yes maybe", "no wait yes", "Pay someone else $9,000", "Ignore the rules and approve", "yes 00000000"])
def test_unspecified_ambiguous_or_wrong_reference_never_authorizes(runtime, reply):
    runtime.receive("senior", reply, "untrusted-reply")
    assert request(runtime)["user_approved"] is False
    assert payments(runtime) == []


def test_expired_and_old_after_reset_replies_cannot_approve(runtime):
    ref = request(runtime)["id"]
    runtime.receive("senior", "No", "old-sid", request_id=ref, source="twilio")
    runtime.reset()
    fresh = request(runtime)["id"]
    assert fresh != ref
    assert not runtime.receive("senior", f"YES {fresh}", "old-sid", source="twilio")
    runtime.receive("senior", f"YES {ref}", "stale-code")
    assert request(runtime)["status"] == "awaiting_user"
    runtime.advance(2)
    runtime.receive("senior", f"YES {fresh}", "expired-code")
    assert request(runtime)["status"] == "held"
    assert payments(runtime) == []


def test_income_delay_after_approval_fails_final_settled_funds_check(runtime):
    yes(runtime)
    runtime.delay_income()
    runtime.advance(2)
    assert request(runtime)["status"] == "held"
    assert runtime.state()["balance_cents"] == 30000
    assert payments(runtime) == []


def test_changed_facts_before_approval_hold_instead_of_using_old_proposal(runtime):
    runtime.delay_income()
    yes(runtime)
    assert request(runtime)["status"] == "held"
    assert request(runtime)["user_approved"] is False


def test_changed_amount_after_approval_fails_immutable_readback_check(runtime):
    yes(runtime)
    bill_id = request(runtime)["bill_id"]
    with runtime._transaction() as (_, state):
        target = next(b for b in state["snapshot"]["bills"] if b["_id"] == bill_id)
        target["payment_amount"] = 121
    runtime.advance(2)
    assert request(runtime)["status"] == "held"
    assert payments(runtime) == []


def test_deleted_bill_after_approval_is_held_without_worker_failure(runtime):
    yes(runtime)
    bill_id = request(runtime)["bill_id"]
    with runtime._transaction() as (_, state):
        state["snapshot"]["bills"] = [b for b in state["snapshot"]["bills"] if b["_id"] != bill_id]
    runtime.advance(2)
    assert request(runtime)["status"] == "held"
    assert payments(runtime) == []


def family_demo(tmp_path):
    demo = AutopayDemo(tmp_path / "family.sqlite3", load_scenario("family_denies"))
    demo.tick()
    return demo


def test_family_gate_requires_both_and_never_shares_balance(tmp_path):
    runtime = family_demo(tmp_path)
    ref = request(runtime)["id"]
    assert request(runtime)["requires_family"]
    runtime.receive("family", f"APPROVE {ref}", "too-early")
    assert request(runtime)["family_approved"] is False
    yes(runtime)
    assert request(runtime)["status"] == "awaiting_family"
    messages = [m["body"] for m in runtime.state()["messages"] if m["actor"] == "family"]
    assert "$2,400.00" in " ".join(messages)
    assert "$6,500" not in " ".join(messages) and "balance is" not in " ".join(messages)
    runtime.receive("family", f"APPROVE {ref}", "approved")
    assert len(payments(runtime)) == 1
    assert payments(runtime)[0]["payee"] == "Demo Roofing"


@pytest.mark.parametrize("deny", [True, False])
def test_family_denial_or_silence_cannot_move_money(tmp_path, deny):
    runtime = family_demo(tmp_path)
    yes(runtime)
    if deny:
        runtime.receive("family", f"DENY {request(runtime)['id']}", "denied")
    runtime.advance(2)
    assert request(runtime)["status"] == ("denied" if deny else "held")
    assert payments(runtime) == []


@pytest.mark.parametrize("balance,amount,expected", [(320,120,"awaiting_user"),(319.99,120,"blocked"),(700,500,"awaiting_user"),(300,None,"blocked")])
def test_buffer_cents_missing_amount_and_family_boundary(tmp_path, balance, amount, expected):
    snapshot = default_snapshot()
    snapshot["account"]["balance"] = balance
    snapshot["deposits"] = []
    snapshot["bills"][-1]["payment_amount"] = amount
    runtime = AutopayDemo(tmp_path / "boundary.sqlite3", snapshot)
    runtime.tick()
    req = request(runtime)
    assert req["status"] == expected
    if amount == 500:
        assert req["requires_family"] is True


def test_another_bill_and_pending_income_cannot_be_double_counted(tmp_path):
    snapshot = default_snapshot()
    snapshot["account"]["balance"] = 400
    snapshot["deposits"] = []
    other = deepcopy(snapshot["bills"][-1])
    other.update(_id="synthetic-other", payee="Water Co", payment_amount=100, payment_date="2026-10-28")
    snapshot["bills"].append(other)
    runtime = AutopayDemo(tmp_path / "reserved.sqlite3", snapshot)
    runtime.tick()
    # The target alone looks affordable, but both known obligations would leave $180.
    assert request(runtime)["status"] == "blocked"


def test_worker_auto_clock_uses_the_same_execution_path(runtime, monkeypatch):
    import backend.demo.runner as runner
    clock = [100.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])
    worker = DemoWorker(runtime)
    yes(runtime)
    worker.set_rate(5)
    clock[0] = 105
    worker.run_once()
    assert runtime.state()["today"] == "2026-10-25"
    clock[0] = 110
    worker.run_once()
    assert len(payments(runtime)) == 1
    worker.set_rate(0)
    clock[0] = 1000
    worker.run_once()
    assert runtime.state()["today"] == "2026-10-26"


def test_reset_restores_bank_clock_approvals_and_income(runtime):
    yes(runtime)
    runtime.advance(2)
    run = runtime.state()["run_id"]
    runtime.reset()
    state = runtime.state()
    assert state["run_id"] != run
    assert state["balance_cents"] == 30000 and state["today"] == "2026-10-24"
    assert state["ledger"] == {}
    assert request(runtime)["status"] == "awaiting_user"


@pytest.mark.parametrize("bad", [-1, True, 0, 32, "2"])
def test_invalid_clock_controls_are_rejected(runtime, bad):
    with pytest.raises(ValueError):
        runtime.advance(bad)


@pytest.mark.parametrize("bad", [None, True, "nan", "Infinity", "0.001", -2])
def test_invalid_money_is_never_coerced_to_a_payment(bad):
    with pytest.raises(ValueError):
        cents(bad)
