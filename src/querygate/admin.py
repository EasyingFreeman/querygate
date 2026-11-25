import asyncio
import hmac
import logging
import secrets
import socket
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import SecretStr, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .adapters import ADAPTERS
from .api_models import MutationId, MutationRecord, MutationState
from .app import GatewayRuntime, QueryBodyLimit, create_app, error_response
from .config import ConfigModel, ConfigurationError, Identifier, ObjectName, ObjectPolicy, SqlPolicy, WritePolicy, WriteTable
from .engines import ENGINE_PROFILES, Engine
from .errors import GatewayError
from .execution import Deadline
from .gateway import Gateway
from .mutations import MutationService
from .setup_config import SetupConfig


class ConnectionDraft(ConfigModel):
    id: Identifier | None = None
    engine: Engine
    credentials: dict[str, SecretStr]


class SaveDatasource(ConnectionDraft):
    id: Identifier
    description: str = ""
    enabled: bool = True
    object_policy: ObjectPolicy
    sql_policy: SqlPolicy | None = None
    write_audit_table: WriteTable | Literal[""] | None = None
    objects: list[ObjectName]
    client_ids: list[Identifier]
    existing: bool = False


class SaveClient(ConfigModel):
    id: Identifier
    previous_id: Identifier | None = None
    datasource_ids: list[Identifier]
    sql_policy: SqlPolicy | None = None
    write_policy: WritePolicy | None = None


