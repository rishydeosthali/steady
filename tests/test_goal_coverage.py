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
