"""Real LangGraph behavioral tests; model and transport are explicit component fakes."""
import pytest

from agent.controller import AgentController, ChatRequest
from agent.graph import NORMAL_EDGES
from agent.runtime_store import RuntimeStore
from agent.contracts import Principal, ToolResult


class Model:
    def __init__(self, kind="order", subject="A", actions=None):
        self.kind, self.subject, self.actions = kind, subject, list(actions or [])
        self.calls = []

    async def understand_goal(self, question, context, *, budget=None):
        if budget:
            budget("understand_goal", 100, 100)
        self.calls.append("understand")
        subject = self.subject if self.subject and self.subject in question else None
        return {"goals": [{"id": "g1", "description": question, "kind": self.kind,
                            "subject_candidate": subject, "source_span": (
                                {"start": question.index(subject), "end": question.index(subject) + len(subject)}
                                if subject else None)}]}

    async def decide_next(self, context, *, budget=None):
        if budget:
            budget("decide_next", 100, 100)
        self.calls.append("decide")
        if self.actions:
            return self.actions.pop(0)
        goal = next(g for g in context["goals"] if g["status"] == "OPEN")
        subject = goal.get("subject")
        observations = context["observations"]
        if not subject:
            action, params, sources = "ask_user", {"question": "请提供订单号", "field": "order_id"}, {}
        elif goal["kind"] == "ticket":
            action, params = "request_ticket", {"order_id": subject, "reason": "help"}
            sources = {"order_id": {"type": "message", "span": {"start": context["question"].index(subject), "end": context["question"].index(subject) + len(subject)}}}
        else:
            action = "query_logistics" if goal["kind"] == "logistics" and observations else "query_order"
            params = {"order_id": subject}
            sources = {"order_id": {"type": "message", "span": {"start": context["question"].index(subject), "end": context["question"].index(subject) + len(subject)}}}
        return {"action_type": action, "goal_ids": [goal["goal_id"]], "parameters": params,
                "parameter_sources": sources, "based_on_observation_ids": [], "public_summary": "查询或办理"}


class SQL:
    def __init__(self):
        self.denied = set()

    def owns_order(self, order_id, principal):
        return principal.user_id == "alice" and order_id not in self.denied


class Tools:
    def __init__(self, store):
        self.store, self.gets, self.posts = store, [], []
        self.read_error, self.write_status = None, "SUCCESS"

    async def query_order(self, order_id, principal, **runtime):
        runtime["reserve_http"]()
        self.gets.append(("order", order_id))
        if self.read_error:
            return ToolResult(status="FAILED", error_code=self.read_error)
        return ToolResult(status="SUCCESS", data={"order_id": order_id, "status": 2, "amount": 88800})

    async def query_logistics(self, order_id, principal, **runtime):
        runtime["reserve_http"]()
        self.gets.append(("logistics", order_id))
        return ToolResult(status="SUCCESS", data={"order_id": order_id, "status": "运输中", "logistics_id": "L1"})

    async def create_ticket(self, order_id, reason, operation_id, principal, *, grant):
        claim = self.store.claim(grant)
        if claim.token is None:
            return claim.result
        self.store.start_send(claim.token, principal)
        self.posts.append(order_id)
        result = ToolResult(status=self.write_status, operation_id=operation_id,
                            data={"order_id": order_id, "ticket_id": "T1"} if self.write_status == "SUCCESS" else None,
                            error_code=None if self.write_status == "SUCCESS" else "pending_reconciliation")
        self.store.settle(operation_id, result)
        return result


