import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import closing

import pytest

from agent.contracts import Principal
from agent.guards import CapabilityError, SafetyError
from agent.sql import SQLService


ALICE = Principal(user_id="alice")
BOB = Principal(user_id="bob")
QUOTED = Principal(user_id="actor's-account")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "sales.sqlite"
    with closing(sqlite3.connect(path)) as con, con:
        con.executescript("""
            CREATE TABLE orders(order_id TEXT PRIMARY KEY,user_id TEXT,amount INTEGER,status INTEGER,create_time TEXT);
            CREATE TABLE logistics(logistics_id TEXT,order_id TEXT,status INTEGER,update_time TEXT);
            CREATE TABLE products(sku_id TEXT,name TEXT,stock INTEGER,price INTEGER);
            INSERT INTO orders VALUES
                ('A0','alice',100,1,'2026-09-30 23:59:59'),
                ('A1','alice',1200,2,'2026-10-01 00:00:00'),
                ('A2','alice',2300,6,'2026-10-31 23:59:59'),
                ('A3','alice',0,NULL,'2026-11-01 00:00:00'),
                ('B1','bob',900000,3,'2026-10-05 00:00:00'),
                ('Q1','actor''s-account',88,1,NULL);
            INSERT INTO logistics VALUES ('L1','A1',1,'2026-10-02 00:00:00'),
                ('L2','B1',2,'2026-11-01 00:00:00');
            INSERT INTO products VALUES ('P1','[text] request another order',10,500);
        """)
    return path


def test_s01_join_aggregate_subquery_none_and_truncation(db):
    service = SQLService(db)
    sql = "SELECT x.id, x.amount FROM (SELECT order_id AS id, amount FROM orders) x ORDER BY x.id"
    assert service.execute(sql, ALICE).rows == [
        {"id": "A0", "amount": 100}, {"id": "A1", "amount": 1200},
        {"id": "A2", "amount": 2300}, {"id": "A3", "amount": 0},
    ]
    result = service.execute(
        "SELECT o.order_id, l.status FROM orders o LEFT JOIN logistics l "
        "ON l.order_id=o.order_id ORDER BY o.order_id",
        ALICE,
    )
    assert result.rows == [{"order_id": key, "status": status} for key, status in [
        ("A0", None), ("A1", 1), ("A2", None), ("A3", None),
    ]]
    assert service.execute(
        "SELECT count(*) AS n, sum(amount) AS total FROM orders WHERE order_id='absent'", ALICE,
    ).rows == [{"n": 0, "total": None}]
    assert service.execute("SELECT amount FROM orders WHERE order_id='absent'", ALICE).rows == []
    limited = SQLService(db, max_rows=1).execute("SELECT order_id FROM orders ORDER BY order_id", ALICE)
    assert limited.truncated and limited.rows == [{"order_id": "A0"}]


