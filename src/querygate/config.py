import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, StringConstraints, model_validator

from .engines import ENGINE_PROFILES, Dialect, Engine, EngineProfile
from .object_names import normalize_object_name

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")]
ObjectName = Annotated[str, AfterValidator(normalize_object_name)]
WriteTable = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}\.[A-Za-z][A-Za-z0-9_]{0,62}$")]
WriteOperation = Literal["update", "insert"]
ObjectPolicy = Literal["allowlist", "denylist"]
ObjectKind = Literal["table", "view", "unknown"]
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]
BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{32,256}")


class ConfigurationError(RuntimeError):
    pass


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class CatalogColumn(ConfigModel):
    name: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=128)
    description: str = ""


class CatalogEntry(ConfigModel):
    kind: ObjectKind = "unknown"
    description: str = ""
    columns: list[CatalogColumn] = Field(default_factory=list)


class SqlPolicy(ConfigModel):
    allow_subqueries: bool = True
    allow_ctes: bool = True
    allow_windows: bool = True
    allow_sqlserver_identity: bool = False


class WriteGrant(ConfigModel):
    operations: list[WriteOperation] = Field(min_length=1, max_length=2)
    primary_key: list[Identifier] = Field(min_length=1, max_length=16)
    columns: list[Identifier] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_grant(self) -> "WriteGrant":
        keys = {name.casefold() for name in self.primary_key}
        columns = {name.casefold() for name in self.columns}
        if len(keys) != len(self.primary_key) or len(columns) != len(self.columns) or keys & columns:
            raise ValueError("Write grants require distinct key and writable columns")
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("Write operations must be unique")
        return self


class WritePolicy(ConfigModel):
    requires_approval: bool = True
    datasources: dict[Identifier, dict[WriteTable, WriteGrant]] = Field(default_factory=dict, max_length=16)


class Datasource(ConfigModel):
    engine: Engine
    enabled: bool = True
    description: str = ""
    object_policy: ObjectPolicy = "allowlist"
    sql_policy: SqlPolicy = Field(default_factory=SqlPolicy)
    write_audit_table: WriteTable | None = None
    allowed_objects: list[ObjectName] = Field(default_factory=list)
    blocked_objects: list[ObjectName] = Field(default_factory=list)
    discover_catalog: bool = False
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
    def profile(self) -> EngineProfile:
        return ENGINE_PROFILES[self.engine]

    @property
    def dialect(self) -> Dialect:
        return self.profile.dialect

    @property
    def uses_live_catalog(self) -> bool:
        return self.discover_catalog or self.object_policy == "denylist" or not self.allowed_objects

    def connection_env(self) -> dict[str, str]:
        return {field.key: getattr(self, field.settings_key) for field in self.profile.fields}

    @model_validator(mode="after")
    def validate_source(self) -> "Datasource":
        expected = {field.settings_key for field in self.profile.fields}
        provided = {
            field.settings_key
            for profile in ENGINE_PROFILES.values() for field in profile.fields
            if getattr(self, field.settings_key) is not None
        }
        if expected != provided:
            raise ValueError(f"{self.profile.label} requires {', '.join(sorted(expected))}")
        if self.engine != "sqlserver" and self.sql_policy.allow_sqlserver_identity:
            raise ValueError("allow_sqlserver_identity is supported only for SQL Server")
        if (self.object_policy == "allowlist" and self.blocked_objects) or (self.object_policy == "denylist" and self.allowed_objects):
            raise ValueError("Use allowed_objects for allowlists and blocked_objects for denylists")
        if len(set(self.allowed_objects)) != len(self.allowed_objects) or len(set(self.blocked_objects)) != len(self.blocked_objects):
            raise ValueError("Object rules must be unique")
        if self.write_audit_table and any(
            name.casefold() == self.write_audit_table.casefold() for name in self.allowed_objects
        ):
            raise ValueError("The write audit table cannot be in the query allowlist")
        if not self.uses_live_catalog and set(self.catalog) - set(self.allowed_objects):
            raise ValueError("catalog entries must belong to allowed_objects")
        if not self.uses_live_catalog and self.profile.requires_column_catalog and any(
            name not in self.catalog or not self.catalog[name].columns for name in self.allowed_objects
        ):
            raise ValueError("Each Oracle object requires its reviewed column list")
        for entry in self.catalog.values():
            if len({column.name for column in entry.columns}) != len(entry.columns):
                raise ValueError("Catalog column names must be unique per object")
        return self


class Client(ConfigModel):
    token_env: EnvName
    datasources: list[Identifier]
    sql_policy: SqlPolicy | None = None
    write_policy: WritePolicy = Field(default_factory=WritePolicy)


class Settings(ConfigModel):
    datasources: dict[Identifier, Datasource] = Field(max_length=16)
    clients: dict[Identifier, Client] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_clients(self) -> "Settings":
        if any(set(client.datasources) - self.datasources.keys() for client in self.clients.values()):
            raise ValueError("clients must reference configured datasources")
        if any(client.write_policy.datasources.keys() - set(client.datasources) for client in self.clients.values()):
            raise ValueError("Write grants must reference datasources assigned to the client")
        return self


def load_settings(path: Path | None = None) -> Settings:
    path = path if path is not None else Path(os.environ.get("QUERYGATE_CONFIG", "querygate.yaml"))
    return Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8-sig")))


def active_settings(settings: Settings, datasource: str | None = None) -> Settings:
    if datasource is not None and (datasource not in settings.datasources or not settings.datasources[datasource].enabled):
        raise ConfigurationError(f"Datasource {datasource!r} is unknown or disabled.")
    sources = {
        name: source for name, source in settings.datasources.items()
        if source.enabled and (datasource is None or name == datasource)
    }
    if not sources:
        raise ConfigurationError("Enable at least one datasource in the Web administration page.")

    clients = {}
    for name, client in settings.clients.items():
        allowed = [source for source in client.datasources if source in sources]
        if allowed:
            policy = client.write_policy.model_copy(update={
                "datasources": {key: grants for key, grants in client.write_policy.datasources.items() if key in allowed},
            })
            clients[name] = client.model_copy(update={"datasources": allowed, "write_policy": policy})
    return Settings(datasources=sources, clients=clients)


def check_required_environment(settings: Settings, environment: Mapping[str, str]) -> None:
    groups = {f"client {name}": (client.token_env,) for name, client in settings.clients.items()}
    groups.update({
        f"datasource {name} ({source.engine})": tuple(source.connection_env().values())
        for name, source in settings.datasources.items()
    })
    missing = []
    for label, variables in groups.items():
        unset = [variable for variable in variables if not environment.get(variable)]
        if unset:
            missing.append(f"{label}: {', '.join(unset)}")
    if missing:
        raise ConfigurationError("Missing configuration values:\n  " + "\n  ".join(missing))


def read_secret(name: str, environment: Mapping[str, str]) -> SecretStr:
    value = environment.get(name)
    if not value:
        raise ConfigurationError(f"Required environment variable {name} is missing")
    return SecretStr(value)


def load_tokens(settings: Settings, environment: Mapping[str, str]) -> dict[str, bytes]:
    tokens = {}
    for client_id, client in settings.clients.items():
        value = read_secret(client.token_env, environment).get_secret_value()
        if not BEARER_TOKEN.fullmatch(value):
            raise ConfigurationError(f"Invalid bearer token in {client.token_env}")
        token = value.encode("ascii")
        if token in tokens.values():
            raise ConfigurationError("Each client must have a distinct bearer token")
        tokens[client_id] = token
    return tokens
