import pytest

from agent.contracts import ToolResult
from agent.runtime_store import RuntimeStore, RuntimeConflict
from test_runtime_store import ALICE, approved


def test_consumed_before_projection_closes_without_fake_operation(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run, intent, receipt, grant = approved(store)
    store.recover_run(run["run_id"], executor_stopped=True)
    assert store.get_run(run["run_id"], ALICE)["status"] == "CLOSED"
    assert store.get_receipt(receipt["receipt_id"])["settlement_reason"] == "NOT_SENT"
    assert store.get_operation(intent["operation_id"]) is None
    with pytest.raises(RuntimeConflict):
        store.claim(grant)


def test_reserved_requires_stopped_proof_and_release_is_atomic(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run, intent, _, grant = approved(store)
    claim = store.claim(grant)
    with pytest.raises(RuntimeConflict):
        store.recover_run(run["run_id"], executor_stopped=False)
    store.recover_run(run["run_id"], executor_stopped=True)
    assert store.get_operation(intent["operation_id"])["result"]["error_code"] == "NOT_SENT"
    _, _, _, next_grant = approved(store, "next")
    assert store.claim(next_grant).token is not None
    with pytest.raises(RuntimeConflict):
        store.start_send(claim.token, ALICE)


def test_send_started_unknown_holds_across_instances(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run, intent, _, grant = approved(store)
    claim = store.claim(grant)
    store.start_send(claim.token, ALICE)
    store.recover_run(run["run_id"], executor_stopped=True)
    other = RuntimeStore(store.path)
    _, _, _, next_grant = approved(other, "next")
    assert other.claim(next_grant).result.error_code == "ORDER_BUSY"
    assert other.get_operation(intent["operation_id"])["state"] == "UNKNOWN"


def test_success_release_and_settlement_never_overwrites_terminal_result(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    _, intent, _, grant = approved(store)
    claim = store.claim(grant)
    store.start_send(claim.token, ALICE)
    store.settle(intent["operation_id"], ToolResult(status="SUCCESS", data={"ticket_id": "T"}))
    store.settle(intent["operation_id"], ToolResult(status="UNKNOWN"))
    assert store.get_operation(intent["operation_id"])["state"] == "SUCCESS"
    _, _, _, next_grant = approved(store, "next")
    assert store.claim(next_grant).token is not None


def test_remote_rejection_recovery_not_misreported_unsent_and_repeat_is_stable(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run, intent, receipt, grant = approved(store)
    store.start_send(store.claim(grant).token, ALICE)
    store.settle(intent["operation_id"], ToolResult(status="FAILED", error_code="service_rejected", attempts=1))
    store.recover_run(run["run_id"], executor_stopped=True)
    assert store.get_receipt(receipt["receipt_id"])["settlement_reason"] == "FAILED"
    store.recover_run(run["run_id"], executor_stopped=True, reason="ANOTHER_REASON")
    assert store.get_receipt(receipt["receipt_id"])["settlement_reason"] == "FAILED"


def test_crash_unknown_retains_actual_send_attempt_count(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run, intent, _, grant = approved(store)
    store.start_send(store.claim(grant).token, ALICE)
    store.recover_run(run["run_id"], executor_stopped=True)
    assert store.get_operation(intent["operation_id"])["result"]["attempts"] == 1


def test_supplied_clarification_does_not_inherit_later_unknown_write(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m1", "help")
    clarification = store.prepare(run["run_id"], run["active_invocation_id"], "ask", "CLARIFY", {"question": "which order?"})
    store.publish_interrupt(clarification["intent_id"], run["active_invocation_id"], "cp1", "fi1")
    supply = store.consume_resume("s", ALICE, clarification["intent_id"], 1, "m2", "A", "SUPPLY", "cp1")
    store.project_receipt(supply["receipt_id"], supply["invocation_id"])
    intent = store.prepare(run["run_id"], supply["invocation_id"], "write", "CONFIRM", {"order_id": "A", "user_id": "alice", "reason": "help"})
    store.publish_interrupt(intent["intent_id"], supply["invocation_id"], "cp2", "fi2")
    approval = store.consume_resume("s", ALICE, intent["intent_id"], 2, "m3", "yes", "APPROVE", "cp2")
    store.project_receipt(approval["receipt_id"], approval["invocation_id"])
    grant = store.issue_grant(run["run_id"], approval["invocation_id"], approval["receipt_id"], ALICE)
    store.start_send(store.claim(grant).token, ALICE)
    store.recover_run(run["run_id"], executor_stopped=True)
    assert store.get_receipt(supply["receipt_id"])["settlement_reason"] == "SUPPLY_APPLIED"
    assert store.get_receipt(approval["receipt_id"])["settlement_reason"] == "UNKNOWN"
    store.recover_run(run["run_id"], executor_stopped=True)
    assert store.get_receipt(supply["receipt_id"])["settlement_reason"] == "SUPPLY_APPLIED"
