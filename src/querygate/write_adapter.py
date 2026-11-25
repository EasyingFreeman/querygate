import math
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import oracledb
import pyodbc

from .api_models import BindValue, MutationAudit, MutationRecord
from .config import Datasource, WriteGrant
from .errors import GatewayError
from .execution import Deadline, json_bytes, normalize_cell

SQLSERVER_PRIMARY_KEY = """
SELECT c.name
FROM sys.tables t
JOIN sys.schemas s ON t.schema_id = s.schema_id
JOIN sys.indexes i ON i.object_id = t.object_id AND i.is_primary_key = 1
JOIN sys.index_columns k ON k.object_id = i.object_id AND k.index_id = i.index_id AND k.key_ordinal > 0
JOIN sys.columns c ON c.object_id = k.object_id AND c.column_id = k.column_id
WHERE s.name = ? AND t.name = ? AND t.is_ms_shipped = 0 AND t.is_memory_optimized = 0
"""

ORACLE_PRIMARY_KEY = """
SELECT c.column_name
FROM sys.all_constraints p
JOIN sys.all_cons_columns c ON c.owner = p.owner AND c.constraint_name = p.constraint_name
JOIN sys.all_tables t ON t.owner = p.owner AND t.table_name = p.table_name
WHERE t.owner = :1 AND t.table_name = :2 AND t.temporary = 'N' AND p.constraint_type = 'P'
  AND p.status = 'ENABLED' AND p.validated = 'VALIDATED' AND p.deferrable = 'NOT DEFERRABLE'
"""

MAX_AUDIT_BYTES = 256 * 1024


class MutationSession:
    def __init__(self, connection: Any, cursor: Any, source: Datasource, deadline: Deadline):
        self.connection = connection
        self.cursor = cursor
        self.oracle = source.engine == "oracle"
        self.deadline = deadline

    def identifier(self, value: str) -> str:
        return f'"{value}"' if self.oracle else f"[{value}]"

    def object_name(self, value: str) -> str:
        return ".".join(self.identifier(part) for part in value.split("."))

    def bind(self, index: int) -> str:
        return f":{index}" if self.oracle else "?"

    def execute(self, statement: str, values: list[Any]) -> None:
        seconds = self.deadline.remaining()
        if self.oracle:
            self.connection.call_timeout = math.ceil(seconds * 1000)
            self.cursor.execute(statement, values, fetch_decimals=True, fetch_lobs=False)
        else:
            self.connection.timeout = math.ceil(seconds)
            self.cursor.execute(statement, values)


@contextmanager
def mutation_session(adapter: Any, source: Datasource, deadline: Deadline):
    with closing(adapter.connect(deadline)) as connection, closing(connection.cursor()) as cursor:
        cancel = connection.cancel if source.engine == "oracle" else cursor.cancel
        try:
            with deadline.cancellable(cancel):
                yield MutationSession(connection, cursor, source, deadline)
        except BaseException:
            with suppress(oracledb.Error, pyodbc.Error):
                connection.rollback()
            raise


def primary_key(session: MutationSession, target: str) -> set[str]:
    session.execute(ORACLE_PRIMARY_KEY if session.oracle else SQLSERVER_PRIMARY_KEY, target.split("."))
    return {row[0] for row in session.cursor.fetchmany(17)}


def load_audit(session: MutationSession, table: str, record: MutationRecord) -> MutationAudit | None:
    target = session.object_name(table)
    operation_id = session.identifier("OPERATION_ID")
    event_json = session.identifier("EVENT_JSON")
    session.execute(f"SELECT {event_json} FROM {target} WHERE {operation_id} = {session.bind(1)}", [record.operation_id])
    rows = session.cursor.fetchmany(2)
    if not rows:
        return None
    if len(rows) != 1:
        raise GatewayError(409, "WRITE_AUDIT_INVALID", "The database audit operation ID is not unique.")
    try:
        saved = MutationRecord.model_validate_json(rows[0][0])
    except (TypeError, ValueError):
        raise GatewayError(409, "WRITE_AUDIT_INVALID", "The saved database audit record cannot be read.") from None
    if (saved.operation_id != record.operation_id or saved.client_id != record.client_id
        or saved.submitted != record.submitted or saved.approval != record.approval
        or saved.status != "succeeded" or saved.audit is None):
        raise GatewayError(409, "WRITE_AUDIT_INVALID", "The saved database audit record does not match the submitted operation.")
    return saved.audit


