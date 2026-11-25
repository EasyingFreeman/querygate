import re
from dataclasses import dataclass
from functools import cache

from sqlglot import Dialect, exp
from sqlglot.errors import ParseError, SqlglotError, TokenError
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.tokens import Token, TokenType

from .api_models import BindValue, QueryRequest
from .config import Datasource
from .errors import GatewayError

SQL_BYTES = 32_768
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,62}\Z")
CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Both node types and populated arguments must be reviewed when the parser changes.
NODE_ARGS = {
    "Select": "expressions distinct from_ joins where group having order limit offset with_",
    "Union": "this expression distinct order limit offset with_",
    "Intersect": "this expression distinct order limit offset with_",
    "Except": "this expression distinct order limit offset with_",
    "With": "expressions",
    "CTE": "this alias",
    "Subquery": "this alias",
    "From": "this",
    "Join": "this on side kind",
    "Where": "this",
    "Group": "expressions",
    "Having": "this",
    "Order": "expressions",
    "Ordered": "this desc nulls_first",
    "Alias": "this alias",
    "TableAlias": "this",
    "Table": "this db alias",
    "Column": "this table",
    "Identifier": "this quoted",
    "Star": "",
    "Literal": "this is_string",
    "National": "this",
    "Null": "",
    "Boolean": "this",
    "Placeholder": "this",
    "Distinct": "expressions",
    "Paren": "this",
    "Tuple": "expressions",
    "Not": "this",
    "Neg": "this",
    "Between": "this low high",
    "In": "this expressions query",
    "Exists": "this",
    "Like": "this expression",
    "Escape": "this expression",
    "Limit": "expression limit_options",
    "Offset": "expression expressions",
    "Fetch": "direction count limit_options",
    "LimitOptions": "percent rows with_ties",
    "Window": "this partition_by order spec over",
    "WindowSpec": "kind start start_side end end_side",
    "Case": "this ifs default",
    "If": "this true false",
    "Cast": "this to",
    "Convert": "this expression style",
    "DataType": "this expressions nested",
    "DataTypeParam": "this",
    "Var": "this",
    "Count": "this expressions big_int",
    "Sum": "this",
    "Avg": "this",
    "Min": "this expressions",
    "Max": "this expressions",
    "Abs": "this",
    "Round": "this decimals truncate",
    "Ceil": "this",
    "Floor": "this",
    "Coalesce": "this expressions is_null is_nvl",
    "Nullif": "this expression",
    "Upper": "this",
    "Lower": "this",
    "Length": "this binary",
    "Substring": "this start length",
    "Replace": "this expression replacement",
    "Trim": "this expression position",
    "DateAdd": "this expression unit",
    "DateDiff": "this expression unit big_int",
    "Extract": "this expression",
    "CurrentTimestamp": "sysdate",
    "CurrentDate": "",
    "TimeToStr": "this format",
    "ToChar": "this format",
    "ToNumber": "this format",
    "RowNumber": "",
    "Rank": "expressions",
    "DenseRank": "expressions",
    "Lag": "this offset default",
    "Lead": "this offset default",
    **dict.fromkeys(
        "Add Sub Mul Div Mod And Or EQ NEQ LT LTE GT GTE Is DPipe".split(),
        "this expression safe typed",
    ),
}
ALLOWED_ARGS = {node: frozenset(args.split()) for node, args in NODE_ARGS.items()}
FUNCTIONS = frozenset("""
    ABS AVG CAST COALESCE COUNT DENSE_RANK FLOOR LAG LEAD LOWER MAX MIN NULLIF
    RANK REPLACE ROUND ROW_NUMBER SUM TRIM UPPER
""".split())
DIALECT_FUNCTIONS = {
    "tsql": frozenset("CEILING CONVERT COUNT_BIG DATEADD DATEDIFF GETDATE ISNULL LEN SUBSTRING".split()),
    "oracle": frozenset("CEIL EXTRACT LENGTH NVL SUBSTR TO_CHAR TO_NUMBER".split()),
}
PAREN_SYNTAX = frozenset("""
    ALL AND AS BETWEEN BY CASE DISTINCT ELSE EXISTS FROM HAVING IN JOIN NOT ON OR
    OVER SELECT THEN WHEN WHERE
""".split())
TYPES = frozenset("""
    BIGINT BINARY BIT CHAR DATE DATETIME DATETIME2 DATETIMEOFFSET DEC DECIMAL DOUBLE
    FLOAT INT INTEGER NCHAR NUMBER NUMERIC NVARCHAR NVARCHAR2 REAL SMALLINT TIME
    TIMESTAMP TINYINT VARBINARY VARCHAR VARCHAR2
""".split())
DATE_UNITS = frozenset("YEAR QUARTER MONTH WEEK DAY DAYOFYEAR WEEKDAY HOUR MINUTE SECOND MILLISECOND MICROSECOND NANOSECOND".split())


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str
    params: list[BindValue] | dict[str, BindValue]


