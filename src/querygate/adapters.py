import math
from collections.abc import Iterator
from contextlib import AbstractContextManager, closing, contextmanager
from typing import Protocol

import oracledb
import pyodbc
from pydantic import SecretStr

from .config import Datasource, read_secret
from .errors import GatewayError, query_timeout
from .execution import Cursor, Deadline
from .policy import ValidatedQuery


class DatabaseAdapter(Protocol):
    def query(self, query: ValidatedQuery, deadline: Deadline) -> AbstractContextManager[Cursor]: ...


def driver_error(unavailable: bool) -> GatewayError:
    if unavailable:
        return GatewayError(503, "DATASOURCE_UNAVAILABLE", "The datasource is unavailable.")
    return GatewayError(422, "SQL_EXECUTION_ERROR", "The database could not execute the query. Check its dialect, column names and parameter types.")


class SqlServerAdapter:
    def __init__(self, connection_string: SecretStr):
        self.connection_string = connection_string

    @contextmanager
    def query(self, query: ValidatedQuery, deadline: Deadline) -> Iterator[Cursor]:
        connecting = True
        try:
            connection = pyodbc.connect(
                self.connection_string.get_secret_value(), timeout=math.ceil(deadline.remaining()), autocommit=False,
            )
            with closing(connection):
                connection.timeout = math.ceil(deadline.remaining())
                with closing(connection.cursor()) as cursor, deadline.cancellable(cursor.cancel):
                    connecting = False
                    deadline.remaining()
                    cursor.execute(query.sql, query.params)
                    yield cursor
        except pyodbc.Error as error:
            state = str(error.args[0]) if error.args else ""
            if state in {"HYT00", "HYT01", "HY008"}:
                raise query_timeout() from None
            raise driver_error(connecting or state.startswith("08")) from None


class OracleAdapter:
    def __init__(self, dsn: SecretStr, user: SecretStr, password: SecretStr):
        self.dsn = dsn
        self.user = user
        self.password = password

    @contextmanager
    def query(self, query: ValidatedQuery, deadline: Deadline) -> Iterator[Cursor]:
        connecting = True
        try:
            connection = oracledb.connect(
                dsn=self.dsn.get_secret_value(), user=self.user.get_secret_value(),
                password=self.password.get_secret_value(), tcp_connect_timeout=deadline.remaining(), retry_count=0,
            )
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
            if code in {"DPY-4024", "DPI-1067", "DPI-1080", "ORA-01013"}:
                raise query_timeout() from None
            raise driver_error(connecting or code in {"DPY-4011", "ORA-03113", "ORA-03114"}) from None


def build_adapters(sources: dict[str, Datasource]) -> dict[str, DatabaseAdapter]:
    # pyodbc applies pooling process-wide before the first connection is opened.
    pyodbc.pooling = False
    adapters: dict[str, DatabaseAdapter] = {}
    for name, source in sources.items():
        if source.engine == "sqlserver":
            assert source.connection_string_env is not None
            adapters[name] = SqlServerAdapter(read_secret(source.connection_string_env))
        else:
            assert source.dsn_env is not None and source.user_env is not None and source.password_env is not None
            adapters[name] = OracleAdapter(read_secret(source.dsn_env), read_secret(source.user_env), read_secret(source.password_env))
    return adapters
