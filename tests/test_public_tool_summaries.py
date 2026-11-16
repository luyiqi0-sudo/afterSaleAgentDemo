"""Public progress is derived from gated actions and newly verified facts."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agent.contracts import ToolResult
from agent.controller import ChatRequest
from agent.events import public_frame
from agent.sql import SQLService
from test_answer_support import baseline_evidence
from test_graph import ALICE, proposal, setup as setup


async def ask(controller, message="查询订单 A", mid="m1", *, emit, **kw):
    return await controller.handle(ALICE, ChatRequest(message=message, client_message_id=mid, **kw), emit=emit)


async def approve(controller, waiting, *, emit):
    return await ask(controller, "确认", "m2", emit=emit, session_id=waiting["session_id"],
                     interrupt_id=waiting["interrupt"]["interrupt_id"], expected_revision=waiting["revision"])


def capture():
    events = []
    return events, lambda name, data: events.append((name, data))


def result_events(events):
    return [(name, data) for name, data in events if name in {"tool_end", "tool_error"}]


async def test_read_plan_and_result_are_readable_before_final_answer(setup):
    c, model, _, tools = setup
    malicious = proposal("query_order", ["g1"], "查询订单 A", "A")
    malicious["public_summary"] = "UNTRUSTED_MODEL_CLAIM"
    model.actions = [malicious]
    events, emit = capture()
    done = await ask(c, emit=emit)
    assert done["status"] == "COMPLETED"
    stages = [data for name, data in events if name == "step"]
    assert stages[0]["action"] == "understand" and stages[0]["summary"]
    assert any(s["action"] == "query_order" and "订单 A" in s["summary"] for s in stages)
    start = next(data for name, data in events if name == "tool_start")
    assert "订单" in start["summary"]
    [(event, result)] = result_events(events)
    assert event == "tool_end" and result["status"] == "VERIFIED_OK"
    assert "已发货" in result["summary"] and "888.00 元" in result["summary"]
    assert "UNTRUSTED_MODEL_CLAIM" not in json.dumps(events, ensure_ascii=False)
    assert [name for name, _ in events].index("tool_end") < [name for name, _ in events].index("answer")
    assert len(tools.gets) == 1


async def test_result_of_first_tool_arrives_while_second_is_still_running(setup):
    c, model, _, tools = setup
    model.kind = "logistics"
    entered, release = asyncio.Event(), asyncio.Event()
    original = tools.query_logistics

    async def slow_logistics(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    tools.query_logistics = slow_logistics
    events, emit = capture()
    pending = asyncio.create_task(ask(c, "查询订单 A 的物流", emit=emit))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        [(name, first)] = result_events(events)
        assert name == "tool_end" and first["tool"] == "query_order"
        assert "已发货" in first["summary"]
        assert "任务已停止" not in first["summary"] and "尚未完成" not in first["summary"]
        assert not any(name == "answer" for name, _ in events)
    finally:
        release.set()
        await pending
    second = result_events(events)[1][1]
    assert second["tool"] == "query_logistics" and "L1" in second["summary"]
    assert "888.00" not in second["summary"]  # no replay of the first tool's facts


@pytest.mark.parametrize("covered", [True, False])
async def test_policy_summary_respects_goal_coverage_and_includes_sources(setup, covered):
    c, model, _, _ = setup
    model.kind, model.subject = "policy", None
    question = "电子发票多久开具？" if covered else "维修费用具体是多少钱？"
    model.actions = [proposal("retrieve_policy", ["g1"], question)]
    evidence = baseline_evidence("invoice")

    class Retriever:
        async def retrieve(self, *args, **kwargs):
            return SimpleNamespace(status="SUCCESS", model_dump=lambda **kw: {"evidences": [evidence]})

    c.retriever = Retriever()
    events, emit = capture()
    await ask(c, question, emit=emit)
    [(name, result)] = result_events(events)
    if covered:
        assert name == "tool_end" and "五个工作日" in result["summary"]
        assert evidence["file_name"] in result["summary"] and "第 1 段" in result["summary"]
    else:
        assert name == "tool_error" and result["status"] == "NO_DATA"
        assert "五个工作日" not in result["summary"] and "未覆盖" in result["summary"]


async def test_unavailable_retrieval_still_emits_one_explained_error(setup):
    c, model, _, _ = setup
    model.kind, model.subject = "policy", None
    model.actions = [proposal("retrieve_policy", ["g1"], "发票政策")]
    events, emit = capture()
    await ask(c, "发票政策", emit=emit)
    [(name, data)] = result_events(events)
    assert name == "tool_error" and "政策检索服务暂不可用" in data["summary"]


async def test_mismatched_tool_result_never_reaches_public_summary(setup):
    c, _, _, tools = setup

    async def foreign(*args, **kwargs):
        return ToolResult(status="SUCCESS", data={"order_id": "SECRET_FOREIGN_ORDER", "status": 2,
                                                   "amount": 99999999, "raw_response": "SECRET_BODY"})

    tools.query_order = foreign
    events, emit = capture()
    await ask(c, emit=emit)
    [(name, data)] = result_events(events)
    assert name == "tool_error" and data["status"] == "RESPONSE_MISMATCH"
    assert "未通过核验" in data["summary"]
    assert all(value not in json.dumps(events) for value in ("SECRET", "99999999", "999999.99"))


@pytest.mark.parametrize("write_status", ["SUCCESS", "UNKNOWN"])
async def test_ticket_summary_respects_confirmation_and_unknown(setup, write_status):
    c, model, _, tools = setup
    model.kind, tools.write_status = "ticket", write_status
    events, emit = capture()
    first = await ask(c, "为订单 A 创建工单", emit=emit)
    assert first["status"] == "WAITING_CONFIRM" and not tools.posts
    preparation = next(data for name, data in events if name == "step" and data["action"] == "request_ticket")
    assert "确认" in preparation["summary"] and "尚未提交" in preparation["summary"]
    assert not result_events(events)
    events.clear()
    done = await approve(c, first, emit=emit)
    [(name, result)] = result_events(events)
    if write_status == "SUCCESS":
        assert name == "tool_end" and "工单已创建" in result["summary"] and "T1" in result["summary"]
    else:
        assert name == "tool_error" and "待核对" in result["summary"] and "工单已创建" not in result["summary"]
    events.clear()
    assert await approve(c, first, emit=emit) == done
    assert not result_events(events) and not any(name == "tool_start" for name, _ in events)
    assert tools.posts == ["A"]


async def test_failed_snapshot_never_announces_successful_result(setup):
    c, _, _, _ = setup
    original = c.store.save_verified_snapshot

    def fail_after_tool(*args, **kwargs):
        if args[-1].get("observations"):
            raise OSError("SECRET_STORAGE_DETAIL")
        return original(*args, **kwargs)

    c.store.save_verified_snapshot = fail_after_tool
    events, emit = capture()
    await ask(c, emit=emit)
    assert not any(name == "tool_end" for name, _ in events)
    [(name, error)] = result_events(events)
    assert name == "tool_error" and error["summary"] and "SECRET" not in json.dumps(events)


def test_sse_serialization_keeps_summary_but_drops_internal_fields():
    raw = public_frame("tool_end", {"tool": "query_order", "status": "VERIFIED_OK",
                       "summary": "订单已发货。", "reasoning": "SECRET_REASONING", "raw_result": "SECRET_BODY"},
                       request_id="req", conversation_id="session")
    data = json.loads(raw.decode().split("data: ", 1)[1])["data"]
    assert data["summary"] == "订单已发货。" and "SECRET" not in raw.decode()


@pytest.mark.parametrize("many", [False, True])
async def test_sql_summary_uses_projected_facts_and_bounds_long_results(setup, tmp_path, many):
    c, model, _, _ = setup
    db = tmp_path / "business.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE orders(order_id TEXT,user_id TEXT,amount INTEGER,status INTEGER,create_time TEXT)")
        con.executemany("INSERT INTO orders VALUES (?,?,?,?,?)",
                        [(f"A{i:03}", "alice", 1234, 2, "SECRET_UNREQUESTED_VALUE") for i in range(100 if many else 1)])
        con.execute("INSERT INTO orders VALUES ('OTHER','bob',99999999,2,'SECRET_OTHER_USER')")
    c.sql = SQLService(db)
    model.kind, model.subject = "sql", None
    question = "查询我的订单金额"
    model.actions = [proposal("query_sql", ["g1"], question)]

    async def sql(*args, **kwargs):
        return "SELECT order_id,amount,create_time FROM orders ORDER BY order_id"

    model.sql = sql
    events, emit = capture()
    done = await ask(c, question, emit=emit)
    [(event, result)] = result_events(events)
    assert event == "tool_end" and result["status"] == "VERIFIED_OK"
    assert len(result["summary"]) <= 1600
    if many:
        assert "省略" in result["summary"] and "最终答复" in result["summary"]
    else:
        assert "人民币 12.34 元" in result["summary"]
    assert "人民币 12.34 元" in done["answer"]
    assert all(value not in json.dumps(events) for value in ("SECRET", "99999999", "999999.99"))


async def test_initial_stage_arrives_before_goal_model_returns(setup):
    c, model, _, _ = setup
    release = asyncio.Event()
    original = model.understand_goal

    async def understand(*args, **kwargs):
        await release.wait()
        return await original(*args, **kwargs)

    model.understand_goal = understand
    seen = asyncio.Event()
    events = []

    def emit(name, data):
        events.append((name, data))
        if name == "step" and data.get("action") == "understand":
            seen.set()

    pending = asyncio.create_task(ask(c, emit=emit))
    try:
        await asyncio.wait_for(seen.wait(), 3)
        assert events[0][1]["summary"] and not pending.done()
    finally:
        release.set()
        await pending
