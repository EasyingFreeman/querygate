import math
import re
from collections.abc import Mapping
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, closing, contextmanager
from typing import Protocol

import oracledb
import pyodbc
from pydantic import SecretStr

from .config import CatalogColumn, CatalogEntry, Datasource, read_secret
from .engines import Engine
from .errors import GatewayError, query_timeout
from .execution import Cursor, Deadline
from .object_names import CATALOG_IDENTIFIER, qualified_object_name
from .policy import ValidatedQuery


class DatabaseAdapter(Protocol):
    def query(self, query: ValidatedQuery, deadline: Deadline) -> AbstractContextManager[Cursor]: ...

    def catalog(self, deadline: Deadline) -> dict[str, CatalogEntry]: ...


SQLSERVER_CATALOG = """
SELECT s.name, o.name, c.name, t.name, CASE WHEN o.type = 'V' THEN 'view' ELSE 'table' END
FROM sys.all_objects AS o
JOIN sys.schemas AS s ON s.schema_id = o.schema_id
JOIN sys.all_columns AS c ON c.object_id = o.object_id
JOIN sys.types AS t ON t.user_type_id = c.user_type_id
WHERE o.type IN ('U', 'V')
  AND HAS_PERMS_BY_NAME(QUOTENAME(s.name) + N'.' + QUOTENAME(o.name), N'OBJECT', N'SELECT', c.name, N'COLUMN') = 1
ORDER BY s.name, o.name, c.column_id
"""

ORACLE_CATALOG = """
SELECT c.owner, c.table_name, c.column_name, c.data_type,
       CASE WHEN o.object_type = 'VIEW' THEN 'view' ELSE 'table' END
FROM sys.all_tab_columns c
JOIN sys.all_objects o ON o.owner = c.owner AND o.object_name = c.table_name
WHERE o.object_type IN ('TABLE', 'VIEW') AND o.subobject_name IS NULL
ORDER BY c.owner, c.table_name, c.column_id
"""


def read_catalog(adapter: DatabaseAdapter, statement: ValidatedQuery, deadline: Deadline) -> dict[str, CatalogEntry]:
    catalog: dict[str, CatalogEntry] = {}
    with adapter.query(statement, deadline) as cursor:
        while True:
            deadline.remaining()
            rows = cursor.fetchmany(256)
            if not rows:
                break
            for schema, table, column, datatype, kind in rows:
                if not re.fullmatch(CATALOG_IDENTIFIER, schema) or not re.fullmatch(CATALOG_IDENTIFIER, table):
                    continue
                name = qualified_object_name(schema, table)
                if name not in catalog:
                    catalog[name] = CatalogEntry(kind=kind)
                entry = catalog[name]
                entry.columns.append(CatalogColumn(name=column, type=datatype))
    deadline.remaining()
    return catalog


def driver_error(unavailable: bool, diagnostic: str | None = None, message: str | None = None) -> GatewayError:
    if unavailable:
        return GatewayError(
            503, "DATASOURCE_UNAVAILABLE",
            message or "The database connection failed. Check the connection configuration and network from the QueryGate host.",
            diagnostic=diagnostic or "DATABASE_CONNECTION_FAILED",
        )
    return GatewayError(422, "SQL_EXECUTION_ERROR", "The database could not execute the query. Check its dialect, column names and parameter types.")