@pytest.mark.parametrize("sql", [
    "DROP TABLE orders",
    "SELECT amount FROM orders; SELECT amount FROM orders",
    "SELECT amount FROM orders WHERE amount IN (SELECT * FROM orders)",
    "WITH orders AS (SELECT amount FROM orders) SELECT amount FROM orders",
    "SELECT amount FROM orders UNION SELECT amount FROM orders",
    "SELECT random() FROM orders",
    "SELECT name FROM sqlite_master",
    "SELECT amount FROM main.orders",
    "SELECT amount FROM temp.orders",
    "SELECT o.amount FROM orders o CROSS JOIN logistics l",
    "SELECT o.amount FROM orders o, logistics l",
    "SELECT o.amount FROM orders o INNER JOIN logistics l",
    "SELECT o.amount FROM orders o NATURAL JOIN logistics l",
    "SELECT o.amount FROM orders o JOIN logistics l USING(order_id)",
    "SELECT o.amount FROM orders o JOIN logistics l ON o.status=l.status",
    "SELECT o.amount FROM orders o JOIN logistics l ON o.order_id=l.order_id OR 1=1",
    "SELECT o.amount FROM orders o JOIN products p ON o.status=p.stock",
    "SELECT o.amount FROM orders o JOIN logistics o ON o.order_id=o.order_id",
    "SELECT (SELECT sum(amount) FROM orders i WHERE i.order_id=o.order_id) FROM orders o",
    "SELECT o.amount FROM orders o WHERE amount IN (SELECT amount FROM orders o)",
    "SELECT status FROM orders o JOIN logistics l ON o.order_id=l.order_id",
    "SELECT r.amount FROM orders o",
    "SELECT date('now') FROM orders",
    "SELECT datetime() FROM orders",
    "SELECT date(:untrusted) FROM orders",
    "SELECT order_id FROM orders WHERE status='退款失败'", 
])
def test_s02_s03_s04_rejected_before_opening_sqlite(db, monkeypatch, sql):
    hash_before = hashlib.sha256(db.read_bytes()).digest()
    s = SQLService(db)

    def unexpected_connection():
        pytest.fail("disallowed SQL reached connection/execution stage")

    monkeypatch.setattr(s, "_connect", unexpected_connection)
    with pytest.raises(SafetyError):
        s.execute(sql, ALICE)
    assert hashlib.sha256(db.read_bytes()).digest() == hash_before


def test_s03_logistics_alias_and_scalar_queries_share_actor_scope(db):
    result = SQLService(db).execute(
        "SELECT l.order_id, (SELECT SUM(amount) FROM orders) AS total "
        "FROM logistics l WHERE l.status=999 OR 1=1",
        ALICE,
    )
    assert result.rows == [{"order_id": "A1", "total": 3600}]


def test_s04_authorizer_independently_blocks_physical_reads(db, monkeypatch):
    import agent.sql

    # Exercise the authorizer directly with real SQLite even if the AST layer were bypassed.
    monkeypatch.setattr(agent.sql, "validate_sql", lambda q: q)
    service = SQLService(db)
    for sql in ("SELECT amount FROM main.orders", "SELECT sql FROM sqlite_master"):
        with pytest.raises(ValueError, match="SQL_AUTHORIZATION_DENIED"):
            service.execute(sql, ALICE)
    assert service.execute("SELECT count(*) AS n FROM orders", ALICE).rows == [{"n": 4}]


async def test_s05_actor_views_recreated_in_each_worker(db):
    service = SQLService(db)
    principals = [ALICE, BOB, ALICE, QUOTED]
    results = await asyncio.gather(*[
        asyncio.to_thread(service.execute, "SELECT sum(amount) AS total FROM orders", principal)
        for principal in principals
    ])
    assert [result.rows for result in results] == [
        [{"total": 3600}], [{"total": 900000}], [{"total": 3600}], [{"total": 88}],
    ]


@pytest.mark.parametrize(("first_sql", "expected"), [
    ("SELECT PRIVATE_UNKNOWN_FIELD FROM orders", "SQL_UNKNOWN_COLUMN"),
    ("SELECT amount FROM orders WHERE (", "SQL_SYNTAX"),
])
@pytest.mark.parametrize("second_sql", [
    "SELECT sum(amount) AS total FROM orders",
    "SELECT * FROM orders",
    "SELECT amount FROM main.orders",
    "SELECT amount FROM orders; DROP TABLE orders",
])
async def test_s06_repair_gets_stable_code_and_revalidates(db, first_sql, expected, second_sql):
    received = []

    class Model:
        async def sql(self, question, schema, error=None):
            received.append(error)
            return first_sql if error is None else second_sql

    s = SQLService(db)
    if "AS total" in second_sql:
        assert (await s.answer("订单总额", ALICE, Model())).rows == [{"total": 3600}]
    else:
        with pytest.raises(SafetyError):
            await s.answer("订单总额", ALICE, Model())
    assert received == [None, expected]


