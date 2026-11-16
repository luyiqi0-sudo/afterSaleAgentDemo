"""Historical dates through ordinary chat, with real graph and policy adjudication."""
from dataclasses import replace
from datetime import date
import sqlite3
import json

import pytest

from agent.contracts import Principal, ToolResult
from agent.controller import ChatRequest
from test_graph import setup as setup, proposal
from test_policy_explanations import Embedder, SCOPE, invoice_chunks, retriever


async def prepare(setup):
    controller, model, _, _ = setup
    controller.retriever = await retriever(invoice_chunks())
    model.kind, model.subject, model.embed = "policy", None, Embedder().embed
    # Deliberately omit/contradict the requested date in the untrusted tool text.
    model.actions = [proposal("retrieve_policy", ["g1"], "按2026-11-15的政策，电子发票多久开具？")]
    return controller


async def chat(controller, text, mid="date-query", **kwargs):
    return await controller.handle(Principal(user_id="alice", conditions=SCOPE),
                                   ChatRequest(message=text, client_message_id=mid, **kwargs))


@pytest.mark.parametrize("text,day,version,duration", [
    ("按2026-09-30的政策，电子发票多久开具？请引用来源文档名和段落。", "2026-09-30", "1", "十"),
    ("在2026年9月30日，电子发票多久开具？", "2026-09-30", "1", "十"),
    ("按2026-09-30的政策，电子发票开具时限，以哪个为准？请引用来源文档名和段落。", "2026-09-30", "1", "十"),
    ("按2026-10-01的政策，电子发票多久开具？", "2026-10-01", "2", "五"),
    ("电子发票多久开具？", "2026-11-15", "2", "五"),
])
async def test_chat_selects_policy_at_requested_date(setup, text, day, version, duration):
    controller = await prepare(setup)
    result = await chat(controller, text)
    assert result["status"] == "COMPLETED", result
    assert f"{duration}个工作日" in result["answer"]
    assert f"invoice-v{version}.md 第 1 段" in result["answer"]
    assert f"适用日期 {day}" in result["answer"]
    if version == "1":
        assert "invoice-v2.md" not in result["answer"] and "五个工作日" not in result["answer"]
    assert controller.business_date == date(2026, 11, 15)
    assert not setup[3].gets and not setup[3].posts


@pytest.mark.parametrize("when", ["2026-02-30", "2026年9月", "2026-09-30还是2026-10-01"])
async def test_ambiguous_date_requires_clarification_and_resumes(setup, when):
    controller = await prepare(setup)
    first = await chat(controller, f"按{when}的政策，电子发票多久开具？")
    assert first["interrupt"] is not None, first
    assert "日期" in first["answer"]
    assert "rag" not in controller.trace
    result = await chat(controller, "2026-09-30", "date-reply", session_id=first["session_id"],
                        interrupt_id=first["interrupt"]["interrupt_id"], expected_revision=first["revision"])
    assert result["status"] == "COMPLETED", result
    assert "invoice-v1.md 第 1 段" in result["answer"]
    assert "适用日期 2026-09-30" in result["answer"]
    assert "invoice-v2.md" not in result["answer"]


async def test_before_first_policy_does_not_fall_back_to_newest(setup):
    controller = await prepare(setup)
    result = await chat(controller, "按2024-12-31的政策，电子发票多久开具？")
    assert result["status"] != "COMPLETED"
    assert "invoice-v2.md" not in result["answer"] and "invoice-v1.md" not in result["answer"]
    assert result["goals"][0]["unmet_reason"] == "NO_DATA"


@pytest.mark.parametrize("when", ["不要按2026-09-30的政策", "按2026-10-01之前的政策", "按去年的政策"])
async def test_unresolved_temporal_meaning_never_selects_a_date(setup, when):
    controller = await prepare(setup)
    result = await chat(controller, when + "，电子发票多久开具？")
    assert result["interrupt"] is not None, result
    assert "rag" not in controller.trace


async def test_historical_date_does_not_erase_unknown_business_condition(setup):
    controller = await prepare(setup)
    result = await chat(controller, "按2026-09-30的政策，海外电子发票多久开具？")
    assert result["status"] != "COMPLETED"
    assert "未覆盖" in result["answer"] and "海外" in result["answer"]


async def test_private_trace_retains_date_provenance(setup, tmp_path):
    from agent.events import PrivateTrace
    controller = await prepare(setup)
    path = tmp_path / "private-trace.jsonl"
    controller.trace_emit = PrivateTrace(path)
    result = await chat(controller, "按2026-09-30的政策，电子发票多久开具？")
    assert result["status"] == "COMPLETED"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    event = next(row for row in rows if row["event"] == "policy_retrieval")
    context = event["query_context"]
    assert context["as_of"] == "2026-09-30" and context["server_date"] == "2026-11-15"
    assert context["sources"][0]["source_message_id"] == "date-query"
    assert context["sources"][0]["source_text"] == "按2026-09-30的政策"
    assert event["evidences"][0]["applied_on"] == context["as_of"]


