"""Data integrity and independent arithmetic oracles, not payment-engine tests.

The oracle enumerates candidate dates and reserves all other due bills. It
does not implement the application's future approval or execution layer.
"""

import json
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

import pytest
import requests

from backend.agent import reply
from backend.agent.facts import gather_facts
from backend.agent.loop import Agent
from backend.demo.data import list_scenarios, load_scenario, validate_catalog
from backend.demo.nessie import FixtureRequestHandler, FixtureSession, create_fixture_app, fixture_client
from scripts.demo_data import main
from tests.conftest import FakeLLM
from tests.test_app import InlineExecutor, RecordingTwilio, form, sign


SCENARIOS = [case["id"] for case in list_scenarios()]


def cents(value):
    result = Decimal(str(value)) * 100
    assert result == result.to_integral_value(), f"Sub-cent value: {value}"
    return int(result)


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Synthetic fixture tests must never use the network")
    monkeypatch.setattr(requests.sessions.Session, "request", fail)


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_snapshot_integrity_and_real_nessie_reader(scenario_id):
    snapshot = load_scenario(scenario_id)
    assert snapshot["synthetic"] is True
    anchor = snapshot["as_of"]
    account = snapshot["account"]
    all_records = [snapshot["customer"], account] + sum(
        [snapshot[k] for k in ("bills", "deposits", "purchases", "merchants")], [])
    ids = [row["_id"] for row in all_records]
    assert len(ids) == len(set(ids))
    assert all(key.startswith("synthetic-") for key in ids)
    cents(account["balance"])
    merchant_ids = {m["_id"] for m in snapshot["merchants"]}
    for kind, amount_field, date_field in (
        ("bills", "payment_amount", "payment_date"),
        ("deposits", "amount", "transaction_date"),
        ("purchases", "amount", "purchase_date"),
    ):
        for row in snapshot[kind]:
            date.fromisoformat(row[date_field])
            if row[amount_field] is not None:
                assert cents(row[amount_field]) >= 0
            if row["status"] == "completed":
                assert row[date_field] <= anchor
            else:
                assert row[date_field] >= anchor
            if kind == "purchases":
                assert row["merchant_id"] in merchant_ids
                assert row["payer_id"] == account["_id"]
            elif kind == "deposits":
                assert row["payee_id"] == account["_id"]
            else:
                assert row["account_id"] == account["_id"]
    # Exercise the existing transport/discovery/formatting rather than a new fake reader.
    client = fixture_client(snapshot)
    assert client.resolve(snapshot["customer"]["first_name"]) == account
    facts = gather_facts(client, account["_id"], ("balance", "bills", "deposits", "purchases"))
    assert all(f["status"] == "ok" for f in facts.values())
    assert facts["bills"]["count"] == len(snapshot["bills"])
    assert len(facts["bills"]["items"]) == len(snapshot["bills"])  # no bill lost to the 10-item cap


def oracle_earliest_date(snapshot):
    """Independent daily ledger through the target deadline; cents only.

    Expected deposits can support a forecast on their date, never earlier.
    This benchmark assumes deposits clear before debits on that day. The real
    executor must verify cleared funds and approvals again before writing.
    """
    context = snapshot["context"]
    target = next(b for b in snapshot["bills"] if b["_id"] == context["target_bill_id"])
    start, deadline = map(date.fromisoformat, (snapshot["as_of"], target["payment_date"]))
    for offset in range((deadline - start).days + 1):
        proposed = start + timedelta(days=offset)
        balance = cents(snapshot["account"]["balance"])
        safe = True
        for day_offset in range((deadline - start).days + 1):
            when = (start + timedelta(days=day_offset)).isoformat()
            balance += sum(cents(d["amount"]) for d in snapshot["deposits"]
                           if d["status"] == "pending" and d["transaction_date"] == when)
            balance -= sum(cents(b["payment_amount"]) for b in snapshot["bills"]
                           if b["status"] == "pending" and b["_id"] != target["_id"]
                           and b["payment_date"] == when)
            if when == proposed.isoformat():
                balance -= cents(target["payment_amount"])
            if balance < context["buffer_cents"]:
                safe = False
        if safe:
            return proposed.isoformat()
    return None


