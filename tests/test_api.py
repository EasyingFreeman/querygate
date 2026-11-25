import threading
from contextlib import contextmanager
from decimal import Decimal

from fastapi.testclient import TestClient

from querygate.app import create_app
from querygate.gateway import Gateway

ANALYST_KEY = "analyst-demo-token-only-for-tests"


class FakeCursor:
    description = [("TOTAL", Decimal)]

    def __init__(self, rows):
        self.rows = iter(rows)

    def fetchmany(self, size):
        assert size == 1
        return [row] if (row := next(self.rows, None)) is not None else []


class FakeDatabase:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    @contextmanager
    def query(self, query, deadline):
        self.calls.append(query)
        yield FakeCursor(self.rows)


def make_client(settings, database):
    settings.clients["limited"] = settings.clients["analyst"].model_copy(update={"datasources": ["erp"]})
    keys = {"analyst": ANALYST_KEY.encode(), "limited": b"other-demo-token-only-for-tests"}
    gateway = Gateway(settings, keys, {name: database for name in settings.datasources})
    return TestClient(create_app(gateway), headers={"Authorization": f"Bearer {ANALYST_KEY}"})


def test_discovery_query_and_status(settings, caplog):
    database = FakeDatabase([(Decimal("12.0001"),), (Decimal("13.0002"),)])
    settings.datasources["finance"].max_rows = 1
    caplog.set_level("INFO", logger="uvicorn.error.querygate.audit")
    sql = "SELECT amount FROM ai_public.v_transactions WHERE transaction_id = ?"
    with make_client(settings, database) as client:
        assert client.get("/v1/datasources", headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert [entry["id"] for entry in client.get("/v1/datasources").json()] == ["finance", "erp"]
        assert client.get("/v1/schema?datasource_id=finance").json()["objects"][0]["name"] == "ai_public.v_transactions"
        result = client.post("/v1/query", json={"datasource_id": "finance", "sql": sql, "params": [1]})
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["rows"] == [["12.0001"]]
        assert body["row_count"] == 1 and body["truncated"] is True
        assert database.calls[0].sql == sql and database.calls[0].params == [1]
        assert result.headers["Cache-Control"] == "no-store"
        status = f"/v1/queries/{body['query_id']}"
        assert client.get(status).json()["status"] == "success"
        other = {"Authorization": "Bearer other-demo-token-only-for-tests"}
        assert client.get(status, headers=other).status_code == 404
        assert client.get("/v1/schema?datasource_id=finance", headers=other).status_code == 404
        assert client.post("/v1/query", headers=other, json={"datasource_id": "finance", "sql": sql}).status_code == 404
    assert body["query_id"] in caplog.text
    assert sql not in caplog.text and "12.0001" not in caplog.text and ANALYST_KEY not in caplog.text


def test_invalid_requests_never_use_driver(settings):
    database = FakeDatabase([])
    with make_client(settings, database) as client:
        too_large = client.post("/v1/query", content=b"x" * (128 * 1024 + 1))
        assert too_large.status_code == 413
        oversized_sql = "SELECT '" + "界" * 12_000 + "'"
        assert client.post("/v1/query", json={"datasource_id": "finance", "sql": oversized_sql}).status_code == 413
        invalid = client.post("/v1/query", json={"datasource_id": "finance", "sql": "SELECT 1", "max_rows": True})
        assert invalid.json()["code"] == "INVALID_REQUEST"
        denied = client.post("/v1/query", json={"datasource_id": "finance", "sql": "SELECT * FROM dbo.private_view"})
        assert denied.json()["code"] == "OBJECT_NOT_ALLOWED"
        assert database.calls == []


def test_response_bytes_and_end_of_result(settings):
    settings.datasources["finance"].max_response_bytes = 1024
    database = FakeDatabase([("界" * 61,), ("界" * 200,)])
    payload = {"datasource_id": "finance", "sql": "SELECT 1"}
    with make_client(settings, database) as client:
        result = client.post("/v1/query", json=payload)
        assert result.status_code == 200
        assert len(result.content) <= 1024
        assert result.json()["row_count"] == 1 and result.json()["truncated"] is True
        database.rows = [("界" * 200,)]
        assert client.post("/v1/query", json=payload).json()["code"] == "RESULT_LIMIT_EXCEEDED"
        database.rows = [("one row",)]
        assert client.post("/v1/query", json={**payload, "max_rows": 1}).json()["truncated"] is False


def test_timeout_retains_slot_until_cleanup(settings):
    cancelled = threading.Event()
    clean_up = threading.Event()
    settings.datasources["finance"].max_concurrency = 1
    settings.datasources["finance"].timeout_seconds = 1

    class DelayedDatabase:
        @contextmanager
        def query(self, query, deadline):
            with deadline.cancellable(cancelled.set):
                assert cancelled.wait(5)
                clean_up.wait(5)
                yield FakeCursor([])

    payload = {"datasource_id": "finance", "sql": "SELECT 1"}
    try:
        with make_client(settings, DelayedDatabase()) as client:
            result = client.post("/v1/query", json=payload)
            assert result.status_code == 504
            assert result.json()["code"] == "QUERY_TIMEOUT"
            assert cancelled.wait(2)
            assert client.post("/v1/query", json=payload).json()["code"] == "DATASOURCE_BUSY"
    finally:
        clean_up.set()
