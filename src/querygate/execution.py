import base64
import json
import logging
import math
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, time, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any, Protocol
from uuid import UUID

from .api_models import BindValue
from .errors import GatewayError, query_timeout, result_limit

log = logging.getLogger("querygate.execution")


class Cursor(Protocol):
    @property
    def description(self) -> Sequence[Sequence[Any]] | None: ...

    def fetchmany(self, size: int, /) -> Sequence[Sequence[Any]]: ...


class Deadline:
    def __init__(self, seconds: int):
        self.started = monotonic()
        self.expires = self.started + seconds
        self._cancel: Callable[[], None] | None = None
        self._stopped = threading.Event()
        self._lock = threading.Lock()

    def remaining(self) -> float:
        seconds = self.expires - monotonic()
        if seconds <= 0 or self._stopped.is_set():
            raise query_timeout()
        return seconds

    @contextmanager
    def cancellable(self, cancel: Callable[[], None]) -> Iterator[None]:
        with self._lock:
            self.remaining()
            self._cancel = cancel
        try:
            yield
        finally:
            with self._lock:
                self._cancel = None

    def abort(self) -> None:
        self._stopped.set()
        threading.Thread(target=self._cancel_operation, daemon=True).start()

    def _cancel_operation(self) -> None:
        # The owner waits for cancellation before closing the cursor or connection.
        with self._lock:
            if self._cancel is not None:
                try:
                    self._cancel()
                except Exception:
                    log.warning("Database driver did not acknowledge cancellation")


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


def normalize_cell(value: Any, max_bytes: int) -> BindValue:
    if isinstance(value, (str, bytes, bytearray, memoryview)) and len(value) > max_bytes:
        raise result_limit()
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 2**53 - 1 else str(value)
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (UUID, timedelta)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(value).decode("ascii")
    raise GatewayError(422, "UNSUPPORTED_RESULT_TYPE", "Cast result columns to supported scalar types.")


def encode_result(cursor: Cursor, query_id: str, deadline: Deadline, max_rows: int, max_bytes: int) -> bytes:
    description = cursor.description
    if not description:
        raise GatewayError(422, "SQL_EXECUTION_ERROR", "The query did not return a result set.")

    columns = []
    for name, driver_type, *_ in description:
        type_name = getattr(driver_type, "name", getattr(driver_type, "__name__", "unknown"))
        columns.append({"name": name, "type": type_name.removeprefix("DB_TYPE_")})

    metadata = {
        "query_id": query_id, "status": "success", "columns": columns,
        "row_count": max_rows, "truncated": False, "execution_ms": 60_000,
    }
    remaining = max_bytes - len(json_bytes(metadata)) - len(b',"rows":[]')
    if remaining < 0:
        raise result_limit()

    rows = []
    truncated = False
    while True:
        deadline.remaining()
        batch = cursor.fetchmany(1)
        if not batch:
            break
        if len(rows) == max_rows:
            truncated = True
            break
        try:
            row = encode_row(batch[0], remaining - bool(rows))
        except GatewayError as error:
            if error.code != "RESULT_LIMIT_EXCEEDED" or not rows:
                raise
            truncated = True
            break
        rows.append(row)
        remaining -= len(row) + (len(rows) > 1)

    deadline.remaining()
    metadata.update(row_count=len(rows), truncated=truncated, execution_ms=int((monotonic() - deadline.started) * 1000))
    return json_bytes(metadata)[:-1] + b',"rows":[' + b",".join(rows) + b"]}"


def encode_row(row: Sequence[Any], max_bytes: int) -> bytes:
    remaining = max_bytes - 2
    cells = []
    for value in row:
        encoded = json_bytes(normalize_cell(value, remaining))
        remaining -= len(encoded) + bool(cells)
        if remaining < 0:
            raise result_limit()
        cells.append(encoded)
    if remaining < 0:
        raise result_limit()
    return b"[" + b",".join(cells) + b"]"
