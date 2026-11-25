import pyodbc
from fastapi.testclient import TestClient

from querygate.admin import create_admin_app, create_local_app
from querygate.setup_config import SetupConfig


def test_local_web_setup_saves_denylist_without_returning_connection_values(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    storage = SetupConfig(tmp_path / "querygate.yaml", tmp_path / ".env")
    config = create_admin_app(storage, tmp_path, "admin-session-fixture", 8123)
    payload = {
        "id": "warehouse", "engine": "sqlserver", "object_policy": "denylist",
        "objects": ["dbo.Payroll"], "credentials": {"connection_string": "fixture private connection"},
        "client_ids": ["analyst"],
    }
    with TestClient(config, base_url="http://127.0.0.1:8123") as client:
        assert client.get("/admin/config").status_code == 401
        assert client.get("/admin/config", headers={"Host": "example.com"}).status_code == 400
        auth = {"Authorization": "Bearer admin-session-fixture"}
        denied = client.post("/admin/datasources", headers={**auth, "Origin": "https://example.com"}, json=payload)
        assert denied.status_code == 403
        assert client.post("/admin/datasources", headers=auth, json=payload).status_code == 204
        returned = client.get("/admin/config", headers=auth)
        assert returned.headers["cache-control"] == "no-store"
        assert "fixture private connection" not in returned.text
        source = returned.json()["datasources"][0]
        assert source["object_policy"] == "denylist" and source["objects"] == ["dbo.Payroll"]
        assert source["configured_fields"] == ["connection_string"]


def test_admin_connection_test_explains_missing_odbc_driver_without_saving(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    storage = SetupConfig(tmp_path / "querygate.yaml", tmp_path / ".env")
    app = create_admin_app(storage, tmp_path, "admin-session-fixture", 8123)

    def missing_driver(*args, **kwargs):
        raise pyodbc.Error("IM002", "fixture private driver message")

    monkeypatch.setattr(pyodbc, "connect", missing_driver)
    with TestClient(app, base_url="http://127.0.0.1:8123") as client:
        request = {
            "id": "warehouse", "engine": "sqlserver",
            "credentials": {"connection_string": "fixture private connection"},
        }
        assert client.post("/admin/test-connection", json=request).status_code == 401
        result = client.post(
            "/admin/test-connection", json=request,
            headers={"Authorization": "Bearer admin-session-fixture"},
        )
        assert result.status_code == 503
        assert result.json()["code"] == "ODBC_DRIVER_UNAVAILABLE"
        assert "fixture private" not in result.text
        assert not storage.config_path.exists()
        assert not storage.env_path.exists()


def test_client_changes_apply_to_running_workbench_and_query_api(tmp_path):
    (tmp_path / "index.html").write_text("<html>Workbench</html>", encoding="utf-8")
    storage = SetupConfig(tmp_path / "querygate.yaml", tmp_path / ".env")
    auth = {"Authorization": "Bearer admin-session-fixture"}
    app = create_local_app(storage, tmp_path, "admin-session-fixture", 8123)
    with TestClient(app, base_url="http://127.0.0.1:8123") as client:
        assert client.get("/workbench/").text == "<html>Workbench</html>"
        assert client.get("/v1/datasources").json()["code"] == "GATEWAY_NOT_CONFIGURED"
        for name in ("warehouse", "payroll"):
            assert client.post("/admin/datasources", headers=auth, json={
                "id": name, "engine": "sqlserver", "object_policy": "allowlist",
                "objects": [], "credentials": {"connection_string": "fixture"},
                "client_ids": ["analyst"],
            }).status_code == 204
        assert client.get("/v1/datasources", headers=auth).status_code == 401

        assert client.post("/admin/clients", headers=auth, json={
            "id": "agent", "datasource_ids": ["warehouse"],
        }).status_code == 204
        token = client.post("/admin/clients/agent/token", headers=auth).json()["token"]
        agent_auth = {"Authorization": f"Bearer {token}"}
        assert client.get("/admin/config", headers=agent_auth).status_code == 401
        assert [item["id"] for item in client.get("/v1/datasources", headers=agent_auth).json()] == ["warehouse"]

        assert client.post("/admin/clients", headers=auth, json={
            "id": "reporting", "previous_id": "agent", "datasource_ids": ["payroll"],
        }).status_code == 204
        assert client.post("/admin/clients/reporting/token", headers=auth).json()["token"] == token
        assert [item["id"] for item in client.get("/v1/datasources", headers=agent_auth).json()] == ["payroll"]
        assert client.get("/v1/schema?datasource_id=warehouse", headers=agent_auth).status_code == 404
        assert client.delete("/admin/clients/reporting", headers=auth).status_code == 204
        assert client.get("/v1/datasources", headers=agent_auth).status_code == 401

        assert client.post("/admin/clients", headers=auth, json={
            "id": "reporting", "datasource_ids": ["payroll"],
        }).status_code == 204
        assert client.get("/v1/datasources", headers=agent_auth).status_code == 401