class SqlServerAdapter:
    def __init__(self, connection_string: SecretStr):
        self.connection_string = connection_string

    def catalog(self, deadline: Deadline) -> dict[str, CatalogEntry]:
        return read_catalog(self, ValidatedQuery(SQLSERVER_CATALOG, []), deadline)

    def connect(self, deadline: Deadline) -> pyodbc.Connection:
        return pyodbc.connect(
            self.connection_string.get_secret_value(), timeout=math.ceil(deadline.remaining()), autocommit=False,
        )

    @contextmanager
    def query(self, query: ValidatedQuery, deadline: Deadline) -> Iterator[Cursor]:
        connecting = True
        try:
            connection = self.connect(deadline)
            with closing(connection):
                connection.timeout = math.ceil(deadline.remaining())
                with closing(connection.cursor()) as cursor, deadline.cancellable(cursor.cancel):
                    connecting = False
                    deadline.remaining()
                    cursor.execute(query.sql, query.params)
                    yield cursor
        except pyodbc.Error as error:
            state = str(error.args[0]) if error.args else ""
            if connecting:
                message = str(error.args[1]).lower() if len(error.args) > 1 else ""
                if state in {"IM002", "IM003", "IM014"}:
                    raise driver_error(True, "ODBC_DRIVER_UNAVAILABLE", "Verify the ODBC driver or DSN name and that its architecture matches Python.") from None
                if state == "28000":
                    raise driver_error(True, "DATABASE_LOGIN_FAILED", "SQL Server rejected the login. Verify the authentication method and database access.") from None
                if state == "08001" and ("ssl provider" in message or "ssl routines" in message):
                    raise driver_error(True, "DATABASE_TLS_FAILED", "SQL Server TLS negotiation failed. Check the server name and certificate trust on the QueryGate host.") from None
                if state in {"HYT00", "HYT01"}:
                    raise driver_error(True, "DATABASE_CONNECT_TIMEOUT", "The connection timed out. Check the SQL Server host, port and network.") from None
            if state in {"HYT00", "HYT01", "HY008"}:
                raise query_timeout() from None
            raise driver_error(connecting or state.startswith("08")) from None


class OracleAdapter:
    def __init__(self, dsn: SecretStr, user: SecretStr, password: SecretStr):
        self.dsn = dsn
        self.user = user
        self.password = password

    def catalog(self, deadline: Deadline) -> dict[str, CatalogEntry]:
        return read_catalog(self, ValidatedQuery(ORACLE_CATALOG, {}), deadline)

    def connect(self, deadline: Deadline) -> oracledb.Connection:
        return oracledb.connect(
            dsn=self.dsn.get_secret_value(), user=self.user.get_secret_value(),
            password=self.password.get_secret_value(), tcp_connect_timeout=deadline.remaining(), retry_count=0,
        )

    @contextmanager
    def query(self, query: ValidatedQuery, deadline: Deadline) -> Iterator[Cursor]:
        connecting = True
        try:
            connection = self.connect(deadline)
            with closing(connection):
                connection.call_timeout = math.ceil(deadline.remaining() * 1000)
                with closing(connection.cursor()) as cursor, deadline.cancellable(connection.cancel):
                    connecting = False
                    cursor.prefetchrows = 0
                    cursor.arraysize = 1
                    cursor.execute("SET TRANSACTION READ ONLY")
                    connection.call_timeout = math.ceil(deadline.remaining() * 1000)
                    cursor.execute(query.sql, query.params, fetch_decimals=True, fetch_lobs=True)
                    yield cursor
        except oracledb.Error as error:
            code = getattr(error.args[0], "full_code", "") if error.args else ""
            if connecting and code in {"ORA-01017", "ORA-28000", "ORA-28001"}:
                raise driver_error(True, "DATABASE_LOGIN_FAILED", "Oracle rejected the login. Check the account, password and account status.") from None
            if connecting and code in {"DPY-6001", "DPY-6003", "ORA-12514", "ORA-12505"}:
                raise driver_error(True, "ORACLE_SERVICE_UNAVAILABLE", "The Oracle listener does not recognize the requested service or SID. Check the DSN.") from None
            if code in {"DPY-4024", "DPI-1067", "DPI-1080", "ORA-01013"}:
                raise query_timeout() from None
            raise driver_error(connecting or code in {"DPY-4011", "ORA-03113", "ORA-03114"}) from None


ADAPTERS: dict[Engine, Callable[..., DatabaseAdapter]] = {"sqlserver": SqlServerAdapter, "oracle": OracleAdapter}
pyodbc.pooling = False


def build_adapters(sources: dict[str, Datasource], environment: Mapping[str, str]) -> dict[str, DatabaseAdapter]:
    return {
        name: ADAPTERS[source.engine](**{key: read_secret(variable, environment) for key, variable in source.connection_env().items()})
        for name, source in sources.items()
    }
