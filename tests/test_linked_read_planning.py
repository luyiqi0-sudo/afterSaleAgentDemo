"""Mixed order/logistics/policy/ticket work keeps the fixed request budget."""
from copy import deepcopy
from pathlib import Path

import pytest

from agent.contracts import Principal
from agent.controller import ChatRequest
from agent.retriever import parse_documents
from test_graph import setup as setup, proposal
from test_policy_explanations import SCOPE, retriever


def install(setup, *, logistics_dependency=False, subject="A"):
    c, model, _, tools = setup
    question = f"查询订单 {subject} 的状态、金额和物流；售后工单一般多久会首次回复？请引用来源文档名和段落。如果订单异常，请以物流异常核查为原因准备工单，先确认再提交。"
    decisions = []
    async def understand(q, context, *, budget=None):
        if budget:
            budget("understand_goal", 100, 300)
        return {"goals": [
            {"id": "g1", "kind": "order", "description": f"查询订单 {subject} 的状态和金额", "subject_candidate": subject},
            {"id": "g2", "kind": "logistics", "description": f"查询订单 {subject} 的物流状态和更新时间", "subject_candidate": subject,
             "deps": ["g1"] if logistics_dependency else []},
            {"id": "g3", "kind": "policy", "description": "售后工单一般多久会首次回复？请引用来源文档名和段落。"},
            {"id": "g4", "kind": "ticket", "description": "异常订单准备工单，先确认再提交", "deps": ["g1", "g2", "g3"]},
        ]}
    async def decide(context, *, budget=None):
        if budget:
            budget("decide_next", 100, 200)
        goals = context["goals"]
        if any(g["kind"] == "order" for g in goals):
            ids = [g["goal_id"] for g in goals if g["kind"] in {"order", "logistics"}]
            result = proposal("query_order", ids, context["question"], subject)
        elif any(g["kind"] == "logistics" for g in goals):
            result = proposal("query_logistics", ["g2"], context["question"], subject)
        elif any(g["kind"] == "policy" for g in goals):
            result = proposal("retrieve_policy", ["g3"], "售后工单一般多久会首次回复？")
        else:
            result = proposal("request_ticket", ["g4"], context["question"], subject)
            result["parameters"]["reason"] = "物流异常核查"
        decisions.append(result["action_type"])
        return result
    async def embed(texts, *, budget=None):
        if budget:
            budget("embed", 100, 0)
        return [[1.0, 0.0] for _ in texts]
    original_order, original_logistics = tools.query_order, tools.query_logistics
    async def order(*args, **kwargs):
        result = await original_order(*args, **kwargs)
        if result.data is not None:
            result.data.update(status=5, amount=122914)
        return result
    async def logistics(*args, **kwargs):
        result = await original_logistics(*args, **kwargs)
        result.data.update(status=4, update_time="2026-08-02 00:00:00")
        return result
    model.understand_goal, model.decide_next, model.embed = understand, decide, embed
    tools.query_order, tools.query_logistics = order, logistics
    return question, decisions


async def policy_index(c):
    root = Path("data/c004-r01/baseline/knowledge")
    c.retriever = await retriever([chunk for chunk in parse_documents(root) if chunk.policy_key == "ticket_response"])


@pytest.mark.parametrize("dependency,subject", [(False, "A"), (True, "X-839")])
async def test_mixed_work_uses_two_fewer_decision_calls_and_waits_for_approval(setup, dependency, subject):
    c, _, _, tools = setup
    question, decisions = install(setup, logistics_dependency=dependency, subject=subject)
    await policy_index(c)
    events = []
    c.trace_emit = lambda event, data: events.append((event, deepcopy(data)))
    principal = Principal(user_id="alice", conditions=SCOPE)
    first = await c.handle(principal, ChatRequest(message=question, client_message_id="mixed"))
    assert first["status"] == "WAITING_CONFIRM", first
    assert decisions == ["retrieve_policy", "request_ticket"]
    assert tools.gets == [("order", subject), ("logistics", subject)] and tools.posts == []
    plans = [data for event, data in events if event == "read_plan"]
    assert [p["action_type"] for p in plans] == ["query_order", "query_logistics"]
    assert all(p["origin"] == "linked_read_rule" for p in plans)
    assert first["goals"][2]["status"] == "FULFILLED"
    result = await c.handle(principal, ChatRequest(message="确认", client_message_id="approve",
        session_id=first["session_id"], interrupt_id=first["interrupt"]["interrupt_id"], expected_revision=first["revision"]))
    assert result["status"] == "COMPLETED", result
    assert tools.posts == [subject]
    assert "二十四小时" in result["answer"] and "ticket_response-v2.md 第 1 段" in result["answer"]


async def test_generated_read_still_requires_order_ownership(setup):
    c, _, sql, tools = setup
    question, decisions = install(setup)
    sql.denied.add("A")
    result = await c.handle(Principal(user_id="alice", conditions=SCOPE), ChatRequest(message=question, client_message_id="denied"))
    assert result["status"] == "REJECTED"
    assert not tools.gets and not tools.posts and not decisions


