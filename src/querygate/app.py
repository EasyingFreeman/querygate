import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .adapters import build_adapters
from .api_models import DatasourceInfo, ErrorResponse, QueryRequest, QueryResponse, QueryStatus, SchemaResponse
from .config import Identifier, load_settings, load_tokens
from .errors import GatewayError
from .gateway import Gateway

BODY_BYTES = 128 * 1024
bearer = HTTPBearer(auto_error=False)


def error_response(error: GatewayError) -> JSONResponse:
    payload = ErrorResponse(code=error.code, message=error.message, query_id=error.query_id)
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


async def get_gateway(request: Request) -> Gateway:
    return request.app.state.gateway


GatewayDep = Annotated[Gateway, Depends(get_gateway)]


async def authenticate(gateway: GatewayDep, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
    return gateway.authenticate(credentials.credentials if credentials else None)


ClientDep = Annotated[str, Depends(authenticate)]


def create_app(gateway: Gateway | None = None) -> FastAPI:
    # SQLGlot diagnostics can include text from the submitted statement.
    logging.getLogger("sqlglot").disabled = True

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if gateway is None:
            settings = load_settings()
            runtime = Gateway(settings, load_tokens(settings), build_adapters(settings.datasources))
        else:
            runtime = gateway
        app.state.gateway = runtime
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title="QueryGate", version="0.1.0", lifespan=lifespan)
    app.add_middleware(QueryBodyLimit)

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(_: Request, error: GatewayError) -> JSONResponse:
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def handle_invalid_request(_: Request, __: RequestValidationError) -> JSONResponse:
        return error_response(GatewayError(422, "INVALID_REQUEST", "The request does not match the API schema."))

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/datasources", response_model=list[DatasourceInfo], responses={401: {"model": ErrorResponse}})
    async def datasources(gateway: GatewayDep, client: ClientDep, response: Response) -> list[DatasourceInfo]:
        response.headers["Cache-Control"] = "no-store"
        return gateway.list_datasources(client)

    @app.get("/v1/schema", responses={code: {"model": ErrorResponse} for code in (401, 404, 422)})
    async def schema(gateway: GatewayDep, client: ClientDep, response: Response, datasource_id: Annotated[Identifier, Query()]) -> SchemaResponse:
        response.headers["Cache-Control"] = "no-store"
        return gateway.schema(client, datasource_id)

    @app.post("/v1/query", response_model=QueryResponse,
              responses={code: {"model": ErrorResponse} for code in (401, 404, 413, 422, 429, 500, 503, 504)})
    async def query(gateway: GatewayDep, client: ClientDep, submitted: QueryRequest) -> Response:
        result = await gateway.query(client, submitted)
        return Response(result, media_type="application/json", headers={"Cache-Control": "no-store"})

    @app.get("/v1/queries/{query_id}", responses={code: {"model": ErrorResponse} for code in (401, 404)})
    async def query_status(gateway: GatewayDep, client: ClientDep, query_id: str, response: Response) -> QueryStatus:
        response.headers["Cache-Control"] = "no-store"
        return gateway.query_status(client, query_id)

    return app
