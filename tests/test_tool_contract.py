import sqlite3

import httpx
import pytest

from agent.contracts import Principal
from agent.runtime_store import RuntimeStore
from agent.snapshot import SnapshotVerifier
from agent.tools import BusinessTools
from test_runtime_store import approved
from test_mock_http import DATASET, TEST_TOKEN, formal_freeze, launch as launch, order as order, ready, ticket_rows  # noqa: F401


class Owns:
    def owns_order(self, order_id, principal):
        return True


def strict_tools(tmp_path, handler):
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    verifier = SnapshotVerifier("data/c004-r01/baseline")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock")
    return BusinessTools(Owns(), store, client, snapshot_verifier=verifier, service_token="unit-token", strict=True, backoff=0), store


async def test_strict_read_uses_authenticated_headers_and_snapshot(tmp_path):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer unit-token"
        assert request.headers["X-Actor-ID"] == "alice"
        return httpx.Response(200, json={"order_id": "A", "status": 1, "amount": True})
    tools, _ = strict_tools(tmp_path, handler)
    result = await tools.query_order("A", Principal(user_id="alice"))
    assert result.error_code == "RESPONSE_MISMATCH"


async def test_strict_client_cannot_forge_confirmed_grant(tmp_path):
    calls = []
    tools, _ = strict_tools(tmp_path, lambda request: calls.append(request) or httpx.Response(201))
    result = await tools.create_ticket("A", "help", "op", Principal(user_id="alice"))
    assert result.error_code == "CONFIRMATION_REQUIRED"
    assert calls == []


async def test_failed_send_marker_commit_means_zero_http(tmp_path, monkeypatch):
    calls = []
    tools, store = strict_tools(tmp_path, lambda request: calls.append(request) or httpx.Response(201))
    _, intent, _, grant = approved(store)
    original = store._commit
    count = 0
    def fail_marker(con):
        nonlocal count
        count += 1
        if count == 2:
            raise sqlite3.OperationalError("disk full")
        original(con)
    monkeypatch.setattr(store, "_commit", fail_marker)
    result = await tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant)
    assert result.error_code == "STORAGE_UNAVAILABLE"
    assert calls == []
    assert store.get_operation(intent["operation_id"])["dispatch_stage"] == "RESERVED"


async def test_strict_success_wrong_order_remains_unknown(tmp_path):
    tools, store = strict_tools(tmp_path, lambda _: httpx.Response(201, json={"ticket_id": "T1", "order_id": "B", "status": "ACCEPTED"}))
    _, intent, _, grant = approved(store)
    result = await tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant)
    assert result.status == "UNKNOWN"
    _, _, _, other = approved(store, "other")
    assert store.claim(other).result.error_code == "ORDER_BUSY"


def approve_real(store, principal, session, order_id):
    run = store.start_run(session, principal, "m1", "ticket")
    intent = store.prepare(run["run_id"], run["active_invocation_id"], "action", "CONFIRM",
                           {"order_id": order_id, "user_id": principal.user_id, "reason": "help"})
    store.publish_interrupt(intent["intent_id"], run["active_invocation_id"], "cp", "fi")
    receipt = store.consume_resume(session, principal, intent["intent_id"], run["state_revision"], "m2", "yes", "APPROVE", "cp")
    store.project_receipt(receipt["receipt_id"], receipt["invocation_id"])
    grant = store.issue_grant(run["run_id"], receipt["invocation_id"], receipt["receipt_id"], principal)
    return intent, grant


async def test_frozen_missing_logistics_flows_through_strict_tools_and_real_graph(launch, tmp_path):
    from agent.controller import AgentController, ChatRequest
    from agent.sql import SQLService
    from test_graph import Model

    store = RuntimeStore(tmp_path / "agent-runtime.sqlite")
    sql = SQLService(DATASET / "database/ecommerce.db")
    async with httpx.AsyncClient(base_url=ready(launch), trust_env=False) as client:
        tools = BusinessTools(sql, store, client, snapshot_verifier=SnapshotVerifier(DATASET),
                              service_token=TEST_TOKEN, strict=True)
        principal = Principal(user_id="acc-edge")
        result = await tools.query_logistics("ACC-009", principal)
        assert result.error_code == "NO_DATA" and result.attempts == 1
        controller = AgentController(store=store, checkpoint_path=tmp_path / "checkpoint.sqlite",
                                     model=Model(kind="logistics", subject="ACC-009"), sql=sql, tools=tools)
        await controller.start()
        try:
            task = await controller.handle(principal, ChatRequest(message="查询订单 ACC-009 的物流", client_message_id="m1"))
            assert task["goals"][0]["unmet_reason"] == "NO_DATA"
            assert "读取服务暂不可用" not in task["answer"]
        finally:
            await controller.aclose()


