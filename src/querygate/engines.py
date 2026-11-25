from dataclasses import dataclass
from typing import Literal

Engine = Literal["sqlserver", "oracle"]
Dialect = Literal["tsql", "oracle"]


@dataclass(frozen=True)
class ConnectionField:
    key: str
    label: str
    env_suffix: str
    masked: bool = True

    @property
    def settings_key(self) -> str:
        return f"{self.key}_env"


@dataclass(frozen=True)
class EngineProfile:
    label: str
    dialect: Dialect
    fields: tuple[ConnectionField, ...]
    requires_column_catalog: bool = False


ENGINE_PROFILES: dict[Engine, EngineProfile] = {
    "sqlserver": EngineProfile("SQL Server", "tsql", (ConnectionField("connection_string", "ODBC 连接串", "ODBC"),)),
    "oracle": EngineProfile("Oracle", "oracle", (
        ConnectionField("dsn", "服务地址（DSN）", "DSN", masked=False),
        ConnectionField("user", "只读用户名", "USER", masked=False),
        ConnectionField("password", "密码", "PASSWORD"),
    ), requires_column_catalog=True),
}
