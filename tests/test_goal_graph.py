"""Real SQLite checkpoints, restarts and financial replay boundaries."""
import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from backend.autonomy.controller import Controller
from backend.autonomy.graph import GoalGraph, initial
from backend.autonomy.memory import Memory
from tests.test_autonomy import action, Reasoner
from tests.test_budget_actions import system, confirm, change


def restart(runtime, controller, model):
    return Controller(Memory(controller.memory.path), model, runtime.bank, runtime.twilio,
                      controller.recipient, payments=runtime.payments, actions=runtime.budget,
                      lock=runtime.lock, source=controller.source)


def checkpoint(controller):
    runner = GoalGraph(controller)
    with SqliteSaver.from_conn_string(runner.path) as saver:
        return runner.build(saver).get_state(runner.config)


def pause_at(controller, *, before=None, after=None):
    runner = GoalGraph(controller)
    with SqliteSaver.from_conn_string(runner.path) as saver:
        graph = runner.build(saver)
        graph.invoke(initial(True), runner.config, durability="sync",
                     interrupt_before=before, interrupt_after=after)
        return graph.get_state(runner.config)


def test_restart_before_execution_resumes_saved_action_without_reasking_model(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Income")
    model.actions = [action("allocate_savings", "car")]
    saved = pause_at(controller, before=["execute_action"])
    assert saved.next == ("execute_action",)
    assert saved.values["action"]["id"] == "car"
    assert runtime.store.state()["savings_pots"] == {"car":0, "emergency":0}
    next_model = Reasoner([action("allocate_savings", "emergency"), action("wait")])
    restarted = restart(runtime, controller, next_model)
    restarted.run()
    assert next_model.contexts[0]["results"][0]["result"]["allocated_cents"] == 10000
    assert runtime.store.state()["savings_pots"] == {"car":10000, "emergency":5000}
    assert not checkpoint(restarted).next
    assert any(e["kind"] == "graph_resumed" for e in restarted.memory.read()["events"])


def test_restart_after_checkpointed_allocation_does_not_execute_it_again(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Income")
    model.actions = [action("allocate_savings", "car")]
    saved = pause_at(controller, after=["execute_action"])
    assert saved.next == ("evaluate_result",)
    assert saved.values["results"][0]["result"]["allocated_cents"] == 10000
    next_model = Reasoner([action("allocate_savings", "emergency"), action("wait")])
    restarted = restart(runtime, controller, next_model)
    restarted.run()
    state = runtime.store.state()
    assert state["savings_pots"] == {"car":10000, "emergency":5000}
    assert len(state["savings_allocations"]) == 2
    assert any(e["kind"] == "graph_resumed" for e in restarted.memory.read()["events"])
    restarted.run()
    assert len(runtime.store.state()["savings_allocations"]) == 2


def test_crash_after_ledger_commit_before_checkpoint_reobserves_without_duplicate(system, monkeypatch):
    runtime, controller, model = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Income")
    model.actions = [action("allocate_savings", "car")]
    class Crash(BaseException):
        pass
    original = controller._tool
    def crash_after_commit(tool, *args):
        result = original(tool, *args)
        if tool == "allocate_savings":
            raise Crash()
        return result
    monkeypatch.setattr(controller, "_tool", crash_after_commit)
    with pytest.raises(Crash):
        controller.run(force=True)
    assert checkpoint(controller).next == ("execute_action",)
    assert runtime.store.state()["savings_pots"]["car"] == 10000
    next_model = Reasoner([action("allocate_savings", "emergency"), action("wait")])
    restarted = restart(runtime, controller, next_model)
    restarted.run()
    state = runtime.store.state()
    assert state["savings_pots"] == {"car":10000, "emergency":5000}
    assert len(state["savings_allocations"]) == 2
    assert any(e["kind"] == "graph_superseded" for e in restarted.memory.read()["events"])
    assert state["balance_cents"] + sum(state["savings_pots"].values()) == 321300


def test_paused_policy_invalidates_an_already_selected_allocation(system):
    runtime, controller, model = system
    ref = confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Income")
    model.actions = [action("allocate_savings", "car")]
    pause_at(controller, before=["execute_action"])
    controller.handle("PAUSE PLAN " + ref, "pause-during-restart")
    restarted = restart(runtime, controller, Reasoner([action("wait")]))
    restarted.run()
    assert runtime.store.state()["savings_pots"] == {"car":0, "emergency":0}
    assert not runtime.store.state().get("savings_allocations")
    assert any(e["kind"] == "graph_superseded" for e in restarted.memory.read()["events"])


def test_new_expense_invalidates_pending_action_and_enforces_current_reserves(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Income")
    model.actions = [action("allocate_savings", "car")]
    pause_at(controller, before=["execute_action"])
    change(runtime, "expense", amount="3000", merchant="Unexpected expense", category="other")
    restarted = restart(runtime, controller, Reasoner([action("allocate_savings", "car"),
                      action("allocate_savings", "emergency"), action("wait")]))
    restarted.run()
    assert runtime.store.state()["savings_pots"] == {"car":0, "emergency":0}
    assert any(e["kind"] == "graph_superseded" for e in restarted.memory.read()["events"])


def test_graph_exposes_explicit_nodes_and_persists_structured_results(system):
    runtime, controller, model = system
    confirm(runtime, controller)
    model.actions = [action("wait")]
    controller.run()
    runner = GoalGraph(controller)
    with SqliteSaver.from_conn_string(runner.path) as saver:
        graph = runner.build(saver)
        assert {"observe", "assess_due", "plan_strategy", "choose_action", "execute_action",
                "evaluate_result", "report"} <= set(graph.get_graph().nodes)
        saved = graph.get_state(runner.config)
        assert saved.values["results"][-1]["tool"] == "wait"
        assert saved.values["cycle_id"]
        assert len(list(graph.get_state_history(runner.config))) >= 8
        assert not saved.next


def test_separate_python_process_resumes_checkpoint_without_duplicate_allocation(system):
    import json
    import subprocess
    import sys
    from pathlib import Path
    runtime, controller, model = system
    confirm(runtime, controller)
    change(runtime, "income", amount="1000", description="Income")
    model.actions = [action("allocate_savings", "car")]
    pause_at(controller, after=["execute_action"])
    program = '''
import json,sys
from pathlib import Path
from backend.autonomy.controller import Controller
from backend.autonomy.memory import Memory
from backend.simulation.runtime import SimulationRuntime
from tests.conftest import settings
from tests.test_app import RecordingTwilio
from tests.test_external_biller import FakeMailer
from tests.test_autonomy import Reasoner,action
root=Path(sys.argv[1]); config=settings.__wrapped__(); messenger=RecordingTwilio(config)
runtime=SimulationRuntime(config,messenger,root,mailer=FakeMailer(),today="2026-11-12")
controller=Controller(Memory(root/"goals.sqlite"),Reasoner([action("allocate_savings","emergency"),action("wait")]),
    runtime.bank,messenger,config.recipient,payments=runtime.payments,actions=runtime.budget,
    lock=runtime.lock,source="local simulation")
controller.run()
state=runtime.store.state()
assert any(e["kind"]=="graph_resumed" for e in controller.memory.read()["events"])
print(json.dumps(dict(pots=state["savings_pots"],receipts=len(state["savings_allocations"]))))
runtime.stop(); runtime.watcher.ledger.close(); runtime.audit._db.close()
'''
    result = subprocess.run([sys.executable, "-c", program, str(runtime.directory)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(pots={"car":10000, "emergency":5000}, receipts=2)
