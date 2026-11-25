import os
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from dotenv import dotenv_values, set_key
from pydantic import TypeAdapter

from .config import CatalogColumn, CatalogEntry, Client, ConfigurationError, Datasource, Identifier, ObjectPolicy, Settings, SqlPolicy, WritePolicy, load_settings
from .engines import ENGINE_PROFILES, Engine
from .object_names import normalize_object_name

DEFAULT_CLIENT = "analyst"
DEFAULT_TOKEN_ENV = "QUERYGATE_ANALYST_TOKEN"
validate_identifier = TypeAdapter(Identifier).validate_python


class SetupConfig:
    def __init__(self, config_path: Path, env_path: Path):
        if config_path.resolve() == env_path.resolve():
            raise ConfigurationError("配置文件和环境文件需要使用不同路径。")
        self.config_path = config_path
        self.env_path = env_path
        self.settings = load_settings(config_path) if config_path.is_file() else None
        self.config_snapshot = config_path.read_bytes() if config_path.is_file() else None
        self.env = self.read_env()

    def read_env(self) -> dict[str, str | None]:
        return dotenv_values(self.env_path, encoding="utf-8-sig", interpolate=False) if self.env_path.is_file() else {}

    @property
    def sources(self) -> dict[str, Datasource]:
        return self.settings.datasources if self.settings is not None else {}

    @property
    def clients(self) -> dict[str, Client]:
        return self.settings.clients if self.settings is not None else {
            DEFAULT_CLIENT: Client(token_env=DEFAULT_TOKEN_ENV, datasources=[]),
        }

    def value(self, variable: str) -> str | None:
        return os.environ.get(variable, self.env.get(variable))

    def client_token(self, client_id: str) -> str | None:
        return self.value(self.clients[client_id].token_env)

    def environment(self) -> dict[str, str]:
        return {**{name: value for name, value in self.env.items() if value is not None}, **os.environ}

    def used_variables(self) -> set[str]:
        used = set(self.env) | set(os.environ) | {client.token_env for client in self.clients.values()}
        for source in self.sources.values():
            used.update(source.connection_env().values())
        return used

    def save_client(
        self, name: str, previous_id: str | None, datasource_ids: list[str], *,
        policy_updates: dict[str, SqlPolicy | WritePolicy | None] | None = None,
    ) -> None:
        validate_identifier(name)
        if previous_id is not None and previous_id not in self.clients:
            raise ConfigurationError("此客户端已不存在。")
        if name != previous_id and name in self.clients:
            raise ConfigurationError("此客户端 ID 已存在。")
        if set(datasource_ids) - self.sources.keys():
            raise ConfigurationError("请选择已配置的数据源。")

        clients = dict(self.clients)
        prior = {}
        if previous_id is None:
            variable = unused_variable(f"QUERYGATE_{name.upper()}_TOKEN", self.used_variables())
        else:
            previous = clients.pop(previous_id)
            variable = previous.token_env
            prior = previous.model_dump()
        clients[name] = Client.model_validate({
            **prior, "token_env": variable, "datasources": list(dict.fromkeys(datasource_ids)), **(policy_updates or {}),
        })
        updates = {} if self.value(variable) else {variable: secrets.token_urlsafe(32)}
        self.persist(Settings(datasources=self.sources, clients=clients), updates)

    def delete_client(self, name: str) -> None:
        if name not in self.clients:
            raise ConfigurationError("此客户端已不存在。")
        clients = {client_id: item for client_id, item in self.clients.items() if client_id != name}
        self.persist(Settings(datasources=self.sources, clients=clients), {})

    def save_source(
        self, name: str, engine: Engine, description: str, enabled: bool,
        views: str, columns: str, credentials: dict[str, str], client_ids: set[str], *, existing: bool,
        object_policy: ObjectPolicy = "allowlist", discover_catalog: bool = False, sql_policy: SqlPolicy | None = None,
        write_audit_table: str | None = None,
    ) -> None:
        validate_identifier(name)
        old_source = self.sources.get(name)
        if (old_source is not None) != existing:
            raise ConfigurationError(f"数据源 ID {name} 已存在或已被修改。")
        if old_source is not None and engine != old_source.engine:
            raise ConfigurationError("请新增数据源来使用不同类型的数据库。")
        if client_ids - self.clients.keys():
            raise ConfigurationError("请选择已配置的客户端。")

        objects = [normalize_object_name(line.strip()) for line in views.splitlines() if line.strip()]
        prior_catalog = old_source.catalog if old_source else {}
        catalog = prior_catalog if discover_catalog else parse_columns(objects, columns, prior_catalog)
        source_data = old_source.model_dump() if old_source else {}
        source_data.update(
            engine=engine, description=description, enabled=enabled, catalog=catalog, discover_catalog=discover_catalog,
            object_policy=object_policy, allowed_objects=objects if object_policy == "allowlist" else [],
            blocked_objects=objects if object_policy == "denylist" else [],
        )
        if sql_policy is not None:
            source_data["sql_policy"] = sql_policy
        if write_audit_table is not None:
            source_data["write_audit_table"] = write_audit_table or None
        used = self.used_variables()
        shared = {client.token_env for client in self.clients.values()}
        for source in self.sources.values():
            if source is not old_source:
                shared.update(source.connection_env().values())
        updates = {}
        for field in ENGINE_PROFILES[engine].fields:
            variable = source_data.get(field.settings_key)
            value = credentials.get(field.key)
            if variable is None or (value and (variable in shared or variable in os.environ)):
                variable = unused_variable(f"QUERYGATE_{name.upper()}_{field.env_suffix}", used)
                source_data[field.settings_key] = variable
            if value:
                updates[variable] = value
            elif enabled and not self.value(variable):
                raise ConfigurationError(f"请输入 {field.label}，或先将此数据源设置为停用。")

        source = Datasource.model_validate(source_data)
        clients = {}
        for client_id, previous in self.clients.items():
            allowed = list(previous.datasources)
            if client_id in client_ids and name not in allowed:
                allowed.append(name)
            if client_id not in client_ids and name in allowed:
                allowed.remove(name)
            variable = previous.token_env
            write_policy = previous.write_policy.model_copy(update={
                "datasources": {key: grants for key, grants in previous.write_policy.datasources.items() if key in allowed},
            })
            clients[client_id] = previous.model_copy(update={"datasources": allowed, "write_policy": write_policy})
            if client_id in client_ids and not self.value(variable):
                updates[variable] = secrets.token_urlsafe(32)

        self.persist(Settings(datasources={**self.sources, name: source}, clients=clients), updates)

    def persist(self, settings: Settings, updates: dict[str, str]) -> None:
        self.write(settings, updates)
        self.settings = settings
        self.config_snapshot = self.config_path.read_bytes()
        self.env = self.read_env()

    def write(self, settings: Settings, updates: dict[str, str]) -> None:
        current = self.config_path.read_bytes() if self.config_path.is_file() else None
        if current != self.config_snapshot:
            raise ConfigurationError("配置文件已被其他程序修改，请重新启动 Web 管理页。")
        data = yaml.safe_dump(settings.model_dump(exclude_none=True, exclude_defaults=True), allow_unicode=True, sort_keys=False)
        with TemporaryDirectory(dir=self.config_path.parent) as config_dir:
            config = Path(config_dir) / self.config_path.name
            config.write_text(data, encoding="utf-8")
            if updates:
                with TemporaryDirectory(dir=self.env_path.parent) as env_dir:
                    env = Path(env_dir) / ".env"
                    env.write_bytes(self.env_path.read_bytes() if self.env_path.is_file() else b"")
                    for variable, value in updates.items():
                        set_key(env, variable, value, encoding="utf-8-sig")
                    os.chmod(env, 0o600)
                    os.replace(env, self.env_path)
            os.replace(config, self.config_path)


def unused_variable(base: str, used: set[str]) -> str:
    return base if base not in used else f"{base}_{secrets.token_hex(4).upper()}"


def parse_columns(objects: list[str], text: str, old: dict[str, CatalogEntry]) -> dict[str, CatalogEntry]:
    catalog = {name: CatalogEntry(description=old[name].description) for name in objects if name in old}
    previous = {(name, col.name): col.description for name, entry in old.items() for col in entry.columns}
    for line in text.splitlines():
        if not line.strip():
            continue
        qualified, separator, datatype = line.partition("|")
        source, _, name = qualified.strip().rpartition(".")
        if not separator or source not in objects or not name or not datatype.strip():
            raise ConfigurationError("列目录每行使用 Schema.视图.列名 | 类型，视图须出现在授权视图列表中。")
        entry = catalog.setdefault(source, CatalogEntry())
        entry.columns.append(CatalogColumn(name=name, type=datatype.strip(), description=previous.get((source, name), "")))
    return catalog
