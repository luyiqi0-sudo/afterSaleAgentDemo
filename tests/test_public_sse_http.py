"""Real TCP verifies graph summaries are delivered before later tools finish."""
import asyncio
import json

import httpx

from agent.config import Settings
from api.main import create_app
from eval.runner import serve
from test_graph import setup as setup


async def test_wire_delivers_verified_order_summary_before_logistics_result(setup):
    controller, model, _, tools = setup
    model.kind = "logistics"
    release = asyncio.Event()
    original = tools.query_logistics

    async def logistics(*args, **kwargs):
        await release.wait()
        return await original(*args, **kwargs)

    tools.query_logistics = logistics
    app = create_app(Settings(_env_file=None, demo_mode=True, auth_tokens={"owner": "alice"}), controller=controller)
    rows = []
    try:
        async with serve(app) as base, httpx.AsyncClient(base_url=base, timeout=5) as client:
            async with client.stream("POST", "/chat", headers={"Authorization": "Bearer owner"},
                                     json={"message": "查询订单 A 的物流", "client_message_id": "stream"}) as response:
                assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    frame = json.loads(line[6:])
                    rows.append(frame)
                    if frame["type"] in {"stage", "tool_start", "tool_end", "tool_error"}:
                        assert frame["data"]["summary"]
                    if frame["type"] == "tool_start" and frame["data"]["tool"] == "query_logistics":
                        first = next(row for row in rows if row["type"] == "tool_end")
                        assert "已发货" in first["data"]["summary"]
                        assert not any(row["type"] == "answer_delta" for row in rows)
                        release.set()
    finally:
        release.set()
    assert rows[0]["data"]["action"] == "understand"
    assert len({row["request_id"] for row in rows}) == 1
    assert len({row["id"] for row in rows}) == len(rows)
    assert [row["type"] for row in rows].count("done") == 1
    assert rows[-1]["data"]["status"] == "COMPLETED"
    ends = [row for row in rows if row["type"] == "tool_end"]
    assert [row["data"]["tool"] for row in ends] == ["query_order", "query_logistics"]
    assert "L1" in ends[-1]["data"]["summary"] and "888.00" not in ends[-1]["data"]["summary"]