@pytest.fixture
async def setup(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    model, sql, tools = Model(), SQL(), Tools(store)
    controller = AgentController(store=store, checkpoint_path=tmp_path / "checkpoint.sqlite",
                                 model=model, sql=sql, tools=tools)
    await controller.start()
    yield controller, model, sql, tools
    await controller.aclose()


ALICE = Principal(user_id="alice")
BOB = Principal(user_id="bob")


async def ask(controller, message="查询订单 A", mid="m1", **kw):
    return await controller.handle(ALICE, ChatRequest(message=message, client_message_id=mid, **kw))


async def approve(controller, waiting, mid="m2", message="确认", **kw):
    return await ask(controller, message, mid, session_id=waiting["session_id"],
                     interrupt_id=waiting["interrupt"]["interrupt_id"], expected_revision=waiting["revision"], **kw)


async def test_real_topology_and_order_result(setup):
    c, model, sql, tools = setup
    result = await ask(c)
    assert len(NORMAL_EDGES) == 41
    assert len(set(c.graph.get_graph().nodes) - {"__start__", "__end__"}) == 19
    assert result["status"] == "COMPLETED"
    assert "人民币 888.00 元" in result["answer"] and "88800 分" not in result["answer"]
    assert tools.gets == [("order", "A")]


async def test_real_interrupt_approval_no_second_prepare(setup):
    c, model, _, tools = setup
    model.kind = "ticket"
    waiting = await ask(c, "订单 A 建工单")
    assert waiting["status"] == "WAITING_CONFIRM" and tools.posts == []
    intent = c.store.get_intent(waiting["interrupt"]["interrupt_id"])
    result = await approve(c, waiting)
    assert result["status"] == "COMPLETED" and tools.posts == ["A"]
    assert c.store.get_intent(intent["intent_id"])["expires_at"] == intent["expires_at"]
    assert c.trace.count("prepare_ticket") == 1 and c.trace.count("confirm") == 2


async def test_model_internal_action_denied_before_side_effect(setup):
    c, model, _, tools = setup
    model.actions = [{"action_type": "execute_confirmed_ticket", "operation_id": "forged"}]
    result = await ask(c)
    assert result["status"] == "REJECTED" and tools.posts == [] and tools.gets == []


async def test_cross_actor_rejected_without_checkpoint_or_mutation(setup):
    c, model, _, tools = setup
    model.kind = "ticket"
    waiting = await ask(c, "订单 A 建工单")
    before = c.store.get_intent(waiting["interrupt"]["interrupt_id"])
    with pytest.raises(Exception, match="SESSION_UNAVAILABLE"):
        await c.handle(BOB, ChatRequest(message="确认", client_message_id="bob", session_id=waiting["session_id"],
                                      interrupt_id=before["intent_id"], expected_revision=waiting["revision"]))
    assert c.store.get_intent(before["intent_id"]) == before and tools.posts == []


def proposal(action, ids, question, subject=None):
    return {"action_type": action, "goal_ids": ids, "parameters": {"order_id": subject} if subject else {"question": question},
            "parameter_sources": {"order_id": {"type": "message", "span": {"start": question.index(subject), "end": question.index(subject) + len(subject)}}} if subject else {},
            "based_on_observation_ids": [], "public_summary": "查询所需事实"}


@pytest.mark.parametrize("question", ["查 ABC-123 的状态", "看看 ABC-123 的状态", "确认 ABC-123 建单"])
async def test_locally_anchored_order_id_can_be_verified_without_order_noun(setup, question):
    c, model, _, tools = setup
    model.subject = "ABC-123"
    result = await ask(c, question)
    assert result["status"] == "COMPLETED" and tools.gets == [("order", "ABC-123")]


@pytest.mark.parametrize("question", ["看看 123 元的订单状态", "看看 ABC-1234 的状态"])
async def test_amount_or_id_substring_cannot_authorize_lookup(setup, question):
    c, model, _, tools = setup
    model.subject = "123" if "元" in question else "ABC-123"
    result = await ask(c, question)
    assert result["status"] == "WAITING_INPUT" and not tools.gets and not tools.posts


async def test_filtered_empty_query_cannot_prove_whole_account_has_no_orders(setup):
    from pathlib import Path
    from agent.sql import SQLService

    c, model, _, _ = setup
    c.sql = SQLService(Path("data/c004-r01/baseline/database/ecommerce.db"))
    model.kind, model.subject = "sql", None
    question = "列出我的订单并统计下单总额。"
    model.actions = [proposal("query_sql", ["g1"], question)]
    async def sql(*args, **kwargs):
        return "SELECT order_id, amount FROM orders WHERE 1=0"
    model.sql = sql
    result = await c.handle(Principal(user_id="eval-u01"), ChatRequest(message=question, client_message_id="m1"))
    assert result["goals"][0]["unmet_reason"] == "NO_DATA"
    assert "当前账号没有订单" not in result["answer"] and "人民币 0.00 元" not in result["answer"]


@pytest.mark.parametrize("response,confirm", [
    ("确认按展示的订单和help原因创建工单。", True),
    ("不用创建工单。", False),
    ("确认按展示的订单和新的退款原因创建工单。", False),
    ("确认订单 B，按展示的订单和help原因创建工单。", False),
    ("确认按展示的订单和help原因创建工单，同时也查订单 B。", False),
    ("不确认按展示的订单和help原因创建工单。", False),
    ("也许可以确认按展示的订单和help原因创建工单。", False),
])
async def test_approval_of_displayed_content_requires_exact_frozen_reason(setup, response, confirm):
    c, model, _, tools = setup
    model.kind = "ticket"
    waiting = await ask(c, "订单 A 建工单")
    result = await approve(c, waiting, message=response)
    if confirm:
        assert result["status"] == "COMPLETED" and tools.posts == ["A"]
    elif response.startswith("不用"):
        assert result["status"] == "CANCELLED" and not tools.posts
    else:
        assert result["status"] == "WAITING_INPUT" and not tools.posts


@pytest.mark.parametrize("decision,message,expected", [
    ("CANCEL", "确认按展示的订单和help原因创建工单。", "WAITING_INPUT"),
    ("AMBIGUOUS", "确认按展示的订单和help原因创建工单。", "WAITING_INPUT"),
    ("APPROVE", "不用创建工单。", "CANCELLED"),
    ("APPROVE", "我不同意确认按展示的订单和help原因创建工单。", "WAITING_INPUT"),
    ("APPROVE", "确认 A，同时也查订单 B。", "WAITING_INPUT"),
    ("APPROVE", "不是 A，是 B，帮我建工单", "WAITING_CONFIRM"),
])
async def test_decision_cannot_authorize_conflicting_confirmation_text(setup, decision, message, expected):
    c, model, _, tools = setup
    model.kind = "ticket"
    waiting = await ask(c, "订单 A 建工单")
    if "是 B" in message:
        model.subject = "B"
    result = await approve(c, waiting, message=message, decision=decision)
    assert result["status"] == expected and tools.posts == []


@pytest.mark.parametrize("read_error", [None, "NO_DATA", "service_unavailable", "order_unavailable"])
async def test_explicit_ticket_dependency_survives_early_proposal_and_failed_read(setup, read_error):
    c, model, _, tools = setup
    question = "先查询订单 A 的状态，然后根据结果申请工单"
    async def understand(q, context, *, budget=None):
        start = q.index("A")
        return {"goals": [
            {"id": "read", "kind": "order", "description": "查询订单状态", "subject_candidate": "A",
             "source_span": {"start": start, "end": start + 1}},
            {"id": "write", "kind": "ticket", "description": "根据状态申请工单", "subject_candidate": "A",
             "source_span": {"start": start, "end": start + 1}, "deps": ["read"]},
        ]}
    contexts = []
    original = model.decide_next
    async def decide(context, *, budget=None):
        contexts.append(context.copy())
        return await original(context, budget=budget)
    model.understand_goal, model.decide_next = understand, decide
    model.actions = [proposal("request_ticket", ["write"], question, "A"),
                     proposal("query_order", ["read"], question, "A"),
                     proposal("request_ticket", ["write"], question, "A")]
    tools.read_error = read_error

    result = await ask(c, question)

    assert tools.gets == [("order", "A")] and not tools.posts
    assert {g["goal_id"] for g in contexts[0]["goals"]} == {"read"}
    assert "request_ticket" not in contexts[0]["allowed_actions"]
    if read_error is None:
        assert result["status"] == "WAITING_CONFIRM"
        assert [g["status"] for g in result["goals"]] == ["FULFILLED", "WAITING_CONFIRM"]
        assert contexts[-1]["goals"][0]["depends_on"] == ["read"]
        assert (await approve(c, result))["status"] == "COMPLETED"
        assert tools.posts == ["A"]
    else:
        assert result["interrupt"] is None
        assert result["goals"][1]["unmet_reason"] == "DEPENDENCY"


async def test_gate_independently_blocks_unready_goal_from_trusted_wrapper(setup):
    from types import SimpleNamespace

    from agent.contracts import ActionProposal
    from agent.graph_nodes import GraphNodes

    c, _, sql, tools = setup
    question = "先查询订单 A，再申请工单"
    action = ActionProposal.model_validate(proposal("request_ticket", ["write"], question, "A"))
    state = {"question": question, "task_id": "task", "scope_epoch": 1, "observations": [],
             "verified_objects": [], "candidate_objects": [{"order_id": "A"}], "goals": [
                 {"goal_id": "read", "status": "OPEN", "kind": "order", "depends_on": [], "subject": "A"},
                 {"goal_id": "write", "status": "OPEN", "kind": "ticket", "depends_on": ["read"], "subject": "A",
                  "subject_scope_bindings": []},
             ]}
    run = c.store.start_run("direct-gate-test", ALICE, "m1", question)
    ctx = SimpleNamespace(run=run, principal=ALICE,
                          gate_input={"kind": "MODEL_PROPOSAL", "invocation_id": run["active_invocation_id"],
                                      "action_ref": "proposed", "proposal": action}, sql=sql, store=c.store)
    result = await GraphNodes().gate(state, ctx)
    assert result["route"] == "halt"
    assert result["outcome"]["class"] == "INVALID_PROPOSAL"
    assert tools.gets == tools.posts == []


@pytest.mark.parametrize("status,new_session", [(5, False), (6, False), (5, True), (6, True)])
async def test_abnormal_read_with_unresolved_operation_finishes_without_new_offer(setup, status, new_session):
    c, model, _, tools = setup
    model.kind, tools.write_status = "ticket", "UNKNOWN"
    waiting = await ask(c, "订单 A 建工单")
    previous = await approve(c, waiting)
    op, = previous["operation_refs"]
    original = tools.query_order
    async def abnormal(*args, **kwargs):
        result = await original(*args, **kwargs)
        result.data["status"] = status
        return result
    tools.query_order = abnormal
    model.kind = "order"

    scope = {} if new_session else {"session_id": waiting["session_id"]}
    result = await ask(c, "查询订单 A 的状态", "m3", **scope)

    assert result["status"] == "COMPLETED" and result["interrupt"] is None
    assert [g["status"] for g in result["goals"]] == ["FULFILLED"]
    assert "待核对" in result["answer"] and "请确认" not in result["answer"]
    assert result["operation_refs"] == [op] and op in result["answer"]
    assert c.store.get_operation(op)["state"] == "UNKNOWN" and tools.posts == ["A"]


async def test_same_task_subject_goal_isolation_retains_a_while_b_empty(setup):
    c, model, _, tools = setup
    question = "查询订单 A 和订单 B 的金额"
    async def understand(q, context, *, budget=None):
        return {"goals": [{"id": "ga", "description": "订单 A 金额", "kind": "order", "subject_candidate": "A", "source_span": {"start": q.index("A"), "end": q.index("A") + 1}},
                           {"id": "gb", "description": "订单 B 金额", "kind": "order", "subject_candidate": "B", "source_span": {"start": q.index("B"), "end": q.index("B") + 1}}]}
    model.understand_goal = understand
    model.actions = [proposal("query_order", ["ga"], question, "A"), proposal("query_order", ["gb"], question, "B")]
    original = tools.query_order
    async def read(order_id, principal, **runtime):
        if order_id == "B":
            runtime["reserve_http"]()
            tools.gets.append(("order", "B"))
            return ToolResult(status="FAILED", error_code="NO_DATA")
        return await original(order_id, principal, **runtime)
    tools.query_order = read
    result = await ask(c, question)
    assert result["status"] == "PARTIAL"
    assert [g["status"] for g in result["goals"]] == ["FULFILLED", "BLOCKED"]
    assert result["answer"].count("人民币 888.00 元") == 1
    assert "人民币 888.00 元" not in result["answer"].split("订单 B 金额")[-1]


@pytest.mark.parametrize("status,goal_status,reason", [("CONFLICT", "BLOCKED", "NO_DATA"),
    ("NO_DATA", "BLOCKED", "NO_DATA"), ("NEED_INPUT", "NEEDS_INPUT", None),
    ("RETRIEVAL_UNAVAILABLE", "BLOCKED", "RETRIEVAL_UNAVAILABLE")])
async def test_policy_outcomes_remain_typed(setup, status, goal_status, reason):
    from agent.retriever import PolicyRetrievalResult
    c, model, _, tools = setup
    model.kind, model.subject = "policy", None
    question = "说明当前退货政策"
    model.actions = [proposal("retrieve_policy", ["g1"], question)]
    class Retriever:
        async def retrieve(self, *args, **kwargs):
            return PolicyRetrievalResult(status=status, missing=["channel"] if status == "NEED_INPUT" else [])
    c.retriever = Retriever()
    result = await ask(c, question)
    assert result["goals"][0]["status"] == goal_status
    assert result["goals"][0]["unmet_reason"] == reason
    assert tools.gets == tools.posts == []
    if status == "CONFLICT":
        assert "未决政策冲突" in result["answer"] and result["status"] == "FAILED"


async def test_logistics_prerequisite_enforced_even_if_model_ignores_allowed(setup):
    c, model, _, tools = setup
    model.kind = "logistics"
    question = "查询订单 A 的物流"
    model.actions = [proposal("query_logistics", ["g1"], question, "A")]
    result = await ask(c, question)
    assert result["status"] == "FAILED" and result["reason"] == "INVALID_PROPOSAL"
    assert tools.gets == []


async def test_false_order_source_does_not_access_tool(setup):
    c, model, _, tools = setup
    question = "金额 A 是多少"
    model.actions = [proposal("query_order", ["g1"], question, "A")]
    result = await ask(c, question)
    assert result["status"] == "WAITING_INPUT" and tools.gets == []


async def test_refund_status_six_not_refund_amount_or_date(setup):
    c, model, _, tools = setup
    original = tools.query_order
    async def refund(*args, **kwargs):
        result = await original(*args, **kwargs)
        result.data["status"] = 6
        return result
    tools.query_order = refund
    result = await ask(c, "订单 A 的退款金额和失败原因")
    assert result["goals"][0]["unmet_reason"] == "UNSUPPORTED"
    assert "订单金额 人民币 888.00 元" in result["answer"] and "仅能确认订单级退款失败" in result["answer"]
    assert "退款金额 人民币 888.00 元" not in result["answer"] and tools.posts == []


async def test_public_tool_events_and_private_trace_are_separated(setup):
    c, _, _, _ = setup
    public, private = [], []
    c.trace_emit = lambda e, d: private.append((e, d))
    await c.handle(ALICE, ChatRequest(message="查询订单 A", client_message_id="m1"), lambda e, d: public.append((e, d)))
    assert [e for e, _ in public].count("done") == 1
    assert {"tool_start", "tool_end", "answer", "done"} <= {e for e, _ in public}
    assert {"node", "http_attempt", "tool_result"} <= {e for e, _ in private}
    assert all("request_id" in data and "task_id" in data for _, data in private)
    assert all("sql" not in data and "headers" not in data for event, data in public if event not in {"done", "answer"})


async def test_abnormal_optional_suggestion_preserves_order_goal_and_decline(setup):
    c, _, _, tools = setup
    original = tools.query_order
    async def abnormal(*args, **kwargs):
        result = await original(*args, **kwargs)
        result.data["status"] = 5
        return result
    tools.query_order = abnormal
    first = await ask(c)
    assert first["status"] == "WAITING_CONFIRM"
    assert first["goals"][0]["status"] == "FULFILLED" and len(first["goals"]) == 2
    assert "售后异常" in first["answer"] and "尚未提交" in first["answer"]
    result = await approve(c, first, message="取消")
    assert result["status"] == "COMPLETED" and tools.posts == []


async def test_abnormal_explicit_readonly_refuses_unsolicited_suggestion(setup):
    c, _, _, tools = setup
    original = tools.query_order
    async def abnormal(*args, **kwargs):
        result = await original(*args, **kwargs)
        result.data["status"] = 5
        return result
    tools.query_order = abnormal
    first = await ask(c, "只查订单 A，不要建工单")
    assert first["status"] == "COMPLETED" and first["interrupt"] is None


async def test_abnormal_sql_optional_suggestion_requires_approval_once(setup):
    from agent.contracts import SQLResult
    c, model, sql, tools = setup
    model.kind = "sql"
    question = "查询订单 A 的状态"
    model.actions = [proposal("query_sql", ["g1"], question)]
    async def answer(*args):
        return SQLResult(columns=["order_id", "status"], rows=[{"order_id": "A", "status": 5}],
                         executed_sql="SELECT order_id,status FROM orders WHERE order_id='A'", duration_ms=1)
    sql.answer = answer
    sql.bound_facts = lambda result: [{"table": "orders", "entity_id": "A", "field": "status", "value": 5, "unit": None}]
    first = await ask(c, question)
    assert first["status"] == "WAITING_CONFIRM" and tools.posts == []
    result = await approve(c, first)
    assert result["status"] == "COMPLETED" and tools.posts == ["A"]
    assert (await approve(c, first)) == result and tools.posts == ["A"]


async def test_real_sql_service_correction_binds_integer_cents_and_private_trace(setup, tmp_path):
    import sqlite3
    from agent.sql import SQLService
    c, model, _, _ = setup
    database = tmp_path / "business.sqlite"
    with sqlite3.connect(database) as con:
        con.executescript("CREATE TABLE orders(order_id TEXT,user_id TEXT,amount INTEGER,status INTEGER,create_time TEXT); CREATE TABLE logistics(logistics_id TEXT,order_id TEXT,status TEXT,update_time TEXT); CREATE TABLE products(sku_id TEXT,name TEXT,stock INTEGER,price INTEGER); INSERT INTO orders VALUES('A','alice',88800,2,'2026-11-01');")
    c.sql = SQLService(database)
    model.kind, model.subject = "sql", None
    question = "只查当前用户所有订单的下单金额合计"
    model.actions = [proposal("query_sql", ["g1"], question)]
    calls, private = [], []
    c.trace_emit = lambda e, d: private.append((e, d))
    async def sql(question, schema, error=None, *, budget=None):
        calls.append(error)
        if budget:
            budget("sql", 100, 100)
        return "SELECT SUM(amunt) AS total FROM orders" if len(calls) == 1 else "SELECT SUM(amount) AS total FROM orders"
    model.sql = sql
    result = await ask(c, question)
    assert result["status"] == "COMPLETED" and "人民币 888.00 元" in result["answer"]
    assert len(calls) == 2 and calls[0] is None and calls[1] is not None
    assert len([e for e, _ in private if e == "sql_candidate"]) == 2


@pytest.mark.parametrize("body", ["七天内可退货。", "退货期限七天。"])
async def test_real_retriever_through_graph_has_scoped_citations(setup, tmp_path, body):
    from test_retrieval import article
    from agent.retriever import PolicyRetriever, parse_documents, collection_for
    from agent.vector_store import MemoryVectorStore
    c, model, _, _ = setup
    directory = tmp_path / "policies"
    directory.mkdir()
    article(directory, "returns-v1", "returns", body, 7)
    chunks = parse_documents(directory)
    spec = collection_for(chunks, "fixture", 2)
    vectors = MemoryVectorStore()
    await vectors.publish(spec, {chunk.chunk_id: [1., 0.] for chunk in chunks})
    async def embed(texts, *, budget=None):
        if budget:
            budget("embed", 100, 0)
        return [[1., 0.] for _ in texts]
    model.embed = embed
    c.retriever = PolicyRetriever(chunks, spec, vectors, model)
    model.kind, model.subject = "policy", None
    question = "当前退货期限是多久"
    model.actions = [proposal("retrieve_policy", ["g1"], question)]
    result = await ask(c, question)
    assert result["status"] == "COMPLETED"
    assert "returns-v1.md" in result["answer"] and "2026-11-15" in result["answer"] and "七天" in result["answer"]


@pytest.mark.parametrize("question,deny,status", [("查询订单 A", True, "REJECTED"), ("金额 A 是多少", False, "WAITING_INPUT")])
async def test_message_candidate_spans_help_copying_but_never_authorize(setup, question, deny, status):
    c, model, sql, tools = setup
    seen = []
    async def decide(context, *, budget=None):
        seen.append(context)
        candidate = context["message_candidates"][0]
        assert candidate == {"value": "A", "span": {"start": question.index("A"), "end": question.index("A") + 1}}
        assert "未授权候选" in context["message_candidates_authority"]
        return {"action_type": "query_order", "goal_ids": ["g1"], "parameters": {"order_id": candidate["value"]},
                "parameter_sources": {"order_id": {"type": "message", "span": candidate["span"]}},
                "based_on_observation_ids": [], "public_summary": "查询订单"}
    model.decide_next = decide
    if deny:
        sql.denied.add("A")
    result = await ask(c, question)
    assert seen and result["status"] == status and tools.gets == tools.posts == []


async def test_delivered_logistics_answer_includes_id_dictionary_status_and_updated_time(setup):
    c, model, _, tools = setup
    model.kind = "logistics"
    original = tools.query_logistics
    async def logistics(*args, **kwargs):
        result = await original(*args, **kwargs)
        result.data.update(logistics_id="L-verified", status=2, update_time="2026-11-14 15:30:00")
        return result
    tools.query_logistics = logistics
    result = await ask(c, "查询订单 A 的物流编号、状态和更新时间")
    assert result["status"] == "COMPLETED"
    assert "L-verified" in result["answer"] and "派送中" in result["answer"]
    assert "2026-11-14 15:30:00" in result["answer"] and "编码 2" in result["answer"]


@pytest.mark.parametrize("count_expression,count_text,task_status", [("COUNT(*)", "订单数量 = 2 单", "COMPLETED"),
    ("COUNT(amount)", "amount 非空值数量 = 2 个", "COMPLETED"),
    ("COUNT(amount + 1)", "字段或计数来源无法核验：order_count", "FAILED")])
async def test_delivered_sql_answer_includes_count_and_amount_with_distinct_units(setup, tmp_path, count_expression, count_text, task_status):
    import sqlite3
    from agent.sql import SQLService
    c, model, _, _ = setup
    database = tmp_path / "business-count.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("CREATE TABLE orders(order_id TEXT,user_id TEXT,amount INTEGER,status INTEGER,create_time TEXT); CREATE TABLE logistics(logistics_id TEXT,order_id TEXT,status INTEGER,update_time TEXT); CREATE TABLE products(sku_id TEXT,name TEXT,stock INTEGER,price INTEGER); INSERT INTO orders VALUES('A','alice',1200,2,'2026-11-01'),('B','alice',2300,2,'2026-11-02'),('X','bob',99999,2,'2026-11-01');")
    c.sql = SQLService(database)
    model.kind, model.subject = "sql", None
    question = "只查当前用户订单金额合计以及数量"
    model.actions = [proposal("query_sql", ["g1"], question)]
    async def sql(*args, **kwargs):
        return f"SELECT SUM(amount) AS amount_cents, {count_expression} AS order_count FROM orders"
    model.sql = sql
    result = await ask(c, question)
    assert result["status"] == task_status
    assert "人民币 35.00 元" in result["answer"] and count_text in result["answer"]
    assert "99999" not in result["answer"] and "999.99 元" not in result["answer"]


async def test_delivered_logistics_missing_fields_and_unknown_status_are_explicit(setup):
    c, model, _, tools = setup
    model.kind = "logistics"
    original = tools.query_logistics
    async def logistics(*args, **kwargs):
        result = await original(*args, **kwargs)
        result.data.update(logistics_id=None, status=99, update_time=None)
        return result
    tools.query_logistics = logistics
    result = await ask(c, "查询订单 A 的物流")
    assert "物流编号 未提供" in result["answer"] and "更新时间 未提供" in result["answer"]
    assert "语义未确认（编码 99）" in result["answer"]
    assert "已签收" not in result["answer"] and "None" not in result["answer"]
