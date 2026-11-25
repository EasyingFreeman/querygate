import oracledb
import pyodbc
import pytest
from fastapi.testclient import TestClient

from querygate.adapters import ADAPTERS
from querygate.admin import create_local_app
from querygate.config import CatalogEntry
from querygate.setup_config import SetupConfig

ADMIN = {"Authorization": "Bearer local-test-admin"}


class FixtureVariable:
    def setvalue(self, position, value):
        self.value = value


class FixtureConnection:
    def __init__(self, database):
        self.database = database
        self.rows = dict(database.rows)
        self.audit = dict(database.audit)
        self.results = []

    def cursor(self):
        return self

    def cancel(self):
        pass

    def close(self):
        pass

    def commit(self):
        self.database.commits += 1
        self.database.rows = self.rows
        self.database.audit = self.audit
        if self.database.lose_commit_reply:
            raise self.database.driver_error("simulated commit disconnect")

    def rollback(self):
        self.database.rollbacks += 1

    def var(self, datatype):
        return FixtureVariable()

    def fetchmany(self, size):
        return self.results[:size]

    def execute(self, sql, values, **kwargs):
        self.database.commands.append((sql, values))
        sql = sql.upper()
        if sql.startswith("\nSELECT"):
            self.results = [("OPERATION_ID",)] if values[1].upper() == "AUDIT_LOG" else [(self.database.key,)]
        elif sql.startswith("SELECT") and "AUDIT_LOG" in sql:
            receipt = self.audit.get(values[0])
            self.results = [(receipt,)] if receipt else []
        elif sql.startswith("SELECT"):
            self.results = [(values[0], self.rows[values[0]])] if values[0] in self.rows else []
        elif sql.startswith("UPDATE"):
            new_value, key = values
            self.rows[key] = new_value.strip()
        elif sql.startswith("INSERT") and "AUDIT_LOG" in sql:
            if self.database.fail_audit:
                raise self.database.driver_error("simulated audit failure")
            key, value = values
            self.audit[key] = value.value if isinstance(value, FixtureVariable) else value
        elif sql.startswith("INSERT"):
            key, new_value = values
            assert key not in self.rows
            self.rows[key] = new_value.strip()
        else:
            raise AssertionError("Unexpected driver statement")


class FixtureDatabase:
    def __init__(self, engine):
        self.key = "ID"
        self.rows = {7: "Queued"}
        self.audit = {}
        self.commands = []
        self.commits = 0
        self.rollbacks = 0
        self.driver_error = oracledb.Error if engine == "oracle" else pyodbc.Error
        self.fail_audit = False
        self.lose_commit_reply = False

    def connect(self, deadline):
        return FixtureConnection(self)

    def catalog(self, deadline):
        return {table: CatalogEntry(kind="table") for table in ("APP.ORDERS", "APP.AUDIT_LOG")}


@pytest.fixture(params=["sqlserver", "oracle"])
def setup(request, tmp_path, monkeypatch):
    engine = request.param
    database = FixtureDatabase(engine)
    monkeypatch.setitem(ADAPTERS, engine, lambda **credentials: database)
    (tmp_path / "index.html").write_text("<html>Workbench</html>", encoding="utf-8")
    storage = SetupConfig(tmp_path / "querygate.yaml", tmp_path / ".env")
    credentials = {"connection_string": "fixture"} if engine == "sqlserver" else {
        "dsn": "fixture", "user": "fixture", "password": "fixture",
    }
    storage.save_source("inventory", engine, "", True, "", "", credentials, {"analyst"},
                        existing=False, write_audit_table="APP.AUDIT_LOG")
    storage.save_client("agent", None, ["inventory"], policy_updates={
        "write_policy": {"datasources": {"inventory": {"APP.ORDERS": {
            "operations": ["update", "insert"], "primary_key": ["ID"], "columns": ["STATUS"],
        }}}},
    })
    return storage, database, tmp_path


def command(**overrides):
    return {
        "request_id": "e3ebd235-adb4-4d53-b947-e00b00217bd4",
        "operation": "update", "datasource_id": "inventory", "table": "APP.ORDERS",
        "reason": "Correct the order status after the warehouse confirmed shipment",
        "key": {"ID": 7}, "values": {"STATUS": "  Shipped  "},
        **overrides,
    }


def headers(storage, client="agent"):
    return {"Authorization": f"Bearer {storage.client_token(client)}"}


def session(storage, web_root):
    return TestClient(create_local_app(storage, web_root, "local-test-admin", 8123), base_url="http://127.0.0.1:8123")


