import json
import asyncio

import httpx
import pytest

from agent.config import Settings


@pytest.mark.parametrize("remaining,limit,code", [(0.01, 0.1, "BUDGET_EXHAUSTED"), (0.1, 0.01, "TIMEOUT")])
async def test_deadline_timeout_distinguishes_request_budget_from_provider_timeout(remaining, limit, code):
    from agent.provider import HTTPModelProvider, ProviderError
    async def slow(req):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={})
    events = []
    config = settings().model_copy(update={"model_timeout_seconds": limit})
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        with pytest.raises(ProviderError, match="^" + code + "$"):
            await HTTPModelProvider(config, client, events.append).embed(["工单响应时限"], budget=lambda *args: remaining)
    assert events[0]["status"] == code
    assert events[0]["timeout_source"] == ("request_budget" if remaining < limit else "provider")


def settings():
    return Settings(model_name="chat-test", embedding_model="embed-test", embedding_dimension=8,
                    model_api_key="DO_NOT_EXPOSE", embedding_api_key="DO_NOT_EXPOSE")


def completion(body, **fields):
    return httpx.Response(200, json={"model": "chat-test", "choices": [{
        "finish_reason": "stop", "message": {"content": json.dumps(body)},
    }], **fields})


@pytest.mark.asyncio
async def test_qwen_goal_schema_and_actual_usage_are_visible():
    from agent.provider import HTTPModelProvider
    goal = {"id": "g1", "description": "说明退货期限", "kind": "policy",
            "subject_candidate": None, "source_span": None, "deps": []}

    sent_messages = []

    def handler(req):
        body = json.loads(req.content)
        sent_messages.extend(body["messages"])
        assert req.url.path.endswith("/chat/completions")
        assert (body["temperature"], body["enable_thinking"], body["stream"]) == (0, False, False)
        fmt = body["response_format"]["json_schema"]
        assert fmt["strict"] is True and fmt["schema"]["additionalProperties"] is False
        assert fmt["schema"]["properties"]["goals"]["maxItems"] == 4
        return completion({"goals": [goal], "clarification": None},
                          usage={"prompt_tokens": 21, "completion_tokens": 11, "total_tokens": 32})

    events, calls = [], []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = HTTPModelProvider(settings(), client, events.append)
        result = await model.understand_goal("退货期限？", {}, budget=lambda *args: calls.append(args) or 0.5)
    assert result.goals[0].kind == "policy"
    assert len(calls) == 1 and calls[0][0] == "understand_goal"
    assert len(events) == 1
    assert events[0]["usage"] == {"prompt_tokens": 21, "completion_tokens": 11, "total_tokens": 32}
    assert events[0]["request_messages"] == sent_messages
    assert events[0]["structured_candidate"] == {"goals": [goal], "clarification": None}
    assert "DO_NOT_EXPOSE" not in repr(events)


@pytest.mark.parametrize("mutation,code", [
    ({"action_type": "execute_confirmed_ticket"}, "UNSAFE_PROPOSAL"),
    ({"parameters": {"order_id": "O100", "operation_id": "forged"}}, "UNSAFE_PROPOSAL"),
    ({"task_id": "forged"}, "UNSAFE_PROPOSAL"),
    ({"parameters": {"order_id": "O100", "confirmed": True}}, "UNSAFE_PROPOSAL"),
    ({"receipt_id": "forged"}, "UNSAFE_PROPOSAL"),
    ({"invocation_id": "forged"}, "UNSAFE_PROPOSAL"),
    ({"parameters": {"order_id": "O100", "proposal_ref": "forged"}}, "UNSAFE_PROPOSAL"),
    ({"action_ref": "forged"}, "UNSAFE_PROPOSAL"),
    ({"kind": "MODEL_PROPOSAL"}, "UNSAFE_PROPOSAL"),
    ({"action_type": "CONFIRMED_TICKET"}, "UNSAFE_PROPOSAL"),
    ({"kind": "CONFIRMED_TICKET"}, "UNSAFE_PROPOSAL"),
    ({"parameters": {"order_id": "O100", "payload_hash": "forged"}}, "UNSAFE_PROPOSAL"),
    ({"parameters": {"question": "编造参数"}}, "INVALID_OUTPUT"),
])
async def test_model_cannot_inject_arguments_or_trusted_state(mutation, code):
    from agent.provider import HTTPModelProvider, ProviderError

    body = {"action_type": "query_order", "goal_ids": ["g1"], "parameters": {"order_id": "O100"},
            "parameter_sources": {}, "based_on_observation_ids": [], "public_summary": "查看订单"}
    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: completion(body | mutation))) as client:
        with pytest.raises(ProviderError, match=code):
            await HTTPModelProvider(settings(), client, events.append).decide_next({})
    assert events[0]["usage"] is None


