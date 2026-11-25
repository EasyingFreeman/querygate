# QueryGate

QueryGate 为内部 AI Agent 提供 SQL Server 和 Oracle 的只读 SQL API。Agent 先发现获准的数据源，再读取审核过的视图目录，最后提交目标数据库的原生 SQL。

## 启动

需要 Python 3.11 或更新版本。SQL Server 需要安装 [Microsoft ODBC Driver 18](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)，Oracle 使用 [python-oracledb Thin 模式](https://python-oracledb.readthedocs.io/en/latest/user_guide/initialization.html)。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
Copy-Item querygate.example.yaml querygate.yaml
$env:QUERYGATE_ANALYST_TOKEN = .\.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"
```

编辑 `querygate.yaml`，填写实际视图和列，并从环境变量注入数据库连接凭证。SQL Server 的 `QUERYGATE_FINANCE_ODBC` 使用带加密和证书校验的连接字符串，例如 `Driver={ODBC Driver 18 for SQL Server};Server=tcp:db.example.net,1433;Database=Finance;Encrypt=yes;TrustServerCertificate=no;...`。Oracle 使用 `QUERYGATE_ERP_DSN`（例如 `db.example.net:1521/service`）、`QUERYGATE_ERP_USER` 和 `QUERYGATE_ERP_PASSWORD`。先接一台数据库时，保留对应数据源及客户端引用即可。可用 `QUERYGATE_CONFIG` 指定其他配置文件路径。

```powershell
.\.venv\Scripts\python -m uvicorn querygate.app:create_app --factory --host 127.0.0.1 --port 8000
```

服务启动时检查配置和所需环境变量。`/healthz` 返回进程健康状态，`/docs` 提供可交互的 OpenAPI 文档。远程使用时在内部 HTTPS 入口后运行。

## REST API

`/v1` 请求使用 `Authorization: Bearer <token>`。每个客户端配置一个独立 token 与数据源列表。

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `/v1/datasources` | 当前客户端可访问的数据源与 SQL 方言 |
| GET | `/v1/schema?datasource_id=finance` | 已配置的视图、列和业务说明 |
| POST | `/v1/query` | 列元数据、数组形式的数据行、截断状态和查询 ID |
| GET | `/v1/queries/{query_id}` | 当前客户端的查询状态、耗时和错误代码 |

```powershell
$headers = @{ Authorization = "Bearer $env:QUERYGATE_ANALYST_TOKEN" }
Invoke-RestMethod 'http://127.0.0.1:8000/v1/datasources' -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/v1/schema?datasource_id=finance' -Headers $headers
$request = @{
    datasource_id = 'finance'
    sql = 'SELECT TOP (10) transaction_id, amount FROM ai_public.v_transactions WHERE amount >= ? ORDER BY amount DESC'
    params = @(100)
    max_rows = 10
} | ConvertTo-Json -Compress
Invoke-RestMethod 'http://127.0.0.1:8000/v1/query' -Headers $headers -ContentType 'application/json' -Method Post -Body $request
```

Oracle 使用命名绑定。参数可省略，或按本例传入标量 JSON 值。

```json
{
  "datasource_id": "erp",
  "sql": "SELECT ORDER_ID, TOTAL_AMOUNT FROM AI_PUBLIC.V_ORDERS WHERE TOTAL_AMOUNT >= :minimum FETCH FIRST 10 ROWS ONLY",
  "params": {"minimum": 100},
  "max_rows": 10
}
```

查询错误返回 `code`、`message` 及已分配的 `query_id`。常见代码包括 `INVALID_PARAMS`、`SQL_PARSE_ERROR`、`POLICY_DENIED`、`OBJECT_NOT_ALLOWED`、`COLUMN_NOT_ALLOWED`、`DATASOURCE_BUSY`、`QUERY_TIMEOUT` 和 `DATASOURCE_UNAVAILABLE`。数据库错误使用固定描述。

API 默认返回最多 100 行，并受数据源上限约束。`row_count` 是本次实际返回的行数。额外行或响应大小超限会设置 `truncated`。若元数据或首行已超出大小上限，返回 `RESULT_LIMIT_EXCEEDED`。高精度数值和超出 JavaScript 安全整数范围的整数使用字符串，日期时间使用 ISO 8601，二进制使用 Base64。Oracle LOB 等类型需在审核过的视图中投影为有限长度的标量。

## 数据库授权与运行边界

- 为每个数据源设置专用账号，逐个向审核过的视图授予 `SELECT`。例如在已创建账号及视图的前提下，SQL Server 使用 `GRANT SELECT ON OBJECT::ai_public.v_transactions TO ai_reader`，Oracle 使用 `GRANT CREATE SESSION TO AI_READER` 和 `GRANT SELECT ON AI_PUBLIC.V_ORDERS TO AI_READER`。
- 审核视图的行列范围、依赖项、函数、同义词、外部对象和 LOB 投影。检查账号从默认角色、`PUBLIC` 及其他授权路径继承的权限。Oracle 可能将未匹配到列的标识符解析为无参函数，视图变更时需同步更新列目录。[Oracle 名称解析文档](https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/About-User-Defined-Functions.html)
- SQLGlot 检查单条读查询、CTE、JOIN、集合操作、子查询、窗口表达式和审核过的函数。物理对象名必须包含 schema。配置使用数据库对象的实际拼写。Oracle 未加引号的名字按大写解析。注释、提示和未审核的 SQL 构造会被拒绝。参数由驱动绑定。
- 单个数据源默认并发 2、查询时限 10 秒和响应上限 1 MiB。SQL 上限 32 KiB，请求体上限 128 KiB。超时会尝试取消驱动操作，并在连接清理结束后释放并发名额。生产数据库可通过资源管理进一步限制查询工作量。
- 当前版本按查询打开和关闭连接。并发配额和最近 1,024 条查询状态按进程维护，首版按单 worker 运行。审计事件写入服务日志，记录客户端、数据源、查询 ID、耗时与结果状态。日志不记录 SQL、参数、结果值或数据库驱动原始错误。

## 测试与参考

```powershell
.\.venv\Scripts\python -m pytest
```

默认测试使用模拟驱动和 ASGI 客户端，无需数据库。启用真实数据源时，用同一个专用账号验证授权视图、未授权对象、参数绑定、超时取消和驱动版本。

使用 [SQLGlot](https://github.com/tobymao/sqlglot) 做方言解析及 CTE 作用域分析，并通过 [pyodbc](https://github.com/mkleehammer/pyodbc) 和 [python-oracledb](https://github.com/oracle/python-oracledb) 执行。REST 和共享执行层的设计参考了 [readonly-sql-mcp](https://github.com/EliasOlie/readonly-sql-mcp)，多连接适配参考了 [sql-mcp](https://github.com/Knuckles-Team/sql-mcp)。MCP 可接入同一个 `Gateway`，当前版本提供 REST。
