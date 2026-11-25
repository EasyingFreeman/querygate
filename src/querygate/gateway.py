import asyncio
import hashlib
import hmac
import logging
import os
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import BoundedSemaphore
from time import monotonic
from typing import TypeVar
from uuid import uuid4

from .adapters import DatabaseAdapter, build_adapters
from .api_models import (
    DatasourceInfo, QueryDetail, QueryHistoryResponse, QueryRequest, QueryState, QueryValidation,
    SchemaObject, SchemaRequest, SchemaResponse,
)
from .config import CatalogEntry, Datasource, Settings, active_settings, check_required_environment, load_tokens
from .errors import GatewayError, query_timeout
from .execution import Deadline, EncodedResult, encode_result, json_bytes
from .policy import SQL_BYTES, ValidatedQuery, validate_query

audit_log = logging.getLogger("uvicorn.error.querygate.audit")
HISTORY_SIZE = 1024
CATALOG_TTL_SECONDS = 30
Result = TypeVar("Result")


class Gateway:
    @classmethod
    def from_settings(
        cls, settings: Settings, datasource: str | None = None, environment: Mapping[str, str] | None = None,
    ) -> "Gateway":
        values = os.environ if environment is None else environment
        active = active_settings(settings, datasource)
        check_required_environment(active, values)
        gateway = cls(active, load_tokens(active, values), build_adapters(active.datasources, values))
        for name, source in active.datasources.items():
            target = [source.engine, source.write_audit_table, *[values[env] for env in source.connection_env().values()]]
            gateway.source_fingerprints[name] = hashlib.sha256(json_bytes(target)).hexdigest()
        return gateway

    def __init__(self, settings: Settings, tokens: dict[str, bytes], adapters: dict[str, DatabaseAdapter]):
        self.settings = settings
        self.token_digests = {name: hashlib.sha256(token).digest() for name, token in tokens.items()}
        self.adapters = adapters
        self.source_fingerprints: dict[str, str] = {}
        self.slots = {
            name: BoundedSemaphore(source.max_concurrency) for name, source in settings.datasources.items()
        }
        self.executor = ThreadPoolExecutor(
            max_workers=sum(source.max_concurrency for source in settings.datasources.values()),
            thread_name_prefix="querygate-db",
        )
        self.history: OrderedDict[str, tuple[str, QueryDetail]] = OrderedDict()
        self.catalog_cache: dict[str, tuple[float, Datasource]] = {}

    def close(self) -> None:
        self.executor.shutdown(wait=False)

    def authenticate(self, token: str | None) -> str:
        if token is not None and len(token) <= 256:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            client = None
            for client_id, expected in self.token_digests.items():
                if hmac.compare_digest(digest, expected):
                    client = client_id
            if client is not None:
                return client
        raise GatewayError(401, "UNAUTHENTICATED", "A valid bearer token is required.")

    def datasource(self, client: str, name: str) -> Datasource:
        if name not in self.settings.clients[client].datasources:
            raise GatewayError(404, "DATASOURCE_NOT_FOUND", "The datasource is unavailable to this client.")
        return self.settings.datasources[name]

    def list_datasources(self, client: str) -> list[DatasourceInfo]:
        access = self.settings.clients[client]
        result = []
        for name, source in self.settings.datasources.items():
            if name not in access.datasources:
                continue
            grants = access.write_policy.datasources.get(name)
            result.append(DatasourceInfo(
                id=name, engine=source.engine, dialect=source.dialect, description=source.description,
                write_grants=grants if source.write_audit_table else None,
                writes_require_approval=access.write_policy.requires_approval if grants and source.write_audit_table else None,
            ))
        return result

    async def catalog_source(self, name: str, source: Datasource, deadline: Deadline) -> Datasource:
        if not source.uses_live_catalog:
            return source
        cached = self.catalog_cache.get(name)
        if cached and cached[0] > monotonic():
            return cached[1]

        try:
            catalog = await self.run_operation(name, deadline, lambda: self.adapters[name].catalog(deadline))
        except GatewayError as error:
            if error.code == "SQL_EXECUTION_ERROR":
                raise GatewayError(503, "CATALOG_UNAVAILABLE", "The database catalog could not be read.") from None
            raise

        allowed = set(source.allowed_objects)
        blocked = {entry.casefold() for entry in source.blocked_objects}
        if source.write_audit_table:
            blocked.add(source.write_audit_table.casefold())
        visible = {
            obj: entry for obj, entry in catalog.items()
            if (not allowed or obj in allowed) and obj.casefold() not in blocked
        }
        for obj in visible.keys() & source.catalog.keys():
            physical, annotation = visible[obj], source.catalog[obj]
            descriptions = {column.name: column.description for column in annotation.columns}
            visible[obj] = CatalogEntry(kind=physical.kind, description=annotation.description, columns=[
                column.model_copy(update={"description": descriptions.get(column.name, "")})
                for column in physical.columns
            ])
        resolved = source.model_copy(update={"allowed_objects": list(visible), "catalog": visible})
        self.catalog_cache[name] = (monotonic() + CATALOG_TTL_SECONDS, resolved)
        return resolved

    async def schema(self, client: str, request: SchemaRequest) -> SchemaResponse:
        name = request.datasource_id
        source = self.datasource(client, name)
        source = await self.catalog_source(name, source, Deadline(source.timeout_seconds))
        search = request.search.casefold() if request.search else None
        matches = []
        for obj in source.allowed_objects:
            entry = source.catalog.get(obj, CatalogEntry())
            if ((request.object_name is None or obj == request.object_name)
                and (request.kind is None or entry.kind == request.kind)
                and (search is None or search in obj.casefold() or search in entry.description.casefold())):
                matches.append((obj, entry))
        end = request.offset + request.limit if request.limit is not None else len(matches)
        include_metadata = (
            request.include_metadata or request.limit is not None or request.offset or request.kind is not None
            or request.search is not None or request.object_name is not None or not request.include_columns
        )
        objects = [
            SchemaObject(
                name=obj, description=entry.description,
                columns=[column.model_copy(update={"description": column.description}) for column in entry.columns]
                if request.include_columns else [],
                **({"kind": entry.kind, "column_count": len(entry.columns)} if include_metadata else {}),
            )
            for obj, entry in matches[request.offset:end]
        ]
        if not include_metadata:
            return SchemaResponse(datasource_id=name, dialect=source.dialect, objects=objects)
        return SchemaResponse(
            datasource_id=name, dialect=source.dialect, objects=objects,
            total_objects=len(matches), next_offset=end if end < len(matches) else None,
        )

    async def check_query(self, client: str, request: QueryRequest) -> QueryValidation:
        source = self.datasource(client, request.datasource_id)
        source = await self.catalog_source(request.datasource_id, source, Deadline(source.timeout_seconds))
        self.validate_client_query(client, request, source)
        return QueryValidation(
            datasource_id=request.datasource_id, dialect=source.dialect,
            max_rows=min(request.max_rows, source.max_rows),
            max_response_bytes=source.max_response_bytes, timeout_seconds=source.timeout_seconds,
        )

    def query_status(self, client: str, query_id: str) -> QueryDetail:
        record = self.history.get(query_id)
        if record is None or record[0] != client:
            raise GatewayError(404, "QUERY_NOT_FOUND", "The query status is unavailable to this client.")
        return record[1]

    def list_queries(
        self, client: str, *, datasource_id: str | None, status: QueryState | None,
        limit: int, cursor: str | None,
    ) -> QueryHistoryResponse:
        if datasource_id is not None:
            self.datasource(client, datasource_id)
        before = None
        if cursor is not None:
            anchor = self.query_status(client, cursor)
            before = (anchor.started_at, anchor.query_id)

        matching = sorted(
            (
                record for owner, record in self.history.values()
                if owner == client
                and (datasource_id is None or record.datasource_id == datasource_id)
                and (status is None or record.status == status)
                and (before is None or (record.started_at, record.query_id) < before)
            ),
            key=lambda record: (record.started_at, record.query_id), reverse=True,
        )
        page = matching[:limit]
        return QueryHistoryResponse(
            queries=page,
            next_cursor=page[-1].query_id if len(matching) > limit else None,
        )

    def remember(self, client: str, status: QueryDetail) -> None:
        self.history[status.query_id] = (client, status)
        self.history.move_to_end(status.query_id)
        if len(self.history) > HISTORY_SIZE:
            self.history.popitem(last=False)

    def validate_client_query(self, client: str, request: QueryRequest, source: Datasource) -> ValidatedQuery:
        if (policy := self.settings.clients[client].sql_policy) is not None:
            source = source.model_copy(update={"sql_policy": policy})
        return validate_query(request, source)

    async def query(self, client: str, request: QueryRequest) -> bytes:
        name = request.datasource_id
        source = self.datasource(client, name)
        deadline = Deadline(source.timeout_seconds)
        started_at = datetime.now(UTC)
        query_id = f"q_{uuid4().hex}"
        try:
            submitted_sql = request.sql if len(request.sql.encode("utf-8")) <= SQL_BYTES else None
        except UnicodeEncodeError:
            submitted_sql = None
        sql_preview = " ".join(submitted_sql.split())[:180] if submitted_sql else None
        record = QueryDetail(
            query_id=query_id, datasource_id=name, client_id=client, dialect=source.dialect,
            status="running", started_at=started_at,
            sql=submitted_sql, sql_preview=sql_preview, reason=request.reason,
        )
        self.remember(client, record)
        error_code = "INTERNAL_ERROR"
        error_message = None
        diagnostic = None
        outcome: EncodedResult | None = None
        try:
            source = await self.catalog_source(name, source, deadline)
            query = self.validate_client_query(client, request, source)
            max_rows = min(request.max_rows, source.max_rows)
            outcome = await self.run_operation(name, deadline, lambda: self.execute(name, query, query_id, deadline, max_rows))
            error_code = None
            return outcome.body
        except GatewayError as error:
            error_code = error.code
            error_message = error.message
            diagnostic = error.diagnostic
            error.query_id = query_id
            raise
        except asyncio.CancelledError:
            error_code = "QUERY_CANCELLED"
            raise
        except Exception as error:
            audit_log.error("Unexpected query failure %s (%s)", query_id, type(error).__name__)
            failure = GatewayError(500, "INTERNAL_ERROR", "The gateway could not complete the query.")
            failure.query_id = query_id
            raise failure from None
        finally:
            status = record.model_copy(update={
                "status": "error" if error_code else "success",
                "error_code": error_code, "error_message": error_message, "diagnostic": diagnostic,
                "execution_ms": int((monotonic() - deadline.started) * 1000),
                "row_count": outcome.row_count if outcome is not None else None,
                "truncated": outcome.truncated if outcome is not None else None,
            })
            self.remember(client, status)
            audit = status.model_dump(mode="json", exclude={"sql", "sql_preview", "error_message", "reason"})
            audit_log.info("%s", json_bytes({"event": "query", **audit}).decode("ascii"))

    async def run_operation(self, name: str, deadline: Deadline, operation: Callable[[], Result]) -> Result:
        deadline.remaining()
        slots = self.slots[name]
        if not slots.acquire(blocking=False):
            raise GatewayError(429, "DATASOURCE_BUSY", "The datasource is at its query concurrency limit.")

        def hold_slot() -> Result:
            try:
                return operation()
            finally:
                slots.release()

        try:
            worker = self.executor.submit(hold_slot)
        except BaseException:
            slots.release()
            raise
        future = asyncio.wrap_future(worker)
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=deadline.remaining())
        except (GatewayError, TimeoutError) as error:
            if isinstance(error, TimeoutError) or error.code == "QUERY_TIMEOUT":
                deadline.abort()
                raise query_timeout() from None
            raise
        except asyncio.CancelledError:
            deadline.abort()
            raise

    def execute(self, name: str, query: ValidatedQuery, query_id: str, deadline: Deadline, max_rows: int) -> EncodedResult:
        with self.adapters[name].query(query, deadline) as cursor:
            response = encode_result(
                cursor, query_id, deadline, max_rows, self.settings.datasources[name].max_response_bytes,
            )
        deadline.remaining()
        return response
