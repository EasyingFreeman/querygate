from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from querygate.app import create_app
from querygate.config import CatalogColumn, CatalogEntry, Datasource
from querygate.gateway import Gateway


class CatalogAdapter:
    def __init__(self, objects):
        self.objects = objects
        self.reads = 0
        self.queries = 0

    def catalog(self, deadline):
        self.reads += 1
        return {
            name: CatalogEntry(kind="view" if name.startswith("dbo.v_") else "table",
                               columns=[CatalogColumn(name="ID", type="INTEGER")])
            for name in self.objects
        }

    @contextmanager
    def query(self, query, deadline):
        self.queries += 1
        cursor = Mock(description=[("ID", int)])
        cursor.fetchmany.side_effect = [[(1,)], []]
        yield cursor


@pytest.mark.parametrize(("mode", "rules", "expected"), [
    ("allowlist", [], {"dbo.Orders", "dbo.Payroll"}),
    ("allowlist", ["dbo.Orders"], {"dbo.Orders"}),
    ("denylist", [], {"dbo.Orders", "dbo.Payroll"}),
    ("denylist", ["[DBO].[payroll]"], {"dbo.Orders"}),
])
def test_policy_applies_to_catalog_validation_and_execution(settings, mode, rules, expected):
    source = Datasource(
        engine="sqlserver", connection_string_env="QUERYGATE_FINANCE_ODBC", object_policy=mode,
        allowed_objects=rules if mode == "allowlist" else [],
        blocked_objects=rules if mode == "denylist" else [], discover_catalog=True,
    )
    settings.datasources["finance"] = source
    database = CatalogAdapter(["dbo.Orders", "dbo.Payroll"])
    gateway = Gateway(settings, {"analyst": b"case-key"}, {"finance": database})
    with TestClient(create_app(gateway), headers={"Authorization": "Bearer case-key"}) as client:
        response = client.get("/v1/schema?datasource_id=finance")
        assert response.status_code == 200
        assert {item["name"] for item in response.json()["objects"]} == expected
        for name in ("dbo.Orders", "dbo.Payroll", "dbo.NotDiscovered"):
            request = {"datasource_id": "finance", "reason": "Verify the query result", "sql": f"SELECT * FROM {name}"}
            for route in ("/v1/query/validate", "/v1/query"):
                response = client.post(route, json=request)
                if name in expected:
                    assert response.status_code == 200
                else:
                    assert response.json()["code"] == "OBJECT_NOT_ALLOWED"
    assert database.reads == 1
    assert database.queries == len(expected)


def test_empty_oracle_policy_still_checks_columns_and_quoted_table_names(settings):
    settings.datasources["erp"].allowed_objects = []
    database = CatalogAdapter(['DEMO."Quarter.Report"'])
    gateway = Gateway(settings, {"analyst": b"case-key"}, {"erp": database})
    with TestClient(create_app(gateway), headers={"Authorization": "Bearer case-key"}) as client:
        allowed = client.post("/v1/query", json={"datasource_id": "erp", "reason": "Verify the query result", "sql": 'SELECT ID FROM DEMO."Quarter.Report"'})
        denied = client.post("/v1/query", json={"datasource_id": "erp", "reason": "Verify the query result", "sql": 'SELECT UNKNOWN FROM DEMO."Quarter.Report"'})
        assert allowed.status_code == 200
        assert denied.json()["code"] == "COLUMN_NOT_ALLOWED"
    assert database.queries == 1


def test_schema_page_counts_only_authorized_objects(settings):
    settings.datasources["finance"] = Datasource(
        engine="sqlserver", connection_string_env="QUERYGATE_FINANCE_ODBC",
        object_policy="denylist", blocked_objects=["dbo.HiddenOrders"],
    )
    database = CatalogAdapter(["dbo.Orders", "dbo.OldOrders", "dbo.HiddenOrders", "dbo.v_orders"])
    with TestClient(create_app(Gateway(settings, {"analyst": b"case-key"}, {"finance": database})),
                    headers={"Authorization": "Bearer case-key"}) as client:
        legacy = client.get("/v1/schema", params={"datasource_id": "finance"}).json()
        assert "total_objects" not in legacy and set(legacy["objects"][0]) == {"name", "description", "columns"}
        params = {"datasource_id": "finance", "kind": "table", "search": "orders", "limit": 1, "include_columns": False}
        first = client.get("/v1/schema", params=params).json()
        assert first["total_objects"] == 2 and first["next_offset"] == 1
        assert first["objects"][0] == {"name": "dbo.Orders", "description": "", "columns": [], "kind": "table", "column_count": 1}
        second = client.get("/v1/schema", params={**params, "offset": first["next_offset"]}).json()
        assert second["objects"][0]["name"] == "dbo.OldOrders" and second["next_offset"] is None
        detail = client.get("/v1/schema", params={"datasource_id": "finance", "object_name": "[dbo].[v_orders]"}).json()
        assert detail["objects"][0]["kind"] == "view" and detail["objects"][0]["columns"][0]["name"] == "ID"
        blocked = client.get("/v1/schema", params={"datasource_id": "finance", "object_name": "dbo.HiddenOrders"}).json()
        assert blocked["total_objects"] == 0 and not blocked["objects"]