@pytest.mark.parametrize("constraint", ["使用 SQL JOIN 查询", "如果满足条件才查询", "不要查询", "different_order", "multiple_pairs", "original_sql"])
async def test_constrained_or_ambiguous_reads_still_require_model_choice(setup, constraint):
    from agent.provider import ProviderError
    c, model, _, tools = setup
    question, _ = install(setup)
    if constraint == "original_sql":
        question += " 请使用 SQL JOIN 关联订单表和物流表。"
    original = model.understand_goal
    async def understand(*args, **kwargs):
        draft = await original(*args, **kwargs)
        if constraint == "different_order":
            draft["goals"][1].update(subject_candidate="B", description="查询订单 B 的物流")
        elif constraint == "multiple_pairs":
            draft["goals"] = draft["goals"][:2] + [
                {"id": "b1", "kind": "order", "description": "订单 B 状态", "subject_candidate": "B"},
                {"id": "b2", "kind": "logistics", "description": "订单 B 物流", "subject_candidate": "B"}]
        elif constraint != "original_sql":
            draft["goals"][0]["description"] += constraint
        return draft
    calls = []
    async def decide(*args, **kwargs):
        calls.append("model_choice")
        raise ProviderError("TIMEOUT")
    model.understand_goal, model.decide_next = understand, decide
    result = await c.handle(Principal(user_id="alice"), ChatRequest(message=question + " 以及订单 B", client_message_id="fallback"))
    assert calls == ["model_choice"] and result["reason"] == "MODEL_UNAVAILABLE"
    assert not tools.gets and not tools.posts


async def test_generated_candidate_cannot_bypass_source_validation(setup, monkeypatch):
    from agent.read_planning import linked_read_proposal
    c, _, _, tools = setup
    question, _ = install(setup)
    def forged_source(state, ready):
        candidate = linked_read_proposal(state, ready)
        candidate["parameter_sources"]["order_id"] = {"type": "message", "span": {"start": 0, "end": 1}}
        return candidate
    monkeypatch.setattr("agent.graph_nodes.linked_read_proposal", forged_source)
    result = await c.handle(Principal(user_id="alice"), ChatRequest(message=question, client_message_id="source"))
    assert result["status"] == "WAITING_INPUT", result
    assert result["interrupt"]["field"] == "order_id"
    assert not tools.gets and not tools.posts


async def test_failed_order_never_runs_dependent_logistics_or_prepares_ticket(setup):
    c, _, _, tools = setup
    question, decisions = install(setup, logistics_dependency=True)
    await policy_index(c)
    tools.read_error = "NO_DATA"
    result = await c.handle(Principal(user_id="alice", conditions=SCOPE), ChatRequest(message=question, client_message_id="read-failed"))
    assert result["status"] == "PARTIAL" and result["interrupt"] is None, result
    assert tools.gets == [("order", "A")] and not tools.posts
    assert decisions == ["retrieve_policy"]
    assert result["goals"][1]["unmet_reason"] == "DEPENDENCY"
    assert result["goals"][3]["unmet_reason"] == "DEPENDENCY"


async def test_linked_reads_still_obey_request_deadline(setup):
    import asyncio
    c, model, _, tools = setup
    question, _ = install(setup)
    original = model.understand_goal
    async def slow(*args, **kwargs):
        draft = await original(*args, **kwargs)
        await asyncio.sleep(0.05)
        return draft
    model.understand_goal = slow
    c.request_seconds = 3.01  # Preserve the three-second finalization reserve.
    result = await c.handle(Principal(user_id="alice"), ChatRequest(message=question, client_message_id="deadline"))
    assert result["reason"] == "BUDGET_EXHAUSTED"
    assert not tools.gets and not tools.posts


@pytest.mark.parametrize("stage", ["understand", "decide", "rag", "sql"])
async def test_request_budget_exhaustion_is_not_reported_as_provider_or_retrieval_failure(setup, stage):
    from agent.provider import ProviderError
    from agent.retriever import PolicyRetrievalResult
    c, model, sql, tools = setup
    model.kind, model.subject = ("sql" if stage == "sql" else "policy"), None
    async def exhausted(*args, **kwargs):
        raise ProviderError("BUDGET_EXHAUSTED")
    class ExhaustedRetriever:
        async def retrieve(self, *args, **kwargs):
            return PolicyRetrievalResult(status="RETRIEVAL_UNAVAILABLE", error_code="BUDGET_EXHAUSTED")
    if stage == "understand":
        model.understand_goal = exhausted
    elif stage == "decide":
        model.decide_next = exhausted
    elif stage == "rag":
        c.retriever = ExhaustedRetriever()
        model.actions = [proposal("retrieve_policy", ["g1"], "工单首次响应时限")]
    else:
        sql.answer = exhausted
        model.actions = [proposal("query_sql", ["g1"], "查询订单金额合计")]
    events = []
    result = await c.handle(Principal(user_id="alice", conditions=SCOPE),
                            ChatRequest(message="工单首次响应时限", client_message_id="budget"),
                            emit=lambda event, data: events.append((event, data)))
    assert result["reason"] == "BUDGET_EXHAUSTED", result
    assert all(g["unmet_reason"] == "BUDGET_EXHAUSTED" for g in result["goals"])
    if stage == "rag":
        assert any(event == "tool_error" and data["tool"] == "retrieve_policy"
                   and data["status"] == "BUDGET_EXHAUSTED" and data["summary"] for event, data in events)
    assert not tools.gets and not tools.posts
