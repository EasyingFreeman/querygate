from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .config import CatalogEntry, Identifier, ObjectKind, ObjectName, WriteGrant, WriteOperation, WriteTable
from .engines import Engine

BindName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")]
BindValue = str | int | float | bool | None
CallReason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
QueryId = Annotated[str, StringConstraints(pattern=r"^q_[a-f0-9]{32}$")]
QueryState = Literal["running", "success", "error"]
MutationId = Annotated[str, StringConstraints(pattern=r"^m_[a-f0-9]{32}$")]
MutationRequestId = Annotated[str, StringConstraints(pattern=r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$")]
MutationState = Literal["pending", "executing", "succeeded", "rejected", "failed", "unknown"]
MutationApproval = Literal["web_admin", "client_exempt"]


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    datasource_id: Identifier
    sql: str = Field(min_length=1, description="Native SQL, limited to 32 KiB in UTF-8.")
    reason: CallReason
    params: list[BindValue] | dict[BindName, BindValue] | None = None
    max_rows: int = Field(default=100, ge=1)


class DatasourceInfo(BaseModel):
    id: str
    engine: Engine
    dialect: str
    description: str
    write_grants: dict[WriteTable, WriteGrant] | None = None
    writes_require_approval: bool | None = None


class SchemaObject(CatalogEntry):
    name: str
    column_count: int | None = None


class SchemaRequest(BaseModel):
    datasource_id: Identifier
    limit: int | None = Field(default=None, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    search: str | None = Field(default=None, min_length=1, max_length=260)
    kind: ObjectKind | None = None
    object_name: ObjectName | None = None
    include_columns: bool = True
    include_metadata: bool = False


class SchemaResponse(BaseModel):
    datasource_id: str
    dialect: str
    objects: list[SchemaObject]
    total_objects: int | None = None
    next_offset: int | None = None


class ResultColumn(BaseModel):
    name: str
    type: str


class QueryResponse(BaseModel):
    query_id: str
    status: Literal["success"]
    columns: list[ResultColumn]
    rows: list[list[BindValue]]
    row_count: int
    truncated: bool
    execution_ms: int


class ErrorResponse(BaseModel):
    code: str
    message: str
    query_id: str | None = None
    diagnostic: str | None = None


class QueryValidation(BaseModel):
    valid: Literal[True] = True
    datasource_id: str
    dialect: str
    max_rows: int
    max_response_bytes: int
    timeout_seconds: int


class QueryStatus(BaseModel):
    query_id: str
    datasource_id: str
    client_id: str | None = None
    dialect: str | None = None
    status: QueryState
    started_at: datetime
    execution_ms: int | None = None
    error_code: str | None = None
    row_count: int | None = None
    truncated: bool | None = None
    sql_preview: str | None = None
    diagnostic: str | None = None


class QueryDetail(QueryStatus):
    sql: str | None = None
    reason: str | None = None
    error_message: str | None = None


class QueryHistoryResponse(BaseModel):
    queries: list[QueryStatus]
    next_cursor: QueryId | None = None


class MutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    request_id: MutationRequestId
    datasource_id: Identifier
    operation: WriteOperation
    table: WriteTable
    reason: CallReason
    key: dict[Identifier, str | int] = Field(min_length=1, max_length=16)
    values: dict[Identifier, BindValue] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_key(self) -> "MutationRequest":
        if any(type(value) not in (str, int) or value == "" for value in self.key.values()):
            raise ValueError("Primary key values must be nonempty strings or integers")
        return self


class MutationAudit(BaseModel):
    before: dict[str, BindValue] | None
    after: dict[str, BindValue]
    changed_at: datetime


class MutationRecord(BaseModel):
    operation_id: MutationId
    client_id: Identifier
    submitted: MutationRequest
    status: MutationState
    submitted_at: datetime
    approval: MutationApproval | None = None
    decided_at: datetime | None = None
    audit: MutationAudit | None = None
    error_code: str | None = None
