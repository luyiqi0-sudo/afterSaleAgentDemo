"""Local TCP/Uvicorn proves incremental delivery and disconnect behavior."""
import asyncio
import json
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn

from agent.config import Settings
from test_api import BoundaryController


@asynccontextmanager
async def serve(app):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="critical",
                                               timeout_graceful_shutdown=5))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 7)


async def test_wire_progress_arrives_before_completion_and_disconnect_cancels():
    from api.main import create_app

    release, cancelled = asyncio.Event(), asyncio.Event()

    class Blocking(BoundaryController):
        async def handle(self, principal, request, emit):
            emit("step", {"action": "query_order", "goal_ids": ["g1"]})
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return await super().handle(principal, request, emit)

    app = create_app(Settings(_env_file=None, demo_mode=True, auth_tokens={"abc": "alice"}),
                     controller=Blocking())
    async with serve(app) as base, httpx.AsyncClient(base_url=base, timeout=3) as client:
        async with client.stream("POST", "/chat", json={"message": "查", "client_message_id": "m1"},
                                 headers={"Authorization": "Bearer abc"}) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = response.aiter_lines()
            assert await anext(lines) == "event: stage"
            release.set()
            events = [line async for line in lines if line.startswith("event: ")]
            assert events == ["event: stage", "event: tool_start", "event: tool_end",
                              "event: answer_delta", "event: done"]
        release.clear()
        async with client.stream("POST", "/chat", json={"message": "查", "client_message_id": "m2"},
                                 headers={"Authorization": "Bearer abc"}) as response:
            assert await anext(response.aiter_lines()) == "event: stage"
        await asyncio.wait_for(cancelled.wait(), 3)


async def test_real_agent_http_and_frozen_mock_verify_order_and_one_ticket(tmp_path):
    from eval.controlled import ControlledModel
    from eval.runner import demo_cases, run_case
    from knowledge.ingest import ingest_knowledge

    root = Path("data/c004-r01/baseline").resolve()
    case = next(c for c in demo_cases(root) if c["id"] == "D05")
    model = ControlledModel(case)
    settings = Settings(
        _env_file=None, vector_backend="memory", embedding_model="controlled-topics-v1",
        embedding_dimension=16, dataset_root=root, database_path=root / "database/ecommerce.db",
        dictionary_path=root / "database/dictionary.json", knowledge_path=root / "knowledge",
    )
    index = await ingest_knowledge(settings, model)
    result = await run_case(root, case, "controlled", settings, model, index, tmp_path / "case", protocol_checks=True)
    assert [item["done"]["status"] for item in result["turns"]] == ["WAITING_CONFIRM", "COMPLETED"]
    assert [(call["tool"], call["turn"], call["http_attempts"]) for call in result["calls"]] == [
        ("query_order", 1, 1), ("create_ticket", 2, 1),
    ]
    assert result["remote_ticket_count"] == 1
    assert result["remote_receipt"]["http_status"] == 201
    assert result["remote_receipt"]["ticket_id"] == result["facts"]["ticket_id"]
    first_wire_request = result["turns"][0]["events"][0]["request_id"]
    first_trace_node = next(item for item in result["trace"] if item["event"] == "node")
    assert first_trace_node["correlation_id"] == first_wire_request
    assert first_trace_node["runtime_request_id"] and first_trace_node["runtime_request_id"] != first_wire_request
    assert result["remote_ticket_counts_by_turn"] == [0, 1]
    protocol = result["protocol_probes"]
    assert protocol["foreign"]["http_status"] == 404
    assert protocol["stale"]["http_status"] == 409
    assert protocol["replay"]["done"] == result["turns"][-1]["done"]
    assert protocol["post_count_after_replay"] == 1


async def test_wire_envelopes_correlate_and_fallback_terminal_is_complete():
    from api.main import create_app

    app = create_app(Settings(_env_file=None, demo_mode=True, auth_tokens={"owner": "alice"}),
                     controller=BoundaryController())
    async with serve(app) as base, httpx.AsyncClient(base_url=base) as client:
        headers = {"Authorization": "Bearer owner"}
        response = await client.post("/chat", headers=headers, json={"message": "查询", "client_message_id": "m"})
        rows = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert rows[0].keys() == {"id", "request_id", "conversation_id", "timestamp", "type", "data"}
        assert [row["type"] for row in rows] == ["stage", "tool_start", "tool_end", "answer_delta", "done"]
        assert len({row["request_id"] for row in rows}) == 1
        assert len({row["id"] for row in rows}) == len(rows)
        assert all(row["timestamp"] for row in rows)
        assert rows[0]["conversation_id"] is None and rows[-1]["conversation_id"] == "s"
        assert rows[-1]["data"]["task_status"] == "PARTIAL"
        assert rows[-1]["data"]["request_status"] == "terminated"
        response = await client.post("/chat", headers=headers,
                                     json={"message": "确认", "client_message_id": "old-m", "session_id": "s",
                                           "interrupt_id": "old", "expected_revision": 1})
        rows = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert [row["type"] for row in rows] == ["error", "done"]
        assert len({row["request_id"] for row in rows}) == 1 and all(row["conversation_id"] == "s" for row in rows)
        assert rows[-1]["data"] == {
            "session_id": "s", "task_id": None, "revision": None, "task_status": None,
            "status": None, "request_status": "terminated", "reason": "STALE_INTERRUPT", "answer": "",
            "goals": [], "interrupt": None, "operation_refs": [], "persisted": False,
        }


