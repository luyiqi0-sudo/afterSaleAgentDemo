from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.contracts import Principal, ToolResult
from agent.runtime_store import RuntimeStore, RuntimeConflict


ALICE = Principal(user_id="alice")


def approved(store, session="s", order="A", *, projected=True):
    run = store.start_run(session, ALICE, "initial", "help")
    intent = store.prepare(run["run_id"], run["active_invocation_id"], "action-1", "CONFIRM", {"order_id": order, "reason": "help", "user_id": "alice"})
    store.publish_interrupt(intent["intent_id"], run["active_invocation_id"], "cp", "fi")
    receipt = store.consume_resume(session, ALICE, intent["intent_id"], run["state_revision"], "approve", "yes", "APPROVE", "cp")
    if not projected:
        return run, intent, receipt, None
    store.project_receipt(receipt["receipt_id"], receipt["invocation_id"])
    grant = store.issue_grant(run["run_id"], receipt["invocation_id"], receipt["receipt_id"], ALICE)
    return run, intent, receipt, grant


def test_same_key_concurrent_claim_only_one_token(tmp_path):
    a, b = RuntimeStore(tmp_path / "r.db"), RuntimeStore(tmp_path / "r.db")
    _, _, _, ga = approved(a, "a")
    _, _, _, gb = approved(b, "b")
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda pair: pair[0].claim(pair[1]), [(a, ga), (b, gb)]))
    assert sum(r.token is not None for r in results) == 1
    assert sorted(r.result.error_code or "" for r in results) == ["ORDER_BUSY", "pending_reconciliation"]


