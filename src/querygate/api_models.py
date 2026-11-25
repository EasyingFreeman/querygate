from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .config import CatalogEntry, Engine, Identifier

BindName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")]
BindValue = str | int | float | bool | None


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    datasource_id: Identifier
    sql: str = Field(min_length=1, description="Native SQL, limited to 32 KiB in UTF-8.")
    params: list[BindValue] | dict[BindName, BindValue] | None = None
    max_rows: int = Field(default=100, ge=1)


class DatasourceInfo(BaseModel):
    id: str
    engine: Engine
    dialect: str
    description: str


class SchemaObject(CatalogEntry):
    name: str


class SchemaResponse(BaseModel):
    datasource_id: str
    dialect: str
    objects: list[SchemaObject]


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


class QueryStatus(BaseModel):
    query_id: str
    datasource_id: str
    status: Literal["running", "success", "error"]
    execution_ms: int | None = None
    error_code: str | None = None
