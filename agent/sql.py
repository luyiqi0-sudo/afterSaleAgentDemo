import asyncio
import json
import re
import sqlite3
import time
import sqlglot
from sqlglot import exp
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.contracts import Principal, SQLResult
from agent.guards import (
    CapabilityError,
    ColumnOrigin,
    SafetyError,
    SQLRepairError,
    TABLES,
    SQLITE_FUNCTIONS,
    check_input,
    sql_projection_origins,
    validate_sql,
)


DEFAULT_ANCHOR_DATE = date(2026, 11, 15)
ENTITY_FIELDS = {"orders": "order_id", "logistics": "order_id", "products": "sku_id"}
UNITS = {"amount": "fen", "price": "fen", "status": "status_code", "stock": "count"}
SQLITE_ERROR_CODES = {
    sqlite3.SQLITE_AUTH: "SQL_AUTHORIZATION_DENIED",
    sqlite3.SQLITE_BUSY: "SQL_BUSY",
    sqlite3.SQLITE_LOCKED: "SQL_BUSY",
    sqlite3.SQLITE_INTERRUPT: "SQL_TIMEOUT",
    sqlite3.SQLITE_TOOBIG: "SQL_RESULT_LIMIT",
}
REFUND_DETAILS = re.compile(
    r"退款(?:进度|金额|明细|流水|阶段|受理时间|原因|失败原因)|"
    r"(?:退款|退钱).{0,12}(?:到账|退了多少|什么时候退|多久退|何时退)|"
    r"(?:到账|何时|多久|退了多少|为什么).{0,8}退款"
)


class SQLExecutionError(ValueError):
    """A sanitized SQLite failure; only SQLRepairError triggers model correction."""


def month_context(anchor_date: date | None = None) -> dict:
    today = anchor_date or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    end = today.replace(day=1)
    start = (end - timedelta(days=1)).replace(day=1)
    return {
        "today": str(today), "period_start": str(start), "period_end": str(end),
        "timezone": "Asia/Shanghai",
    }


def _execution_error(exc: sqlite3.Error) -> ValueError:
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    if code in SQLITE_ERROR_CODES:
        return SQLExecutionError(SQLITE_ERROR_CODES[code])
    if code == sqlite3.SQLITE_ERROR:
        # Classify a known driver error without propagating any part of its text.
        message = str(exc)
        if message.startswith(("access to ", "not authorized to use function")):
            return SQLExecutionError("SQL_AUTHORIZATION_DENIED")
        if message.startswith("near ") and message.endswith(": syntax error"):
            return SQLRepairError("SQL_SYNTAX")
    return SQLExecutionError("SQL_EXECUTION_FAILED")


