"""TC-STATE component scenarios through the real graph and two SQLite files."""
import sqlite3
from copy import deepcopy

import pytest

from agent.controller import AgentController, ChatRequest
from agent.runtime_store import RuntimeStore
from agent.runtime_types import RuntimeConflict
from test_graph import ALICE, BOB, ask, approve, setup as setup


async def waiting(setup):
    c, model, _, _ = setup
    model.kind = "ticket"
    return await ask(c, "订单 A 建工单")


def run_for(c, result):
    session = c.store.get_session(result["session_id"], ALICE)
    return c.store.get_run(session["run_id"])


async def test_tc01_replace_invalidate_before_new_object_no_double_epoch(setup):
    c, model, _, tools = setup
    first = await waiting(setup)
    original = c.store.get_intent(first["interrupt"]["interrupt_id"])
    model.subject = "B"
    second = await approve(c, first, message="不是 A，是 B，帮我建工单")
    assert second["status"] == "WAITING_CONFIRM"
    assert c.store.get_intent(original["intent_id"])["status"] == "SUPERSEDED"
    updated = run_for(c, second)
    assert updated["scope_epoch"] == 2 and updated["goal_revision"] == 2
    assert second["interrupt"]["order_id"] == "B" and tools.posts == []
    assert c.store.get_intent(second["interrupt"]["interrupt_id"])["operation_id"] != original["operation_id"]
    assert c.store.get_operation(original["operation_id"]) is None


async def test_tc02_approval_replays_and_content_conflict(setup):
    c, _, _, tools = setup
    first = await waiting(setup)
    final = await approve(c, first)
    replay = await approve(c, first)
    assert replay == final and tools.posts == ["A"]
    with pytest.raises(RuntimeConflict, match="STALE_INTERRUPT"):
        await approve(c, first, mid="new-approval")
    with pytest.raises(RuntimeConflict, match="MESSAGE_CONFLICT"):
        await approve(c, first, message="取消")
    assert tools.posts == ["A"]


async def test_tc02_revoked_owner_after_receipt_before_gate(setup, monkeypatch):
    c, _, sql, tools = setup
    first = await waiting(setup)
    original = c.store.project_receipt
    def revoked(*args):
        sql.denied.add("A")
        return original(*args)
    monkeypatch.setattr(c.store, "project_receipt", revoked)
    result = await approve(c, first)
    assert result["status"] == "REJECTED" and tools.posts == []


async def test_tc03_intruder_preserves_rightful_waiting_and_can_resume(setup):
    c, _, _, tools = setup
    first = await waiting(setup)
    before = deepcopy(run_for(c, first))
    with pytest.raises(RuntimeConflict, match="SESSION_UNAVAILABLE"):
        await c.handle(BOB, ChatRequest(message="确认", client_message_id="b", session_id=first["session_id"],
                                      interrupt_id=first["interrupt"]["interrupt_id"], expected_revision=first["revision"]))
    assert run_for(c, first) == before
    assert (await approve(c, first))["status"] == "COMPLETED" and tools.posts == ["A"]


async def test_tc04_unknown_stops_task_and_holds_reservation(setup):
    c, _, _, tools = setup
    first = await waiting(setup)
    op = c.store.get_intent(first["interrupt"]["interrupt_id"])["operation_id"]
    tools.write_status = "UNKNOWN"
    result = await approve(c, first)
    assert result["status"] == "FAILED" and result["goals"][0]["unmet_reason"] == "UNKNOWN"
    assert c.store.get_operation(op)["state"] == "UNKNOWN"
    with sqlite3.connect(c.store.path) as con:
        assert con.execute("SELECT count(*) FROM rt_reservations").fetchone()[0] == 1
    assert tools.posts == ["A"] and "待核对" in result["answer"]


@pytest.mark.parametrize("error,reason", [("NO_DATA", "NO_DATA"), ("service_unavailable", "READ_EXHAUSTED")])
async def test_tc05_distinguish_empty_and_failure_no_cross_subject_amount(setup, error, reason):
    c, model, _, tools = setup
    first = await ask(c)
    assert first["status"] == "COMPLETED" and "人民币 888.00 元" in first["answer"]
    model.subject = "B"
    tools.read_error = error
    second = await ask(c, "查询订单 B 的金额", "m2", session_id=first["session_id"])
    assert second["goals"][0]["unmet_reason"] == reason
    assert "人民币 888.00 元" not in second["answer"] and tools.posts == []


