import asyncio
import hashlib
import hmac
import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from time import monotonic
from uuid import uuid4

from .adapters import DatabaseAdapter
from .api_models import DatasourceInfo, QueryRequest, QueryStatus, SchemaObject, SchemaResponse
from .config import CatalogEntry, Datasource, Settings
from .errors import GatewayError, query_timeout
from .execution import Deadline, encode_result, json_bytes
from .policy import ValidatedQuery, validate_query

audit_log = logging.getLogger("uvicorn.error.querygate.audit")
HISTORY_SIZE = 1024


class Gateway:
    def __init__(self, settings: Settings, tokens: dict[str, bytes], adapters: dict[str, DatabaseAdapter]):
        self.settings = settings
        self.token_digests = {name: hashlib.sha256(token).digest() for name, token in tokens.items()}
        self.adapters = adapters
        self.slots = {
            name: BoundedSemaphore(source.max_concurrency) for name, source in settings.datasources.items()
        }
        self.executor = ThreadPoolExecutor(
            max_workers=sum(source.max_concurrency for source in settings.datasources.values()),
            thread_name_prefix="querygate-db",
        )
        self.history: OrderedDict[str, tuple[str, QueryStatus]] = OrderedDict()

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
        return [
            DatasourceInfo(id=name, engine=source.engine, dialect=source.dialect, description=source.description)
            for name, source in self.settings.datasources.items()
            if name in self.settings.clients[client].datasources
        ]

    def schema(self, client: str, name: str) -> SchemaResponse:
        source = self.datasource(client, name)
        objects = [
            SchemaObject(name=obj, **source.catalog.get(obj, CatalogEntry()).model_dump())
            for obj in source.allowed_objects
        ]
        return SchemaResponse(datasource_id=name, dialect=source.dialect, objects=objects)

    def query_status(self, client: str, query_id: str) -> QueryStatus:
        record = self.history.get(query_id)
        if record is None or record[0] != client:
            raise GatewayError(404, "QUERY_NOT_FOUND", "The query status is unavailable to this client.")
        return record[1]

    def remember(self, client: str, status: QueryStatus) -> None:
        self.history[status.query_id] = (client, status)
        self.history.move_to_end(status.query_id)
        if len(self.history) > HISTORY_SIZE:
            self.history.popitem(last=False)

    async def query(self, client: str, request: QueryRequest) -> bytes:
        name = request.datasource_id
        source = self.datasource(client, name)
        deadline = Deadline(source.timeout_seconds)
        query_id = f"q_{uuid4().hex}"
        self.remember(client, QueryStatus(query_id=query_id, datasource_id=name, status="running"))
        error_code = "INTERNAL_ERROR"
        try:
            query = validate_query(request, source)
            deadline.remaining()
            response = await self.run_query(name, query, query_id, deadline, min(request.max_rows, source.max_rows))
            error_code = None
            return response
        except GatewayError as error:
            error_code = error.code
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
            status = QueryStatus(
                query_id=query_id, datasource_id=name, status="error" if error_code else "success",
                error_code=error_code, execution_ms=int((monotonic() - deadline.started) * 1000),
            )
            self.remember(client, status)
            audit_log.info("%s", json_bytes({"event": "query", "client_id": client, **status.model_dump()}).decode("ascii"))

    async def run_query(self, name: str, query: ValidatedQuery, query_id: str, deadline: Deadline, max_rows: int) -> bytes:
        slots = self.slots[name]
        if not slots.acquire(blocking=False):
            raise GatewayError(429, "DATASOURCE_BUSY", "The datasource is at its query concurrency limit.")
        try:
            worker = self.executor.submit(self.execute, name, query, query_id, deadline, max_rows)
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

    def execute(self, name: str, query: ValidatedQuery, query_id: str, deadline: Deadline, max_rows: int) -> bytes:
        try:
            with self.adapters[name].query(query, deadline) as cursor:
                response = encode_result(
                    cursor, query_id, deadline, max_rows, self.settings.datasources[name].max_response_bytes,
                )
            deadline.remaining()
            return response
        finally:
            self.slots[name].release()