def create_admin_app(
    config: SetupConfig, web_root: Path, session_token: str, port: int, *,
    on_change: Callable[[], None] | None = None,
    mutations: MutationService | None = None,
    runtime: GatewayRuntime | None = None,
) -> FastAPI:
    hosts = ["127.0.0.1", "localhost"]
    origins = {f"http://{host}:{port}" for host in hosts}
    test_lock = asyncio.Lock()

    async def authorize(request: Request) -> None:
        supplied = request.headers.get("authorization", "").encode("utf-8")
        if not hmac.compare_digest(supplied, f"Bearer {session_token}".encode("ascii")):
            raise HTTPException(401, "请从启动脚本重新打开管理页。")
        if (origin := request.headers.get("origin")) is not None and origin not in origins:
            raise HTTPException(403, "请从服务电脑的本地管理页访问。")

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.add_middleware(QueryBodyLimit)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def secure_response(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": "请检查数据源 ID、连接字段和 Schema.对象 格式。"}, status_code=422)

    @app.exception_handler(GatewayError)
    async def gateway_error(_: Request, error: GatewayError) -> JSONResponse:
        return error_response(error)

    @app.get("/admin/config", dependencies=[Depends(authorize)])
    async def get_configuration():
        return {
            "engines": [
                {"id": name, "label": profile.label, "fields": [
                    {"key": field.key, "label": field.label, "masked": field.masked} for field in profile.fields
                ]} for name, profile in ENGINE_PROFILES.items()
            ],
            "clients": [
                {
                    "id": client_id, "datasource_ids": client.datasources, "has_token": bool(config.client_token(client_id)),
                    "sql_policy": client.sql_policy, "write_policy": client.write_policy,
                }
                for client_id, client in config.clients.items()
            ],
            "datasources": [
                {
                    "id": name, "engine": source.engine, "description": source.description, "enabled": source.enabled,
                    "object_policy": source.object_policy,
                    "sql_policy": source.sql_policy,
                    "write_audit_table": source.write_audit_table,
                    "objects": source.blocked_objects if source.object_policy == "denylist" else source.allowed_objects,
                    "configured_fields": [key for key, variable in source.connection_env().items() if config.value(variable)],
                    "client_ids": [client for client, grant in config.clients.items() if name in grant.datasources],
                } for name, source in config.sources.items()
            ],
        }

    @app.post("/admin/datasources", status_code=204, dependencies=[Depends(authorize)])
    async def save_datasource(submitted: SaveDatasource) -> None:
        fields = {field.key for field in ENGINE_PROFILES[submitted.engine].fields}
        if submitted.credentials.keys() - fields:
            raise HTTPException(422, "连接字段与数据库类型不符。")
        try:
            config.save_source(
                submitted.id, submitted.engine, submitted.description, submitted.enabled, "\n".join(submitted.objects), "",
                {key: value.get_secret_value() for key, value in submitted.credentials.items()},
                set(submitted.client_ids), existing=submitted.existing, object_policy=submitted.object_policy,
                discover_catalog=True, sql_policy=submitted.sql_policy, write_audit_table=submitted.write_audit_table,
            )
        except ConfigurationError as error:
            raise HTTPException(422, str(error)) from None
        except ValidationError:
            raise HTTPException(422, "数据源配置不符合要求，请检查对象清单和客户端。") from None
        except OSError:
            raise HTTPException(503, "无法写入本地配置文件。") from None
        if on_change is not None:
            on_change()

    @app.post("/admin/test-connection", dependencies=[Depends(authorize)])
    async def test_connection(submitted: ConnectionDraft):
        fields = ENGINE_PROFILES[submitted.engine].fields
        if submitted.credentials.keys() - {field.key for field in fields}:
            raise HTTPException(422, "连接字段与数据库类型不符。")
        source = config.sources.get(submitted.id)
        stored = source.connection_env() if source is not None and source.engine == submitted.engine else {}
        values = {}
        for field in fields:
            submitted_value = submitted.credentials.get(field.key)
            value = submitted_value.get_secret_value() if submitted_value else config.value(stored[field.key]) if field.key in stored else None
            if not value:
                raise HTTPException(422, f"请填写 {field.label}。")
            values[field.key] = SecretStr(value)
        if test_lock.locked():
            raise HTTPException(429, "已有连接测试正在运行。")
        try:
            adapter = ADAPTERS[submitted.engine](**values)
            async with test_lock:
                catalog = await run_in_threadpool(adapter.catalog, Deadline(source.timeout_seconds if source else 10))
        except GatewayError as error:
            if error.code == "SQL_EXECUTION_ERROR":
                return JSONResponse({"code": "CATALOG_UNAVAILABLE"}, status_code=503)
            return JSONResponse({"code": error.diagnostic or error.code, "message": error.message}, status_code=error.status)
        return {"object_count": len(catalog)}

    @app.post("/admin/clients", status_code=204, dependencies=[Depends(authorize)])
    async def save_client(submitted: SaveClient) -> None:
        try:
            policies = {}
            if "sql_policy" in submitted.model_fields_set:
                policies["sql_policy"] = submitted.sql_policy
            if submitted.write_policy is not None:
                policies["write_policy"] = submitted.write_policy
            config.save_client(submitted.id, submitted.previous_id, submitted.datasource_ids, policy_updates=policies)
        except ConfigurationError as error:
            raise HTTPException(422, str(error)) from None
        except ValidationError:
            raise HTTPException(422, "客户端配置不符合要求。") from None
        except OSError:
            raise HTTPException(503, "无法写入本地配置文件。") from None
        if on_change is not None:
            on_change()

    @app.delete("/admin/clients/{client_id}", status_code=204, dependencies=[Depends(authorize)])
    async def delete_client(client_id: Identifier) -> None:
        try:
            config.delete_client(client_id)
        except ConfigurationError as error:
            raise HTTPException(422, str(error)) from None
        except OSError:
            raise HTTPException(503, "无法写入本地配置文件。") from None
        if on_change is not None:
            on_change()

    @app.post("/admin/clients/{client_id}/token", dependencies=[Depends(authorize)])
    async def copy_token(client_id: Identifier):
        if client_id not in config.clients or not (token := config.client_token(client_id)):
            raise HTTPException(404, "该客户端的 token 尚未配置。")
        return {"token": token}

    if mutations is not None and runtime is not None:
        @app.get("/admin/mutations", dependencies=[Depends(authorize)])
        async def list_mutations(state: Annotated[MutationState | None, Query()] = None) -> list[MutationRecord]:
            return mutations.store.list(state)

        @app.post("/admin/mutations/{operation_id}/reject", dependencies=[Depends(authorize)])
        async def reject_mutation(operation_id: MutationId) -> MutationRecord:
            return mutations.reject(operation_id)

        @app.post("/admin/mutations/{operation_id}/{action}", dependencies=[Depends(authorize)])
        async def decide_mutation(operation_id: MutationId, action: Literal["approve", "reconcile"]) -> MutationRecord:
            if runtime.current is None:
                raise GatewayError(503, "GATEWAY_NOT_CONFIGURED", "No active datasource configuration is available.")
            if action == "approve":
                return await mutations.approve(runtime.current, operation_id)
            return await mutations.reconcile(runtime.current, operation_id)

    app.mount("/workbench", StaticFiles(directory=web_root, html=True), name="workbench")
    return app


def create_local_app(config: SetupConfig, web_root: Path, token: str, port: int, datasource: str | None = None) -> FastAPI:
    app = create_app(locally_managed=True, mutation_path=config.config_path.with_suffix(".mutations.sqlite3"))

    def reload_configuration() -> None:
        gateway = None
        if config.settings is not None and any(source.enabled for source in config.sources.values()):
            try:
                gateway = Gateway.from_settings(config.settings, datasource, config.environment())
            except ConfigurationError as error:
                logging.getLogger("uvicorn.error").warning("%s", error)
        runtime = app.state.runtime
        previous = runtime.current
        if gateway and previous:
            unchanged_clients = {
                client for client, digest in gateway.token_digests.items() if previous.token_digests.get(client) == digest
            }
            gateway.history.update(
                (key, (client, status)) for key, (client, status) in previous.history.items()
                if client in unchanged_clients and status.status != "running"
                and status.datasource_id in gateway.settings.clients[client].datasources
            )
        runtime.replace(gateway)

    reload_configuration()
    app.mount("/", create_admin_app(
        config, web_root, token, port, on_change=reload_configuration,
        mutations=app.state.mutations, runtime=app.state.runtime,
    ))
    return app


def serve_local(
    config_path: Path, env_path: Path, web_root: Path, port: int, *, open_browser: bool, datasource: str | None,
) -> None:
    if not (web_root / "index.html").is_file():
        raise ConfigurationError("Build the Web workbench with npm --prefix web run build, or use the server ZIP.")
    settings = SetupConfig(config_path, env_path)
    token = secrets.token_urlsafe(32)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            create_local_app(settings, web_root, token, port, datasource), host="127.0.0.1", port=port, proxy_headers=False,
        ))
        print(f"QueryGate API: http://127.0.0.1:{port}", flush=True)
        print(f"QueryGate workbench: http://127.0.0.1:{port}/workbench/", flush=True)
        print(f"QueryGate admin code: {token}", flush=True)

        async def serve() -> None:
            running = asyncio.create_task(server.serve(sockets=[listener]))
            while not server.started and not running.done():
                await asyncio.sleep(0.025)
            if server.started and open_browser:
                url = f"http://127.0.0.1:{port}/workbench/?setup=1#session={token}"
                if not await asyncio.to_thread(webbrowser.open, url):
                    print("无法打开默认浏览器。请打开工作台并输入终端中的管理密钥。", file=sys.stderr)
            await running

        asyncio.run(serve())