async def test_business_readiness_authenticates_read_only_to_frozen_mock(tmp_path):
    from api.main import create_app
    from eval.controlled import ControlledModel
    from eval.runner import ROOT, serve as serve_frozen
    from knowledge.ingest import ingest_knowledge
    from mock_service.app import create_app as create_mock

    root = ROOT.resolve()
    mock = create_mock(root, tmp_path / "mock.sqlite", "synthetic-service-correct")
    async with serve_frozen(mock) as business:
        for token, expected in (("synthetic-service-correct", 200), ("synthetic-service-wrong", 503)):
            settings = Settings(
                _env_file=None, vector_backend="memory", embedding_model="controlled-topics-v1",
                embedding_dimension=16, dataset_root=root, database_path=root / "database/ecommerce.db",
                dictionary_path=root / "database/dictionary.json", knowledge_path=root / "knowledge",
                runtime_dir=tmp_path / str(expected), demo_mode=True, auth_tokens={"demo-credential": "eval-empty"},
                business_service_token=token, business_base_url=business,
            )
            model = ControlledModel()
            index = await ingest_knowledge(settings, model)
            async with serve_frozen(create_app(settings, model=model, retriever=index)) as api:
                async with httpx.AsyncClient(base_url=api) as client:
                    result = await client.get("/health?mode=ready")
                    assert result.status_code == expected
                    assert result.json()["checks"]["business"] == ("ok" if expected == 200 else "unavailable")


async def test_wire_captures_actual_read_timeouts_and_single_unknown_write(tmp_path):
    from eval.controlled import ControlledModel
    from eval.eval import evaluate_cases
    from eval.runner import ROOT, demo_cases, load_cases, run_case
    from knowledge.ingest import ingest_knowledge

    root = ROOT.resolve()
    settings = Settings(
        _env_file=None, vector_backend="memory", embedding_model="controlled-topics-v1",
        embedding_dimension=16, dataset_root=root, database_path=root / "database/ecommerce.db",
        dictionary_path=root / "database/dictionary.json", knowledge_path=root / "knowledge",
    )
    case_list = [next(c for c in load_cases(root) if c["id"] == "GOLD-29"),
                 next(c for c in demo_cases(root) if c["id"] == "D07")]
    for case in case_list:
        model = ControlledModel(case)
        index = await ingest_knowledge(settings, model)
        result = await run_case(root, case, "controlled", settings, model, index, tmp_path / case["id"])
        wire = result["http_attempt_outcomes"]
        trace = [e for e in result["trace"] if e["event"] == "http_timeout"]
        if case["id"] == "GOLD-29":
            assert result["turns"][0]["done"]["status"] == "WAITING_CONFIRM"
            assert result["turns"][0]["done"]["interrupt"]["order_id"] == "EVAL-002"
            assert result["remote_ticket_count"] == 0
            assert [e["result"] for e in wire] == [200, "timeout", "timeout", "timeout"]
            assert [(e["tool"], e["attempt"]) for e in trace] == [("query_logistics", i) for i in range(1, 4)]
            assert [e["action"] for e in result["mock_consumed_faults"]] == ["ok"] + ["timeout_before"] * 3
            report = evaluate_cases(root, {"GOLD-29": result}, contract="mvp-agent-v2")
            assert report["summary"]["passed"] == 1
            missing_consumption = {**result, "mock_consumed_faults": []}
            report = evaluate_cases(root, {"GOLD-29": missing_consumption}, contract="mvp-agent-v2")
            assert report["summary"]["failed"] == 1
            missing_offer = {**result, "facts": {**result["facts"], "confirmation_kind": None}}
            report = evaluate_cases(root, {"GOLD-29": missing_offer}, contract="mvp-agent-v2")
            assert report["summary"]["failed"] == 1
        else:
            assert [(e["tool"], e["result"]) for e in wire] == [("create_ticket", "timeout")]
            assert [(e["tool"], e["attempt"]) for e in trace] == [("create_ticket", 1)]
            assert [e["action"] for e in result["mock_consumed_faults"]] == ["timeout_after"]
            assert result["remote_ticket_effects"] == [{"order_id": "EVAL-003", "reason": "退款失败核查"}]
            assert result["operation_bindings"][0]["operation_state"] == "UNKNOWN"
            assert result["turns"][-1]["done"]["operation_refs"]