def deny() -> GatewayError:
    return GatewayError(422, "POLICY_DENIED", "Only supported read-only SQL constructs are allowed.")


def bad_params() -> GatewayError:
    return GatewayError(422, "INVALID_PARAMS", "Parameters must match the SQL bind markers for this datasource.")


def validate_query(request: QueryRequest, source: Datasource) -> ValidatedQuery:
    sql = request.sql.strip()
    try:
        if len(request.sql.encode("utf-8")) > SQL_BYTES:
            raise GatewayError(413, "REQUEST_TOO_LARGE", "SQL exceeds 32 KiB.")
    except UnicodeEncodeError:
        raise deny() from None
    if CONTROLS.search(request.sql):
        raise deny()

    try:
        dialect = Dialect.get_or_raise(source.dialect)
        tokens = dialect.tokenize(sql)
        if tokens and tokens[-1].token_type == TokenType.SEMICOLON:
            if tokens[-1].comments:
                raise deny()
            sql = sql[:tokens.pop().start].rstrip()
        validate_tokens(tokens, source.dialect)
        statements = dialect.parser().parse(tokens, sql)
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.SetOperation)):
            raise deny()
        tree = statements[0]
        validate_nodes(tree, source.dialect)
        scopes = traverse_scope(tree)
        validate_tables(tree, source, scopes)
        if source.engine == "oracle":
            validate_oracle_columns(tree, source, scopes)
        params = validate_params(tree, tokens, request.params, source.dialect)
    except (ParseError, TokenError, RecursionError):
        raise GatewayError(422, "SQL_PARSE_ERROR", "SQL could not be parsed in the datasource dialect.") from None
    except SqlglotError:
        raise deny() from None

    return ValidatedQuery(sql, params)


def validate_tokens(tokens: list[Token], dialect: str) -> None:
    if not tokens or len(tokens) > 4096 or tokens[0].token_type not in {TokenType.SELECT, TokenType.WITH}:
        raise deny()
    allowed_paren = FUNCTIONS | DIALECT_FUNCTIONS[dialect] | PAREN_SYNTAX | TYPES
    if dialect == "tsql":
        allowed_paren |= {"TOP"}
    depth = 0
    for index, token in enumerate(tokens):
        kind = token.token_type
        if token.comments or kind in {
            TokenType.HINT, TokenType.SEMICOLON, TokenType.PARAMETER, TokenType.L_BRACE, TokenType.R_BRACE,
        }:
            raise deny()
        if kind == TokenType.L_PAREN:
            depth += 1
            if depth > 32:
                raise deny()
        elif kind == TokenType.R_PAREN:
            depth -= 1
        if index + 1 == len(tokens) or tokens[index + 1].token_type != TokenType.L_PAREN:
            continue
        if index and tokens[index - 1].token_type == TokenType.DOT:
            raise deny()
        if kind == TokenType.IDENTIFIER or (
            kind != TokenType.STRING and IDENTIFIER.fullmatch(token.text)
            and token.text.upper() not in allowed_paren
        ):
            raise deny()


def validate_nodes(tree: exp.Expression, dialect: str) -> None:
    for node in tree.walk():
        allowed_args = ALLOWED_ARGS.get(type(node).__name__)
        if allowed_args is None or node.comments or any(
            key not in allowed_args and value is not None and value is not False and value != []
            for key, value in node.args.items()
        ):
            raise deny()
        if isinstance(node, exp.Identifier):
            if not IDENTIFIER.fullmatch(node.name):
                raise deny()
            if dialect == "oracle" and not node.quoted:
                node.set("this", node.name.upper())
        elif isinstance(node, exp.Column) and node.name.upper() in {"NEXTVAL", "CURRVAL"}:
            raise deny()
        elif isinstance(node, exp.DataType) and node.this.value not in TYPES:
            raise deny()
        elif isinstance(node, exp.Var):
            if not isinstance(node.parent, (exp.DateAdd, exp.DateDiff, exp.Extract)) or node.name.upper() not in DATE_UNITS:
                raise deny()


