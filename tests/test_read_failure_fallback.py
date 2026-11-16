"""Real graph recovery; explicit model/transport doubles, no remote writes."""
import pytest

from agent.events import PrivateTrace
from agent.runtime_types import RuntimeConflict
from test_graph import ask, approve, setup as setup


async def test_read_exhaustion_offers_confirm_then_one_ticket(setup):
    controller, model, _, tools = setup
    tools.read_error = "service_unavailable"
    first = await ask(controller)
    assert first["status"] == "WAITING_CONFIRM"
    assert "读取服务暂不可用" in first["answer"]
    assert "订单不存在" in first["answer"] and "尚未提交" in first["answer"]
    assert first["interrupt"]["reason"] == "订单查询服务持续失败，请人工核查"
    assert first["goals"][0]["unmet_reason"] == "READ_EXHAUSTED"
    assert tools.posts == [] and tools.gets == [("order", "A")]
    assert model.calls == ["understand", "decide"]  # recovery must not need another LLM call

    done = await approve(controller, first)
    assert done["goals"][0]["unmet_reason"] == "READ_EXHAUSTED"
    assert "工单已创建" in done["answer"]
    assert tools.posts == ["A"] and tools.gets == [("order", "A")]
    assert await approve(controller, first) == done
    with pytest.raises(RuntimeConflict, match="STALE_INTERRUPT"):
        await approve(controller, first, mid="duplicate")
    assert tools.posts == ["A"]


async def test_read_exhaustion_cancel_never_submits(setup):
    controller, _, _, tools = setup
    tools.read_error = "service_unavailable"
    first = await ask(controller)
    assert first["status"] == "WAITING_CONFIRM"
    done = await approve(controller, first, message="取消")
    assert "已取消，未提交" in done["answer"] and not tools.posts


@pytest.mark.parametrize("suffix", ["，只查询", "，不用创建售后工单", "，不需要建单"])
async def test_read_only_failure_never_offers_ticket(setup, suffix):
    controller, _, _, tools = setup
    tools.read_error = "service_unavailable"
    done = await ask(controller, "查询订单 A" + suffix)
    assert done["interrupt"] is None and "读取服务暂不可用" in done["answer"]
    assert all(g.get("subject") != "A" or g["goal_id"] == "g1" for g in done["goals"])
    assert not tools.posts


@pytest.mark.parametrize("error", ["NO_DATA", "order_unavailable", "RESPONSE_MISMATCH"])
async def test_other_failures_do_not_offer_recovery_ticket(setup, error):
    controller, _, _, tools = setup
    tools.read_error = error
    done = await ask(controller)
    assert done["interrupt"] is None and not tools.posts
    assert len(done["goals"]) == 1


def test_retry_delay_is_retained_in_private_trace(tmp_path):
    import json

    path = tmp_path / "trace.jsonl"
    PrivateTrace(path)("tool_retry", {"tool": "query_order", "attempt": 1,
                                    "delay_seconds": .25, "authorization": "secret"})
    assert path.exists(), "retry event must be observable"
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["delay_seconds"] == .25 and event["attempt"] == 1
    assert "authorization" not in event