async def test_tc06_repeat_same_facts_stops_before_second_get(setup):
    c, model, _, tools = setup
    model.kind = "other"  # read succeeds but does not satisfy this broader goal
    result = await ask(c)
    assert result["reason"] == "NO_PROGRESS" and tools.gets == [("order", "A")]
    assert result["status"] == "FAILED" and "人民币 888.00 元" in result["answer"]


async def test_tc07_reopen_saver_real_resume_preserves_frozen_intent(setup):
    c, model, sql, tools = setup
    first = await waiting(setup)
    intent = c.store.get_intent(first["interrupt"]["interrupt_id"])
    binding = c.store.get_binding(intent["intent_id"])
    await c.aclose()
    store = RuntimeStore(c.store.path)
    tools.store = store
    restarted = AgentController(store=store, checkpoint_path=c.checkpoint_path, model=model, sql=sql, tools=tools)
    await restarted.start()
    try:
        result = await approve(restarted, first)
        assert result["status"] == "COMPLETED" and tools.posts == ["A"]
        after = store.get_intent(intent["intent_id"])
        assert all(after[k] == intent[k] for k in ("operation_id", "payload_hash", "issued_at", "expires_at"))
        assert store.get_binding(intent["intent_id"]) == binding
        assert restarted.trace[:3] == ["confirm", "resume_gate", "gate"]
        assert "prepare_ticket" not in restarted.trace
    finally:
        await restarted.aclose()


async def test_tc08_ambiguous_invalidates_and_asks_without_post(setup):
    c, _, _, tools = setup
    first = await waiting(setup)
    result = await approve(c, first, message="先等等，建单后多久联系我？")
    assert result["status"] == "WAITING_INPUT" and tools.posts == []
    assert c.store.get_intent(first["interrupt"]["interrupt_id"])["status"] == "SUPERSEDED"
    assert run_for(c, result)["scope_epoch"] == 1


async def test_tc09_expired_confirm_creates_no_fake_operation(setup):
    c, _, _, tools = setup
    first = await waiting(setup)
    intent = c.store.get_intent(first["interrupt"]["interrupt_id"])
    c.store.clock = lambda: intent["expires_at"] + 1
    result = await approve(c, first)
    assert result["status"] == "FAILED" and result["goals"][0]["unmet_reason"] == "EXPIRED"
    assert c.store.get_intent(intent["intent_id"])["status"] == "EXPIRED"
    assert c.store.get_operation(intent["operation_id"]) is None and tools.posts == []


async def inject_consumed(c, first):
    run = run_for(c, first)
    intent_id = first["interrupt"]["interrupt_id"]
    binding = c.store.get_binding(intent_id)
    request = ChatRequest(message="确认", client_message_id="m2", session_id=first["session_id"],
                          interrupt_id=intent_id, expected_revision=first["revision"])
    receipt = c.store.consume_resume(first["session_id"], ALICE, intent_id, first["revision"], "m2",
                                     request.model_dump(exclude={"session_id", "client_message_id"}), "APPROVE", binding["checkpoint_id"])
    return run, receipt


@pytest.mark.parametrize("sent", [False, True])
async def test_tc10_recovery_reserved_vs_send_started(setup, sent):
    c, model, sql, tools = setup
    first = await waiting(setup)
    run, receipt = await inject_consumed(c, first)
    c.store.project_receipt(receipt["receipt_id"], receipt["invocation_id"])
    grant = c.store.issue_grant(run["run_id"], receipt["invocation_id"], receipt["receipt_id"], ALICE)
    claim = c.store.claim(grant)
    if sent:
        c.store.start_send(claim.token, ALICE)
    await c.aclose()
    store = RuntimeStore(c.store.path)
    tools.store = store
    restarted = AgentController(store=store, checkpoint_path=c.checkpoint_path, model=model, sql=sql, tools=tools)
    try:
        await restarted.start()
        operation = store.get_operation(grant.operation_id)
        assert operation["state"] == ("UNKNOWN" if sent else "FAILED")
        assert store.get_run(run["run_id"])["status"] == "CLOSED" and tools.posts == []
        with sqlite3.connect(store.path) as con:
            assert con.execute("SELECT count(*) FROM rt_reservations").fetchone()[0] == int(sent)
    finally:
        await restarted.aclose()