@pytest.mark.parametrize("body,expected", [
    ({"error": {"code": "logistics_unavailable", "accepted": 0}}, "invalid_response"),
    ({"error": {"code": "logistics_unavailable", "accepted": False}, "extra": True}, "invalid_response"),
    ({"error": {"code": "order_unavailable", "accepted": False}}, "order_unavailable"),
])
async def test_strict_logistics_404_does_not_turn_other_envelopes_into_no_data(tmp_path, body, expected):
    tools, _ = strict_tools(tmp_path, lambda _: httpx.Response(404, json=body))
    async with tools.client:
        denied = await tools.query_logistics("A", Principal(user_id="alice", tenant="outside-demo"))
        assert denied.error_code == "order_unavailable" and denied.attempts == 0
        result = await tools.query_logistics("A", Principal(user_id="alice"))
        assert result.error_code == expected and result.attempts == 1


async def test_frozen_real_http_retries_and_post_accept_timeout_holds_key(launch, tmp_path, order):
    from agent.sql import SQLService
    url = ready(launch, faults="runtime-http.json")
    principal = Principal(user_id=order["user_id"])
    store = RuntimeStore(tmp_path / "agent-runtime.sqlite")
    requests = []
    async def observe(request):
        requests.append((request.method, request.url.path))
    async with httpx.AsyncClient(base_url=url, trust_env=False, event_hooks={"request": [observe]}) as client:
        tools = BusinessTools(SQLService(DATASET / "database/ecommerce.db"), store, client, timeout=0.2,
                              backoff=0, snapshot_verifier=SnapshotVerifier(DATASET), service_token=TEST_TOKEN, strict=True)
        read = await tools.query_order(order["order_id"], principal)
        assert read.status == "SUCCESS" and read.attempts == 3
        assert type(read.data["amount"]) is int and read.data["amount"] == order["amount"]
        intent, grant = approve_real(store, principal, "first", order["order_id"])
        result = await tools.create_ticket(order["order_id"], "help", intent["operation_id"], principal, grant=grant)
        assert result.status == "UNKNOWN" and result.attempts == 1
        assert len(ticket_rows(tmp_path / "tickets.sqlite")) == 1
        restarted = RuntimeStore(store.path)
        next_intent, next_grant = approve_real(restarted, principal, "second", order["order_id"])
        other = BusinessTools(tools.sql, restarted, client, snapshot_verifier=tools.snapshot_verifier, service_token=TEST_TOKEN, strict=True)
        blocked = await other.create_ticket(order["order_id"], "help", next_intent["operation_id"], principal, grant=next_grant)
        assert blocked.error_code == "ORDER_BUSY"
        assert requests.count(("POST", "/tickets")) == 1
        assert requests.count(("GET", "/orders/" + order["order_id"])) == 3
        assert len(ticket_rows(tmp_path / "tickets.sqlite")) == 1


async def test_frozen_real_http_success_releases_key(launch, tmp_path, order):
    from agent.sql import SQLService
    url = ready(launch)
    principal = Principal(user_id=order["user_id"])
    store = RuntimeStore(tmp_path / "agent-runtime.sqlite")
    async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
        tools = BusinessTools(SQLService(DATASET / "database/ecommerce.db"), store, client,
                              snapshot_verifier=SnapshotVerifier(DATASET), service_token=TEST_TOKEN, strict=True)
        intent, grant = approve_real(store, principal, "first", order["order_id"])
        result = await tools.create_ticket(order["order_id"], "help", intent["operation_id"], principal, grant=grant)
        assert result.status == "SUCCESS" and result.data["order_id"] == order["order_id"]
        assert len(ticket_rows(tmp_path / "tickets.sqlite")) == 1
        _, next_grant = approve_real(store, principal, "second", order["order_id"])
        assert store.claim(next_grant).token is not None


