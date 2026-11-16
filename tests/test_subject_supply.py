"""Missing order ID across dependent goals, through durable Graph resume."""
from copy import deepcopy
import json

import pytest

from test_graph import setup as setup, ask, approve, proposal


QUESTION = "订单现在是什么状态，金额是多少，对应的快递单号是什么？再看看包裹目前的物流状态，最近一次更新是什么时间。只查询，不用创建售后工单。"
GOALS = [
    {"id": "g1", "kind": "order", "description": "查询订单现在的状态和金额", "deps": []},
    {"id": "g2", "kind": "logistics", "description": "查询对应的快递单号", "deps": ["g1"]},
    {"id": "g3", "kind": "logistics", "description": "查看包裹目前的物流状态", "deps": ["g1"]},
    {"id": "g4", "kind": "logistics", "description": "查询最近一次物流更新的时间", "deps": ["g1"]},
]


def install(setup, goals=GOALS, *, split_reads=False, reversed_draft=False):
    controller, model, _, tools = setup
    contexts = []
    async def understand(question, context, **kwargs):
        draft = deepcopy(goals)
        if context["existing_goals"]:
            for goal in draft:
                goal["subject_candidate"] = "EVAL-002"
            if len(draft) == 4:
                # The live model replanned dependencies on SUPPLY. Existing
                # task requirements must survive this untrusted reordering.
                draft[2]["deps"], draft[3]["deps"] = ["g2"], ["g3"]
            if reversed_draft:
                draft.reverse()
        return {"goals": draft}

    async def decide(context, **kwargs):
        contexts.append(deepcopy(context))
        ready = context["goals"]
        ready = [g for g in ready if g["subject"]] or ready
        ids = [g["goal_id"] for g in ready]
        orders = [o for o in context["observations"] if o["action_type"] == "query_order"]
        if any(not g["subject"] for g in ready):
            if orders and all(g["kind"] == "logistics" for g in ready):
                # Replay the actual bad decision: missing subjects make the
                # existing order unavailable as a prerequisite for logistics.
                action, subject = "query_order", orders[0]["subject"]
            else:
                return {"action_type": "ask_user", "goal_ids": [ids[0]],
                        "parameters": {"question": "请提供订单号", "field": "order_id"},
                        "parameter_sources": {}, "based_on_observation_ids": [], "public_summary": "询问订单号"}
        else:
            subject = ready[0]["subject"]
            action = "query_logistics" if all(g["kind"] == "logistics" for g in ready) and orders else "query_order"
        if split_reads:
            ids = ids[:1]
        result = proposal(action, ids, context["question"], subject)
        obj = next((o for o in context["objects"] if o["order_id"] == subject), None)
        if obj:
            result["parameter_sources"] = {"order_id": {"type": "object", "ref_id": obj["ref_id"]}}
        return result

    original = tools.query_logistics
    async def logistics(*args, **kwargs):
        result = await original(*args, **kwargs)
        result.data.update(logistics_id="L-EVAL-002", status=1, update_time="2026-08-17 17:34:20")
        return result
    model.understand_goal, model.decide_next, tools.query_logistics = understand, decide, logistics
    return contexts


@pytest.mark.parametrize("split_reads", [False, True])
@pytest.mark.parametrize("reversed_draft", [False, True])
async def test_supply_resumes_all_same_order_goals(setup, tmp_path, monkeypatch, split_reads, reversed_draft):
    from agent.events import PrivateTrace
    c, _, _, tools = setup
    trace_path = tmp_path / "trace.jsonl"
    c.trace_emit = PrivateTrace(trace_path)
    contexts = install(setup, split_reads=split_reads, reversed_draft=reversed_draft)
    if split_reads:
        # Force separate model proposals to exercise the read-reuse gate;
        # the ordinary path now batches these logistics goals before the model.
        monkeypatch.setattr("agent.graph_nodes.linked_read_proposal", lambda *_: None)
    waiting = await ask(c, QUESTION)
    assert waiting["status"] == "WAITING_INPUT", waiting
    assert tools.gets == []
    result = await approve(c, waiting, message="订单号是 EVAL-002，请继续查询。")
    assert result["status"] == "COMPLETED", result
    assert all(g["status"] == "FULFILLED" and g["subject"] == "EVAL-002" for g in result["goals"])
    assert tools.gets == [("order", "EVAL-002"), ("logistics", "EVAL-002")]
    assert tools.posts == []
    assert "L-EVAL-002" in result["answer"] and "运输中" in result["answer"]
    assert "2026-08-17 17:34:20" in result["answer"]
    snapshot = c.store.list_runs()[0]["verified_snapshot"]
    assert all(g["depends_on"] == ["g1"] for g in snapshot["goals"][1:])
    if split_reads:
        ready = next(ctx for ctx in contexts if ctx["observations"])
        assert "query_logistics" in ready["allowed_actions"]
        assert "query_order" not in ready["allowed_actions"]
    reused = [row for line in trace_path.read_text(encoding="utf-8").splitlines()
              if (row := json.loads(line))["event"] == "read_reuse"]
    assert len(reused) == (2 if split_reads else 0)
    assert all(row["tool"] == "query_logistics" and row["source_observation_id"] for row in reused)


