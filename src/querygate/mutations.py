import asyncio
import hmac
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .api_models import MutationApproval, MutationRecord, MutationRequest, MutationState
from .config import Datasource, WriteGrant
from .errors import GatewayError
from .execution import Deadline
from .gateway import Gateway
from .write_adapter import apply_mutation, reconcile_mutation


def mutation_not_found() -> GatewayError:
    return GatewayError(404, "WRITE_NOT_FOUND", "The mutation is unavailable.")


@dataclass(frozen=True)
class StoredMutation:
    record: MutationRecord
    owner: bytes
    target: str


class MutationStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(self.path, timeout=2)) as db, db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS mutations (
                        id TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        owner BLOB NOT NULL,
                        target TEXT NOT NULL,
                        status TEXT NOT NULL,
                        body TEXT NOT NULL,
                        UNIQUE (client_id, owner, request_id)
                    )
                """)
                yield db
        except sqlite3.Error:
            raise GatewayError(503, "WRITE_QUEUE_UNAVAILABLE", "The local mutation queue is unavailable.") from None

    def submit(self, client_id: str, owner: bytes, target: str, request: MutationRequest) -> tuple[StoredMutation, bool]:
        record = MutationRecord(
            operation_id=f"m_{uuid4().hex}", client_id=client_id, submitted=request,
            status="pending", submitted_at=datetime.now(UTC),
        )
        with self.connection() as db:
            inserted = db.execute(
                """INSERT INTO mutations (id, client_id, request_id, owner, target, status, body)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)
                   ON CONFLICT(client_id, owner, request_id) DO NOTHING""",
                (record.operation_id, client_id, request.request_id.lower(), owner, target, record.model_dump_json()),
            )
            if inserted.rowcount == 1:
                return StoredMutation(record, owner, target), True
            current = db.execute(
                "SELECT body, owner, target FROM mutations WHERE client_id = ? AND owner = ? AND request_id = ?",
                (client_id, owner, request.request_id.lower()),
            ).fetchone()
            assert current is not None
            existing = self.decode(current)
            if existing.record.submitted.model_copy(update={"request_id": request.request_id}) != request:
                raise GatewayError(409, "WRITE_REQUEST_ID_REUSED", "This request ID already identifies a different mutation.")
            return existing, False

    @staticmethod
    def decode(row: tuple) -> StoredMutation:
        body, owner, target = row
        return StoredMutation(MutationRecord.model_validate_json(body), owner, target)

    def get(self, operation_id: str) -> StoredMutation:
        with self.connection() as db:
            row = db.execute("SELECT body, owner, target FROM mutations WHERE id = ?", (operation_id,)).fetchone()
        if row is None:
            raise mutation_not_found()
        return self.decode(row)

    def list(self, status: MutationState | None) -> list[MutationRecord]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT body FROM mutations WHERE (? IS NULL OR status = ?) ORDER BY rowid DESC LIMIT 50",
                (status, status),
            ).fetchall()
        return [MutationRecord.model_validate_json(body) for (body,) in rows]

    def transition(self, record: MutationRecord, *, expected: MutationState) -> bool:
        with self.connection() as db:
            changed = db.execute(
                "UPDATE mutations SET status = ?, body = ? WHERE id = ? AND status = ?",
                (record.status, record.model_dump_json(), record.operation_id, expected),
            )
            return changed.rowcount == 1


def grant_for(gateway: Gateway, client: str, request: MutationRequest) -> tuple[Datasource, WriteGrant]:
    source = gateway.datasource(client, request.datasource_id)
    allowed = gateway.settings.clients[client].write_policy.datasources.get(request.datasource_id, {})
    grant = allowed.get(request.table)
    if not grant or request.operation not in grant.operations:
        raise GatewayError(403, "WRITE_NOT_ALLOWED", "The client's policy does not allow this operation on the table.")
    if not source.write_audit_table:
        raise GatewayError(409, "WRITE_AUDIT_REQUIRED", "This datasource requires a database audit table before writes can run.")
    blocked = {name.casefold() for name in source.blocked_objects} | {source.write_audit_table.casefold()}
    if request.table.casefold() in blocked or (source.allowed_objects and request.table not in source.allowed_objects):
        raise GatewayError(403, "WRITE_OBJECT_BLOCKED", "The table is outside the datasource's object policy.")
    if request.key.keys() != set(grant.primary_key):
        raise GatewayError(422, "WRITE_KEY_REQUIRED", "Supply exactly the configured primary key columns.")
    if request.values.keys() - set(grant.columns):
        raise GatewayError(403, "WRITE_COLUMNS_NOT_ALLOWED", "The request contains a column outside the client's write grant.")
    return source, grant


def target_fingerprint(gateway: Gateway, source_id: str) -> str:
    if source_id in gateway.source_fingerprints:
        return gateway.source_fingerprints[source_id]
    source = gateway.settings.datasources[source_id]
    return f"{source.engine}/{source.write_audit_table}/{source.connection_env()}"


class MutationService:
    def __init__(self, store: MutationStore):
        self.store = store
        self.running: set[asyncio.Task[MutationRecord]] = set()

    async def submit(self, gateway: Gateway, client: str, request: MutationRequest) -> MutationRecord:
        grant_for(gateway, client, request)
        stored, created = self.store.submit(
            client, gateway.token_digests[client], target_fingerprint(gateway, request.datasource_id), request,
        )
        if created and not gateway.settings.clients[client].write_policy.requires_approval:
            return await self.execute(gateway, stored, "client_exempt")
        return stored.record

    def get(self, gateway: Gateway, client: str, operation_id: str) -> MutationRecord:
        entry = self.store.get(operation_id)
        if entry.record.client_id != client or not hmac.compare_digest(entry.owner, gateway.token_digests[client]):
            raise mutation_not_found()
        gateway.datasource(client, entry.record.submitted.datasource_id)
        return entry.record

    def reject(self, operation_id: str) -> MutationRecord:
        record = self.store.get(operation_id).record.model_copy(update={
            "status": "rejected", "decided_at": datetime.now(UTC),
        })
        if not self.store.transition(record, expected="pending"):
            raise GatewayError(409, "WRITE_ALREADY_DECIDED", "The mutation has already been decided.")
        return record

    async def approve(self, gateway: Gateway, operation_id: str) -> MutationRecord:
        return await self.execute(gateway, self.store.get(operation_id), "web_admin")

    async def execute(
        self, gateway: Gateway, stored: StoredMutation, approval: MutationApproval,
    ) -> MutationRecord:
        record = stored.record
        client = record.client_id
        if record.status != "pending":
            raise GatewayError(409, "WRITE_ALREADY_DECIDED", "The mutation has already been decided.")
        digest = gateway.token_digests.get(client)
        if digest is None or not hmac.compare_digest(stored.owner, digest):
            raise GatewayError(409, "WRITE_CLIENT_CHANGED", "The submitting client is no longer authorized.")
        source, grant = grant_for(gateway, client, record.submitted)
        name = record.submitted.datasource_id
        if target_fingerprint(gateway, name) != stored.target:
            raise GatewayError(409, "WRITE_TARGET_CHANGED", "The datasource connection or audit table changed after submission.")
        executing = record.model_copy(update={
            "status": "executing", "approval": approval, "decided_at": datetime.now(UTC),
        })
        if not self.store.transition(executing, expected="pending"):
            raise GatewayError(409, "WRITE_ALREADY_DECIDED", "The mutation has already been decided.")

        async def run() -> MutationRecord:
            deadline = Deadline(source.timeout_seconds)
            try:
                audit = await gateway.run_operation(
                    name, deadline, lambda: apply_mutation(gateway.adapters[name], source, grant, executing, deadline),
                )
                updated = executing.model_copy(update={"status": "succeeded", "audit": audit})
            except GatewayError as error:
                unresolved = error.code in {"QUERY_TIMEOUT", "WRITE_COMMIT_UNKNOWN"}
                updated = executing.model_copy(update={
                    "status": "unknown" if unresolved else "failed", "error_code": error.code,
                })
            except Exception:
                updated = executing.model_copy(update={"status": "unknown", "error_code": "WRITE_STATUS_UNKNOWN"})
            if self.store.transition(updated, expected="executing"):
                return updated
            return self.store.get(executing.operation_id).record

        task = asyncio.create_task(run())
        self.running.add(task)

        def finish(done: asyncio.Task[MutationRecord]) -> None:
            self.running.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finish)
        return await asyncio.shield(task)

    async def reconcile(self, gateway: Gateway, operation_id: str) -> MutationRecord:
        stored = self.store.get(operation_id)
        record = stored.record
        if record.status not in {"unknown", "executing"}:
            return record
        name = record.submitted.datasource_id
        source = gateway.settings.datasources.get(name)
        if not source or not source.write_audit_table or target_fingerprint(gateway, name) != stored.target:
            raise GatewayError(409, "WRITE_TARGET_CHANGED", "The original datasource audit table is unavailable.")
        deadline = Deadline(source.timeout_seconds)
        audit = await gateway.run_operation(
            name, deadline, lambda: reconcile_mutation(gateway.adapters[name], source, record, deadline),
        )
        if audit is None:
            return record
        updated = record.model_copy(update={"status": "succeeded", "audit": audit, "error_code": None})
        if self.store.transition(updated, expected=record.status):
            return updated
        return self.store.get(operation_id).record