def test_same_operation_never_gets_second_token_and_payload_frozen(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run, intent, receipt, grant = approved(store)
    first = store.claim(grant)
    assert first.token is not None
    assert store.claim(grant).token is None
    with pytest.raises(RuntimeConflict):
        store.prepare(run["run_id"], receipt["invocation_id"], "action-1", "CONFIRM", {"order_id": "B"})


def test_replay_is_read_only_and_principal_conflict_does_not_mutate(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run, intent, receipt, grant = approved(store)
    replay = store.consume_resume("s", ALICE, intent["intent_id"], 1, "approve", "yes", "APPROVE", "cp")
    assert replay["replay"] is True
    assert replay["receipt_id"] == receipt["receipt_id"]
    with pytest.raises(RuntimeConflict):
        store.consume_resume("s", ALICE, intent["intent_id"], 1, "approve", "changed", "APPROVE", "cp")
    with pytest.raises(RuntimeConflict):
        store.get_run(run["run_id"], Principal(user_id="bob"))
    assert store.get_run(run["run_id"], ALICE)["status"] == "LIVE"


def test_budget_idempotency_and_unknown_reservation_survive_restart(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    store.reserve_budget(run["run_id"], run["active_invocation_id"], "model-1", model_calls=1, input_tokens=8192, output_tokens=2048)
    store.reserve_budget(run["run_id"], run["active_invocation_id"], "model-1", model_calls=1, input_tokens=8192, output_tokens=2048)
    other = RuntimeStore(store.path)
    assert other.get_budget(run["run_id"])["output_tokens"] == 2048
    with pytest.raises(RuntimeConflict):
        other.reserve_budget(run["run_id"], run["active_invocation_id"], "too-big", input_tokens=8193)


def test_failed_commit_produces_no_token(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path / "r.db")
    _, _, _, grant = approved(store)
    def fail(_con):
        raise OSError("disk failed")
    monkeypatch.setattr(store, "_commit", fail)
    with pytest.raises(OSError):
        store.claim(grant)
    assert store.get_operation(grant.operation_id) is None


def test_session_helpers_and_verified_snapshot_cas(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    assert store.get_session("s", ALICE)["run_id"] == run["run_id"]
    assert store.list_runs("LIVE")[0]["run_id"] == run["run_id"]
    store.save_verified_snapshot(run["run_id"], run["active_invocation_id"], run["state_revision"], {"facts": ["verified"]})
    assert store.get_run(run["run_id"])["verified_snapshot"] == {"facts": ["verified"]}
    with pytest.raises(RuntimeConflict):
        store.save_verified_snapshot(run["run_id"], run["active_invocation_id"], 999, {})


def test_action_fingerprint_and_budget_survive_replay(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    store.reserve_action(run["run_id"], run["active_invocation_id"], "action-1", "query_order", {"order_id": "A"}, ["facts-v1"])
    with pytest.raises(RuntimeConflict, match="NO_PROGRESS"):
        store.reserve_action(run["run_id"], run["active_invocation_id"], "action-2", "query_order", {"order_id": "A"}, ["facts-v1"])
    assert store.get_budget(run["run_id"])["logical_actions"] == 1


def test_foreign_tenant_and_forged_grant_cannot_reuse_receipt(tmp_path):
    from dataclasses import replace
    store = RuntimeStore(tmp_path / "r.db")
    run, _, receipt, grant = approved(store)
    with pytest.raises(RuntimeConflict):
        store.issue_grant(run["run_id"], receipt["invocation_id"], receipt["receipt_id"], Principal(user_id="alice", tenant="other"))
    with pytest.raises(RuntimeConflict):
        store.claim(replace(grant))


def test_real_single_worker_process_lock(tmp_path):
    import subprocess
    import sys
    store = RuntimeStore(tmp_path / "r.db")
    store.acquire_worker()
    script = "from agent.runtime_store import RuntimeStore; import sys; RuntimeStore(sys.argv[1]).acquire_worker()"
    result = subprocess.run([sys.executable, "-c", script, str(store.path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert "WORKER_ALREADY_RUNNING" in result.stderr
    store.close()
    result = subprocess.run([sys.executable, "-c", script, str(store.path)], capture_output=True, text=True)
    assert result.returncode == 0


def test_replace_and_mixed_approval_preserve_task_budget_and_invalidate_intent(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    for session, decision, additions in [("replace", "REPLACE", []), ("mixed", "APPROVE", [{"subject": "B"}])]:
        run = store.start_run(session, ALICE, "m1", "help")
        intent = store.prepare(run["run_id"], run["active_invocation_id"], "a", "CONFIRM", {"order_id": "A", "reason": "help", "user_id": "alice"})
        store.reserve_budget(run["run_id"], run["active_invocation_id"], session + "budget", model_calls=1)
        store.publish_interrupt(intent["intent_id"], run["active_invocation_id"], "cp", "fi")
        receipt = store.consume_resume(session, ALICE, intent["intent_id"], 1, "m2", "message", decision, "cp", addition_intents=additions)
        assert store.get_intent(intent["intent_id"])["status"] == "SUPERSEDED"
        assert receipt["scope_epoch"] == (2 if decision == "REPLACE" else 1)
        assert receipt["decision"] == ("REPLACE" if decision == "REPLACE" else "AMBIGUOUS")
        assert store.get_budget(run["run_id"])["model_calls"] == 1
        with pytest.raises(RuntimeConflict):
            store.issue_grant(run["run_id"], receipt["invocation_id"], receipt["receipt_id"], ALICE)


def test_ttl_at_consume_and_send_no_refresh(tmp_path):
    now = [1000.0]
    store = RuntimeStore(tmp_path / "r.db", clock=lambda: now[0])
    run, intent, _, grant = approved(store)
    claim = store.claim(grant)
    now[0] = 2000.0
    with pytest.raises(RuntimeConflict, match="EXPIRED"):
        store.start_send(claim.token, ALICE)
    assert store.get_intent(intent["intent_id"])["expires_at"] == 1900.0
    store.recover_run(run["run_id"], executor_stopped=True)
    assert store.get_operation(intent["operation_id"])["result"]["error_code"] == "NOT_SENT"


def test_atomic_settlement_failure_preserves_occupancy(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path / "r.db")
    _, intent, _, grant = approved(store)
    store.start_send(store.claim(grant).token, ALICE)
    original = store._commit
    monkeypatch.setattr(store, "_commit", lambda _: (_ for _ in ()).throw(OSError("fail")))
    with pytest.raises(OSError):
        store.settle(intent["operation_id"], ToolResult(status="SUCCESS", data={"ticket_id": "T"}))
    monkeypatch.setattr(store, "_commit", original)
    _, _, _, other = approved(store, "other")
    assert store.claim(other).result.error_code == "ORDER_BUSY"
    assert store.get_operation(intent["operation_id"])["state"] == "IN_FLIGHT"


def test_legacy_unknown_without_identity_disables_new_writes(tmp_path):
    import sqlite3
    path = tmp_path / "r.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE operations(id TEXT PRIMARY KEY,payload_hash TEXT,result TEXT)")
        con.execute("INSERT INTO operations VALUES (?,?,?)", ("old", "oldhash", ToolResult(status="UNKNOWN").model_dump_json()))
    store = RuntimeStore(path)
    _, _, _, grant = approved(store)
    with pytest.raises(RuntimeConflict, match="LEGACY_RECONCILIATION_REQUIRED"):
        store.claim(grant)


def test_goal_revision_change_cannot_mint_grant_from_old_receipt(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run, _, receipt, _ = approved(store)
    store.advance_goals(run["run_id"], receipt["invocation_id"], "new-goal")
    with pytest.raises(RuntimeConflict, match="STALE_RECEIPT"):
        store.issue_grant(run["run_id"], receipt["invocation_id"], receipt["receipt_id"], ALICE)


def test_provider_usage_overflow_persistently_blocks_new_calls(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    store.reserve_budget(run["run_id"], run["active_invocation_id"], "call", model_calls=1, input_tokens=10, output_tokens=10)
    with pytest.raises(RuntimeConflict, match="PROVIDER_USAGE_EXCEEDED_RESERVATION"):
        store.settle_budget("call", {"prompt_tokens": 11, "completion_tokens": 1})
    with pytest.raises(RuntimeConflict, match="BUDGET_EXHAUSTED"):
        store.reserve_budget(run["run_id"], run["active_invocation_id"], "next", model_calls=1)


def test_preparation_survives_proposal_loss_without_early_intent_operation_or_ttl(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    candidate = {"order_id": "A", "user_id": "alice", "reason": "help"}
    prep = store.save_preparation(run["run_id"], run["active_invocation_id"], "action", "CONFIRM", candidate,
                                  ["g1"], object_refs=["verified-A"], sources=["message-m"])
    candidate.clear()
    durable = RuntimeStore(store.path).get_preparation(prep["prepare_request_id"])
    assert durable["payload"]["order_id"] == "A"
    assert durable["object_refs"] == ["verified-A"]
    assert durable["goal_ids"] == ["g1"]
    assert "expires_at" not in durable and "operation_id" not in durable and "intent_id" not in durable
    frozen = store.freeze_preparation(prep["prepare_request_id"], run["active_invocation_id"])
    replay = store.freeze_preparation(prep["prepare_request_id"], run["active_invocation_id"])
    assert frozen == replay
    assert frozen["operation_id"] and frozen["expires_at"] > frozen["issued_at"]


def test_preparation_source_replay_rejects_changed_goals_and_epoch(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    payload = {"order_id": "A", "user_id": "alice", "reason": "help"}
    prep = store.save_preparation(run["run_id"], run["active_invocation_id"], "a", "CONFIRM", payload, ["g1"])
    assert store.save_preparation(run["run_id"], run["active_invocation_id"], "a", "CONFIRM", payload, ["g1"]) == prep
    with pytest.raises(RuntimeConflict, match="PREPARATION_CONFLICT"):
        store.save_preparation(run["run_id"], run["active_invocation_id"], "a", "CONFIRM", payload, ["g2"])
    store.advance_goals(run["run_id"], run["active_invocation_id"], "replace", scope_changed=True)
    with pytest.raises(RuntimeConflict, match="PREPARATION_CONFLICT"):
        store.freeze_preparation(prep["prepare_request_id"], run["active_invocation_id"])


def test_old_epoch_intent_cannot_be_published(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    intent = store.prepare(run["run_id"], run["active_invocation_id"], "a", "CONFIRM", {"order_id": "A", "user_id": "alice", "reason": "help"})
    store.advance_goals(run["run_id"], run["active_invocation_id"], "changed", scope_changed=True)
    with pytest.raises(RuntimeConflict):
        store.publish_interrupt(intent["intent_id"], run["active_invocation_id"], "cp", "fi")
    assert store.get_intent(intent["intent_id"])["status"] == "UNPUBLISHED"


def test_recorded_receipt_cannot_get_grant_until_projected(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run, _, receipt, _ = approved(store, projected=False)
    with pytest.raises(RuntimeConflict, match="STALE_RECEIPT"):
        store.issue_grant(run["run_id"], receipt["invocation_id"], receipt["receipt_id"], ALICE)
    store.project_receipt(receipt["receipt_id"], receipt["invocation_id"])
    assert store.issue_grant(run["run_id"], receipt["invocation_id"], receipt["receipt_id"], ALICE)


def test_action_budget_event_cannot_fund_different_fingerprints(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    store.reserve_action(run["run_id"], run["active_invocation_id"], "one-event", "query_order", {"order_id": "A"}, [])
    with pytest.raises(RuntimeConflict, match="BUDGET_EVENT_CONFLICT"):
        store.reserve_action(run["run_id"], run["active_invocation_id"], "one-event", "query_order", {"order_id": "B"}, [])
    assert store.get_budget(run["run_id"])["logical_actions"] == 1


def test_every_connection_explicitly_closes_without_gc(tmp_path, monkeypatch):
    import sqlite3
    opened = []
    class Tracked(sqlite3.Connection):
        closed = False
        def close(self):
            self.closed = True
            return super().close()
    real = sqlite3.connect
    def connect(*args, **kwargs):
        connection = real(*args, **kwargs, factory=Tracked)
        opened.append(connection)
        return connection
    monkeypatch.setattr(sqlite3, "connect", connect)
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    store.get_run(run["run_id"])
    store.get_budget(run["run_id"])
    store.close()
    assert all(connection.closed for connection in opened)
    store.path.rename(tmp_path / "closed.sqlite")


def test_binding_version_corruption_blocks_consume_without_consuming_intent(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    intent = store.prepare(run["run_id"], run["active_invocation_id"], "a", "CONFIRM", {"order_id": "A", "user_id": "alice", "reason": "help"})
    store.publish_interrupt(intent["intent_id"], run["active_invocation_id"], "cp", "fi")
    # Simulate mismatched persisted cross-store mapping, not a client-authorized edit.
    with store._transaction() as con:
        binding = store._get(con, "binding", intent["intent_id"])
        binding["scope_epoch"] += 1
        store._put(con, "binding", intent["intent_id"], binding)
    with pytest.raises(RuntimeConflict):
        store.consume_resume("s", ALICE, intent["intent_id"], run["state_revision"], "m2", "yes", "APPROVE", "cp")
    assert store.get_intent(intent["intent_id"])["status"] == "PENDING"
    assert store.get_run(run["run_id"])["status"] == "WAITING"


def test_binding_checkpoint_change_after_projection_blocks_claim(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    _, intent, _, grant = approved(store)
    with store._transaction() as con:
        binding = store._get(con, "binding", intent["intent_id"])
        binding["framework_interrupt_id"] = "different"
        store._put(con, "binding", intent["intent_id"], binding)
    with pytest.raises(RuntimeConflict, match="STALE_RECEIPT"):
        store.claim(grant)
    assert store.get_operation(intent["operation_id"]) is None


def test_provider_budget_commit_rechecks_remaining_time(tmp_path, monkeypatch):
    from agent.budgets import BudgetScope
    import agent.budgets as budgets
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    now = [10.0]
    monkeypatch.setattr(budgets.time, "monotonic", lambda: now[0])
    scope = BudgetScope(store, run["run_id"], run["active_invocation_id"], deadline=14.0)
    original = store.reserve_budget
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] += 2.0
        return result
    monkeypatch.setattr(store, "reserve_budget", delayed)
    with pytest.raises(RuntimeConflict, match="BUDGET_EXHAUSTED"):
        scope("test-model", 10, 10)
    assert store.get_budget(run["run_id"])["model_calls"] == 1


def test_configured_runtime_wait_bounds_sqlite_and_process_mutex(tmp_path):
    import sqlite3
    import threading
    import time
    store = RuntimeStore(tmp_path / "r.db", busy_timeout_seconds=0.02)
    run = store.start_run("s", ALICE, "m", "help")
    with sqlite3.connect(store.path) as writer:
        writer.execute("BEGIN IMMEDIATE")
        before = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            store.reserve_budget(run["run_id"], run["active_invocation_id"], "locked", model_calls=1)
        assert time.monotonic() - before < 0.2
        writer.rollback()
    locked, release = threading.Event(), threading.Event()
    def holder():
        with store._mutex:
            locked.set()
            release.wait(1)
    thread = threading.Thread(target=holder)
    thread.start()
    locked.wait(1)
    try:
        before = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            store.reserve_budget(run["run_id"], run["active_invocation_id"], "mutex", model_calls=1)
        assert time.monotonic() - before < 0.2
    finally:
        release.set()
        thread.join(1)


def test_transaction_deadline_spans_begin_writer_and_commit_reader_wait(tmp_path):
    import sqlite3
    import threading
    import time
    store = RuntimeStore(tmp_path / "r.db", busy_timeout_seconds=0.25)
    run = store.start_run("s", ALICE, "m", "help")
    writer = sqlite3.connect(store.path, check_same_thread=False)
    reader = sqlite3.connect(store.path)
    writer.execute("BEGIN IMMEDIATE")
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM rt_records").fetchall()
    def release_writer():
        time.sleep(0.16)
        writer.rollback()
    thread = threading.Thread(target=release_writer)
    thread.start()
    try:
        before = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            store.reserve_budget(run["run_id"], run["active_invocation_id"], "busy", model_calls=1)
        elapsed = time.monotonic() - before
        assert elapsed < 0.35
        assert reader.in_transaction
    finally:
        thread.join(1)
        writer.close()
        reader.rollback()
        reader.close()
    assert store.get_budget(run["run_id"])["model_calls"] == 0


def test_lookup_message_checks_owner_hash_without_mutation(tmp_path):
    store = RuntimeStore(tmp_path / "r.db")
    run = store.start_run("s", ALICE, "m", "help")
    assert store.lookup_message("s", ALICE, "new", "anything") is None
    replay = store.lookup_message("s", ALICE, "m", "help")
    assert replay["replay"] and replay["run_id"] == run["run_id"]
    with pytest.raises(RuntimeConflict, match="MESSAGE_CONFLICT"):
        store.lookup_message("s", ALICE, "m", "changed")
    with pytest.raises(RuntimeConflict, match="SESSION_UNAVAILABLE"):
        store.lookup_message("s", Principal(user_id="bob"), "m", "help")
    assert store.get_budget(run["run_id"])["messages"] == 1
