# QueryGate

QueryGate 为内部 AI Agent 提供 SQL Server 和 Oracle 的只读查询与受控写入。Agent 可浏览授权目录、执行原生 SELECT，并按客户端权限提交需要审计的 UPDATE 或 INSERT。

## Windows 启动

QueryGate 服务可和 Claude 插件运行在同一台电脑。安装 Python 3.11 或更新版本后，在解压目录或仓库根目录打开终端，运行以下命令。首次运行会创建虚拟环境并安装依赖。服务端 ZIP 自带 Web 页面，从源码运行时先执行下文的前端安装和构建命令。

```powershell
.\Start-QueryGate.ps1
```

1. 打开 `http://127.0.0.1:8000/workbench/`，点右上角「数据源管理」，输入启动终端显示的 `QueryGate admin code`。首次启动也可以直接用 `--configure` 打开管理页。
2. 点击「新增数据源」，填写连接并点「测试连接」。测试会读取数据库目录。SQL Server 填 ODBC 连接串，Oracle 填服务地址、只读用户名和密码。[Microsoft ODBC Driver 18](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)
3. 在「表和视图」选择白名单或黑名单。每行填写一个 `Schema.对象`。白名单有条目时只开放列出的对象，黑名单有条目时排除列出的对象。任一模式留空时均开放数据库账号可读的表和视图。
4. 在「授权客户端」新增、编辑或删除客户端，并为其勾选允许访问的数据源。保存后可复制客户端 token。修改立即应用于后续查询，删除客户端会撤销其 token。工作台连接服务和 Claude 插件使用客户端 token。

在「授权客户端」配置 SQL 规则、写入授权和审批模式。已有客户端可沿用原数据源的 SQL 规则，选择「使用客户端级 SQL 规则」后，可分别设置子查询、公用表表达式、窗口函数及 SQL Server 的 `DB_NAME()` 和 `@@SERVERNAME`。

```powershell
.\Start-QueryGate.cmd
```

两个脚本接受相同的参数。若电脑的执行策略禁止运行 PowerShell 脚本，可使用上面的 `.cmd`。

同一台电脑上的 Claude 插件填写 `http://127.0.0.1:8000` 和 Web 管理页中复制的客户端 token。浏览器可打开 `http://127.0.0.1:8000/docs`。查询工作台位于 `http://127.0.0.1:8000/workbench/`。按 `Ctrl+C` 停止服务。跨电脑使用时，在服务端为查询 API 配置可从客户端访问的 HTTPS 入口。

也可以运行 `--initialize` 生成示例 YAML 和 `.env`，再手动填写实际启用的连接。`enabled: false` 会保留配置并停用该数据源。使用 `--datasource erp` 可只为本次运行启用 YAML 中已启用的 `erp`，同时保留客户端原有的访问限制。脚本还支持 `--port`、`--config`、`--env-file` 和 `--install`。

如果需要重建虚拟环境，先停止 QueryGate。在解压目录运行 `Remove-Item -LiteralPath .\.venv -Recurse -Force`，再运行启动脚本。在新目录升级时复制 `querygate.yaml` 和 `.env` 即可，启动脚本会为该目录准备虚拟环境。

本机启动脚本在回环地址提供管理服务，每次启动生成新的管理密钥。数据源设置写入 YAML，连接凭证写入本地 `.env`。`.env` 连接值按字面读取，已有同名系统环境变量优先。升级时可以复制已有的 `querygate.yaml` 和 `.env`。`/healthz` 用于检查服务进程。

## REST API

`/v1` 请求使用 `Authorization: Bearer <token>`。每个客户端配置一个独立 token 与数据源列表。

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `/v1/datasources` | 当前客户端可访问的数据源与 SQL 方言 |
| GET | `/v1/schema?datasource_id=finance` | 策略允许的表、视图、列和业务说明。支持筛选和分页 |
| POST | `/v1/query/validate` | 检查 SQL 策略和绑定参数，返回生效的行数、响应大小和超时上限 |
| POST | `/v1/query` | 列元数据、数组形式的数据行、截断状态和查询 ID |
| GET | `/v1/queries` | 当前客户端的查询历史和 SQL 预览，支持按数据源和状态筛选 |
| GET | `/v1/queries/{query_id}` | 单次查询的详情，包括提交的 SQL 原文 |
| POST | `/v1/mutations` | 提交受控 UPDATE／INSERT |
| GET | `/v1/mutations/{operation_id}` | 查看本客户端提交的写入状态和审计值 |

