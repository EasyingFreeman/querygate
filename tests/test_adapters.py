from unittest.mock import Mock, call

import oracledb
import pyodbc
import pytest
from pydantic import SecretStr

from querygate.adapters import OracleAdapter, SqlServerAdapter
from querygate.errors import GatewayError
from querygate.execution import Deadline
from querygate.policy import ValidatedQuery


def mock_connect(monkeypatch, driver):
    cursor = Mock()
    connection = Mock()
    connection.cursor.return_value = cursor
    connect = Mock(return_value=connection)
    monkeypatch.setattr(driver, "connect", connect)
    return connect, connection, cursor


def test_sqlserver_bind_and_cleanup(monkeypatch):
    connect, connection, cursor = mock_connect(monkeypatch, pyodbc)
    adapter = SqlServerAdapter(SecretStr("trusted ODBC string"))
    with adapter.query(ValidatedQuery("SELECT ?", [12]), Deadline(10)) as result:
        assert result is cursor
    connect.assert_called_once_with("trusted ODBC string", timeout=10, autocommit=False)
    cursor.execute.assert_called_once_with("SELECT ?", [12])
    cursor.close.assert_called_once()
    connection.close.assert_called_once()


def test_oracle_read_only_bind_and_cleanup(monkeypatch):
    connect, connection, cursor = mock_connect(monkeypatch, oracledb)
    adapter = OracleAdapter(SecretStr("dsn"), SecretStr("user"), SecretStr("password"))
    sql = "SELECT :VAL FROM AI_PUBLIC.V_ORDERS"
    with adapter.query(ValidatedQuery(sql, {"VAL": 12}), Deadline(10)) as result:
        assert result is cursor
    assert connect.call_args.kwargs["retry_count"] == 0
    assert cursor.execute.call_args_list == [
        call("SET TRANSACTION READ ONLY"), call(sql, {"VAL": 12}, fetch_decimals=True, fetch_lobs=True),
    ]
    assert cursor.arraysize == 1 and cursor.prefetchrows == 0
    cursor.close.assert_called_once()
    connection.close.assert_called_once()


def test_driver_error_sanitized_and_connection_closed(monkeypatch):
    _, connection, cursor = mock_connect(monkeypatch, pyodbc)
    cursor.execute.side_effect = pyodbc.Error("42000", "Internal data and connection details")
    adapter = SqlServerAdapter(SecretStr("trusted ODBC string"))
    with pytest.raises(GatewayError) as error:
        with adapter.query(ValidatedQuery("SELECT 1", []), Deadline(10)):
            pytest.fail("The adapter yielded after a failed execute")
    assert error.value.code == "SQL_EXECUTION_ERROR"
    assert "connection details" not in str(error.value)
    connection.close.assert_called_once()