def validate_tables(tree: exp.Expression, source: Datasource, scopes: list[Scope]) -> None:
    cte_references = {
        id(node)
        for scope in scopes
        for node, resolved in scope.selected_sources.values()
        if isinstance(node, exp.Table) and not node.db
        and node.name in scope.cte_sources and resolved is scope.cte_sources[node.name]
    }
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise deny()
        if table.db and f"{table.db}.{table.name}" in source.allowed_objects:
            continue
        if id(table) not in cte_references:
            raise GatewayError(422, "OBJECT_NOT_ALLOWED", "Use a schema-qualified object listed in the datasource catalog.")


def validate_oracle_columns(tree: exp.Expression, source: Datasource, scopes: list[Scope]) -> None:
    # Oracle can resolve a missing column name as a zero-argument stored function.
    @cache
    def source_columns(scope: Scope) -> dict[str, set[str]]:
        columns = {}
        for alias, (_, resolved) in scope.selected_sources.items():
            if isinstance(resolved, exp.Table):
                entry = source.catalog[f"{resolved.db}.{resolved.name}"]
                columns[alias] = {column.name for column in entry.columns}
            else:
                columns[alias] = output_columns(resolved)
        return columns

    @cache
    def output_columns(scope: Scope) -> set[str]:
        if isinstance(scope.expression, exp.SetOperation):
            return output_columns(scope.set_operation_scopes[0])
        names = set()
        for projection in scope.expression.selects:
            if isinstance(projection, exp.Star):
                names.update(*source_columns(scope).values())
            elif isinstance(projection, exp.Column) and projection.is_star:
                names.update(source_columns(scope).get(projection.table, ()))
            elif isinstance(projection, (exp.Alias, exp.Column)):
                names.add(projection.output_name)
        return names

    def resolves_column(column: exp.Column, scope: Scope) -> bool:
        current = scope
        while current is not None:
            available = source_columns(current)
            if column.table and column.table in available:
                return column.is_star or column.name in available[column.table]
            if not column.table and any(column.name in names for names in available.values()):
                return True
            current = current.parent if current.can_be_correlated else None
        return False

    checked = set()
    for scope in scopes:
        for column in scope.find_all(exp.Column):
            checked.add(id(column))
            if column.name == "ROWNUM" and not column.table and not column.this.quoted:
                continue
            if resolves_column(column, scope):
                continue
            order = scope.expression.args.get("order")
            if (not column.table and isinstance(column.parent, exp.Ordered)
                and column.parent.parent is order and column.name in output_columns(scope)):
                continue
            raise GatewayError(422, "COLUMN_NOT_ALLOWED", "Use a column listed in the Oracle datasource catalog or projected by the query.")
    if any(id(column) not in checked for column in tree.find_all(exp.Column)):
        raise deny()


def validate_params(
    tree: exp.Expression,
    tokens: list[Token],
    params: list[BindValue] | dict[str, BindValue] | None,
    dialect: str,
) -> list[BindValue] | dict[str, BindValue]:
    markers = list(tree.find_all(exp.Placeholder))
    question_marks = sum(token.token_type == TokenType.PLACEHOLDER for token in tokens)
    colons = sum(token.token_type == TokenType.COLON for token in tokens)
    if dialect == "tsql":
        if colons or question_marks != len(markers) or any(marker.this is not None for marker in markers):
            raise bad_params()
        if not markers and (params is None or params == {}):
            return []
        if not isinstance(params, list) or len(params) != len(markers) or len(params) > 128:
            raise bad_params()
        return params

    names = {marker.name.upper() for marker in markers}
    if question_marks or colons != len(markers) or len(markers) > 128 or any(
        not IDENTIFIER.fullmatch(name) for name in names
    ):
        raise bad_params()
    if not markers and (params is None or params == []):
        return {}
    if not isinstance(params, dict) or len(params) > 128:
        raise bad_params()
    params_by_name = {name.upper(): value for name, value in params.items()}
    if len(params_by_name) != len(params) or params_by_name.keys() != names:
        raise bad_params()
    return params_by_name
