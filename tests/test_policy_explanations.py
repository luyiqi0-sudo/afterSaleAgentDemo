"""Real policy adjudication and Graph regressions for version explanations."""
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from agent.answer_support import policy_for_goal
from agent.contracts import Principal
from agent.controller import ChatRequest
from agent.retriever import PolicyRetriever, collection_for, parse_documents
from agent.vector_store import MemoryVectorStore
from test_graph import setup as setup, proposal


QUESTION = "电子发票开具时限，有的政策说10个工作日，有的说5个工作日，当前应以哪个为准？请说明新旧政策差异，并引用来源文档名和段落。"
SCOPE = {"channel": "online", "region": "CN"}
DAY = date(2026, 11, 15)


class Embedder:
    async def embed(self, texts, *, budget=None):
        return [[1.0, 0.0] for _ in texts]


def invoice_chunks():
    root = Path(__file__).resolve().parents[1] / "data/c004-r01/baseline/knowledge"
    return [c for c in parse_documents(root) if c.policy_key == "invoice"]


async def retriever(chunks):
    spec = collection_for(chunks, "component-embedding", 2)
    store = MemoryVectorStore()
    await store.publish(spec, {c.chunk_id: [1.0, 0.0] for c in chunks})
    return PolicyRetriever(chunks, spec, store, Embedder(), top_k=1)


async def test_replacement_exposes_old_source_without_making_it_current():
    rag = await retriever(invoice_chunks())
    result = await rag.retrieve(QUESTION, query_date=DAY, conditions=SCOPE)
    assert result.status == "SUCCESS"
    assert [e.doc_id for e in result.evidences] == ["invoice-v2"]
    replacements = getattr(result, "replacements", [])
    assert len(replacements) == 1
    comparison = replacements[0]
    assert comparison.current_doc_id == "invoice-v2"
    assert comparison.previous_doc_id == "invoice-v1"
    assert comparison.supersedes_path == ["invoice-v2", "invoice-v1"]
    assert len(comparison.sources) == 1
    old = comparison.sources[0]
    assert (old.file_name, old.paragraph, old.version) == ("invoice-v1.md", 1, "1")
    assert old.text == "订单完成且开票资料核验通过后，电子发票在十个工作日内开具。"
    assert old.rules == {"issue_business_days": 10}


async def test_future_version_is_not_used_as_a_replacement():
    rag = await retriever(invoice_chunks())
    result = await rag.retrieve(QUESTION, query_date=date(2026, 9, 30), conditions=SCOPE)
    assert [e.doc_id for e in result.evidences] == ["invoice-v1"]
    assert not getattr(result, "replacements", [])


@pytest.mark.parametrize("split_goals", [False, True, "live_paraphrase"])
async def test_original_question_delivers_both_versions_and_citations(setup, split_goals):
    c, model, _, tools = setup
    c.retriever = await retriever(invoice_chunks())
    model.embed = Embedder().embed

    async def understand(*args, **kwargs):
        if split_goals is True:
            return {"goals": [
                {"id": "g1", "kind": "policy", "description": "查询电子发票开具时限的政策规定，确认当前应以哪个为准（10个工作日还是5个工作日）"},
                {"id": "g2", "kind": "policy", "description": "说明新旧政策差异", "deps": ["g1"]},
                {"id": "g3", "kind": "policy", "description": "引用来源文档名和段落", "deps": ["g1"]},
            ]}
        description = ("查询电子发票开具时限政策，说明当前应以哪个为准（10个工作日还是5个工作日），并说明新旧政策差异、适用版本说明，引用来源文档名和段落。"
                       if split_goals == "live_paraphrase" else QUESTION)
        return {"goals": [{"id": "g1", "kind": "policy", "description": description}]}

    model.understand_goal = understand
    model.actions = [proposal("retrieve_policy", ["g1"], QUESTION)]
    result = await c.handle(Principal(user_id="alice", conditions=SCOPE),
                            ChatRequest(message=QUESTION, client_message_id="version-check"))
    assert result["status"] == "COMPLETED", result["answer"]
    assert len(result["goals"]) == 1 and result["goals"][0]["status"] == "FULFILLED"
    for literal in ("五个工作日", "十个工作日", "invoice-v2.md 第 1 段", "invoice-v1.md 第 1 段",
                    "2026-10-01", "2025-01-01", "替代", "不作为当前"):
        assert literal in result["answer"]
    assert c.trace.count("rag") == 1
    assert tools.gets == [] and tools.posts == []


