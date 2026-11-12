"""Conversational goal intake and production worker lifecycle, using fakes."""
from types import SimpleNamespace
from unittest.mock import Mock

from tests.test_autonomy import make, Reasoner


def extraction(**fields):
    return {
        **dict(target="", deadline="", buffer="", weekly_essentials=""), **fields}


def test_partial_goal_survives_restart_and_short_amount_answer(tmp_path, settings):
    model = Reasoner(goal=extraction())
    controller = make(tmp_path, settings, model)
    assert "total amount" in controller.handle("My goal is to buy a car", "start")[0]
    assert controller.memory.read()["intake"]
    restarted = make(tmp_path, settings, Reasoner(goal=extraction(target="22 k")))
    response = restarted.handle("around like 22 k", "amount")[0]
    assert "$22,000.00" in response and "what date" in response
    assert not restarted.memory.read()["goals"]
    assert restarted.handle("around like 22 k", "amount") == []
    assert restarted.handle("yes", "payment-answer") is None
    assert restarted.handle("what is my balance?", "balance") is None


def test_intake_collects_missing_fields_then_requires_exact_confirmation(tmp_path, settings):
    model = Reasoner(goal=extraction(target="$300"))
    controller = make(tmp_path, settings, model)
    controller.handle("My savings goal is $300", "start")
    model.goal = extraction(target="$300", deadline="2026-12-25")
    assert "each week" in controller.handle("by 2026-12-25", "deadline")[0]
    assert not controller.memory.read()["drafts"]
    model.goal["weekly_essentials"] = "$100"
    response = controller.handle("$100 weekly for essentials", "essentials")[0]
    assert "CONFIRM GOAL" in response
    state = controller.memory.read()
    assert not state["goals"] and not state["intake"]
    ref = next(iter(state["drafts"]))
    assert "$300.00" in controller.handle("What is my savings goal?", "recall")[0]
    assert controller.handle("yes", "bare-yes") is None
    controller.handle("CONFIRM GOAL " + ref, "confirmed")
    assert controller.memory.read()["goals"][ref]["status"] == "active"


def test_unsubstantiated_followup_cannot_invent_a_target(tmp_path, settings):
    model = Reasoner(goal=extraction())
    controller = make(tmp_path, settings, model)
    controller.handle("My goal is to buy a car", "start")
    model.goal = extraction(target="$22000", deadline="2026-12-25", weekly_essentials="$100")
    controller.handle("around like 22 k", "amount")
    assert not controller.memory.read()["drafts"]
    assert not controller.memory.read()["goals"]


def test_sentence_punctuation_is_not_rejected_as_part_of_money(tmp_path, settings):
    controller = make(tmp_path, settings, Reasoner(goal=extraction(target="$300")))
    result = controller.handle("My goal is to save $300.", "period")[0]
    assert "$300.00" in result and "what date" in result


def test_amount_cannot_be_extracted_from_part_of_a_decimal(tmp_path, settings):
    controller = make(tmp_path, settings, Reasoner(goal=extraction(target="$300")))
    result = controller.handle("My goal is to save $300.50", "decimal")[0]
    assert "what date" not in result
    assert not controller.memory.read()["drafts"]


def test_real_model_date_phrase_with_by_is_normalized_after_grounding(tmp_path, settings):
    # Observed extraction from the actual model on this fictional message.
    model = Reasoner(goal=extraction(target="$300", deadline="by December 25, 2026",
                                    buffer="$200", weekly_essentials="$100"))
    controller = make(tmp_path, settings, model)
    response = controller.handle("Fictional sandbox test: my goal is to save $300 by December 25, 2026, "
                                 "keep a $200 cushion, and allow $100 weekly for essentials.", "full")[0]
    assert "CONFIRM GOAL" in response
    assert next(iter(controller.memory.read()["drafts"].values()))["deadline"] == "2026-12-25"


def test_hosted_starts_and_stops_goal_worker(monkeypatch, settings):
    from backend import app, hosted, config
    from backend.agent import audit
    from backend.autonomy import controller
    import waitress

    runtime = Mock()
    application = SimpleNamespace(extensions={"simulation": runtime, "autonomy": object()})
    goals, idle = Mock(), Mock()
    factory = Mock(return_value=goals)
    monkeypatch.setenv("STEADY_SIMULATION", "1")
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(app, "create_app", lambda _: application)
    monkeypatch.setattr(controller, "GoalWorker", factory)
    monkeypatch.setattr(audit, "IdleWorker", Mock(return_value=idle))

    def serve(*args, **kwargs):
        goals.start.assert_called_once()
        runtime.start.assert_called_once()

    monkeypatch.setattr(waitress, "serve", serve)
    hosted.main()
    factory.assert_called_once_with(application.extensions["autonomy"])
    goals.stop.assert_called_once()
    runtime.stop.assert_called_once()


def test_simulation_goal_memory_uses_account_directory(monkeypatch, tmp_path, settings):
    from backend.app import create_app
    from tests.conftest import FakeLLM
    from tests.test_app import InlineExecutor, RecordingTwilio
    from tests.test_external_biller import FakeMailer

    monkeypatch.setenv("STEADY_SIMULATION", "1")
    monkeypatch.setenv("STEADY_EXTERNAL_BILLER", "0")
    monkeypatch.setenv("STEADY_AUTONOMY", "1")
    monkeypatch.setenv("STEADY_SIMULATION_DIR", str(tmp_path))
    monkeypatch.delenv("STEADY_GOAL_DB", raising=False)
    monkeypatch.setattr("backend.app.LLM", lambda *_: FakeLLM())
    monkeypatch.setattr("backend.payments.integration.build_mailer", lambda _: FakeMailer())
    application = create_app(settings, twilio=RecordingTwilio(settings), executor=InlineExecutor())
    assert application.extensions["autonomy"].memory.path == str(tmp_path / "goals.sqlite")
