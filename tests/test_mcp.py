import asyncio
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import Mock

import pytest

from querygate.app import create_app
from querygate.gateway import Gateway

httpx = pytest.importorskip("httpx2")
mcp = pytest.importorskip("mcp")

from querygate.mcp_server import create_server, gateway_url


def test_mcp_uses_existing_gateway_authorization_and_policy(settings):
    secret = "test-client-token-for-mcp-0123456789"
    settings.clients["analyst"].datasources = ["erp"]

    class TestAdapter:
        def __init__(self):
            self.queries = []

        @contextmanager
        def query(self, query, deadline):
            self.queries.append(query)
            cursor = Mock(description=[("TOTAL_AMOUNT", Decimal)])
            cursor.fetchmany.side_effect = [[(Decimal("25.125"),)], []]
            yield cursor

    adapter = TestAdapter()
    gateway = Gateway(settings, {"analyst": secret.encode()}, {"erp": adapter})
    app = create_app(gateway)

    async def exercise():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://querygate",
            headers={"Authorization": f"Bearer {secret}"},
        ) as rest, mcp.Client(create_server(rest), read_timeout_seconds=5) as client:
            tools = (await client.list_tools()).tools
            assert {tool.name for tool in tools} == {"list_datasources", "get_schema", "execute_query", "submit_mutation", "get_mutation"}
            assert {tool.name for tool in tools if not tool.annotations.read_only_hint} == {"submit_mutation"}

            discovery = await client.call_tool("list_datasources", {})
            assert [record["id"] for record in discovery.structured_content["datasources"]] == ["erp"]

            hidden = await client.call_tool("get_schema", {"datasource_id": "finance"})
            assert hidden.is_error and hidden.structured_content["code"] == "DATASOURCE_NOT_FOUND"

            schema = await client.call_tool("get_schema", {"datasource_id": "erp"})
            assert schema.structured_content["objects"][0]["name"] == "AI_PUBLIC.V_ORDERS"
            assert schema.structured_content["objects"][0]["columns"] == []
            detail = await client.call_tool("get_schema", {
                "datasource_id": "erp", "object_name": "AI_PUBLIC.V_ORDERS", "include_columns": True,
            })
            assert detail.structured_content["objects"][0]["columns"][0]["name"] == "ORDER_ID"

            denied = await client.call_tool("execute_query", {
                "datasource_id": "erp", "reason": "Verify the query result", "sql": "SELECT * FROM AI_PUBLIC.OTHER_VIEW",
            })
            assert denied.is_error
            assert denied.structured_content["code"] == "OBJECT_NOT_ALLOWED"
            assert denied.structured_content["query_id"].startswith("q_")

            sql = "SELECT TOTAL_AMOUNT FROM AI_PUBLIC.V_ORDERS WHERE ORDER_ID = :order_id"
            success = await client.call_tool("execute_query", {
                "datasource_id": "erp", "reason": "Verify the query result", "sql": sql, "params": {"order_id": 1},
            })
            assert not success.is_error
            assert success.structured_content["rows"] == [["25.125"]]
            assert len(adapter.queries) == 1 and adapter.queries[0].params == {"ORDER_ID": 1}
            query_id = success.structured_content["query_id"]
            assert (await rest.get(f"/v1/queries/{query_id}")).json()["status"] == "success"

    asyncio.run(exercise())


@pytest.mark.parametrize("status, body, code", [
    (401, {"code": "UNAUTHENTICATED", "message": "A valid bearer token is required."}, "UNAUTHENTICATED"),
    (502, "upstream details", "GATEWAY_RESPONSE_ERROR"),
    (200, {"invalid": "unexpected response"}, "GATEWAY_RESPONSE_ERROR"),
])
def test_mcp_gateway_errors_are_structured(status, body, code):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))

    async def exercise():
        async with httpx.AsyncClient(transport=transport, base_url="http://querygate") as rest:
            async with mcp.Client(create_server(rest), read_timeout_seconds=5) as client:
                response = await client.call_tool("list_datasources", {})
                assert response.is_error and response.structured_content["code"] == code
                assert "upstream details" not in response.content[0].text

    asyncio.run(exercise())


@pytest.mark.parametrize("url", ["http://db.example.com", "https://user:pass@example.com", "file:///secret"])
def test_mcp_gateway_url_restricts_cleartext_and_credentials(url):
    with pytest.raises(ValueError, match="QUERYGATE_URL"):
        gateway_url(url)
