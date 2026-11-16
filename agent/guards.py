import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope


class SafetyError(ValueError):
    """A policy rejection; never eligible for model correction."""


class CapabilityError(ValueError):
    """The supplied data cannot establish the requested fact."""


class SQLRepairError(ValueError):
    """A stable syntax or unknown-column code eligible for bounded correction."""


TABLES = {
    "orders": ("order_id", "user_id", "amount", "status", "create_time"),
    "logistics": ("logistics_id", "order_id", "status", "update_time"),
    "products": ("sku_id", "name", "stock", "price"),
}
SQLITE_FUNCTIONS = {
    "sum", "count", "avg", "min", "max", "round", "coalesce", "ifnull", "nullif", "abs",
    "date", "datetime", "strftime", "julianday", "lower", "upper", "length", "substr", "like",
}
AST_FUNCTIONS = {name.upper() for name in SQLITE_FUNCTIONS} | {
    "CAST", "TIME_TO_STR", "TS_OR_DS_TO_DATE", "TS_OR_DS_TO_TIMESTAMP", "SUBSTRING", "AND", "OR",
}
SELECT_CLAUSES = {
    "expressions", "from_", "joins", "where", "group", "having", "order", "limit", "offset",
    "distinct",
}
PHYSICAL_SCHEMAS = {"main", "temp"}
TIME_COLUMNS = {("orders", "create_time"), ("logistics", "update_time")}
TRUSTED_DATE_PARAMETERS = {"anchor_date", "period_start", "period_end"}


@dataclass(frozen=True)
class ColumnOrigin:
    table: str
    field: str
    aggregate: str | None = None


def check_input(question: str) -> None:
    if re.search(
        r"\b(drop|delete|update|insert|alter|attach|detach|pragma|vacuum|replace|truncate)"
        r"\b\s+(table\b|database\b|from\b|into\b|orders\b|logistics\b|products\b)",
        question,
        re.I,
    ):
        raise SafetyError("检测到危险数据库操作，已拦截。")


def _parse_select(sql: str) -> exp.Select:
    if not isinstance(sql, str) or len(sql) > 8000:
        raise SafetyError("SQL_SIZE_LIMIT")
    try:
        tokens = sqlglot.tokenize(sql, read="sqlite")
    except sqlglot.errors.TokenError:
        raise SafetyError("SQL_UNREADABLE") from None
    if not tokens or tokens[0].token_type.name != "SELECT" or any(
        token.token_type.name == "SEMICOLON" for token in tokens[:-1]
    ):
        raise SafetyError("SQL_SINGLE_SELECT_REQUIRED")
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.ParseError:
        # A malformed candidate that also shows unsafe syntax is ineligible for repair.
        unsafe = {
            "ATTACH", "DELETE", "DROP", "EXCEPT", "INSERT", "INTERSECT", "INTO", "MERGE",
            "PRAGMA", "STAR", "UNION", "UPDATE", "WITH",
        }
        if any(token.token_type.name in unsafe for token in tokens):
            raise SafetyError("SQL_UNSUPPORTED_STRUCTURE") from None
        raise SQLRepairError("SQL_SYNTAX") from None
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise SafetyError("SQL_SINGLE_SELECT_REQUIRED")
    return statements[0]


def _check_structure(tree: exp.Select) -> None:
    nodes = list(tree.walk())
    if len(nodes) > 400 or len(list(tree.find_all(exp.Join))) > 4:
        raise SafetyError("SQL_COMPLEXITY_LIMIT")
    forbidden = (
        exp.DDL, exp.DML, exp.Command, exp.Into, exp.With, exp.Union, exp.Intersect, exp.Except,
        exp.Window, exp.Values, exp.Collate,
    )
    if any(isinstance(node, forbidden) for node in nodes):
        raise SafetyError("SQL_UNSUPPORTED_STRUCTURE")
    for select in tree.find_all(exp.Select):
        if any(value for key, value in select.args.items() if key not in SELECT_CLAUSES):
            raise SafetyError("SQL_UNSUPPORTED_STRUCTURE")
    for star in tree.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count) or star.parent.this is not star:
            raise SafetyError("SQL_STAR_PROJECTION")
    for table in tree.find_all(exp.Table):
        if (
            not isinstance(table.this, exp.Identifier) or table.name.lower() not in TABLES
            or any(value for key, value in table.args.items() if key not in {"this", "alias"})
        ):
            raise SafetyError("SQL_TABLE_NOT_ALLOWED")
    for alias in tree.find_all(exp.TableAlias):
        if alias.columns or not re.fullmatch(r"[a-z_][a-z0-9_]*", alias.name.lower(), re.ASCII):
            raise SafetyError("SQL_UNSUPPORTED_ALIAS")
    for func in tree.find_all(exp.Func):
        name = func.name.upper() if isinstance(func, exp.Anonymous) else func.sql_name().upper()
        if name not in AST_FUNCTIONS:
            raise SafetyError("SQL_FUNCTION_NOT_ALLOWED")


