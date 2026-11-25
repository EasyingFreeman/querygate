"""Local MCP stdio transport for an existing QueryGate REST service."""

import asyncio
import json
import os
import sys
from typing import Any

import httpx2
from mcp import stdio_server, types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .api_models import (
    DatasourceInfo, ErrorResponse, MutationId, MutationRecord, MutationRequest, QueryRequest, QueryResponse,
    SchemaRequest, SchemaResponse,
)
from .config import BEARER_TOKEN

HTTP_TIMEOUT = 65


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class McpSchemaRequest(SchemaRequest):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=100)
    include_columns: bool = False


class DatasourcesResult(BaseModel):
    datasources: list[DatasourceInfo]


class MutationLookup(EmptyRequest):
    operation_id: MutationId


def result(payload: BaseModel, *, error: bool = False) -> types.CallToolResult:
    data = payload.model_dump(mode="json", exclude_unset=True)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=True, separators=(",", ":")))],
        structured_content=data,
        is_error=error,
    )


def failure(code: str, message: str) -> types.CallToolResult:
    return result(ErrorResponse(code=code, message=message), error=True)


def tool_definitions() -> list[types.Tool]:
    read_only = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
    return [
        types.Tool(
            name="list_datasources",
            description="List datasources available to this caller and their native SQL dialects.",
            input_schema=EmptyRequest.model_json_schema(),
            output_schema=DatasourcesResult.model_json_schema(),
            annotations=read_only,
        ),
        types.Tool(
            name="get_schema",
            description=(
                "Browse authorized tables and views. Returns 50 objects without columns by default. "
                "Filter with search or kind, and pass next_offset as offset to continue. "
                "For columns, pass an exact schema-qualified object_name and include_columns=true."
            ),
            input_schema=McpSchemaRequest.model_json_schema(),
            output_schema=SchemaResponse.model_json_schema(),
            annotations=read_only,
        ),
        types.Tool(
            name="execute_query",
            description=(
                "Run one read-only query using native SQL and schema-qualified catalog objects. "
                "For SQL Server, bind ? markers with a positional array. "
                "For Oracle, bind :name markers with an object. Inspect get_schema first. "
                "Provide a reason that explains what the user is trying to accomplish."
            ),
            input_schema=QueryRequest.model_json_schema(),
            output_schema=QueryResponse.model_json_schema(),
            annotations=read_only,
        ),
        types.Tool(
            name="submit_mutation",
            description=(
                "Submit one authorized UPDATE or INSERT with its full primary key, values and business reason. "
                "Generate a UUID request_id and reuse it for retries of the same request. "
                "A pending result requires local Web approval. Check get_mutation for the outcome."
            ),
            input_schema=MutationRequest.model_json_schema(),
            output_schema=MutationRecord.model_json_schema(),
            annotations=types.ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True),
        ),
        types.Tool(
            name="get_mutation",
            description="Read the status and audit values for a mutation submitted with this client token.",
            input_schema=MutationLookup.model_json_schema(),
            output_schema=MutationRecord.model_json_schema(),
            annotations=read_only,
        ),
    ]


def create_server(client: httpx2.AsyncClient) -> Server:
    tools = tool_definitions()

    async def list_tools(_: ServerRequestContext, __: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def call_tool(_: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
        arguments = params.arguments or {}
        try:
            async with asyncio.timeout(HTTP_TIMEOUT):
                if params.name == "list_datasources":
                    EmptyRequest.model_validate(arguments)
                    response = await client.get("v1/datasources")
                elif params.name == "get_schema":
                    schema_request = McpSchemaRequest.model_validate(arguments, strict=True)
                    response = await client.get("v1/schema", params=schema_request.model_dump(exclude_none=True))
                elif params.name == "execute_query":
                    query = QueryRequest.model_validate(arguments)
                    response = await client.post("v1/query", json=query.model_dump(mode="json"))
                elif params.name == "submit_mutation":
                    mutation = MutationRequest.model_validate(arguments)
                    response = await client.post("v1/mutations", json=mutation.model_dump(mode="json"))
                elif params.name == "get_mutation":
                    lookup = MutationLookup.model_validate(arguments)
                    response = await client.get(f"v1/mutations/{lookup.operation_id}")
                else:
                    return failure("UNKNOWN_TOOL", "This QueryGate tool is unavailable.")
        except (ValidationError, UnicodeError):
            return failure("INVALID_REQUEST", "Arguments do not match the tool schema.")
        except (TimeoutError, httpx2.TimeoutException):
            return failure("GATEWAY_TIMEOUT", "The QueryGate API did not respond in time.")
        except httpx2.RequestError:
            return failure("GATEWAY_UNAVAILABLE", "The QueryGate API could not be reached.")

        return decode_response(params.name, response)

    return Server("querygate", on_list_tools=list_tools, on_call_tool=call_tool)


def decode_response(name: str, response: httpx2.Response) -> types.CallToolResult:
    try:
        data: Any = response.json()
        if not response.is_success:
            if response.is_client_error or response.is_server_error:
                return result(ErrorResponse.model_validate(data, strict=True), error=True)
        elif name == "list_datasources":
            return result(DatasourcesResult.model_validate({"datasources": data}, strict=True))
        elif name == "get_schema":
            return result(SchemaResponse.model_validate(data, strict=True))
        elif name in {"submit_mutation", "get_mutation"}:
            return result(MutationRecord.model_validate_json(response.content, strict=True))
        else:
            return result(QueryResponse.model_validate(data, strict=True))
    except (ValueError, RecursionError):
        pass
    return failure("GATEWAY_RESPONSE_ERROR", "The QueryGate API returned an unexpected response.")


def gateway_url(value: str) -> httpx2.URL:
    error = "QUERYGATE_URL must be an HTTPS URL or an HTTP loopback URL, without credentials, query or fragment."
    try:
        url = httpx2.URL(value)
    except httpx2.InvalidURL:
        raise ValueError(error) from None
    if not url.host or url.userinfo or url.query or url.fragment:
        raise ValueError(error)
    if url.scheme == "https" or (url.scheme == "http" and url.host in {"127.0.0.1", "::1", "localhost"}):
        return url
    raise ValueError(error)


async def run(url: httpx2.URL, token: str) -> None:
    async with httpx2.AsyncClient(
        base_url=url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(HTTP_TIMEOUT, connect=5.0),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        server = create_server(client)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


def main() -> None:
    token = os.environ.get("QUERYGATE_MCP_TOKEN", "")
    if not BEARER_TOKEN.fullmatch(token):
        sys.exit("Set QUERYGATE_MCP_TOKEN to a valid QueryGate client token.")
    try:
        url = gateway_url(os.environ.get("QUERYGATE_URL", "http://127.0.0.1:8000"))
    except ValueError as error:
        sys.exit(str(error))
    asyncio.run(run(url, token))


if __name__ == "__main__":
    main()
