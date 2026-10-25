from backend.agent import reply
from backend.agent.loop import Agent
from tests.conftest import FakeLLM, FakeNessie


def intent(label, needs=(), conf=0.95, restatement=""):
    return {"intent": label, "confidence": conf, "needs": list(needs), "restatement": restatement}


def agent(llm, nessie):
    return Agent(llm, nessie, "Margaret")


def test_unclear_reasks_and_never_calls_nessie():
    nessie = FakeNessie()
    out = agent(FakeLLM(intent=intent("unclear", conf=0.2)), nessie).respond("asdkjh qwe")
    assert out == [reply.REASK]
    assert nessie.calls == []


def test_balance_answer_uses_nessie_balance():
    nessie = FakeNessie(balance=1500.5)
    llm = FakeLLM(intent=intent("question", ["balance"], restatement="checking balance"),
                  reply={"messages": ["You have $1,500.50 in checking."]})
    out = agent(llm, nessie).respond("What is my balance?")
    assert out == ["You have $1,500.50 in checking."]
    assert nessie.calls == ["resolve", "balance"]          # only what the question needs


def test_invented_amount_is_rejected_and_real_facts_are_sent():
    nessie = FakeNessie(balance=1500.5)
    llm = FakeLLM(intent=intent("question", ["balance"]),
                  reply={"messages": ["You have $9,999.00 in checking."]})
    out = agent(llm, nessie).respond("balance?")
    assert out == ["Your checking balance is $1,500.50."]
    assert all("9,999" not in m for m in out)


def test_invented_date_is_rejected():
    llm = FakeLLM(intent=intent("question", ["bills"]),
                  reply={"messages": ["Austin Energy, $120.00, due Nov 15, pending."]})
    out = agent(llm, FakeNessie()).respond("what bills do I have")
    assert "Nov 15" not in " ".join(out)
    assert "Oct 31" in " ".join(out)


def test_bills_list_has_payee_amount_date_status_including_austin_energy():
    llm = FakeLLM(intent=intent("question", ["bills"]), reply={"messages": []})  # forces fallback
    out = " ".join(agent(llm, FakeNessie()).respond("What bills do I have?"))
    for expected in ("Austin Energy", "$120.00", "Oct 31", "pending", "Water Co", "$45.25"):
        assert expected in out


def test_no_bills_is_stated_not_invented():
    llm = FakeLLM(intent=intent("question", ["bills"]), reply={"messages": []})
    out = agent(llm, FakeNessie(bills=[])).respond("bills?")
    assert out == ["I don't see any bills."]


def test_missing_balance_is_unknown_not_a_number():
    llm = FakeLLM(intent=intent("question", ["balance"]), reply={"messages": []})
    out = agent(llm, FakeNessie(balance=None)).respond("balance?")
    assert out == ["I can't see your balance right now."]
    assert "$" not in out[0]


def test_missing_customer_replies_cannot_see_and_does_not_create_anything():
    nessie = FakeNessie(customers=("Someone Else",))
    llm = FakeLLM(intent=intent("question", ["balance"]))
    out = agent(llm, nessie).respond("balance?")
    assert out == [reply.CANT_SEE_ACCOUNT]
    assert nessie.calls == ["resolve"]
    assert [c[0] for c in llm.calls] == ["steady_intent"]   # no reply-writer call


def test_pay_request_says_not_turned_on_and_never_touches_nessie():
    nessie = FakeNessie()
    out = agent(FakeLLM(intent=intent("pay_request")), nessie).respond("Pay Austin Energy")
    assert out == [reply.PAY_NOT_ON]
    assert "not turned on" in out[0]
    assert nessie.calls == []


def test_yes_no_undo_forwarded_are_recognised_but_unavailable():
    for label in ("yes", "no", "undo", "forwarded_message"):
        nessie = FakeNessie()
        out = agent(FakeLLM(intent=intent(label)), nessie).respond("whatever")
        assert out == [reply.NOT_AVAILABLE]
        assert nessie.calls == []


def test_no_write_methods_exist_on_the_nessie_client():
    from backend.nessie.client import NessieClient
    public = [n for n in dir(NessieClient) if not n.startswith("_")]
    assert not any(n.startswith(("post", "put", "delete", "create", "cancel", "pay", "execute"))
                   for n in public)


def test_loop_does_not_import_money_contracts():
    import backend.agent.loop as loop_mod
    assert "contracts" not in vars(loop_mod)