def _validate_join(scope: Scope, sources: dict) -> None:
    joins = scope.expression.args.get("joins") or []
    if not joins:
        return
    join = joins[0]
    on = join.args.get("on")
    if (
        len(joins) != 1 or not on
        or any(value for key, value in join.args.items() if key not in {"this", "on", "kind", "side"})
        or join.kind not in {"", "INNER", "OUTER"}
        or join.side not in {"", "LEFT"}
        or (join.kind == "OUTER" and join.side != "LEFT")
        or len(sources) != 2 or any(not isinstance(source, exp.Table) for source in sources.values())
        or {source.name.lower() for source in sources.values()} != {"orders", "logistics"}
    ):
        raise SafetyError("SQL_JOIN_NOT_ALLOWED")

    def registered_key(predicate):
        predicate = predicate.unnest()
        if not isinstance(predicate, exp.EQ):
            return False
        columns = (predicate.this, predicate.expression)
        return (
            all(isinstance(column, exp.Column) and column.name.lower() == "order_id" for column in columns)
            and {column.table.lower() for column in columns} == sources.keys()
        )

    # An AND condition preserves the registered equality; an OR branch does not.
    def has_key(predicate):
        predicate = predicate.unnest()
        if isinstance(predicate, exp.And):
            return has_key(predicate.this) or has_key(predicate.expression)
        return registered_key(predicate)

    if not has_key(on):
        raise SafetyError("SQL_JOIN_KEY_REQUIRED")


def _resolve_column(column: exp.Column, relations: dict, aliases: set[str]):
    name, qualifier = column.name.lower(), column.table.lower()
    if column.db or column.catalog or qualifier in PHYSICAL_SCHEMAS:
        raise SafetyError("SQL_QUALIFIER_NOT_ALLOWED")
    if qualifier:
        if qualifier not in relations:
            raise SafetyError("SQL_UNKNOWN_SCOPE")
        candidates = [relations[qualifier]] if name in relations[qualifier] else []
    else:
        candidates = [fields for fields in relations.values() if name in fields]
    if len(candidates) == 1:
        return candidates[0][name]
    if len(candidates) > 1:
        raise SafetyError("SQL_AMBIGUOUS_COLUMN")
    parent = column.parent
    while parent is not None and not isinstance(parent, exp.Select):
        if not qualifier and name in aliases and isinstance(parent, (exp.Order, exp.Group, exp.Having)):
            return None
        parent = parent.parent
    raise SQLRepairError("SQL_UNKNOWN_COLUMN")


def _check_date_function(func: exp.Func, bindings: dict):
    name = func.name.upper() if isinstance(func, exp.Anonymous) else func.sql_name().upper()
    if name not in {"DATE", "DATETIME", "JULIANDAY", "STRFTIME", "TIME_TO_STR", "TS_OR_DS_TO_DATE"}:
        return
    argument = func.expressions[0] if isinstance(func, exp.Anonymous) and func.expressions else func.this
    if name == "STRFTIME":
        argument = func.expressions[1] if len(func.expressions) > 1 else None
    if isinstance(argument, exp.TsOrDsToTimestamp):
        argument = argument.this
    if isinstance(argument, exp.Column):
        origin = bindings.get(id(argument))
        if origin and not origin.aggregate and (origin.table, origin.field) in TIME_COLUMNS:
            return
    if isinstance(argument, exp.Placeholder) and argument.name in TRUSTED_DATE_PARAMETERS:
        return
    if isinstance(argument, exp.Literal) and (
        not argument.is_string or re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T][0-9:.+-]+)?", argument.this)
    ):
        return
    raise SafetyError("SQL_DATE_SOURCE_REQUIRED")