v0.2.0 起，查询、预检和写入均要求 `reason`，长度为 1–500 字。MCP 由 Agent 填写具体的业务原因，工作台手动查询时由操作者填写。服务端和插件需同步升级。

以下示例中的 `$clientToken` 取自客户端的 token 配置。

```powershell
$headers = @{ Authorization = "Bearer $clientToken" }
Invoke-RestMethod 'http://127.0.0.1:8000/v1/datasources' -Headers $headers
Invoke-RestMethod 'http://127.0.0.1:8000/v1/schema?datasource_id=finance' -Headers $headers
$request = @{
    datasource_id = 'finance'
    reason = 'Review the reportable transactions for this month'
    sql = 'SELECT TOP (10) transaction_id, amount FROM ai_public.v_transactions WHERE amount >= ? ORDER BY amount DESC'
    params = @(100)
    max_rows = 10
} | ConvertTo-Json -Compress
Invoke-RestMethod 'http://127.0.0.1:8000/v1/query/validate' -Headers $headers -ContentType 'application/json' -Method Post -Body $request
Invoke-RestMethod 'http://127.0.0.1:8000/v1/query' -Headers $headers -ContentType 'application/json' -Method Post -Body $request
Invoke-RestMethod 'http://127.0.0.1:8000/v1/queries?datasource_id=finance&limit=25' -Headers $headers
```

Oracle 使用命名绑定。参数可省略，或按本例传入标量 JSON 值。

```json
{
  "datasource_id": "erp",
  "reason": "Verify the monthly order totals",
  "sql": "SELECT ORDER_ID, TOTAL_AMOUNT FROM AI_PUBLIC.V_ORDERS WHERE TOTAL_AMOUNT >= :minimum FETCH FIRST 10 ROWS ONLY",
  "params": {"minimum": 100},
  "max_rows": 10
}
```