def row_values(
    session: MutationSession, target: str, columns: list[str], where: str, key: dict[str, str | int], *, lock: bool,
) -> dict[str, BindValue]:
    projection = ", ".join(session.identifier(name) for name in [*key, *columns])
    hint = " WITH (UPDLOCK, HOLDLOCK)" if lock and not session.oracle else ""
    suffix = " FOR UPDATE" if lock and session.oracle else ""
    session.execute(f"SELECT {projection} FROM {target}{hint} WHERE {where}{suffix}", list(key.values()))
    rows = session.cursor.fetchmany(2)
    if len(rows) != 1:
        raise GatewayError(409, "WRITE_ROW_NOT_UNIQUE", "The primary key must identify exactly one row.")
    row = tuple(rows[0])
    raw_key, values = row[:len(key)], row[len(key):]
    for expected, actual in zip(key.values(), raw_key, strict=True):
        normalized = normalize_cell(actual, 32_768)
        if normalized == expected and type(normalized) is type(expected):
            continue
        if type(expected) is int and isinstance(actual, Decimal) and actual == expected:
            continue
        if isinstance(expected, str) and type(actual) is int and str(actual) == expected:
            continue
        raise GatewayError(409, "WRITE_KEY_VALUE_MISMATCH", "The selected row's key differs from the requested key.")
    return {name: normalize_cell(value, 32_768) for name, value in zip(columns, values, strict=True)}


def apply_mutation(
    adapter: Any, source: Datasource, grant: WriteGrant, record: MutationRecord, deadline: Deadline,
) -> MutationAudit:
    request = record.submitted
    audit_table = source.write_audit_table
    assert audit_table is not None
    committing = False
    try:
        with mutation_session(adapter, source, deadline) as session:
            if primary_key(session, audit_table) != {"OPERATION_ID"}:
                raise GatewayError(409, "WRITE_AUDIT_INVALID", "The audit table must have OPERATION_ID as its primary key.")
            previous = load_audit(session, audit_table, record)
            if previous is not None:
                return previous

            if primary_key(session, request.table) != set(grant.primary_key):
                raise GatewayError(409, "WRITE_KEY_MISMATCH", "The configured key does not match the database table's primary key.")

            target = session.object_name(request.table)
            key = {column: request.key[column] for column in grant.primary_key}
            where = " AND ".join(f"{session.identifier(column)} = {session.bind(index)}"
                                 for index, column in enumerate(grant.primary_key, 1))
            columns = [column for column in grant.columns if column in request.values]
            values = [request.values[column] for column in columns]
            before = None
            if request.operation == "update":
                before = row_values(session, target, columns, where, key, lock=True)
                assignments = ", ".join(f"{session.identifier(column)} = {session.bind(index)}"
                                        for index, column in enumerate(columns, 1))
                update_where = " AND ".join(f"{session.identifier(column)} = {session.bind(index)}"
                                           for index, column in enumerate(grant.primary_key, len(columns) + 1))
                session.execute(f"UPDATE {target} SET {assignments} WHERE {update_where}", values + list(key.values()))
            else:
                insert_columns = ", ".join(session.identifier(column) for column in grant.primary_key + columns)
                placeholders = ", ".join(session.bind(index) for index in range(1, len(key) + len(values) + 1))
                session.execute(f"INSERT INTO {target} ({insert_columns}) VALUES ({placeholders})", list(key.values()) + values)
            after = row_values(session, target, columns, where, key, lock=False)
            audit = MutationAudit(before=before, after=after, changed_at=datetime.now(UTC))
            payload = json_bytes(record.model_copy(update={"status": "succeeded", "audit": audit}).model_dump(mode="json"))
            if len(payload) > MAX_AUDIT_BYTES:
                raise GatewayError(413, "WRITE_AUDIT_LIMIT", "The before and after values exceed the audit size limit.")

            audit_value = payload.decode("ascii")
            if session.oracle:
                audit_value = session.cursor.var(oracledb.DB_TYPE_CLOB)
                audit_value.setvalue(0, payload.decode("ascii"))
            audit_target = session.object_name(audit_table)
            operation_id = session.identifier("OPERATION_ID")
            event_json = session.identifier("EVENT_JSON")
            session.execute(
                f"INSERT INTO {audit_target} ({operation_id}, {event_json}) VALUES ({session.bind(1)}, {session.bind(2)})",
                [record.operation_id, audit_value],
            )
            if load_audit(session, audit_table, record) != audit:
                raise GatewayError(409, "WRITE_AUDIT_INVALID", "The mutation audit could not be verified before commit.")
            deadline.remaining()
            committing = True
            session.connection.commit()
            return audit
    except (oracledb.Error, pyodbc.Error):
        if committing:
            raise GatewayError(503, "WRITE_COMMIT_UNKNOWN", "The database did not confirm commit. Check the audit table.") from None
        raise GatewayError(422, "WRITE_DATABASE_ERROR", "The database refused the mutation or the audit insert.") from None


def reconcile_mutation(adapter: Any, source: Datasource, record: MutationRecord, deadline: Deadline) -> MutationAudit | None:
    assert source.write_audit_table is not None
    try:
        with mutation_session(adapter, source, deadline) as session:
            return load_audit(session, source.write_audit_table, record)
    except (oracledb.Error, pyodbc.Error):
        raise GatewayError(503, "WRITE_AUDIT_UNAVAILABLE", "The database audit table cannot be read.") from None
