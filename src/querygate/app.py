import logging
import os
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api_models import (
    DatasourceInfo, ErrorResponse, MutationId, MutationRecord, MutationRequest, QueryDetail, QueryHistoryResponse, QueryId, QueryRequest, QueryResponse,
    QueryState, QueryValidation, SchemaRequest, SchemaResponse,
)
from .config import Identifier, load_settings
from .errors import GatewayError
from .gateway import Gateway
from .mutations import MutationService, MutationStore

BODY_BYTES = 128 * 1024
bearer = HTTPBearer(auto_error=False)


def error_response(error: GatewayError) -> JSONResponse:
    payload = ErrorResponse(code=error.code, message=error.message, query_id=error.query_id, diagnostic=error.diagnostic)
    headers = {"Cache-Control": "no-store"}
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(payload.model_dump(exclude_none=True), status_code=error.status, headers=headers)


class QueryBodyLimit:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            chunk = event.get("body", b"")
            if len(body) + len(chunk) > BODY_BYTES:
                response = error_response(GatewayError(413, "REQUEST_TOO_LARGE", "The request body exceeds 128 KiB."))
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not event.get("more_body"):
                break

        async def replay() -> Message:
            if not buffered:
                return await receive()
            return buffered.pop()

        buffered: list[Message] = [{"type": "http.request", "body": bytes(body), "more_body": False}]
        await self.app(scope, replay, send)


class GatewayRuntime:
    def __init__(self, current: Gateway | None):
        self.current = current
        self.in_use: Counter[Gateway] = Counter()

    def replace(self, next_gateway: Gateway | None) -> None:
        previous = self.current
        self.current = next_gateway
        if previous and previous is not next_gateway and previous not in self.in_use:
            previous.close()


async def get_gateway(request: Request) -> AsyncIterator[Gateway]:
    runtime: GatewayRuntime = request.app.state.runtime
    gateway = runtime.current
    if gateway is None:
        raise GatewayError(503, "GATEWAY_NOT_CONFIGURED", "Open Datasource Management in the workbench to configure QueryGate.")
    runtime.in_use[gateway] += 1
    try:
        yield gateway
    finally:
        runtime.in_use[gateway] -= 1
        if not runtime.in_use[gateway]:
            del runtime.in_use[gateway]
            if gateway is not runtime.current:
                gateway.close()


GatewayDep = Annotated[Gateway, Depends(get_gateway)]


async def authenticate(gateway: GatewayDep, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
    return gateway.authenticate(credentials.credentials if credentials else None)


ClientDep = Annotated[str, Depends(authenticate)]


def create_app(gateway: Gateway | None = None, *, locally_managed: bool = False, mutation_path: Path | None = None) -> FastAPI:
    # SQLGlot diagnostics can include text from the submitted statement.
    logging.getLogger("sqlglot").disabled = True

    runtime = GatewayRuntime(gateway)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not locally_managed and gateway is None:
            runtime.replace(Gateway.from_settings(load_settings()))
        try:
            yield
        finally:
            runtime.replace(None)

    app = FastAPI(title="QueryGate", version="0.2.0", lifespan=lifespan)
    app.state.runtime = runtime
    mutation_path = mutation_path or Path(os.environ.get("QUERYGATE_CONFIG", "querygate.yaml")).with_suffix(".mutations.sqlite3")
    mutations = MutationService(MutationStore(mutation_path))
    app.state.mutations = mutations
    app.add_middleware(QueryBodyLimit)

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(_: Request, error: GatewayError) -> JSONResponse:
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def handle_invalid_request(_: Request, error: RequestValidationError) -> JSONResponse:
        if any(item["loc"] == ("body", "reason") for item in error.errors()):
            return error_response(GatewayError(422, "CALL_REASON_REQUIRED", "Provide a call reason between 1 and 500 characters."))
        return error_response(GatewayError(422, "INVALID_REQUEST", "The request does not match the API schema."))

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/datasources", response_model=list[DatasourceInfo], response_model_exclude_none=True,
             responses={401: {"model": ErrorResponse}})
    async def datasources(gateway: GatewayDep, client: ClientDep, response: Response) -> list[DatasourceInfo]:
        response.headers["Cache-Control"] = "no-store"
        return gateway.list_datasources(client)

    @app.get("/v1/schema", response_model_exclude_unset=True,
             responses={code: {"model": ErrorResponse} for code in (401, 404, 422, 429, 503, 504)})
    async def schema(gateway: GatewayDep, client: ClientDep, response: Response, filters: Annotated[SchemaRequest, Query()]) -> SchemaResponse:
        response.headers["Cache-Control"] = "no-store"
        return await gateway.schema(client, filters)

    @app.post("/v1/query", response_model=QueryResponse,
              responses={code: {"model": ErrorResponse} for code in (401, 404, 413, 422, 429, 500, 503, 504)})
    async def query(gateway: GatewayDep, client: ClientDep, submitted: QueryRequest) -> Response:
        result = await gateway.query(client, submitted)
        return Response(result, media_type="application/json", headers={"Cache-Control": "no-store"})

    @app.post("/v1/query/validate", responses={code: {"model": ErrorResponse} for code in (401, 404, 413, 422, 429, 503, 504)})
    async def check_query(gateway: GatewayDep, client: ClientDep, submitted: QueryRequest, response: Response) -> QueryValidation:
        response.headers["Cache-Control"] = "no-store"
        return await gateway.check_query(client, submitted)

    @app.get("/v1/queries", responses={code: {"model": ErrorResponse} for code in (401, 404, 422)})
    async def queries(
        gateway: GatewayDep, client: ClientDep, response: Response,
        datasource_id: Annotated[Identifier | None, Query()] = None,
        status: Annotated[QueryState | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        cursor: Annotated[QueryId | None, Query()] = None,
    ) -> QueryHistoryResponse:
        response.headers["Cache-Control"] = "no-store"
        return gateway.list_queries(client, datasource_id=datasource_id, status=status, limit=limit, cursor=cursor)

    @app.get("/v1/queries/{query_id}", responses={code: {"model": ErrorResponse} for code in (401, 404)})
    async def query_status(gateway: GatewayDep, client: ClientDep, query_id: str, response: Response) -> QueryDetail:
        response.headers["Cache-Control"] = "no-store"
        return gateway.query_status(client, query_id)

    @app.post("/v1/mutations", status_code=202)
    async def submit_mutation(
        gateway: GatewayDep, client: ClientDep, submitted: MutationRequest, response: Response,
    ) -> MutationRecord:
        response.headers["Cache-Control"] = "no-store"
        return await mutations.submit(gateway, client, submitted)

    @app.get("/v1/mutations/{operation_id}")
    async def get_mutation(gateway: GatewayDep, client: ClientDep, operation_id: MutationId, response: Response) -> MutationRecord:
        response.headers["Cache-Control"] = "no-store"
        return mutations.get(gateway, client, operation_id)

    if (web_root := os.environ.get("QUERYGATE_WEB_ROOT")) and not locally_managed:
        directory = Path(web_root)
        if not (directory / "index.html").is_file():
            raise RuntimeError("QUERYGATE_WEB_ROOT must point to the built workbench directory")
        app.mount("/workbench", StaticFiles(directory=directory, html=True), name="workbench")

    return app