@pytest.mark.parametrize("reply", ["按2026年9月30日的政策", "改为2026-09-30"])
async def test_natural_date_clarification_does_not_replace_the_question(setup, reply):
    controller = await prepare(setup)
    first = await chat(controller, "按2026年9月的政策，电子发票多久开具？")
    result = await chat(controller, reply, "natural-reply", session_id=first["session_id"],
                        interrupt_id=first["interrupt"]["interrupt_id"], expected_revision=first["revision"])
    assert result["status"] == "COMPLETED", result
    assert "invoice-v1.md 第 1 段" in result["answer"]


@pytest.mark.parametrize("description", ["电子发票多久开具？", "按2026-10-01的政策，电子发票多久开具？"])
async def test_model_cannot_omit_or_change_date_in_a_mixed_request(setup, description):
    controller = await prepare(setup)
    async def understand(*args, **kwargs):
        return {"goals": [{"id": "g1", "kind": "policy", "description": description},
                          {"id": "g2", "kind": "sql", "description": "查询上个月消费总额"}]}
    setup[1].understand_goal = understand
    result = await chat(controller, "按2026-09-30的政策，电子发票多久开具？另外查询上个月消费总额。")
    assert result["interrupt"] is not None
    assert "rag" not in controller.trace


async def test_model_cannot_borrow_sql_date_for_policy(setup):
    controller = await prepare(setup)
    async def understand(*args, **kwargs):
        return {"goals": [
            {"id": "g1", "kind": "policy", "description": "按2026-09-30的政策，电子发票多久开具？"},
            {"id": "g2", "kind": "sql", "description": "按2026-09-30统计消费"},
        ]}
    setup[1].understand_goal = understand
    result = await chat(controller, "按2026-09-30统计消费，另外电子发票多久开具？")
    assert result["interrupt"] is not None, result
    assert "rag" not in controller.trace


async def test_mixed_request_keeps_sql_month_at_server_date(setup, tmp_path):
    from agent.sql import SQLService
    controller = await prepare(setup)
    model = setup[1]
    policy = "按2026-09-30的政策，电子发票多久开具？"
    sql_question = "查询上个月的有效订单金额合计（不扣除退款）"
    async def understand(*args, **kwargs):
        return {"goals": [{"id": "g1", "kind": "policy", "description": policy},
                          {"id": "g2", "kind": "sql", "description": sql_question}]}
    model.understand_goal = understand
    model.actions.append(proposal("query_sql", ["g2"], sql_question))
    database = tmp_path / "business.sqlite"
    with sqlite3.connect(database) as con:
        con.executescript("CREATE TABLE orders(order_id TEXT,user_id TEXT,amount INTEGER,status INTEGER,create_time TEXT); CREATE TABLE logistics(logistics_id TEXT,order_id TEXT,status TEXT,update_time TEXT); CREATE TABLE products(sku_id TEXT,name TEXT,stock INTEGER,price INTEGER); INSERT INTO orders VALUES('A','alice',100,3,'2026-10-01'),('B','alice',999,3,'2026-08-01');")
    controller.sql = SQLService(database, anchor_date=controller.business_date)
    async def sql(question, schema, error=None, **kwargs):
        assert "2026-10-01" in schema and "2026-11-01" in schema
        return "SELECT SUM(amount) AS total FROM orders WHERE create_time >= '2026-10-01' AND create_time < '2026-11-01' AND status IN (1,2,3,5,6)"
    model.sql = sql
    result = await chat(controller, policy + "另外" + sql_question)
    assert result["status"] == "COMPLETED", result
    assert "人民币 1.00 元" in result["answer"] and "人民币 9.99 元" not in result["answer"]
    assert "适用日期 2026-09-30" in result["answer"]


@pytest.mark.parametrize("requested,created,version", [("2026-09-30", "2026-10-01", "2"), ("2026-10-01", "2026-09-30", "1")])
async def test_verified_order_date_controls_order_date_policies(setup, requested, created, version):
    controller = await prepare(setup)
    # This fixture explicitly changes the policy time basis; frozen documents stay unchanged.
    controller.retriever = await retriever([replace(c, time_basis="order_create_date") for c in invoice_chunks()])
    model, tools = setup[1], setup[3]
    policy = f"按{requested}的政策，电子发票多久开具？"
    async def understand(*args, **kwargs):
        return {"goals": [{"id": "g1", "kind": "order", "description": "查询订单 A", "subject_candidate": "A"},
                          {"id": "g2", "kind": "policy", "description": policy, "subject_candidate": "A", "deps": ["g1"]}]}
    async def order(order_id, principal, **runtime):
        runtime["reserve_http"]()
        return ToolResult(status="SUCCESS", data={"order_id": "A", "status": 3, "create_time": created + "T12:00:00+08:00"})
    model.understand_goal, tools.query_order = understand, order
    question = "查询订单 A；" + policy
    model.actions = [proposal("query_order", ["g1"], question, "A"), proposal("retrieve_policy", ["g2"], policy)]
    result = await chat(controller, question)
    assert result["status"] == "COMPLETED", result
    assert f"invoice-v{version}.md 第 1 段" in result["answer"]
    assert f"适用日期 {created}" in result["answer"]
