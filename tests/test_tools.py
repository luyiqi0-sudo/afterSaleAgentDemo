import httpx
import pytest

from test_sql import db as db, alice, service  # noqa: F401


def make_tools(db, tmp_path, handler, **kwargs):
    from agent.operations import OperationStore
    from agent.tools import BusinessTools
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://mock")
    return BusinessTools(service(db), OperationStore(tmp_path / "operations.db"), client, backoff=0, **kwargs)


async def test_query_retry_429_then_success(db, tmp_path):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"order_id": "A1", "status": 1, "amount": 1200})
    result = await make_tools(db, tmp_path, handler).query_order("A1", alice())
    assert result.status == "SUCCESS"
    assert result.data["amount"] == 1200
    assert len(calls) == result.attempts == 2


async def test_500_has_finite_retry_and_explicit_failure(db, tmp_path):
    result = await make_tools(db, tmp_path, lambda _: httpx.Response(500)).query_order("A1", alice())
    assert result.status == "FAILED"
    assert result.attempts == 3
    assert result.error_code == "service_unavailable"


async def test_foreign_order_never_reaches_http(db, tmp_path):
    def handler(request):
        pytest.fail("unauthorized order reached HTTP")
    result = await make_tools(db, tmp_path, handler).query_order("B1", alice())
    assert result.status == "FAILED"
    assert result.error_code == "order_unavailable"


async def test_invalid_order_never_reaches_http(db, tmp_path):
    def handler(request):
        pytest.fail("invalid order reached HTTP")
    result = await make_tools(db, tmp_path, handler).query_order("../orders", alice())
    assert result.error_code == "invalid_parameters"


async def test_ticket_timeout_is_unknown_and_restart_does_not_repeat(db, tmp_path):
    calls = []
    def handler(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("response lost", request=request)
    first = await make_tools(db, tmp_path, handler).create_ticket("A1", "物流异常", "op-1", alice())
    second = await make_tools(db, tmp_path, handler).create_ticket("A1", "物流异常", "op-1", alice())
    assert first.status == second.status == "UNKNOWN"
    assert calls == ["POST"]


async def test_same_operation_different_payload_rejected(db, tmp_path):
    tools = make_tools(db, tmp_path, lambda _: httpx.Response(200, json={"ticket_id": "T1"}))
    assert (await tools.create_ticket("A1", "物流异常", "op-1", alice())).status == "SUCCESS"
    result = await tools.create_ticket("A2", "物流异常", "op-1", alice())
    assert result.status == "FAILED"
    assert result.error_code == "operation_conflict"


async def test_malformed_ticket_response_is_unknown(db, tmp_path):
    result = await make_tools(db, tmp_path, lambda _: httpx.Response(200, json={"ok": True})).create_ticket("A1", "异常", "op-1", alice())
    assert result.status == "UNKNOWN"


async def test_wrong_order_response_is_rejected(db, tmp_path):
    result = await make_tools(db, tmp_path, lambda _: httpx.Response(200, json={"order_id": "B1", "amount": 900000})).query_order("A1", alice())
    assert result.status == "FAILED"
    assert result.data is None