async def test_supply_does_not_bind_independent_unknown_order(setup):
    c, _, _, tools = setup
    install(setup, goals=[GOALS[0], {"id": "g2", "kind": "order", "description": "查询另一笔订单的状态", "deps": []}])
    waiting = await ask(c, "查一笔订单的金额，还要查另一笔订单的状态")
    result = await approve(c, waiting, message="第一笔订单号是 EVAL-002")
    assert result["status"] == "WAITING_INPUT", result
    assert result["goals"][0]["subject"] == "EVAL-002"
    assert result["goals"][1]["subject"] is None
    assert tools.gets == [("order", "EVAL-002")] and tools.posts == []


async def test_supplied_foreign_order_still_denied_before_reads(setup):
    c, _, sql, tools = setup
    install(setup)
    waiting = await ask(c, QUESTION)
    sql.denied.add("EVAL-002")
    result = await approve(c, waiting, message="订单号是 EVAL-002，请继续查询。")
    assert result["status"] == "REJECTED"
    assert not tools.gets and not tools.posts


async def test_reuse_rechecks_ownership_before_delivering_to_new_goals(setup, monkeypatch):
    c, model, sql, tools = setup
    install(setup, split_reads=True)
    # Inject the model's separate reads so ownership changes between them.
    monkeypatch.setattr("agent.graph_nodes.linked_read_proposal", lambda *_: None)
    original = model.decide_next
    async def decide(context, **kwargs):
        if any(o["action_type"] == "query_logistics" for o in context["observations"]):
            sql.denied.add("EVAL-002")
        return await original(context, **kwargs)
    model.decide_next = decide
    waiting = await ask(c, QUESTION)
    result = await approve(c, waiting, message="订单号是 EVAL-002，请继续查询。")
    assert result["status"] == "REJECTED", result
    assert tools.gets == [("order", "EVAL-002"), ("logistics", "EVAL-002")]
    assert result["goals"][2]["status"] != "FULFILLED" and not tools.posts


async def test_reordered_supply_keeps_each_goal_with_its_own_order(setup):
    c, model, _, tools = setup
    initial = [
        {"id": "first", "kind": "order", "description": "第一笔订单状态"},
        {"id": "second", "kind": "order", "description": "第二笔订单金额"},
    ]
    async def understand(question, context, **kwargs):
        if not context["existing_goals"]:
            return {"goals": deepcopy(initial), "clarification": "请提供两笔订单号"}
        return {"goals": [{**initial[1], "subject_candidate": "B"}, {**initial[0], "subject_candidate": "A"}]}
    model.understand_goal = understand
    waiting = await ask(c, "查询第一笔订单状态和第二笔订单金额")
    result = await approve(c, waiting, message="第一笔订单 A，第二笔订单 B")
    assert result["status"] == "COMPLETED", result
    assert [(g["goal_id"], g["subject"]) for g in result["goals"]] == [("first", "A"), ("second", "B")]
    assert tools.gets == [("order", "A"), ("order", "B")]


async def test_read_reuse_never_crosses_orders(setup):
    c, model, _, tools = setup
    async def understand(*args, **kwargs):
        return {"goals": [{"id": "ga", "kind": "logistics", "description": "订单 A 的物流", "subject_candidate": "A"},
                          {"id": "gb", "kind": "logistics", "description": "订单 B 的物流", "subject_candidate": "B"}]}
    async def decide(context, **kwargs):
        goal = context["goals"][0]
        has_order = any(o["subject"] == goal["subject"] and o["action_type"] == "query_order" for o in context["observations"])
        return proposal("query_logistics" if has_order else "query_order", [goal["goal_id"]], context["question"], goal["subject"])
    model.understand_goal, model.decide_next = understand, decide
    result = await ask(c, "查询订单 A 和订单 B 的物流")
    assert result["status"] == "COMPLETED", result
    assert tools.gets == [("order", "A"), ("logistics", "A"), ("order", "B"), ("logistics", "B")]
    assert not tools.posts


async def test_missing_entity_id_cannot_be_expanded_into_all_orders(setup):
    c, model, sql, tools = setup
    calls = []
    async def answer(*args, **kwargs):
        calls.append("unscoped_sql")
        raise AssertionError("An entity query needs its missing order ID before SQL")
    sql.answer = answer
    model.kind, model.subject = "order", None
    model.actions = [proposal("query_sql", ["g1"], "查询当前用户所有订单的订单ID、订单状态和订单金额，按订单ID排序")]
    result = await ask(c, "订单现在是什么状态，金额是多少？")
    assert calls == [], "An untrusted SQL proposal widened an entity query"
    assert result["status"] == "WAITING_INPUT", result
    assert result["interrupt"]["field"] == "order_id"
    assert not tools.gets and not tools.posts