class SQLService:
    def __init__(
        self, path: Path, max_rows: int = 100, timeout_seconds: float = 1.0,
        dictionary_path: Path | None = None, emit=None, anchor_date: date | str | None = None,
    ):
        self.path = Path(path)
        self.max_rows = max_rows
        self.timeout_seconds = timeout_seconds
        self.dictionary = json.loads(dictionary_path.read_text(encoding="utf-8")) if dictionary_path else {}
        self.emit = emit or (lambda *_args, **_kwargs: None)
        self.anchor_date = date.fromisoformat(anchor_date) if isinstance(anchor_date, str) else anchor_date or DEFAULT_ANCHOR_DATE

    def _connect(self):
        if not self.path.is_file():
            raise FileNotFoundError("业务 SQLite 未提供")
        con = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        try:
            con.row_factory = sqlite3.Row
            con.enable_load_extension(False)
            con.execute("PRAGMA busy_timeout=200")
            if hasattr(con, "setlimit"):
                con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 65536)
                con.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
                con.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 50)
            return con
        except BaseException:
            con.close()
            raise

    def schema(self, question: str) -> str:
        if any(word in question for word in ("物流", "logistics")):
            tables = ["orders", "logistics"]
        elif any(word in question for word in ("库存", "商品价格")):
            tables = ["products"]
        else:
            tables = ["orders"]
        try:
            with closing(self._connect()) as con:
                catalog = {
                    table: [
                        {"column": row["name"], "type": row["type"]}
                        for row in con.execute(f'PRAGMA table_info("{table}")')
                        if row["name"] in TABLES[table]
                    ]
                    for table in tables
                }
        except sqlite3.Error as exc:
            error = _execution_error(exc)
        else:
            return json.dumps({
                "tables": catalog, "dictionary": self.dictionary,
                "date": month_context(self.anchor_date),
                "relations": ["orders.order_id = logistics.order_id"],
                "missing_capabilities": ["没有退款记录；不能确定退款进度或退款金额", "orders 与 products 没有 SKU 关系"],
                "scope": (
                    "视图已限制当前用户，不需要猜 user_id；只用明确列；允许 COUNT(*)；禁止 WITH/UNION；"
                    "JOIN 只接受用表别名限定的 order_id 等值连接；"
                    "日期参数 :anchor_date/:period_start/:period_end 由服务端绑定"
                ),
            }, ensure_ascii=False)
        raise error

    def _configure_views(self, con, principal: Principal):
        # Identity is server supplied; SQLite cannot parameterize CREATE VIEW.
        if "\x00" in principal.user_id:
            raise SafetyError("SQL_INVALID_PRINCIPAL")
        identity = "'" + principal.user_id.replace("'", "''") + "'"
        available = {r["name"] for r in con.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
        if "orders" not in available:
            raise CapabilityError("数据库缺少订单表")
        for table, fields in TABLES.items():
            if table not in available:
                continue
            actual = {r["name"] for r in con.execute(f'PRAGMA table_info("{table}")')}
            if not set(fields).issubset(actual):
                raise CapabilityError(f"{table} 字段不符合已核对契约")
            columns = ",".join(f'"{table}"."{field}"' for field in fields)
            where = {
                "orders": f" WHERE user_id={identity}",
                "logistics": f" WHERE order_id IN (SELECT order_id FROM main.orders WHERE user_id={identity})",
            }.get(table, "")
            con.execute(f'CREATE TEMP VIEW "{table}" AS SELECT {columns} FROM main."{table}"{where}')
        con.execute("PRAGMA query_only=ON")
        con.set_authorizer(self._authorize)

    @staticmethod
    def _authorize(action, arg1, arg2, database, source):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if (arg2 or "").lower() in SQLITE_FUNCTIONS else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            if database == "temp" and arg1 in TABLES:
                return sqlite3.SQLITE_OK
            allowed_source = {
                "orders": {"orders", "logistics"}, "logistics": {"logistics"}, "products": {"products"},
            }
            if (
                database == "main" and source in allowed_source.get(arg1, ())
                and (arg2 in TABLES.get(arg1, ()) or arg2 == "")
            ):
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def execute(self, sql: str, principal: Principal, parameters: dict | None = None) -> SQLResult:
        sql = validate_sql(sql)
        start = time.monotonic()
        context = month_context(self.anchor_date)
        params = {
            **(parameters or {}), "anchor_date": context["today"],
            "period_start": context["period_start"], "period_end": context["period_end"],
        }
        try:
            with closing(self._connect()) as con:
                self._configure_views(con, principal)
                con.set_progress_handler(lambda: int(time.monotonic() - start > self.timeout_seconds), 100)
                cursor = con.execute(sql, params)
                columns = [description[0] for description in cursor.description]
                if len(columns) != len({column.lower() for column in columns}):
                    raise CapabilityError("结果列名重复，请使用显式别名")
                raw = cursor.fetchmany(self.max_rows + 1)
                result = SQLResult(
                    columns=columns, rows=[dict(row) for row in raw[:self.max_rows]], truncated=len(raw) > self.max_rows,
                    executed_sql=sql, duration_ms=round((time.monotonic() - start) * 1000, 3),
                )
        except sqlite3.Error as exc:
            error = _execution_error(exc)
        else:
            self.emit("sql_result", {
                "row_count": len(result.rows), "truncated": result.truncated,
                "duration_ms": result.duration_ms,
            })
            return result
        raise error

    def bound_facts(self, result: SQLResult) -> list[dict]:
        """Project only known business fields from a trusted execute()/answer() result."""
        origins, count_scopes = self._fact_projection(result.executed_sql)
        facts = []
        for row in result.rows:
            entities = {
                origin.table: row[column]
                for column, origin in zip(result.columns, origins)
                if origin and not origin.aggregate and origin.field == ENTITY_FIELDS[origin.table]
            }
            for index, (column, origin) in enumerate(zip(result.columns, origins)):
                if origin is None:
                    continue
                fact = {
                    "entity_id": None if origin.aggregate else entities.get(origin.table),
                    "table": origin.table, "field": origin.field, "unit": UNITS.get(origin.field),
                    "value": row[column],
                }
                if origin.aggregate:
                    fact["aggregate"] = origin.aggregate
                if index in count_scopes:
                    fact.update(unit="count", count_scope=count_scopes[index])
                facts.append(fact)
        return facts

    @staticmethod
    def _fact_projection(sql):
        """Extend validated column lineage only for direct, ungrouped table counts.

        COUNT(*) measures source rows. COUNT(column) measures non-null values,
        not money or a claimed entity cardinality. Derived tables, joins,
        DISTINCT, grouping and arbitrary expressions require a richer proof and
        remain unbound. Output aliases never supply field meaning.
        """
        origins = sql_projection_origins(sql)
        counts = {}
        tree = sqlglot.parse_one(sql, read="sqlite")
        from_clause = tree.args.get("from_")
        source = from_clause.this if from_clause is not None else None
        if (not isinstance(source, exp.Table) or source.name.lower() not in TABLES or
                tree.args.get("joins") or tree.args.get("group")):
            return origins, counts
        table = source.name.lower()
        qualifiers = {"", source.alias_or_name.lower()}
        for index, projection in enumerate(tree.expressions):
            expression = (projection.this if isinstance(projection, exp.Alias) else projection).unnest()
            if not isinstance(expression, exp.Count):
                continue
            operand = expression.this
            if isinstance(operand, exp.Star):
                field, scope = "*", "rows"
            elif (isinstance(operand, exp.Column) and operand.table.lower() in qualifiers and
                  operand.name.lower() in TABLES[table]):
                field, scope = operand.name.lower(), "non_null_values"
            else:
                continue
            origins[index] = ColumnOrigin(table, field, "count")
            counts[index] = scope
        return origins, counts

    def unbound_columns(self, result: SQLResult) -> list[str]:
        """Explicit answer gaps; never fill them by interpreting an output alias."""
        origins, _ = self._fact_projection(result.executed_sql)
        return [column for column, origin in zip(result.columns, origins) if origin is None]

    @staticmethod
    def verified_empty_orders(result: SQLResult) -> bool:
        """Prove zero owned rows only from the complete, unfiltered list shape."""
        return (not result.rows and not result.truncated and result.columns == ["order_id", "amount"]
                and sqlglot.parse_one(result.executed_sql, read="sqlite")
                == sqlglot.parse_one("SELECT order_id, amount FROM orders", read="sqlite"))

    async def answer(self, question: str, principal: Principal, model) -> SQLResult:
        check_input(question)
        if REFUND_DETAILS.search(question):
            raise CapabilityError("当前数据没有退款明细，无法查询退款阶段、退款金额、到账时间或失败原因。")
        error = None
        schema = self.schema(question)
        for attempt in range(3):
            sql = await model.sql(question, schema, error)
            self.emit("sql_generated", {"attempt": attempt + 1})
            try:
                return await asyncio.to_thread(self.execute, sql, principal)
            except SQLRepairError as exc:
                if attempt == 2:
                    raise
                error = str(exc)
                self.emit("sql_correction", {"error": error, "attempt": attempt + 1})
        raise RuntimeError("unreachable")

    def owns_order(self, order_id: str, principal: Principal) -> bool:
        return bool(self.execute(
            "SELECT order_id FROM orders WHERE order_id=:order_id", principal, {"order_id": order_id},
        ).rows)

    def abnormal(self, rows: list[dict]) -> bool:
        abnormal_codes = {str(code) for code in self.dictionary.get("abnormal_order_statuses", [])}
        return any(str(row.get("status")) in abnormal_codes for row in rows)