async def test_model_cannot_omit_an_unknown_facet_of_a_single_policy_question(setup):
    c, model, _, _ = setup
    c.retriever = await retriever(invoice_chunks())
    model.embed = Embedder().embed

    async def understand(*args, **kwargs):
        return {"goals": [{"id": "g1", "kind": "policy", "description": "电子发票开具时限"}]}

    model.understand_goal = understand
    model.actions = [proposal("retrieve_policy", ["g1"], "电子发票开具时限")]
    question = "电子发票开具时限，以及手续费是多少？"
    result = await c.handle(Principal(user_id="alice", conditions=SCOPE),
                            ChatRequest(message=question, client_message_id="missing-facet"))
    assert result["status"] != "COMPLETED" and "未覆盖" in result["answer"]
    assert "手续费" in result["answer"]


async def test_comparison_requires_verified_history_not_just_a_note():
    rag = await retriever(invoice_chunks())
    result = await rag.retrieve(QUESTION, query_date=DAY, conditions=SCOPE)
    data = result.model_dump(mode="json")
    data["replacements"] = []
    projected, reason, gaps = policy_for_goal({"description": QUESTION}, data)
    assert reason == "NO_DATA" and gaps.get("policy_uncovered")
    assert projected and projected["evidences"][0]["doc_id"] == "invoice-v2"


async def test_bad_historical_body_cannot_be_quoted_as_checked_history():
    chunks = [replace(c, text="电子发票在九十九个工作日内开具。")
              if c.doc_id == "invoice-v1" and c.anchor == "policy-rule" else c for c in invoice_chunks()]
    result = await (await retriever(chunks)).retrieve(QUESTION, query_date=DAY, conditions=SCOPE)
    assert result.status == "SUCCESS" and result.evidences[0].doc_id == "invoice-v2"
    assert not getattr(result, "replacements", [])
    _, reason, gaps = policy_for_goal({"description": QUESTION}, result.model_dump(mode="json"))
    assert reason == "NO_DATA" and gaps.get("policy_uncovered")


async def test_historical_metadata_alone_does_not_prove_comparison_content():
    chunks = [replace(c, text="会员积分可以兑换商品。")
              if c.doc_id == "invoice-v1" and c.anchor == "policy-rule" else c for c in invoice_chunks()]
    result = await (await retriever(chunks)).retrieve(QUESTION, query_date=DAY, conditions=SCOPE)
    projected, reason, gaps = policy_for_goal({"description": QUESTION}, result.model_dump(mode="json"))
    assert reason == "NO_DATA" and gaps.get("policy_uncovered")
    assert projected and not projected["replacements"]


@pytest.mark.parametrize("extra", ["说明跨境税率差异", "引用发票申请入口网址", "说明手续费是多少"])
async def test_output_instructions_do_not_hide_missing_business_facts(extra):
    result = await (await retriever(invoice_chunks())).retrieve(QUESTION, query_date=DAY, conditions=SCOPE)
    _, reason, gaps = policy_for_goal({"description": "电子发票开具时限；" + extra}, result.model_dump(mode="json"))
    assert reason == "NO_DATA" and gaps.get("policy_uncovered")