async def test_tc11_soft_node_budget_survives_wait_and_blocks_model(setup):
    c, model, _, tools = setup
    first = await waiting(setup)
    run = run_for(c, first)
    with c.store._transaction() as con:
        budget = c.store._get(con, "budget", run["run_id"])
        budget.update(business_nodes=58, model_calls=11)
        c.store._put(con, "budget", run["run_id"], budget)
    before = len(model.calls)
    result = await approve(c, first)
    assert result["request_status"] == "terminated" and result["reason"] == "BUDGET_EXHAUSTED"
    assert len(model.calls) == before and tools.posts == []
    assert c.store.get_budget(run["run_id"])["business_nodes"] >= 60


@pytest.mark.parametrize("proposal", [{"action_type": "execute_confirmed_ticket"},
    {"action_type": "request_ticket", "parameters": {"order_id": "A", "operation_id": "fake"}}])
async def test_tc12_forged_internal_authority_denied(setup, proposal):
    c, model, _, tools = setup
    model.actions = [proposal]
    result = await ask(c)
    assert result["status"] == "REJECTED" and tools.posts == [] and tools.gets == []
    assert c.store.list_operations(run_for(c, result)["run_id"]) == []


async def test_tc13_consumption_without_command_restart_tombstones(setup):
    c, model, sql, tools = setup
    first = await waiting(setup)
    run, receipt = await inject_consumed(c, first)
    await c.aclose()
    store = RuntimeStore(c.store.path)
    tools.store = store
    restarted = AgentController(store=store, checkpoint_path=c.checkpoint_path, model=model, sql=sql, tools=tools)
    try:
        await restarted.start()
        result = await approve(restarted, first)
        assert result["request_status"] == "terminated" and "尚未发送" in result["answer"]
        assert store.get_receipt(receipt["receipt_id"])["settlement_reason"] == "NOT_SENT"
        assert store.get_run(run["run_id"])["status"] == "CLOSED"
        assert tools.posts == [] and store.list_operations(run["run_id"]) == []
    finally:
        await restarted.aclose()


async def test_tc14_unknown_allows_new_read_but_blocks_same_write_key(setup):
    c, model, _, tools = setup
    first = await waiting(setup)
    # Another session obtained a confirmation before the first claim. That
    # confirmation still cannot bypass the store's atomic reservation check.
    earlier_wait = await ask(c, "订单 A 建工单", "earlier")
    tools.write_status = "UNKNOWN"
    initial = await approve(c, first)
    model.kind = "logistics"
    result = await ask(c, "查询订单 A 的物流", "m3", session_id=first["session_id"])
    assert result["status"] == "COMPLETED"
    assert tools.gets == [("order", "A"), ("logistics", "A")]
    model.kind = "ticket"
    denied = await ask(c, "订单 A 建工单", "m4", session_id=first["session_id"])
    assert denied["interrupt"] is None and denied["operation_refs"] == initial["operation_refs"]
    assert denied["goals"][0]["unmet_reason"] == "ORDER_BUSY"
    old_approval = await approve(c, earlier_wait, "m5")
    assert old_approval["goals"][0]["unmet_reason"] == "ORDER_BUSY" and tools.posts == ["A"]


async def test_tc15_mixed_approval_retains_addition_and_no_epoch_change(setup):
    c, _, _, tools = setup
    first = await waiting(setup)
    result = await approve(c, first, message="确认 A，同时也查 B", decision="APPROVE")
    run = run_for(c, result)
    assert result["status"] == "WAITING_INPUT" and run["goal_revision"] == run["scope_epoch"] == 1
    snapshot = await c.graph.aget_state(c._config(run))
    assert snapshot.values["pending_addition_intents"][0]["requested_subject"] == "B"
    assert snapshot.values["goals"][0]["subject"] == "A" and tools.posts == []
    assert c.store.get_intent(first["interrupt"]["interrupt_id"])["status"] == "SUPERSEDED"