@pytest.mark.parametrize("scenario_id", [c["id"] for c in list_scenarios()
                                       if "earliest_safe_day" in c["expected"]])
def test_hand_authored_payment_dates_match_independent_cash_ledger(scenario_id):
    snapshot = load_scenario(scenario_id)
    assert oracle_earliest_date(snapshot) == snapshot["expected"]["earliest_safe_date"]


def test_optional_purchase_shortfall_and_cent_precision():
    snapshot = load_scenario("spending_shortfall")
    target = next(b for b in snapshot["bills"] if b["_id"] == snapshot["context"]["target_bill_id"])
    remaining = (cents(snapshot["account"]["balance"])
                 - snapshot["context"]["proposed_purchase"]["amount_cents"]
                 - cents(target["payment_amount"]))
    assert remaining == snapshot["expected"]["balance_after_purchase_and_bill_cents"] == 16500
    assert remaining < snapshot["context"]["buffer_cents"]
    margaret = load_scenario()
    assert sum(cents(b["payment_amount"]) for b in margaret["bills"] if b["status"] == "pending") == 25524


def test_income_delay_changes_only_future_deposit_and_invalidates_safe_date():
    original = load_scenario("wait_for_income")
    delayed = load_scenario("income_delayed")
    assert original["deposits"][:-1] == delayed["deposits"][:-1]
    assert oracle_earliest_date(original) == "2026-10-30"
    assert oracle_earliest_date(delayed) is None


def test_subscription_history_supports_increase_without_claiming_fraud():
    snapshot = load_scenario("subscription_increase")
    merchant = next(m for m in snapshot["merchants"] if m["name"] == "Demo Streaming")
    amounts = [cents(p["amount"]) for p in snapshot["purchases"] if p["merchant_id"] == merchant["_id"]]
    assert amounts == [1500, 1500, 1500, 3500]
    new = load_scenario("new_subscription")
    merchant = next(m for m in new["merchants"] if m["name"] == "Demo Wellness Club")
    assert sum(p["merchant_id"] == merchant["_id"] for p in new["purchases"]) == 1
    assert new["expected"]["alert"] == "possible_new_subscription"


def test_scam_references_completed_bill_and_family_cases_have_sufficient_cash():
    snapshot = load_scenario("gift_card_scam")
    claim_id = snapshot["context"]["claimed_bill_id"]
    assert next(b for b in snapshot["bills"] if b["_id"] == claim_id)["status"] == "completed"
    for name in ("family_denies", "family_approves", "family_timeout"):
        snapshot = load_scenario(name)
        context = snapshot["context"]
        assert context["payment_request"]["payee"] not in context["known_payees"]
        assert context["payment_request"]["amount_cents"] > context["family_threshold_cents"]
        assert cents(snapshot["account"]["balance"]) - context["payment_request"]["amount_cents"] > context["buffer_cents"]


@pytest.mark.parametrize("anchor", [date(2027, 1, 29), date(2028, 2, 29)])
@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_rebase_dates_remain_valid_across_month_end_and_leap_year(scenario_id, anchor):
    snapshot = load_scenario(scenario_id, as_of=anchor)
    assert snapshot["as_of"] == anchor.isoformat()
    assert snapshot == load_scenario(scenario_id, as_of=anchor)
    for key, field in (("bills", "payment_date"), ("deposits", "transaction_date"), ("purchases", "purchase_date")):
        for row in snapshot[key]:
            when = date.fromisoformat(row[field])
            assert (when <= anchor) if row["status"] == "completed" else (when >= anchor)
    if "earliest_safe_date" in snapshot["expected"]:
        assert oracle_earliest_date(snapshot) == snapshot["expected"]["earliest_safe_date"]


def test_reset_and_returned_responses_are_isolated():
    snapshot = load_scenario()
    original = deepcopy(snapshot)
    client = fixture_client(snapshot)
    account_id = snapshot["account"]["_id"]
    snapshot["account"]["balance"] = 0
    response = client.get_bills(account_id)
    response[0]["payment_amount"] = 0
    assert client.get_balance(account_id) == 2213
    assert client.get_bills(account_id)[0]["payment_amount"] == original["bills"][0]["payment_amount"]
    assert load_scenario() == original