@pytest.mark.parametrize("status,body", [
    (404, {"error": {"accepted": False}}),
    (404, {"error": {"code": "actor_mismatch", "accepted": False}}),
    (201, {"ticket_id": "T", "order_id": "A", "status": "ACCEPTED", "_meta": None}),
])
async def test_malformed_success_or_rejection_preserves_unknown_key(tmp_path, status, body):
    tools, store = strict_tools(tmp_path, lambda _: httpx.Response(status, json=body))
    _, intent, _, grant = approved(store)
    result = await tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant)
    assert result.status == "UNKNOWN"
    _, _, _, next_grant = approved(store, "next")
    assert store.claim(next_grant).result.error_code == "ORDER_BUSY"


async def test_send_marker_commit_past_deadline_never_reaches_http(tmp_path, monkeypatch):
    import time
    calls = []
    tools, store = strict_tools(tmp_path, lambda request: calls.append(request) or httpx.Response(500))
    run, intent, receipt, _ = approved(store)
    grant = store.issue_grant(run["run_id"], receipt["invocation_id"], receipt["receipt_id"], Principal(user_id="alice"), deadline=time.monotonic() + 0.08)
    original = store._commit
    def delayed(con):
        operation = store._operation(con, intent["operation_id"])
        if operation and operation["dispatch_stage"] == "SEND_STARTED":
            time.sleep(0.1)
        original(con)
    monkeypatch.setattr(store, "_commit", delayed)
    result = await tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant)
    assert calls == []
    assert result.status == "UNKNOWN"


async def test_read_budget_commit_cannot_extend_deadline(tmp_path):
    import time
    calls = []
    tools, _ = strict_tools(tmp_path, lambda request: calls.append(request) or httpx.Response(500))
    def reserve():
        time.sleep(0.05)
        return 1
    result = await tools.query_order("A", Principal(user_id="alice"), reserve_http=reserve, deadline=time.monotonic() + 0.025)
    assert calls == [] and result.error_code == "BUDGET_EXHAUSTED"


async def test_cancellation_after_send_preserves_late_success_in_original_ledger(tmp_path):
    import asyncio
    started, complete = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request.method)
        started.set()
        await complete.wait()
        return httpx.Response(201, json={"ticket_id": "T", "order_id": "A", "status": "ACCEPTED", "_meta": dict(tools.snapshot_verifier.metadata, observed_at="2026-11-15T12:00:00+08:00")})
    tools, store = strict_tools(tmp_path, handler)
    run, intent, _, grant = approved(store)
    outer = asyncio.create_task(tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant))
    await started.wait()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    store.recover_run(run["run_id"], executor_stopped=True)
    complete.set()
    await tools.drain_sends(run["run_id"], timeout=1)
    assert store.get_operation(intent["operation_id"])["state"] == "SUCCESS"
    assert store.get_run(run["run_id"])["status"] == "CLOSED"
    assert calls == ["POST"]


async def test_complete_v1_rejection_releases_key(tmp_path):
    tools, store = strict_tools(tmp_path, lambda _: httpx.Response(404, json={"error": {"code": "order_unavailable", "accepted": False}}))
    _, intent, _, grant = approved(store)
    result = await tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant)
    assert result.status == "FAILED" and result.error_code == "service_rejected"
    _, _, _, next_grant = approved(store, "next")
    assert store.claim(next_grant).token is not None


async def test_cancellation_before_send_does_not_create_owned_send_task(tmp_path, monkeypatch):
    import asyncio
    calls = []
    tools, store = strict_tools(tmp_path, lambda request: calls.append(request) or httpx.Response(500))
    run, intent, _, grant = approved(store)
    entered = asyncio.Event()
    count = 0
    async def authorize(*_args):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
            await asyncio.Event().wait()
    monkeypatch.setattr(tools, "_authorize", authorize)
    outer = asyncio.create_task(tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant))
    await entered.wait()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    store.recover_run(run["run_id"], executor_stopped=True)
    assert tools.pending_sends(run["run_id"]) == () and calls == []
    assert store.get_operation(intent["operation_id"])["result"]["error_code"] == "NOT_SENT"