async def test_tc15_supply_adds_b_once_and_requires_fresh_a_confirmation(setup):
    c, model, _, tools = setup
    first = await waiting(setup)
    ambiguous = await approve(c, first, message="确认 A，同时也查 B")
    async def understand(question, context, *, budget=None):
        if budget:
            budget("understand_goal", 100, 100)
        return {"goals": [{"id": "g1", "description": "订单 A 建工单", "kind": "ticket", "subject_candidate": "A",
                            "source_span": {"start": question.index("A"), "end": question.index("A") + 1}},
                           {"id": "g2", "description": "查询订单 B", "kind": "order", "subject_candidate": "B",
                            "source_span": {"start": question.index("B"), "end": question.index("B") + 1}}]}
    model.understand_goal = understand
    next_wait = await approve(c, ambiguous, mid="m3", message="都保留", decision="SUPPLY")
    run = run_for(c, next_wait)
    assert next_wait["status"] == "WAITING_CONFIRM"
    assert run["scope_epoch"] == 1 and run["goal_revision"] == 2
    assert {g["subject"] for g in next_wait["goals"]} == {"A", "B"}
    assert next_wait["interrupt"]["interrupt_id"] != first["interrupt"]["interrupt_id"]
    assert tools.posts == []


async def test_cancel_marks_goal_cancelled_and_no_ledger(setup):
    c, _, _, tools = setup
    first = await waiting(setup)
    result = await approve(c, first, message="取消")
    assert result["status"] == "CANCELLED" and tools.posts == []
    assert c.store.list_operations(run_for(c, result)["run_id"]) == []


async def test_checkpoint_failure_after_prepare_uses_outer_finalize(setup, monkeypatch):
    c, model, _, tools = setup
    model.kind = "ticket"
    original = c.graph.checkpointer.aput
    count = 0
    async def fail(*args, **kwargs):
        nonlocal count
        count += 1
        if count >= 5:
            raise sqlite3.OperationalError("checkpoint unavailable")
        return await original(*args, **kwargs)
    monkeypatch.setattr(c.graph.checkpointer, "aput", fail)
    events = []
    result = await c.handle(ALICE, ChatRequest(message="订单 A 建工单", client_message_id="m1"), lambda e, d: events.append(e))
    assert result["request_status"] == "terminated" and tools.posts == []
    assert events.count("done") == 1 and run_for(c, result)["status"] == "CLOSED"


async def test_expired_waiting_after_restart_does_not_replay_old_waiting_result(setup):
    c, model, sql, tools = setup
    first = await waiting(setup)
    run = run_for(c, first)
    await c.aclose()
    store = RuntimeStore(c.store.path, clock=lambda: run["expires_at"] + 1)
    tools.store = store
    restarted = AgentController(store=store, checkpoint_path=c.checkpoint_path, model=model, sql=sql, tools=tools)
    try:
        await restarted.start()
        result = await ask(restarted, "订单 A 建工单", session_id=first["session_id"])
        assert result["status"] == "EXPIRED" and result["interrupt"] is None
    finally:
        await restarted.aclose()


async def test_shutdown_keeps_worker_lock_until_real_settlement_stopped(setup):
    c, _, _, tools = setup
    stopped = False
    async def close(*, timeout):
        return stopped
    tools.aclose = close
    assert await c.aclose() is False
    contender = RuntimeStore(c.store.path)
    with pytest.raises(RuntimeConflict, match="WORKER_ALREADY_RUNNING"):
        contender.acquire_worker()
    with pytest.raises(RuntimeConflict, match="SERVICE_STOPPING"):
        await ask(c)
    stopped = True
    assert await c.aclose() is True
    contender.acquire_worker()
    contender.close()


async def test_outer_finalize_real_sqlite_lock_is_bounded_and_cleanup_cannot_send(setup):
    import asyncio
    import time
    c, _, _, tools = setup
    first = await waiting(setup)
    run, receipt = await inject_consumed(c, first)
    run = c.store.get_run(run["run_id"])
    connection = sqlite3.connect(c.store.path)
    connection.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        result = await c._recover(run, "PERSISTENCE_FAILURE")
        elapsed = time.monotonic() - started
        assert elapsed < 2.5 and result["persisted"] is False
        assert tools.posts == []
        assert await c.aclose() is False
    finally:
        connection.rollback()
        connection.close()
    # The actual cleanup Future finishes after releasing the real external lock.
    for future in c._cleanup_futures:
        await asyncio.wrap_future(future)
    assert c.store.get_run(run["run_id"])["status"] == "CLOSED"
    assert c.store.list_operations(run["run_id"]) == [] and tools.posts == []
    assert await c.aclose() is True


