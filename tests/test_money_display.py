"""Currency is converted only after verification, for both public outputs."""
import sqlite3

import pytest

from agent.controller import ChatRequest
from agent.sql import SQLService
from test_graph import ALICE, proposal, setup as setup


@pytest.mark.parametrize("fen,yuan", [(0, "0.00"), (1, "0.01"), (137402, "1374.02"), (1000000, "10000.00")])
async def test_order_amount_uses_yuan_in_summary_and_final_answer(setup, fen, yuan):
    controller, _, _, tools = setup
    query_order = tools.query_order

    async def read_order(*args, **kwargs):
        result = await query_order(*args, **kwargs)
        result.data["amount"] = fen
        return result

    tools.query_order = read_order
    events = []
    done = await controller.handle(ALICE, ChatRequest(message="查询订单 A 的状态和金额", client_message_id="money"),
                                   emit=lambda name, data: events.append((name, data)))
    assert done["status"] == "COMPLETED"
    summary = next(data["summary"] for name, data in events if name == "tool_end")
    for text in (summary, done["answer"]):
        assert f"人民币 {yuan} 元" in text
        assert " 分" not in text
        assert "已发货" in text and "编码 2" in text


@pytest.mark.parametrize("question,query,expected,missing", [
    ("统计我的订单金额合计和数量",
     "SELECT SUM(amount) AS total, COUNT(*) AS n, COUNT(amount) AS available FROM orders WHERE status=2",
     ["人民币 5.35 元", "订单数量 = 3 单", "amount 非空值数量 = 2 个"], []),
    ("统计我的平均订单金额",
     "SELECT AVG(amount) AS average FROM orders WHERE status=2",
     ["人民币 2.68 元"], ["2.67 元"]),
    ("统计我的最低和最高订单金额",
     "SELECT MIN(amount) AS low, MAX(amount) AS high FROM orders WHERE status=2",
     ["人民币 0.01 元", "人民币 5.34 元"], []),
    ("查看我的订单金额",
     "SELECT order_id,amount FROM orders WHERE order_id='A0'",
     ["人民币 0.00 元"], []),
    ("查询我的订单金额和数量",
     "SELECT SUM(amount) AS total, COUNT(*) AS n FROM orders WHERE order_id='absent'",
     ["amount 未提供，不能推断为 0", "订单数量 = 0 单"], ["人民币 0.00 元"]),
    ("查询商品价格和库存",
     "SELECT sku_id,price,stock FROM products",
     ["人民币 19.99 元", "stock = 12 件"], ["0.12"]),
    ("查询我的订单金额",
     "SELECT order_id,amount AS yuan FROM orders WHERE order_id='A2'",
     ["人民币 5.34 元"], ["534.00 元"]),
    ("查询我的订单数量",
     "SELECT COUNT(amount) AS amount_yuan FROM orders WHERE status=2",
     ["amount 非空值数量 = 2 个"], ["人民币", "0.02"]),
])
async def test_sql_money_counts_and_unknown_values_keep_their_meaning(setup, tmp_path, question, query, expected, missing):
    controller, model, _, _ = setup
    database = tmp_path / "money.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE orders(order_id TEXT,user_id TEXT,amount INTEGER,status INTEGER,create_time TEXT);
            CREATE TABLE products(sku_id TEXT,name TEXT,stock INTEGER,price INTEGER);
            INSERT INTO orders VALUES
                ('A0','alice',0,1,'2026-10-01'),('A1','alice',1,2,'2026-10-02'),
                ('A2','alice',534,2,'2026-10-03'),('N','alice',NULL,2,'2026-10-04'),
                ('FOREIGN','bob',900000,2,'2026-10-05');
            INSERT INTO products VALUES ('P1','商品',12,1999);
        """)
    service = controller.sql = SQLService(database)
    model.kind, model.subject = "sql", None
    model.actions = [proposal("query_sql", ["g1"], question)]

    async def sql(*args, **kwargs):
        return query

    model.sql = sql
    events = []
    done = await controller.handle(ALICE, ChatRequest(message=question, client_message_id="money"),
                                   emit=lambda name, data: events.append((name, data)))
    summary = next(data["summary"] for name, data in events if name in {"tool_end", "tool_error"})
    for text in (summary, done["answer"]):
        assert all(fragment in text for fragment in expected), text
        assert not any(fragment in text for fragment in missing), text
        assert " 分" not in text and "9000.00" not in text
    # Display conversion cannot alter the storage or verifier-owned numeric facts.
    raw = service.execute("SELECT order_id,amount FROM orders WHERE order_id='A2'", ALICE)
    assert raw.rows == [{"order_id": "A2", "amount": 534}]
    money = next(f for f in service.bound_facts(raw) if f["field"] == "amount")
    assert (money["value"], money["unit"]) == (534, "fen")
