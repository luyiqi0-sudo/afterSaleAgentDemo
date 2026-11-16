import sqlite3
import pytest


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "orders.db"
    with sqlite3.connect(path) as con:
        con.executescript("""
        CREATE TABLE orders(order_id TEXT PRIMARY KEY,user_id TEXT,amount INTEGER,status INTEGER,create_time TEXT);
        CREATE TABLE logistics(logistics_id TEXT,order_id TEXT,status INTEGER,update_time TEXT);
        CREATE TABLE products(sku_id TEXT,name TEXT,stock INTEGER,price INTEGER);
        INSERT INTO orders VALUES('A1','alice',1200,1,'2026-10-01'),('A2','alice',2300,2,'2026-10-31'),('B1','bob',900000,9,'2026-10-05');
        INSERT INTO logistics VALUES('L1','A1',1,'2026-11-01'),('L2','B1',2,'2026-11-01');
        """)
    return path


def service(path, **kwargs):
    from agent.sql import SQLService
    return SQLService(path, **kwargs)


def alice():
    from agent.contracts import Principal
    return Principal(user_id="alice")


def test_aggregate_only_current_user(db):
    result = service(db).execute("SELECT SUM(amount) AS total FROM orders", alice())
    assert result.rows == [{"total": 3500}]
    assert result.status == "SUCCESS"


def test_join_filters_logistics_and_orders(db):
    result = service(db).execute("SELECT o.order_id, l.status FROM orders o JOIN logistics l ON o.order_id=l.order_id", alice())
    assert result.rows == [{"order_id": "A1", "status": 1}]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM orders", "SELECT o.* FROM orders o", "DELETE FROM orders",
    "SELECT amount FROM orders; DROP TABLE orders", "PRAGMA table_info(orders)",
    "SELECT amount FROM main.orders", "SELECT sql FROM sqlite_master",
    "SELECT load_extension('x')", "SELECT randomblob(99999999)",
    "WITH orders AS (SELECT amount FROM main.orders) SELECT amount FROM orders",
    "ATTACH DATABASE 'x' AS x", "SELECT amount INTO other FROM orders",
])
def test_unsafe_sql_is_rejected(db, sql):
    from agent.guards import SafetyError
    with pytest.raises(SafetyError):
        service(db).execute(sql, alice())
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3


def test_or_subquery_cannot_expand_user_scope(db):
    result = service(db).execute("SELECT order_id FROM orders WHERE user_id='bob' OR amount IN (SELECT amount FROM orders)", alice())
    assert {r["order_id"] for r in result.rows} == {"A1", "A2"}
    assert service(db).execute("SELECT order_id FROM logistics WHERE order_id='B1'", alice()).rows == []


def test_read_limit_is_explicit(db):
    result = service(db, max_rows=1).execute("SELECT order_id FROM orders ORDER BY order_id", alice())
    assert result.rows == [{"order_id": "A1"}]
    assert result.truncated is True


async def test_sql_correction_then_real_execution(db):
    class Model:
        def __init__(self):
            self.calls = 0
        async def sql(self, question, schema, error=None):
            self.calls += 1
            return "SELECT missing FROM orders" if error is None else "SELECT SUM(amount) AS total FROM orders"
    model = Model()
    result = await service(db).answer("总金额", alice(), model)
    assert result.rows == [{"total": 3500}]
    assert model.calls == 2


async def test_correction_cannot_bypass_safety(db):
    from agent.guards import SafetyError
    class Model:
        async def sql(self, question, schema, error=None):
            return "SELECT missing FROM orders" if error is None else "DELETE FROM orders"
    with pytest.raises(SafetyError):
        await service(db).answer("总金额", alice(), Model())


def test_missing_database_does_not_silently_create(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(FileNotFoundError):
        service(path).execute("SELECT order_id FROM orders", alice())
    assert not path.exists()


@pytest.mark.parametrize("expression,field,scope", [("COUNT(*)", "*", "rows"), ("COUNT(o.amount)", "amount", "non_null_values")])
def test_count_lineage_uses_direct_table_and_count_units(db, expression, field, scope):
    sql = service(db)
    result = sql.execute(f"SELECT {expression} AS cents FROM orders AS o", alice())
    assert sql.bound_facts(result) == [{"entity_id": None, "table": "orders", "field": field,
                                      "unit": "count", "value": 2, "aggregate": "count", "count_scope": scope}]


@pytest.mark.parametrize("query", [
    "SELECT COUNT(1) AS order_count FROM orders",
    "SELECT COUNT(amount + 1) AS order_count FROM orders",
    "SELECT COUNT(DISTINCT amount) AS order_count FROM orders",
    "SELECT COUNT(*) AS order_count FROM orders o JOIN logistics l ON o.order_id=l.order_id",
    "SELECT COUNT(*) AS order_count FROM (SELECT order_id FROM orders) q",
    "SELECT COUNT(*) AS order_count FROM orders GROUP BY status",
    "SELECT amount + 1 AS order_count FROM orders",
])
def test_count_alias_cannot_assign_unproved_lineage(db, query):
    sql = service(db)
    result = sql.execute(query, alice())
    assert sql.bound_facts(result) == []
    assert sql.unbound_columns(result) == ["order_count"]


def test_count_distinguishes_rows_non_null_values_and_valid_zero(db):
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO orders VALUES('A3','alice',NULL,2,'2026-11-01')")
    sql = service(db)
    result = sql.execute("SELECT COUNT(*) AS n, COUNT(amount) AS available FROM orders", alice())
    facts = sql.bound_facts(result)
    assert [(fact["value"], fact["count_scope"]) for fact in facts] == [(3, "rows"), (2, "non_null_values")]
    empty = sql.execute("SELECT COUNT(*) AS n FROM orders WHERE order_id='unavailable'", alice())
    assert sql.bound_facts(empty)[0]["value"] == 0 and sql.unbound_columns(empty) == []
