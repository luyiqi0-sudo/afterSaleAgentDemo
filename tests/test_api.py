import json

from fastapi.testclient import TestClient

from agent.config import Settings
from agent.runtime_types import RuntimeConflict


class BoundaryController:
    def __init__(self):
        self.calls = []
        self.approved = set()

    async def start(self):
        pass

    async def aclose(self):
        return True

    async def preflight(self, principal, request):
        if request.session_id and principal.user_id != "alice":
            raise RuntimeConflict("SESSION_UNAVAILABLE")

    async def handle(self, principal, request, emit):
        self.calls.append((principal.user_id, request.client_message_id))
        if request.interrupt_id == "old":
            raise RuntimeConflict("STALE_INTERRUPT")
        self.approved.add(request.client_message_id)
        emit("step", {"action": "query_order", "goal_ids": ["g1"], "reasoning": "hidden"})
        emit("tool_start", {"tool": "query_order"})
        emit("tool_end", {"tool": "query_order", "status": "VERIFIED_OK", "attempts": 1})
        emit("answer", {"text": "已办理"})
        result = {"session_id": "s", "status": "PARTIAL", "request_status": "terminated",
                  "answer": "已办理", "persisted": False}
        emit("done", result)
        return result


def test_demo_bearer_body_spoof_foreign_session_stale_and_replay():
    from api.main import create_app

    ctrl = BoundaryController()
    settings = Settings(_env_file=None, demo_mode=True, auth_tokens={"alice-key": "alice", "bob-key": "bob"})
    request = {"message": "确认", "client_message_id": "m", "session_id": "s",
               "interrupt_id": "i", "expected_revision": 2}
    headers = {"Authorization": "Bearer alice-key"}
    with TestClient(create_app(settings, controller=ctrl)) as client:
        assert client.get("/health").json()["demo_mode"] is True
        assert client.post("/chat", json=request).status_code == 401
        for key in ("user_id", "Command", "command"):
            assert client.post("/chat", json=request | {key: "alice-key"}, headers=headers).status_code == 422
        assert client.post("/chat", json=request, headers={"Authorization": "Bearer bob-key"}).status_code == 404
        old = client.post("/chat", json=request | {"interrupt_id": "old"}, headers=headers)
        assert "event: error" in old.text and '"code":"STALE_INTERRUPT"' in old.text
        assert old.text.count("event: done") == 1
        assert "m" not in ctrl.approved
        for _ in range(2):
            result = client.post("/chat", json=request, headers=headers)
            assert result.text.count("event: done") == 1
            assert result.headers["X-Agent-Demo-Mode"] == "true"
            assert '"persisted":false' in result.text and '"request_status":"terminated"' in result.text
            assert "hidden" not in result.text
        assert ctrl.calls == [("alice", "m")] * 3
        assert len(ctrl.approved) == 1


def test_prod_denies_demo_credentials_and_readiness_probe_failure_is_visible():
    from api.main import create_app

    async def unhealthy():
        return {"business": "unavailable"}

    with TestClient(create_app(Settings(_env_file=None, auth_tokens={"alice-key": "alice"}),
                               controller=BoundaryController(), readiness=unhealthy)) as client:
        assert client.post("/chat", json={"message": "x", "client_message_id": "m"},
                           headers={"Authorization": "Bearer alice-key"}).status_code == 401
        assert client.get("/health?mode=live").status_code == 200
        result = client.get("/health?mode=ready")
        assert result.status_code == 503 and result.json()["checks"]["business"] == "unavailable"
        metrics = client.get("/metrics").text
        assert "after_sales_chat" in metrics and "alice" not in metrics


def test_private_model_trace_keeps_redacted_prompt_and_structured_candidate(tmp_path):
    from agent.events import PrivateTrace

    path = tmp_path / "trace.jsonl"
    trace = PrivateTrace(path, ("synthetic-key-123",))
    trace("model_usage", {
        "operation": "decide_next", "usage": {"total_tokens": 17}, "latency_ms": 9,
        "request_messages": [
            {"role": "system", "content": "对候选操作核验"},
            {"role": "user", "content": json.dumps({
                "question": "订单 EVAL-002；synthetic-key-123",
                "authorization": "never-record-authorization",
                "reasoningContent": "hidden nested user JSON analysis",
                "prefix-synthetic-key-123": "synthetic attribute",
            })},
        ],
        "structured_candidate": {"action_type": "query_order", "reasoning_content": "hidden reasoning",
                                 "parameter_sources": {"order_id": {"span": {"start": 3, "end": 11},
                                                                    "chain-of-thought": "hidden nested candidate analysis",
                                                                    "synthetic-key-123": "synthetic attribute"}}},
        "raw_response": "private raw response",
    })
    text = path.read_text(encoding="utf-8")
    row = json.loads(text)
    assert "EVAL-002" in row["request_messages"][1]["content"]
    assert "[REDACTED]" in text and "synthetic-key-123" not in text
    assert "never-record-authorization" not in text and "hidden reasoning" not in text
    assert "hidden nested" not in text
    assert "private raw response" not in text
    assert row["structured_candidate"]["parameter_sources"]["order_id"]["span"]["start"] == 3


def test_private_trace_redacts_candidate_and_nested_executed_sql(tmp_path):
    from agent.events import PrivateTrace

    path = tmp_path / "trace.jsonl"
    trace = PrivateTrace(path)
    sql = "SELECT amount FROM orders WHERE order_id = 'synthetic-private-literal'; -- private comment"
    trace("sql_candidate", {"sql": sql})
    trace("sql_execution", {"result": {"executed_sql": sql}})
    candidate, executed = map(json.loads, path.read_text(encoding="utf-8").splitlines())
    assert "synthetic-private-literal" not in executed["result"]["executed_sql"]
    assert "private comment" not in executed["result"]["executed_sql"]
    assert candidate["sql"] == executed["result"]["executed_sql"]
    assert "SELECT amount FROM orders WHERE order_id =" in candidate["sql"]


def test_identity_readiness_uses_same_valid_map_as_ingress():
    from api.main import create_app

    settings = Settings(_env_file=None, demo_mode=True, auth_tokens={"bad token": "alice", "good-token": "\n"})
    with TestClient(create_app(settings, controller=BoundaryController())) as client:
        assert client.get("/health?mode=ready").status_code == 503
        assert client.get("/health?mode=ready").json()["checks"]["identity"] == "unavailable"