def resume_request(first, *, message="确认", mid="m2", **changes):
    fields = {"message": message, "client_message_id": mid, "session_id": first["session_id"],
              "interrupt_id": first["interrupt"]["interrupt_id"], "expected_revision": first["revision"]}
    return ChatRequest(**(fields | changes))


def control_rows(c):
    with sqlite3.connect(c.store.path) as connection:
        return connection.execute("SELECT kind,id,data FROM rt_records ORDER BY kind,id").fetchall()


@pytest.mark.parametrize("change", [{"expected_revision": 999}, {"interrupt_id": "unknown-intent"}])
async def test_preflight_new_stale_resume_rejected_without_mutation(setup, change):
    c, _, _, tools = setup
    first = await waiting(setup)
    before = control_rows(c)
    with pytest.raises(RuntimeConflict, match="STALE_INTERRUPT"):
        await c.preflight(ALICE, resume_request(first, **change))
    assert control_rows(c) == before and tools.posts == []


async def test_preflight_active_new_message_rejected_without_mutation(setup):
    c, _, _, _ = setup
    first = await waiting(setup)
    before = control_rows(c)
    with pytest.raises(RuntimeConflict, match="RUN_BUSY"):
        await c.preflight(ALICE, ChatRequest(message="查询订单 B", client_message_id="new", session_id=first["session_id"]))
    assert control_rows(c) == before


async def test_preflight_replay_hash_priority_and_consumed_new_message(setup):
    c, _, _, tools = setup
    first = await waiting(setup)
    result = await approve(c, first)
    before = control_rows(c)
    await c.preflight(ALICE, resume_request(first))  # consumed + old revision, but exact replay
    with pytest.raises(RuntimeConflict, match="MESSAGE_CONFLICT"):
        await c.preflight(ALICE, resume_request(first, message="取消"))
    with pytest.raises(RuntimeConflict, match="STALE_INTERRUPT"):
        await c.preflight(ALICE, resume_request(first, mid="new-approval"))
    assert control_rows(c) == before and tools.posts == ["A"]
    assert await c.handle(ALICE, resume_request(first)) == result


async def test_preflight_unowned_session_hides_message_conflict(setup):
    c, _, _, _ = setup
    first = await waiting(setup)
    before = control_rows(c)
    with pytest.raises(RuntimeConflict, match="SESSION_UNAVAILABLE"):
        await c.preflight(BOB, resume_request(first, mid="m1", message="different"))
    assert control_rows(c) == before


async def test_preflight_valid_resume_readonly_and_handle_consumes_exactly_once(setup, monkeypatch):
    c, _, _, tools = setup
    first = await waiting(setup)
    before = control_rows(c)
    calls = []
    original = c.store.consume_resume
    def counted(*args, **kwargs):
        calls.append("consume")
        return original(*args, **kwargs)
    monkeypatch.setattr(c.store, "consume_resume", counted)
    await c.preflight(ALICE, resume_request(first))
    assert control_rows(c) == before and calls == []
    await c.handle(ALICE, resume_request(first))
    assert calls == ["consume"] and tools.posts == ["A"]


async def test_preflight_approved_replay_after_new_task_uses_original_result(setup):
    c, model, _, tools = setup
    first = await waiting(setup)
    original = await approve(c, first)
    model.kind = "order"
    current = await ask(c, "查询订单 A", "m3", session_id=first["session_id"])
    assert current["task_id"] != original["task_id"]
    before = control_rows(c)
    await c.preflight(ALICE, resume_request(first))
    replay = await c.handle(ALICE, resume_request(first))
    assert replay == original and tools.posts == ["A"]
    assert control_rows(c) == before


async def test_preflight_published_waiting_replay_without_delivery_record_is_readonly(setup):
    c, _, _, tools = setup
    first = await waiting(setup)
    run = run_for(c, first)
    with c.store._transaction() as connection:
        connection.execute("DELETE FROM rt_records WHERE kind='request_result' AND id=?", (run["request_id"],))
    before = control_rows(c)
    request = ChatRequest(message="订单 A 建工单", client_message_id="m1", session_id=first["session_id"])
    await c.preflight(ALICE, request)
    replay = await c.handle(ALICE, request)
    assert replay == first and tools.posts == [] and control_rows(c) == before