查询错误返回 `code`、`message` 及已分配的 `query_id`。对于 `DATASOURCE_UNAVAILABLE`，`diagnostic` 提供可判断的原因，例如 ODBC 驱动或 DSN、登录被拒绝、TLS 握手失败或网络连接失败。管理页「测试连接」会显示对应的排查方向。[ODBC 连接状态](https://learn.microsoft.com/en-us/sql/odbc/reference/syntax/sqldriverconnect-function) · [SQL Server TLS 排查](https://learn.microsoft.com/en-us/sql/connect/odbc/connection-troubleshooting)

对于 `POLICY_DENIED`，`diagnostic` 标识具体规则，例如 `SQL_SUBQUERY_DISABLED` 或 `SQLSERVER_IDENTITY_DISABLED`。`message` 说明拦截原因。目录、预检和执行使用同一份对象策略。预检解析 SQL 并检查对象授权及绑定参数。启用实时目录的连接在缓存过期后读取数据库元数据，目录缓存默认保留 30 秒。最终的读取权限由数据库账号的实际授权决定。

目录支持 `search`（对象名或说明）、`kind=table|view|unknown`、`object_name`（完整限定名）、`include_columns=false` 和 `limit`（1–100）。分页时用响应中的 `next_offset` 作为下一次请求的 `offset`，并保留其他筛选条件。`total_objects` 为当前授权范围内符合筛选条件的对象总数。静态目录可通过 `kind` 标注类型，未标注的对象类型为 `unknown`。

查询历史按开始时间倒序返回，支持 `datasource_id`、`status`（`running`、`success`、`error`）、`limit`（默认 25，范围 1–100）和 `cursor`。继续翻页时使用响应中的 `next_cursor` 并保留筛选条件。详情包括 Client ID、调用原因、SQL 方言和最多 32 KiB 的 SQL 原文。客户端只能查看自己提交的记录。查询历史保留在进程内，服务重启后从空列表开始。

API 默认返回最多 100 行，并受数据源上限约束。`row_count` 是本次实际返回的行数。额外行或响应大小超限会设置 `truncated`。若元数据或首行已超出大小上限，返回 `RESULT_LIMIT_EXCEEDED`。高精度数值和超出 JavaScript 安全整数范围的整数使用字符串，日期时间使用 ISO 8601，二进制使用 Base64。Oracle LOB 等类型需在审核过的视图中投影为有限长度的标量。

## 数据库授权与运行边界

每个数据源实例拥有自己的 ID、连接、授权目录和限额。相同引擎的实例共用驱动适配器。引擎定义提供配置字段和方言，新引擎还需实现连接适配、原生 SQL 校验及参数绑定。

- 为每个数据源设置专用账号，逐个向审核过的视图授予 `SELECT`。例如在已创建账号及视图的前提下，SQL Server 使用 `GRANT SELECT ON OBJECT::ai_public.v_transactions TO ai_reader`，Oracle 使用 `GRANT CREATE SESSION TO AI_READER` 和 `GRANT SELECT ON AI_PUBLIC.V_ORDERS TO AI_READER`。
- 审核视图的行列范围、依赖项、函数、同义词、外部对象和 LOB 投影。对象规则作用于 SQL 直接引用的表和视图，视图内部依赖由数据库处理。检查账号从默认角色、`PUBLIC` 及其他授权路径继承的权限。Oracle 可能将未匹配到列的标识符解析为无参函数，静态目录中的视图变更时需同步更新列目录。[Oracle 名称解析文档](https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/About-User-Defined-Functions.html)
- SQLGlot 检查单条读查询、CTE、JOIN、集合操作、子查询、窗口表达式和审核过的函数。物理对象名必须包含 schema。配置使用数据库对象的实际拼写。Oracle 未加引号的名字按大写解析。注释、提示和未审核的 SQL 构造会被拒绝。参数由驱动绑定。
- 单个数据源默认并发 2、查询时限 10 秒和响应上限 1 MiB。SQL 上限 32 KiB，请求体上限 128 KiB。超时会尝试取消驱动操作，并在连接清理结束后释放并发名额。生产数据库可通过资源管理进一步限制查询工作量。
- 当前版本按查询打开和关闭连接。并发配额和最近 1,024 条查询状态按进程维护，首版按单 worker 运行。审计事件写入服务日志，记录客户端、数据源、查询 ID、耗时与结果状态。日志不记录 SQL、参数、结果值或数据库驱动原始错误。

## 写入授权、审批与审计

写入仅接受结构化的单行 UPDATE／INSERT。每个请求必须包含目标表的完整主键、修改列和调用原因。INSERT 也需提供主键值，因此本版适用于可显式提供主键的表。SQL Server 写入要求 2014 或以上的普通磁盘表，Oracle 写入要求非临时表和已验证的不可延迟主键。数据库写入由固定的参数化语句生成，普通 SQL 查询入口保持只读。 [SQL Server 表目录](https://learn.microsoft.com/sql/relational-databases/system-catalog-views/sys-tables-transact-sql) · [Oracle 数据库参考](https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/ALL_TABLES.html)

1. 由 DBA 在目标数据库创建审计表，再在「数据源 → 写入审计」填写表名。应用连接账号需要审计表的 `SELECT` 和 `INSERT` 权限，以及业务表中目标列所需的 `SELECT`、`UPDATE` 或 `INSERT` 权限。授权前审核业务表触发器和约束的副作用。
2. 在「授权客户端 → 写入策略」为指定客户端添加表和列，填入数据库的真实主键列，并选择 UPDATE、INSERT 或两者。默认开启 Web 审批。确需自动执行的客户端可取消勾选。
3. 在「数据源管理 → 写入审批」核对 Client ID、原因、主键和提交值，再批准或拒绝。UPDATE 在事务中锁定目标行，读取修改列的原值，执行更新并读回实际结果。数据变化和审计在同一个数据库事务内提交。结果不确定时可使用「核对数据库审计」。 [SQL Server 锁定提示](https://learn.microsoft.com/en-us/sql/t-sql/queries/hints-transact-sql-table) · [Oracle SELECT FOR UPDATE](https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/SELECT.html)

SQL Server 审计表示例：

```sql
CREATE TABLE dbo.QueryGateMutationAudit (
    OPERATION_ID varchar(34) NOT NULL PRIMARY KEY,
    EVENT_JSON nvarchar(max) NOT NULL
);
```

Oracle 审计表示例，假设 schema `QUERYGATE_AUDIT` 已存在：

```sql
CREATE TABLE QUERYGATE_AUDIT.MUTATIONS (
    OPERATION_ID varchar2(34) NOT NULL PRIMARY KEY,
    EVENT_JSON clob NOT NULL
);
```

`POST /v1/mutations` 示例：

```json
{
  "request_id": "e3ebd235-adb4-4d53-b947-e00b00217bd4",
  "datasource_id": "inventory",
  "operation": "update",
  "table": "dbo.Orders",
  "key": {"OrderId": 7},
  "values": {"Status": "Shipped"},
  "reason": "仓库确认订单已出库，需要同步订单状态"
}
```

同一次请求的重试须沿用原 `request_id`，同一请求不会重复执行。审批队列和状态存放在配置文件旁的 `querygate.mutations.sqlite3`，数据库审计表保存已提交的修改前后值及请求元数据。升级时如已有写入记录，可随配置一起迁移该文件。

## Web 查询工作台

工作台支持按数据源浏览和搜索获准的表和视图、编辑 SQL 与绑定参数、预检与执行和查看结果。顶部「查询历史」打开独立页面，可筛选记录、查看错误规则和复制提交的 SQL。客户端 token 仅保留在当前页面内存中。界面提供浅色和深色主题。

在项目根目录执行以下命令启动前端开发服务器。Vite 使用 Node.js 22.12+ 或 24+。

```powershell
npm --prefix web ci
npm --prefix web run dev
```

在浏览器打开 `http://127.0.0.1:5173/workbench/?demo=1` 可以用模拟数据体验界面。连接实际服务时，先按上文启动 REST 服务，再打开 `http://127.0.0.1:5173/workbench/` 并输入已获授权的客户端 token。Vite 将 `/v1` 请求转发到本机 8000 端口。

共享部署时，用以下命令构建前端并启动服务。工作台会自动挂载到 `http://127.0.0.1:8000/workbench/`。

```powershell
npm --prefix web run build
.\Start-QueryGate.cmd
```

如需从其他目录加载构建产物，设置 `QUERYGATE_WEB_ROOT`。实际数据由 `/v1` 的客户端授权规则保护。

## MCP stdio

先启动 QueryGate REST 服务，再安装可选 MCP 依赖。

```powershell
.\.venv\Scripts\python -m pip install -e ".[mcp]"
```

MCP 客户端用 `python -m querygate.mcp_server` 启动 stdio 进程。在支持 `mcpServers` 格式的客户端中，可使用以下配置，将 `command` 换成虚拟环境 Python 的绝对路径，并通过客户端的密钥配置注入 `QUERYGATE_MCP_TOKEN`。

```json
{
  "mcpServers": {
    "querygate": {
      "command": "C:\\path\\to\\QueryGate\\.venv\\Scripts\\python.exe",
      "args": ["-m", "querygate.mcp_server"],
      "env": {
        "QUERYGATE_URL": "http://127.0.0.1:8000",
        "QUERYGATE_MCP_TOKEN": "<scoped-querygate-client-token>"
      }
    }
  }
}
```

Agent 可调用 `list_datasources`、`get_schema` 和 `execute_query`。`get_schema` 默认每页返回 50 个授权对象。如需列详情，传入 `object_name` 和 `include_columns=true`。使用 `submit_mutation` 提交受控写入，并用 `get_mutation` 查看审批和审计结果。查询错误保留 `code`、`message`、具体规则 `diagnostic` 和已分配的 `query_id`。

stdio 进程使用该 token 调用现有 REST 服务，与 REST 客户端共享数据源授权、限额和审计。数据库凭证由 REST 服务持有。`QUERYGATE_URL` 默认指向本机 8000 端口，远程 REST 地址使用 HTTPS。

## Claude 安装包

`integrations/claude/` 可以构建 Claude Code 本地插件市场 ZIP 和 Claude Desktop `.mcpb`。包内使用同一个 MCP 工具契约，Claude 侧只需 QueryGate REST 地址与客户端 token。安装方法见 [Claude 安装说明](integrations/claude/INSTALL.md)。

从项目根目录运行以下命令。构建需要 Node.js 20.3+ 和已安装 `.[mcp]` 的 Python 虚拟环境。

```powershell
$env:QUERYGATE_BUILD_PYTHON = (Resolve-Path '.venv/Scripts/python.exe').Path
npm --prefix integrations/claude ci
npm --prefix integrations/claude run package
```

两个安装包和 SHA-256 校验值写入 `dist/claude/`。

## 测试与参考

```powershell
.\.venv\Scripts\python -m pip install -e ".[test,mcp]"
.\.venv\Scripts\python -m pytest
```

默认测试使用模拟驱动和 ASGI 客户端，无需数据库。启用真实数据源时，用同一个专用账号验证获准的表和视图、策略拒绝的对象、参数绑定、超时取消和驱动版本。

查询解析、执行与 MCP 使用现成实现。REST 和共享执行层借鉴了 `readonly-sql-mcp`，多连接适配参考了 `sql-mcp`。[SQLGlot](https://github.com/tobymao/sqlglot) · [pyodbc](https://github.com/mkleehammer/pyodbc) · [python-oracledb](https://github.com/oracle/python-oracledb) · [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) · [readonly-sql-mcp](https://github.com/EliasOlie/readonly-sql-mcp) · [sql-mcp](https://github.com/Knuckles-Team/sql-mcp)