async def test_drain_returns_while_real_sqlite_writer_lock_remains_held(tmp_path):
    import asyncio
    import time
    started, reply = asyncio.Event(), asyncio.Event()
    calls = []
    async def handler(request):
        calls.append(request.method)
        started.set()
        await reply.wait()
        return httpx.Response(201, json={"ticket_id": "T", "order_id": "A", "status": "ACCEPTED", "_meta": dict(tools.snapshot_verifier.metadata, observed_at="2026-11-15T12:00:00+08:00")})
    tools, store = strict_tools(tmp_path, handler)
    run, intent, _, grant = approved(store)
    outer = asyncio.create_task(tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant))
    await started.wait()
    writer = sqlite3.connect(store.path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        reply.set()
        before = time.monotonic()
        assert await tools.drain_sends(run["run_id"], timeout=0.05) == []
        assert time.monotonic() - before < 0.2
        assert writer.in_transaction
        assert tools.pending_settlements(run["run_id"])
        assert not await tools.aclose(timeout=0.01)
        writer.rollback()
        result = await asyncio.wait_for(outer, timeout=1)
        assert result.status == "SUCCESS"
        assert store.get_operation(intent["operation_id"])["state"] == "SUCCESS"
        assert calls == ["POST"]
        assert await tools.aclose(timeout=0.2)
        assert tools.pending_settlements() == ()
    finally:
        writer.rollback()
        writer.close()


async def test_cancelled_asyncio_waiter_does_not_hide_running_settlement_thread(tmp_path):
    import asyncio
    started, reply = asyncio.Event(), asyncio.Event()
    async def handler(_request):
        started.set()
        await reply.wait()
        return httpx.Response(404, json={"error": {"code": "order_unavailable", "accepted": False}})
    tools, store = strict_tools(tmp_path, handler)
    run, intent, _, grant = approved(store)
    outer = asyncio.create_task(tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant))
    await started.wait()
    writer = sqlite3.connect(store.path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        reply.set()
        await tools.drain_sends(timeout=0.02)
        owned = tools.pending_sends(run["run_id"])
        assert tools.pending_settlements(run["run_id"])
        for task in owned:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert not await tools.aclose(timeout=0.01)
        assert tools.pending_settlements(run["run_id"])
        writer.rollback()
        await tools.drain_sends(timeout=0.5)
        assert await tools.aclose(timeout=0.1)
        assert store.get_operation(intent["operation_id"])["state"] == "FAILED"
    finally:
        writer.rollback()
        writer.close()


async def test_close_accounts_for_suspended_reserved_executor_and_prevents_send(tmp_path, monkeypatch):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    tools, store = strict_tools(tmp_path, lambda request: calls.append(request) or httpx.Response(500))
    run, intent, _, grant = approved(store)
    count = 0
    async def authorize(*_args):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
            await release.wait()
    monkeypatch.setattr(tools, "_authorize", authorize)
    outer = asyncio.create_task(tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant))
    await entered.wait()
    try:
        assert not await tools.aclose(timeout=0.01)
    finally:
        release.set()
        await outer
    assert await tools.aclose(timeout=0.1)
    assert calls == []
    operation = store.get_operation(intent["operation_id"])
    assert operation["dispatch_stage"] == "RESERVED"
    assert operation["result"]["error_code"] == "NOT_SENT"


