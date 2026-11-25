import os
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")]
ObjectName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}\.[A-Za-z][A-Za-z0-9_]{0,62}$")]
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]
Engine = Literal["sqlserver", "oracle"]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class CatalogColumn(ConfigModel):
    name: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=128)
    description: str = ""


class CatalogEntry(ConfigModel):
    description: str = ""
    columns: list[CatalogColumn] = Field(default_factory=list)


class Datasource(ConfigModel):
    engine: Engine
    description: str = ""
    allowed_objects: list[ObjectName] = Field(min_length=1)
    catalog: dict[ObjectName, CatalogEntry] = Field(default_factory=dict)
    connection_string_env: EnvName | None = None
    dsn_env: EnvName | None = None
    user_env: EnvName | None = None
    password_env: EnvName | None = None
    max_rows: int = Field(default=1000, ge=1, le=10_000)
    max_response_bytes: int = Field(default=1_048_576, ge=1024, le=1_048_576)
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    max_concurrency: int = Field(default=2, ge=1, le=16)

    @property
    def dialect(self) -> Literal["tsql", "oracle"]:
        return "tsql" if self.engine == "sqlserver" else "oracle"

    @model_validator(mode="after")
    def validate_source(self) -> "Datasource":
        oracle_env = (self.dsn_env, self.user_env, self.password_env)
        if self.engine == "sqlserver":
            if not self.connection_string_env or any(oracle_env):
                raise ValueError("SQL Server requires connection_string_env only")
        elif self.connection_string_env or not all(oracle_env):
            raise ValueError("Oracle requires dsn_env, user_env, and password_env")
        if len(set(self.allowed_objects)) != len(self.allowed_objects):
            raise ValueError("allowed_objects must be unique")
        if set(self.catalog) - set(self.allowed_objects):
            raise ValueError("catalog entries must belong to allowed_objects")
        if self.engine == "oracle" and any(
            name not in self.catalog or not self.catalog[name].columns for name in self.allowed_objects
        ):
            raise ValueError("Each Oracle object requires its reviewed column list")
        for entry in self.catalog.values():
            if len({column.name for column in entry.columns}) != len(entry.columns):
                raise ValueError("Catalog column names must be unique per object")
        return self


class Client(ConfigModel):
    token_env: EnvName
    datasources: list[Identifier] = Field(min_length=1)


class Settings(ConfigModel):
    datasources: dict[Identifier, Datasource] = Field(min_length=1, max_length=16)
    clients: dict[Identifier, Client] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_clients(self) -> "Settings":
        if any(set(client.datasources) - self.datasources.keys() for client in self.clients.values()):
            raise ValueError("clients must reference configured datasources")
        return self


def load_settings() -> Settings:
    path = Path(os.environ.get("QUERYGATE_CONFIG", "querygate.yaml"))
    return Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8-sig")))


def read_secret(name: str) -> SecretStr:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is missing")
    return SecretStr(value)


def load_tokens(settings: Settings) -> dict[str, bytes]:
    tokens = {}
    for client_id, client in settings.clients.items():
        value = read_secret(client.token_env).get_secret_value()
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{32,256}", value):
            raise RuntimeError(f"Invalid bearer token in {client.token_env}")
        token = value.encode("ascii")
        if token in tokens.values():
            raise RuntimeError("Each client must have a distinct bearer token")
        tokens[client_id] = token
    return tokens