async def test_allowed_order_action_schema_does_not_advertise_ticket_parameters():
    from agent.provider import HTTPModelProvider

    def handler(req):
        body = json.loads(req.content)
        schema = body["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["action_type"]["enum"] == ["query_order"]
        params = schema["properties"]["parameters"]
        assert params == {"$ref": "#/$defs/OrderParameters"}
        return completion({"action_type": "query_order", "goal_ids": ["g1"],
                           "parameters": {"order_id": "O100"}, "public_summary": "查询订单状态",
                           "parameter_sources": {"order_id": {"type": "object", "ref_id": "o1"}},
                           "based_on_observation_ids": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HTTPModelProvider(settings(), client).decide_next({"allowed_actions": ["query_order"]})
    assert result.parameters.order_id == "O100"


@pytest.mark.parametrize("action", ["query_sql", "retrieve_policy"])
async def test_topic_only_action_schema_does_not_request_entity_source_references(action):
    from agent.provider import HTTPModelProvider

    def handler(req):
        body = json.loads(req.content)
        sources = body["response_format"]["json_schema"]["schema"]["properties"]["parameter_sources"]
        # A question is a tool input, not an entity reference to authorize.
        # Advertising arbitrary references caused the live model to invent a
        # message source with no span, twice, instead of querying the order.
        assert sources["additionalProperties"] is False
        assert sources["properties"] == {} and sources["maxProperties"] == 0
        return completion({"action_type": action, "goal_ids": ["g1"],
            "parameters": {"question": "查询订单 A100 的状态和金额"}, "parameter_sources": {},
            "based_on_observation_ids": [], "public_summary": "查询所需信息"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HTTPModelProvider(settings(), client).decide_next({"allowed_actions": [action, "ask_user"]})
    assert result.action_type == action and result.parameter_sources == {}


async def test_decide_prompt_teaches_candidate_coordinates_without_rewriting_model_span():
    from agent.provider import HTTPModelProvider

    context = {
        "question": "请查订单 A100", "allowed_actions": ["query_order"],
        "message_candidates": [{"value": "A100", "span": {"start": 5, "end": 9}}],
        "message_candidates_authority": "仅用于坐标，不授权", "objects": [],
    }

    def handler(req):
        system, user = json.loads(req.content)["messages"]
        assert all(word in system["content"] for word in (
            "message_candidates", "复制", "span.start", "span.end", "objects", "优先", "不授予",
        ))
        assert json.loads(user["content"]) == context
        return completion({"action_type": "query_order", "goal_ids": ["g1"], "parameters": {"order_id": "A100"},
                           "parameter_sources": {"order_id": {"type": "message", "span": {"start": 4, "end": 8}}},
                           "based_on_observation_ids": [], "public_summary": "查询订单"})

    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HTTPModelProvider(settings(), client, events.append).decide_next(context)
    assert result.parameter_sources["order_id"].span.model_dump() == {"start": 4, "end": 8}
    assert events[0]["structured_candidate"]["parameter_sources"]["order_id"]["span"] == {"start": 4, "end": 8}


async def test_invalid_candidate_is_audited_in_one_completion_event_without_hidden_reasoning():
    from agent.provider import HTTPModelProvider, ProviderError

    invalid = {
        "action_type": "query_order",
        "parameters": {"question": "错误参数", "reasoning": "NESTED_THOUGHT", "reasoningContent": "ALIAS_THOUGHT"},
        "parameter_sources": {"order_id": {"type": "message", "span": {"start": 4, "end": 8, "custom": "noise"}}},
        "DO_NOT_EXPOSE": "SECRET_IN_KEY", "arbitrary": {"data": "RAW_UNSCOPED"},
    }

    def handler(req):
        return httpx.Response(200, json={"model": "chat-test", "choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(invalid),
                                                "reasoning_content": "HIDDEN_THOUGHT"},
        }], "usage": {"total_tokens": 17}})

    events, calls = [], []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = HTTPModelProvider(settings(), client, events.append)
        with pytest.raises(ProviderError, match="^INVALID_OUTPUT$"):
            await model.decide_next({}, budget=lambda *args: calls.append(args) or 0.5)
    assert len(events) == len(calls) == 1
    assert events[0]["status"] == "INVALID_OUTPUT" and events[0]["usage"]["total_tokens"] == 17
    assert events[0]["structured_candidate"] == {
        "action_type": "query_order", "parameters": {"question": "错误参数"},
        "parameter_sources": {"order_id": {"type": "message", "span": {"start": 4, "end": 8}}},
    }
    assert "HIDDEN_THOUGHT" not in repr(events) and "NESTED_THOUGHT" not in repr(events)
    assert "DO_NOT_EXPOSE" not in repr(events)


@pytest.mark.parametrize("allowed", [[], ["unknown"], ["finish", "unknown"],
                                      ["finish", "finish"], "finish", None, [1], [{}]])
async def test_explicit_invalid_allowlist_fails_before_budget_and_http(allowed):
    from agent.provider import HTTPModelProvider, ProviderError

    def unexpected(*_):
        pytest.fail("explicit invalid allowlist used budget or attempted HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        provider = HTTPModelProvider(settings(), client)
        with pytest.raises(ProviderError, match="^INVALID_INPUT$"):
            await provider.decide_next({"allowed_actions": allowed}, budget=unexpected)


def test_only_observation_source_accepts_a_field():
    from pydantic import ValidationError

    from agent.contracts import ParameterSource

    assert ParameterSource(type="observation", ref_id="o1", field="order_id").field == "order_id"
    with pytest.raises(ValidationError):
        ParameterSource(type="object", ref_id="o1", field="order_id")


@pytest.mark.parametrize("failure", ["TIMEOUT", "INVALID_OUTPUT", "RATE_LIMITED"])
async def test_transport_failures_never_retry_or_leak_payload(failure):
    from agent.provider import HTTPModelProvider, ProviderError
    calls, events = [], []

    def handler(req):
        calls.append(None)
        if failure == "TIMEOUT":
            raise httpx.ReadTimeout("DO_NOT_EXPOSE")
        if failure == "RATE_LIMITED":
            return httpx.Response(429, content="DO_NOT_EXPOSE")
        return httpx.Response(200, content="DO_NOT_EXPOSE")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match=failure) as exc:
            await HTTPModelProvider(settings(), client, events.append).decide_next({})
    assert len(calls) == len(events) == 1
    assert events[0]["usage"] is None
    assert "DO_NOT_EXPOSE" not in str(exc.value) + repr(events)


@pytest.mark.parametrize("bad_vector", [[1.0] * 7, [1.0] * 7 + ["NaN"], [0.0] * 8])
async def test_embedding_rejects_bad_dimensions_nonfinite_and_zero(bad_vector):
    from agent.provider import HTTPModelProvider, ProviderError

    def handler(req):
        assert json.loads(req.content)["dimensions"] == 8
        return httpx.Response(200, json={"model": "embed-test", "data": [{"index": 0, "embedding": bad_vector}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="INVALID_OUTPUT"):
            await HTTPModelProvider(settings(), client).embed(["正文"])


async def test_budget_denial_and_oversize_context_never_start_http():
    from agent.provider import HTTPModelProvider, ProviderError

    def denied(*_):
        raise RuntimeError("budget used")

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("sent HTTP"))) as client:
        provider = HTTPModelProvider(settings(), client)
        with pytest.raises(RuntimeError, match="budget used"):
            await provider.decide_next({}, budget=denied)
        with pytest.raises(ProviderError, match="INPUT_TOO_LARGE"):
            await provider.decide_next({"question": "中文" * 5_000})


async def test_sql_and_policy_summary_are_bounded_structured_outputs():
    from agent.provider import HTTPModelProvider

    def handler(req):
        body = json.loads(req.content)
        assert body["max_tokens"] <= 2_048
        name = body["response_format"]["json_schema"]["name"]
        if name == "sql_draft":
            return completion({"sql": "SELECT COUNT(*) FROM orders"})
        assert name == "grounded_answer"
        return completion({"claims": [{"text": "签收后十四天可申请。", "evidence_ids": ["e1"]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = HTTPModelProvider(settings(), client)
        assert await provider.sql("几单", "orders(order_id)") == "SELECT COUNT(*) FROM orders"
        result = await provider.answer("售后期限？", {"evidences": [{"evidence_id": "e1"}]})
        assert result.claims[0].evidence_ids == ["e1"]