def test_pending_survives_restart_and_only_web_admin_can_execute(setup):
    storage, database, web_root = setup
    agent = headers(storage)
    with session(storage, web_root) as client:
        record = client.post("/v1/mutations", headers=agent, json=command()).json()
        operation_id = record["operation_id"]
        assert record["status"] == "pending" and database.commands == []
        schema = client.get("/v1/schema?datasource_id=inventory", headers=agent).json()
        assert [item["name"] for item in schema["objects"]] == ["APP.ORDERS"]
        mutation = f"/v1/mutations/{operation_id}"
        approve = f"/admin/mutations/{operation_id}/approve"
        assert client.post(approve, headers=agent).status_code == 401
        assert client.get(mutation, headers=headers(storage, "analyst")).status_code == 404
        assert client.post("/v1/mutations", headers=agent, json=command()).json()["operation_id"] == operation_id
        assert client.post("/v1/mutations", headers=agent, json=command(reason="different")).status_code == 409

    persisted = SetupConfig(storage.config_path, storage.env_path)
    with session(persisted, web_root) as client:
        assert client.get("/admin/mutations?state=pending", headers=ADMIN).json()[0]["operation_id"] == operation_id
        accepted = client.post(approve, headers=ADMIN).json()
        assert accepted["status"] == "succeeded" and accepted["approval"] == "web_admin"
        assert accepted["audit"]["before"] == {"STATUS": "Queued"}
        assert accepted["audit"]["after"] == {"STATUS": "Shipped"}
        assert accepted["submitted"]["reason"] == command()["reason"]
        assert database.rows[7] == "Shipped" and database.commits == 1
        assert any("UPDLOCK" in sql or "FOR UPDATE" in sql for sql, _ in database.commands)
        assert sum("  Shipped  " in values for _, values in database.commands) == 1
        assert all("  Shipped  " not in sql for sql, _ in database.commands)
        assert client.post(approve, headers=ADMIN).status_code == 409
        assert client.get(mutation, headers=agent).json() == accepted


def test_bypass_works_for_insert_but_audit_failure_rolls_back(setup):
    storage, database, web_root = setup
    policy = storage.clients["agent"].write_policy.model_copy(update={"requires_approval": False})
    storage.save_client("agent", "agent", ["inventory"], policy_updates={"write_policy": policy})
    with session(storage, web_root) as client:
        agent = headers(storage)
        database.fail_audit = True
        failure = client.post("/v1/mutations", headers=agent, json=command()).json()
        assert failure["status"] == "failed"
        assert database.rows[7] == "Queued" and not database.audit and database.rollbacks == 1

        database.fail_audit = False
        request = command(
            request_id="e3ebd235-adb4-4d53-b947-e00b00217bd5", operation="insert", key={"ID": 8},
            values={"STATUS": "Ready"},
        )
        inserted = client.post("/v1/mutations", headers=agent, json=request).json()
        assert inserted["status"] == "succeeded" and inserted["approval"] == "client_exempt"
        assert inserted["audit"]["before"] is None and inserted["audit"]["after"] == {"STATUS": "Ready"}
        assert client.post("/v1/mutations", headers=agent, json=request).json() == inserted
        assert database.commits == 1


def test_unknown_commit_can_be_confirmed_without_repeating_write(setup):
    storage, database, web_root = setup
    database.lose_commit_reply = True
    with session(storage, web_root) as client:
        submitted = client.post("/v1/mutations", headers=headers(storage), json=command()).json()
        endpoint = f"/admin/mutations/{submitted['operation_id']}"
        unknown = client.post(f"{endpoint}/approve", headers=ADMIN).json()
        assert unknown["status"] == "unknown" and unknown["error_code"] == "WRITE_COMMIT_UNKNOWN"
        result = client.post(f"{endpoint}/reconcile", headers=ADMIN).json()
        assert result["status"] == "succeeded" and result["audit"]["after"] == {"STATUS": "Shipped"}
        assert database.commits == 1


def test_write_requires_reason_and_revalidates_permissions_before_approval(setup):
    storage, database, web_root = setup
    with session(storage, web_root) as client:
        agent = headers(storage)
        assert client.post("/v1/query", headers=agent, json={"datasource_id": "inventory", "sql": "SELECT 1"}).json()["code"] == "CALL_REASON_REQUIRED"
        for change, code in [
            ({"operation": "delete"}, "INVALID_REQUEST"),
            ({"reason": " "}, "CALL_REASON_REQUIRED"),
            ({"key": {"OTHER": 7}}, "WRITE_KEY_REQUIRED"),
            ({"values": {"SECRET": "fixture"}}, "WRITE_COLUMNS_NOT_ALLOWED"),
        ]:
            failure = client.post("/v1/mutations", headers=agent, json=command(**change)).json()
            assert failure["code"] == code
        assert not database.commands
        submitted = client.post("/v1/mutations", headers=agent, json=command()).json()
        assert client.post("/admin/clients", headers=ADMIN, json={
            "id": "agent", "previous_id": "agent", "datasource_ids": ["inventory"], "write_policy": {},
        }).status_code == 204
        endpoint = f"/admin/mutations/{submitted['operation_id']}"
        assert client.post(f"{endpoint}/approve", headers=ADMIN).json()["code"] == "WRITE_NOT_ALLOWED"
        assert client.post(f"{endpoint}/reject", headers=ADMIN).json()["status"] == "rejected"
        assert not database.commands
