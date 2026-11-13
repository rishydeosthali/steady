"""Every active savings goal receives a forecast even when the model waits."""
from tests.test_autonomy import make, activate, Reasoner, action


def test_wait_cannot_starve_budget_and_changed_cash_replans(tmp_path, settings):
    model = Reasoner([action("wait")])
    controller = make(tmp_path, settings, model)
    goal = activate(controller, weekly=0)
    controller.run()
    controller.flush()
    assert controller.memory.read()["reports"][goal["id"]]["status"] == "on_track"
    assert len(controller.twilio.sent) == 1
    model.actions = [action("wait")]
    controller.nessie.balance = 250
    controller.run()
    controller.flush()
    assert controller.memory.read()["reports"][goal["id"]]["status"] == "at_risk"
    assert "gap" in controller.twilio.sent[-1][1]
    assert len(controller.twilio.sent) == 2
    model.actions = [action("wait")]
    controller.run(force=True)
    controller.flush()
    assert len(controller.twilio.sent) == 2
    assert controller.memory.read()["goals"][goal["id"]]["target_cents"] == 30000


def test_step_limit_still_reports_budget(tmp_path, settings):
    model = Reasoner([action("refresh")])
    controller = make(tmp_path, settings, model)
    controller.max_steps = 1
    goal = activate(controller)
    controller.run()
    assert goal["id"] in controller.memory.read()["reports"]
    assert controller.memory.read()["outbox"]


def test_exact_referenced_payment_decision_does_not_depend_on_model(tmp_path, settings):
    from types import SimpleNamespace
    from backend.agent.loop import Agent
    from tests.conftest import FakeNessie
    class UnavailableModel:
        def json_completion(self, *args):
            raise AssertionError("An exact payment command must not need a model")
    calls = []
    payments = SimpleNamespace(handle=lambda *args: calls.append(args) or ["Cancelled"])
    agent = Agent(UnavailableModel(), FakeNessie(), "Margaret", payments=payments)
    assert agent.respond("NO 8FF09A1E", message_id="slack:EvCancel") == ["Cancelled"]
    assert calls == [("NO 8FF09A1E", "no", "slack:EvCancel")]
    agent.respond("YES 8FF09A1E", message_id="slack:EvYes")
    assert calls[-1][1] == "yes"