async def test_metadata_only_question_does_not_invent_a_policy_topic():
    result = await (await retriever(invoice_chunks())).retrieve(QUESTION, query_date=DAY, conditions=SCOPE)
    data, reason, _ = policy_for_goal({"description": "说明新旧政策差异"}, result.model_dump(mode="json"))
    assert data is None and reason == "NO_DATA"


async def test_newer_date_alone_cannot_resolve_a_real_conflict():
    chunks = [replace(c, supersedes=()) for c in invoice_chunks()]
    result = await (await retriever(chunks)).retrieve(QUESTION, query_date=DAY, conditions=SCOPE)
    assert result.status == "CONFLICT" and result.conflicts[0].reason == "NO_AUTHORITATIVE_REPLACEMENT"
    assert not result.evidences and not result.replacements


@pytest.mark.parametrize("scope,status", [({}, "NEED_INPUT"), ({"channel": "offline", "region": "CN"}, "NO_DATA")])
async def test_history_does_not_bypass_applicability(scope, status):
    result = await (await retriever(invoice_chunks())).retrieve(QUESTION, query_date=DAY, conditions=scope)
    assert result.status == status and not result.evidences and not result.replacements


async def test_order_date_still_controls_return_policy_comparison():
    root = Path(__file__).resolve().parents[1] / "data/c004-r01/baseline/knowledge"
    # Explicit component fixture for order-date policies; the frozen baseline
    # itself declares query_date and is not rewritten by this test.
    chunks = [replace(c, time_basis="order_create_date") for c in parse_documents(root) if c.policy_key == "returns"]
    rag = await retriever(chunks)
    older = await rag.retrieve("普通商品退货期限", query_date=DAY, conditions=SCOPE,
                               order_create_date=date(2026, 9, 30))
    newer = await rag.retrieve("普通商品退货期限", query_date=DAY, conditions=SCOPE,
                               order_create_date=date(2026, 10, 1))
    assert [e.doc_id for e in older.evidences] == ["returns-v1"] and not older.replacements
    assert [e.doc_id for e in newer.evidences] == ["returns-v2"]
    assert newer.replacements[0].previous_doc_id == "returns-v1"


async def test_expired_successor_does_not_reactivate_old_policy():
    chunks = [replace(c, expires_at=date(2026, 11, 1)) if c.doc_id == "invoice-v2" else c
              for c in invoice_chunks()]
    result = await (await retriever(chunks)).retrieve(QUESTION, query_date=DAY, conditions=SCOPE)
    assert result.status == "NO_DATA" and not result.evidences and not result.replacements


def test_output_dependency_chain_is_independent_of_model_goal_order():
    from agent.contracts import GoalDraft
    from agent.policy_requests import merge_policy_presentations

    draft = GoalDraft.model_validate({"goals": [
        {"id": "g3", "kind": "policy", "description": "引用来源文档名和段落", "deps": ["g2"]},
        {"id": "g2", "kind": "policy", "description": "说明新旧政策差异", "deps": ["g1"]},
        {"id": "g1", "kind": "policy", "description": "电子发票开具时限"},
    ]})
    merged = merge_policy_presentations(draft)
    assert len(merged.goals) == 1 and merged.goals[0].id == "g1"
    assert "新旧政策差异" in merged.goals[0].description and "引用来源" in merged.goals[0].description


def test_business_or_ambiguous_dependencies_are_not_collapsed():
    from agent.contracts import GoalDraft
    from agent.policy_requests import merge_policy_presentations

    draft = GoalDraft.model_validate({"goals": [
        {"id": "a", "kind": "policy", "description": "电子发票开具时限"},
        {"id": "b", "kind": "policy", "description": "普通商品退货期限"},
        {"id": "c", "kind": "policy", "description": "说明新旧政策差异", "deps": ["a", "b"]},
        {"id": "d", "kind": "policy", "description": "说明新旧政策差异和跨境税率", "deps": ["a"]},
    ]})
    assert merge_policy_presentations(draft) == draft