@pytest.mark.parametrize("cancel_waiter", [False, True], ids=["complete", "cancel-waiter"])
async def test_reserved_close_recovery_respects_deadline_under_real_writer_lock(tmp_path, monkeypatch, cancel_waiter):
    import asyncio
    import threading
    import time
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = []
    tools, store = strict_tools(tmp_path, lambda request: calls.append(request) or httpx.Response(500))
    run, intent, receipt, grant = approved(store)
    count = 0
    async def authorize(*_args):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
            await resume.wait()
    monkeypatch.setattr(tools, "_authorize", authorize)
    outer = asyncio.create_task(tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant))
    await entered.wait()
    writer = sqlite3.connect(store.path, check_same_thread=False)
    writer.execute("BEGIN IMMEDIATE")
    # A real independent release also lets the pre-fix blocking path terminate.
    unlock = threading.Timer(0.36, writer.rollback)
    unlock.start()
    asyncio.get_running_loop().call_later(0.01, resume.set)
    try:
        before = time.monotonic()
        closed = await tools.aclose(timeout=0.05)
        elapsed = time.monotonic() - before
        assert elapsed < 0.2, f"0.05-second close blocked for {elapsed:.3f} seconds"
        assert not closed and writer.in_transaction
        assert tools.pending_settlements(run["run_id"])
        assert not tools.client.is_closed
        assert calls == []
        operation = store.get_operation(intent["operation_id"])
        assert operation["dispatch_stage"] == "RESERVED" and operation["state"] == "IN_FLIGHT"
        if cancel_waiter:
            outer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await outer
            assert tools.pending_pre_sends(run["run_id"]) == ()
            assert tools.pending_sends(run["run_id"]) == ()
            assert not await tools.aclose(timeout=0.01)
            assert tools.pending_settlements(run["run_id"])
        unlock.cancel()
        unlock.join()
        writer.rollback()
        if not cancel_waiter:
            result = await asyncio.wait_for(outer, timeout=1)
            assert result.error_code == "NOT_SENT"
        assert await tools.aclose(timeout=1)
        assert tools.pending_settlements() == ()
        operation = store.get_operation(intent["operation_id"])
        assert operation["dispatch_stage"] == "RESERVED" and operation["attempt_count"] == 0
        assert operation["state"] == "FAILED" and operation["result"]["error_code"] == "NOT_SENT"
        assert store.get_receipt(receipt["receipt_id"])["settlement_reason"] == "NOT_SENT"
        assert store.get_run(run["run_id"])["status"] == "CLOSED"
        _, _, _, next_grant = approved(store, "next")
        assert store.claim(next_grant).token is not None
        assert calls == []
    finally:
        unlock.cancel()
        unlock.join()
        writer.rollback()
        writer.close()
        resume.set()
        await asyncio.gather(outer, return_exceptions=True)
        await tools.aclose(timeout=1)
        await tools.client.aclose()


async def test_reserved_close_failed_recovery_commit_does_not_claim_durable_not_sent(tmp_path, monkeypatch):
    import asyncio
    entered, resume = asyncio.Event(), asyncio.Event()
    calls = []
    tools, store = strict_tools(tmp_path, lambda request: calls.append(request) or httpx.Response(500))
    run, intent, _, grant = approved(store)
    _, _, _, next_grant = approved(store, "next")
    original_commit = store._commit
    count = 0
    async def authorize(*_args):
        nonlocal count
        count += 1
        if count == 2:
            entered.set()
            await resume.wait()
    def fail_recovery_commit(con):
        if store._get(con, "run", run["run_id"])["status"] == "CLOSED":
            raise sqlite3.OperationalError("recovery commit failed")
        original_commit(con)
    monkeypatch.setattr(tools, "_authorize", authorize)
    monkeypatch.setattr(store, "_commit", fail_recovery_commit)
    outer = asyncio.create_task(tools.create_ticket("A", "help", intent["operation_id"], Principal(user_id="alice"), grant=grant))
    await entered.wait()
    try:
        assert not await tools.aclose(timeout=0.01)
        resume.set()
        result = await asyncio.wait_for(outer, timeout=1)
        assert result.error_code == "STORAGE_UNAVAILABLE"
        operation = store.get_operation(intent["operation_id"])
        assert operation["state"] == "IN_FLIGHT" and operation["dispatch_stage"] == "RESERVED"
        assert operation["result"]["error_code"] == "pending_reconciliation"
        assert store.get_run(run["run_id"])["status"] == "LIVE"
        assert store.claim(next_grant).result.error_code == "ORDER_BUSY"
        assert calls == []
    finally:
        resume.set()
        await asyncio.gather(outer, return_exceptions=True)
        await tools.aclose(timeout=1)
        await tools.client.aclose()
