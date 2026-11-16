"""An order-ID reply fills the persisted clarification, despite planner drift."""
from copy import deepcopy

import pytest

from agent.provider import ProviderError
from agent.controller import ChatRequest
from agent.runtime_types import RuntimeConflict
from agent.sql import SQLService
from test_graph import setup as setup, ask, approve, proposal, ALICE
from test_sql import db as db


def drifting_model(setup, db, *, drift="split", goals=None):
    controller, model, _, tools = setup
    controller.sql = SQLService(db)
    original = goals or [{"id": "g1", "kind": "sql", "description": "帮我查订单"}]
    sql_calls = []

    async def understand(question, context, *, budget=None):
        if budget:
            budget("understand_goal", 100, 100)
        if not context["existing_goals"]:
            return {"goals": deepcopy(original), "clarification": "请提供订单号。"}
        if drift == "timeout":
            raise ProviderError("TIMEOUT")
        if len(original) > 1:
            return {"goals": deepcopy(original)}
        supplied = {"id": "g2", "kind": "order", "description": "查询订单 A1 的详情", "subject_candidate": "A1"}
        return {"goals": [*deepcopy(original), supplied] if drift == "split" else [supplied]}

    async def decide(context, *, budget=None):
        if budget:
            budget("decide_next", 100, 100)
        goal = context["goals"][0]
        assert goal["subject"], "Unresolved clarification reached a query decision"
        return proposal("query_sql", [goal["goal_id"]], f"查询订单 {goal['subject']} 的状态和金额")

    async def sql(question, schema, error=None, *, budget=None):
        if budget:
            budget("sql", 100, 100)
        sql_calls.append(question)
        return "SELECT order_id, amount, status FROM orders WHERE order_id='A1'"

    model.understand_goal, model.decide_next, model.sql = understand, decide, sql
    return controller, tools, sql_calls


@pytest.mark.parametrize("drift,reply", [
    ("split", "订单号是 A1，请继续查询。"),
    ("renamed", "A1"),
    ("timeout", "订单编号：A1"),
])
async def test_single_slot_resumes_original_sql_goal_without_replanning(setup, db, drift, reply):
    c, tools, calls = drifting_model(setup, db, drift=drift)
    first = await ask(c, "帮我查订单；DROP TABLE orders;")
    assert first["status"] == "WAITING_INPUT"
    result = await approve(c, first, message=reply)
    assert result["status"] == "COMPLETED", result
    assert [(g["goal_id"], g["subject"], g["status"]) for g in result["goals"]] == [("g1", "A1", "FULFILLED")]
    assert "人民币 12.00 元" in result["answer"] and len(calls) == 1
    snapshot = c.store.list_runs()[0]["verified_snapshot"]
    assert snapshot["goals"][0]["kind"] == "sql" and snapshot["goals"][0]["depends_on"] == []
    span = snapshot["goals"][0]["candidate_span"]
    combined = "帮我查订单；DROP TABLE orders;\n" + reply
    assert combined[span["start"]:span["end"]] == "A1" and span["start"] >= combined.index("\n")
    assert snapshot["goals"][0]["input_refs"][-1] == "m2"
    assert not tools.gets and not tools.posts


async def test_slot_binding_still_rejects_foreign_order_before_sql_generation(setup, db):
    c, tools, calls = drifting_model(setup, db)
    first = await ask(c, "帮我查订单")
    result = await approve(c, first, message="订单号是 B1，请继续查询。")
    assert result["status"] == "REJECTED", result
    assert not calls and not tools.gets and not tools.posts


@pytest.mark.parametrize("reply", [
    "A1 或 A2，还没确定", "不是 A1", "金额是 1200 元", "订单号 A1; DROP TABLE orders;",
])
async def test_ambiguous_or_extra_content_is_not_treated_as_a_simple_order_slot(setup, db, reply):
    c, tools, calls = drifting_model(setup, db)
    first = await ask(c, "帮我查订单")
    result = await approve(c, first, message=reply)
    assert result["status"] == "WAITING_INPUT", result
    assert result["goals"][0]["subject"] is None
    assert not calls and not tools.gets and not tools.posts


async def test_one_id_cannot_fill_two_independent_waiting_goals(setup, db):
    c, tools, calls = drifting_model(setup, db, goals=[
        {"id": "g1", "kind": "sql", "description": "第一笔订单状态"},
        {"id": "g2", "kind": "sql", "description": "第二笔订单金额"},
    ])
    first = await ask(c, "帮我查两笔订单")
    result = await approve(c, first, message="订单号是 A1，请继续查询。")
    assert result["status"] == "WAITING_INPUT", result
    assert all(g["subject"] is None for g in result["goals"])
    assert not calls and not tools.gets and not tools.posts


async def test_slot_completion_replays_but_consumed_interrupt_rejects_a_new_message(setup, db):
    c, tools, calls = drifting_model(setup, db)
    first = await ask(c, "帮我查订单")
    result = await approve(c, first, message="订单号是 A1，请继续查询。")
    assert result["status"] == "COMPLETED", result
    replay = await approve(c, first, message="订单号是 A1，请继续查询。")
    assert replay == result and len(calls) == 1
    with pytest.raises(RuntimeConflict, match="STALE_INTERRUPT"):
        await approve(c, first, mid="another-message", message="订单号是 A1，请继续查询。")
    assert len(calls) == 1 and not tools.posts


async def test_latest_interrupt_with_old_revision_still_rejected_then_valid_reply_resumes(setup, db):
    c, tools, calls = drifting_model(setup, db)
    first = await ask(c, "帮我查订单")
    second = await approve(c, first, message="A1 或 A2，还没确定")
    assert second["status"] == "WAITING_INPUT" and second["revision"] == 2
    stale = ChatRequest(message="订单号是 A1，请继续查询。", client_message_id="stale",
        session_id=second["session_id"], interrupt_id=second["interrupt"]["interrupt_id"], expected_revision=1)
    with pytest.raises(RuntimeConflict, match="STALE_INTERRUPT"):
        await c.handle(ALICE, stale)
    assert not calls and not tools.gets and not tools.posts
    result = await approve(c, second, mid="m3", message="订单号是 A1，请继续查询。")
    assert result["status"] == "COMPLETED" and len(calls) == 1, result