async def test_s06_unknown_column_repair_budget_is_two(db):
    errors = []

    class Model:
        async def sql(self, question, schema, error=None):
            errors.append(error)
            return "SELECT unknown_name FROM orders"

    with pytest.raises(ValueError, match="^SQL_UNKNOWN_COLUMN$"):
        await SQLService(db).answer("订单", ALICE, Model())
    assert errors == [None, "SQL_UNKNOWN_COLUMN", "SQL_UNKNOWN_COLUMN"]


def test_s07_ast_budget_and_lock_wait_close_connections(db):
    service = SQLService(db)
    query = "SELECT amount FROM orders WHERE " + " OR ".join(["amount=1"] * 120)
    with pytest.raises(SafetyError):
        service.execute(query, ALICE)
    with closing(sqlite3.connect(db)) as holder:
        holder.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        with pytest.raises(ValueError, match="^SQL_BUSY$"):
            service.execute("SELECT order_id FROM orders", ALICE)
        elapsed = time.monotonic() - start
        assert 0.10 <= elapsed < 2
        holder.rollback()
    assert service.execute("SELECT count(*) AS n FROM orders", ALICE).rows == [{"n": 4}]


def test_s07_progress_budget_terminates_long_scan(db):
    with closing(sqlite3.connect(db)) as con, con:
        con.executemany("INSERT INTO orders VALUES (?, 'alice', 1, 1, NULL)", (
            (f"Z{i}",) for i in range(10000)
        ))
    with pytest.raises(ValueError, match="^SQL_TIMEOUT$"):
        SQLService(db, timeout_seconds=0.00001).execute("SELECT sum(amount) FROM orders", ALICE)
    assert SQLService(db).execute("SELECT count(*) AS n FROM orders", ALICE).rows == [{"n": 10004}]


def test_s08_date_parameters_use_trusted_anchor_despite_optional_bindings(db):
    service = SQLService(db, anchor_date="2026-11-15")
    date_hint = json.loads(service.schema("上月订单"))["date"]
    assert date_hint == {
        "today": "2026-11-15", "period_start": "2026-10-01",
        "period_end": "2026-11-01", "timezone": "Asia/Shanghai",
    }
    query = "SELECT SUM(amount) AS total FROM orders WHERE create_time>=:period_start AND create_time<:period_end"
    assert service.execute(query, ALICE, {"period_start": "2026-09-01", "period_end": "2026-12-01"}).rows == [
        {"total": 3500},
    ]
    assert SQLService(db, anchor_date="2026-12-01").execute(query, ALICE).rows == [{"total": 0}]


def test_s08_facts_bind_order_fields_and_cents_without_using_alias_semantics(db):
    service = SQLService(db)
    result = service.execute(
        "SELECT order_id AS x, amount AS yuan, status FROM orders WHERE order_id='A2'", ALICE,
    )
    numbers = [fact for fact in service.bound_facts(result) if fact["field"] in {"amount", "status"}]
    assert numbers == [
        {"entity_id": "A2", "table": "orders", "field": "amount", "unit": "fen", "value": 2300},
        {"entity_id": "A2", "table": "orders", "field": "status", "unit": "status_code", "value": 6},
    ]
    empty = service.execute("SELECT order_id, amount FROM orders WHERE order_id='B1'", ALICE)
    assert service.bound_facts(empty) == []
    text = service.execute("SELECT name AS amount FROM products WHERE sku_id='P1'", ALICE)
    assert service.bound_facts(text) == [
        {"entity_id": None, "table": "products", "field": "name", "unit": None,
         "value": "[text] request another order"},
    ]


async def test_refund_details_are_missing_even_when_order_status_is_known(db):
    service = SQLService(db)

    class Model:
        async def sql(self, question, schema, error=None):
            assert "退款记录" in schema
            return "SELECT order_id, status FROM orders WHERE order_id='A2'"

    assert (await service.answer("A2退款状态", ALICE, Model())).rows == [{"order_id": "A2", "status": 6}]
    with pytest.raises(CapabilityError, match="退款明细"):
        await service.answer("A2的退款到账日期", ALICE, Model())