def _check_enum_comparison(node, bindings: dict):
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        comparisons = [(node.this, [node.expression]), (node.expression, [node.this])]
    elif isinstance(node, exp.In):
        comparisons = [(node.this, node.expressions)]
    elif isinstance(node, exp.Between):
        comparisons = [(node.this, [node.args["low"], node.args["high"]])]
    else:
        return
    for operand, values in comparisons:
        origin = bindings.get(id(operand.unnest()))
        if origin and origin.field == "status" and any(
            isinstance(item.unnest(), exp.Literal) and not item.unnest().is_int for item in values
        ):
            raise SafetyError("SQL_STATUS_REQUIRES_INTEGER")


def _projection_origin(node, bindings: dict, group_result: bool) -> ColumnOrigin | None:
    expression = (node.this if isinstance(node, exp.Alias) else node).unnest()
    if isinstance(expression, exp.Column) and not group_result:
        return bindings.get(id(expression))
    if isinstance(expression, (exp.Sum, exp.Avg, exp.Min, exp.Max)):
        origin = bindings.get(id(expression.this.unnest()))
        if origin and not origin.aggregate and origin.field in {"amount", "price", "stock"}:
            return ColumnOrigin(origin.table, origin.field, expression.key)
    return None


def _validate_scopes(tree: exp.Select) -> list[ColumnOrigin | None]:
    scopes = traverse_scope(tree)
    outputs: dict[int, dict] = {}
    unknown_column = False
    projections: list[ColumnOrigin | None] = []
    for scope in scopes:  # Children precede their parents.
        source_nodes = {}
        relations = {}
        for alias, (_, source) in scope.selected_sources.items():
            alias = alias.lower()
            if alias in relations or alias in PHYSICAL_SCHEMAS:
                raise SafetyError("SQL_AMBIGUOUS_SCOPE")
            source_nodes[alias] = source
            if isinstance(source, exp.Table):
                table = source.name.lower()
                relations[alias] = {field: ColumnOrigin(table, field) for field in TABLES[table]}
            elif isinstance(source, Scope) and source.is_derived_table:
                relations[alias] = outputs[id(source)]
            else:
                raise SafetyError("SQL_UNSUPPORTED_SCOPE")
        explicit_aliases = {
            alias.lower() for alias, (_, source) in scope.selected_sources.items()
            if isinstance(source, Scope) or source.alias
        }
        ancestor = scope.parent
        while ancestor:
            if {alias.lower() for alias in ancestor.selected_sources} & explicit_aliases:
                raise SafetyError("SQL_SHADOWED_SCOPE")
            ancestor = ancestor.parent
        _validate_join(scope, source_nodes)

        nodes = list(scope.walk())
        selections = scope.expression.selects
        aliases = {node.alias.lower() for node in selections if isinstance(node, exp.Alias)}
        bindings = {}
        for column in scope.find_all(exp.Column):
            try:
                bindings[id(column)] = _resolve_column(column, relations, aliases)
            except SQLRepairError:
                unknown_column = True
        for node in nodes:
            if isinstance(node, exp.Func):
                _check_date_function(node, bindings)
            _check_enum_comparison(node, bindings)

        grouped = bool(scope.expression.args.get("group")) or any(
            isinstance(node, exp.AggFunc) for node in nodes
        )
        projections = [_projection_origin(node, bindings, grouped) for node in selections]
        if scope.is_derived_table:
            names = [node.output_name.lower() for node in selections]
            if not all(names) or len(names) != len(set(names)):
                raise SafetyError("SQL_DERIVED_COLUMNS_REQUIRE_NAMES")
            outputs[id(scope)] = dict(zip(names, projections))
    if unknown_column:
        raise SQLRepairError("SQL_UNKNOWN_COLUMN")
    return projections


def sql_projection_origins(sql: str) -> list[ColumnOrigin | None]:
    tree = _parse_select(sql)
    _check_structure(tree)
    try:
        return _validate_scopes(tree)
    except sqlglot.errors.OptimizeError:
        raise SafetyError("SQL_AMBIGUOUS_SCOPE") from None


def validate_sql(sql: str) -> str:
    sql_projection_origins(sql)
    # SQLite executes the original dialect; sqlglot is used for validation only.
    return sql.strip().rstrip(";")


def valid_order_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value))