def test_fixture_transport_rejects_real_origins_and_missing_paths():
    session = FixtureSession(load_scenario())
    with pytest.raises(ValueError, match="synthetic"):
        session.get("https://api.nessieisreal.com/customers")
    assert session.get("https://synthetic.nessie.invalid/unknown").status_code == 404


def test_local_http_access_log_omits_api_key_query():
    handler = object.__new__(FixtureRequestHandler)
    handler.command = "GET"
    handler.path = "/customers?key=do-not-log-this"
    lines = []
    handler.log = lambda level, template, *args: lines.append(template % args)
    handler.log_request(200)
    assert lines == ["GET /customers 200"]


def test_fixture_http_server_exposes_only_bank_reads_never_expected_answers():
    snapshot = load_scenario()
    client = create_fixture_app(snapshot).test_client()
    assert client.get("/healthz").json["synthetic"] is True
    assert client.get("/customers").json == [snapshot["customer"]]
    path = f"/accounts/{snapshot['account']['_id']}/bills"
    assert client.get(path).json == snapshot["bills"]
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(path, json={}).status_code == 405
    assert client.get("/expected").status_code == 404
    assert client.get("/context").status_code == 404


def test_missing_amount_stays_unknown_and_read_only_agent_still_refuses_payment():
    snapshot = load_scenario("unknown_bill_amount")
    client = fixture_client(snapshot)
    account_id = snapshot["account"]["_id"]
    facts = gather_facts(client, account_id, ("bills",))
    pending = next(b for b in facts["bills"]["items"] if b["status"] == "pending")
    assert pending["amount"] is None
    llm = FakeLLM(intent={"intent": "pay_request", "confidence": 0.99, "needs": [], "restatement": ""})
    assert Agent(llm, client, "Margaret").respond("Pay it") == [reply.PAY_NOT_ON]


def test_signed_whatsapp_question_uses_fixture_through_existing_agent(settings):
    from backend.app import create_app
    snapshot = load_scenario()
    llm = FakeLLM(intent={"intent": "question", "confidence": 0.99, "needs": ["bills"],
                         "restatement": "upcoming bills"}, reply={"messages": []})
    twilio = RecordingTwilio(settings)
    app = create_app(settings, agent=Agent(llm, fixture_client(snapshot), "Margaret"),
                     twilio=twilio, executor=InlineExecutor())
    params = form("What bills do I have?")
    response = app.test_client().post("/whatsapp/inbound", data=params,
                                     headers={"X-Twilio-Signature": sign(params)})
    assert response.status_code == 200
    sent = " ".join(text for _, text in twilio.sent)
    assert "Austin Energy" in sent and "$120.00" in sent and "Oct 31" in sent
    assert "Water Co" in sent and "$45.25" in sent and "Spectrum" in sent


def test_cli_export_validate_facts_and_errors(tmp_path, capsys):
    output = tmp_path / "nested" / "scenario.json"
    main(["export", "--scenario", "wait_for_income", "--as-of", "2027-01-01", "--output", str(output)])
    assert json.loads(output.read_text())["expected"]["earliest_safe_date"] == "2027-01-07"
    capsys.readouterr()
    main(["facts", "--scenario", "safe_payment"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["facts"]["balance"]["amount"] == "$2,213.00"
    assert "expected" not in payload
    main(["validate"])
    assert "15 scenarios" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        main(["export", "--scenario", "not_a_case"])
    assert exc.value.code == 2


@pytest.mark.parametrize("bad_value", [-1, True, 1.5, "100"])
def test_catalog_rejects_invalid_money(monkeypatch, bad_value):
    from backend.demo import data
    original = data._read
    def read(name):
        result = original(name)
        if name == "profiles.json":
            result["profiles"][0]["balance_cents"] = bad_value
        return result
    monkeypatch.setattr(data, "_read", read)
    with pytest.raises(ValueError, match="cents"):
        validate_catalog()
