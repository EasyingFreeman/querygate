export type Cell = string | number | boolean | null
export type QueryState = 'running' | 'success' | 'error'

export interface Datasource {
  id: string
  engine: 'sqlserver' | 'oracle'
  dialect: 'tsql' | 'oracle'
  description: string
}

export interface Column {
  name: string
  type: string
  description?: string
}

export interface CatalogObject {
  name: string
  description: string
  columns: Column[]
  kind?: 'table' | 'view' | 'unknown'
}

export function objectNameParts(value: string): [string, string] {
  const parts = /^(?:"((?:[^"]|"")*)"|([^.\[\]"]+))\.(?:"((?:[^"]|"")*)"|([^.\[\]"]+))$/.exec(value)
  if (!parts) return ['', value]
  return [parts[1]?.replaceAll('""', '"') ?? parts[2], parts[3]?.replaceAll('""', '"') ?? parts[4]]
}

export interface QueryRequest {
  datasource_id: string
  sql: string
  reason: string
  params: Cell[] | Record<string, Cell> | null
  max_rows: number
}

export interface QueryValidation {
  valid: true
  max_rows: number
  max_response_bytes: number
  timeout_seconds: number
}

export interface QueryResult {
  query_id: string
  status: 'success'
  columns: Column[]
  rows: Cell[][]
  row_count: number
  truncated: boolean
  execution_ms: number
}

export interface QueryStatus {
  query_id: string
  datasource_id: string
  client_id: string | null
  dialect: string | null
  status: QueryState
  started_at: string
  execution_ms: number | null
  error_code: string | null
  row_count: number | null
  truncated: boolean | null
  sql_preview: string | null
  diagnostic: string | null
}

export interface QueryDetail extends QueryStatus {
  sql: string | null
  error_message: string | null
  reason: string | null
}

export const sqlPolicyLabels = {
  allow_subqueries: '子查询',
  allow_ctes: '公用表表达式（WITH）',
  allow_windows: '窗口函数（OVER）',
  allow_sqlserver_identity: 'SQL Server 标识（DB_NAME() 和 @@SERVERNAME）',
}
export type SqlPolicy = Record<keyof typeof sqlPolicyLabels, boolean>
export const defaultSqlPolicy: SqlPolicy = {
  allow_subqueries: true, allow_ctes: true, allow_windows: true, allow_sqlserver_identity: false,
}
export type WriteOperation = 'update' | 'insert'
export interface WriteGrant {
  operations: WriteOperation[]
  primary_key: string[]
  columns: string[]
}
export interface WritePolicy {
  requires_approval: boolean
  datasources: Record<string, Record<string, WriteGrant>>
}
export type MutationState = 'pending' | 'executing' | 'succeeded' | 'rejected' | 'failed' | 'unknown'
export interface MutationRecord {
  operation_id: string
  client_id: string
  status: MutationState
  submitted_at: string
  decided_at: string | null
  approval: 'web_admin' | 'client_exempt' | null
  error_code: string | null
  submitted: {
    datasource_id: string
    operation: WriteOperation
    table: string
    reason: string
    key: Record<string, string | number>
    values: Record<string, Cell>
  }
  audit: { before: Record<string, Cell> | null; after: Record<string, Cell>; changed_at: string } | null
}

export interface QueryHistory {
  queries: QueryStatus[]
  next_cursor: string | null
}

export interface HistoryFilter {
  datasource_id?: string
  status?: QueryState
  cursor?: string
}

export interface WorkbenchClient {
  catalog(id: string, signal?: AbortSignal): Promise<CatalogObject[]>
  validate(query: QueryRequest, signal?: AbortSignal): Promise<QueryValidation>
  execute(query: QueryRequest, signal?: AbortSignal): Promise<QueryResult>
  history(filter: HistoryFilter, signal?: AbortSignal): Promise<QueryHistory>
  queryDetail(id: string, signal?: AbortSignal): Promise<QueryDetail>
}

export class ApiError extends Error {
  constructor(readonly code: string, message: string, readonly queryId?: string) {
    super(message)
  }
}

const databaseMessages: Record<string, string> = {
  ODBC_DRIVER_UNAVAILABLE: '找不到或无法加载 ODBC 驱动或 DSN。请核对名称，并确认驱动与 Python 的位数一致。',
  DATABASE_LOGIN_FAILED: '数据库拒绝登录。请核对身份验证方式、账号和访问权限。',
  DATABASE_TLS_FAILED: 'SQL Server TLS 握手失败。请核对连接串中的服务器名称和本机信任的证书链。',
  DATABASE_CONNECT_TIMEOUT: '连接数据库超时。请在运行 QueryGate 的电脑上检查主机、端口和网络。',
  DATABASE_CONNECTION_FAILED: '无法建立或保持数据库连接。请在「数据源管理」中测试连接，并检查服务器地址和网络。',
  ORACLE_SERVICE_UNAVAILABLE: 'Oracle 监听器找不到指定服务。请核对 DSN 中的服务名或 SID。',
  CATALOG_UNAVAILABLE: '已尝试连接数据库，但读取表和视图目录失败。请检查该数据库账号的目录访问权限。',
  GATEWAY_NOT_CONFIGURED: 'QueryGate 尚未加载可用的数据源配置。请打开「数据源管理」检查配置。',
  QUERY_TIMEOUT: '数据库操作超时。请检查网络和数据库响应。',
  SQLSERVER_IDENTITY_DISABLED: '当前客户端的 SQL 策略禁用了 DB_NAME() 和 @@SERVERNAME。可在授权客户端中设置。',
  SQL_FUNCTION_NOT_ALLOWED: '查询使用了不受支持的函数。请改用内置只读函数。',
  SQL_READ_ONLY_REQUIRED: '每次只能提交一条只读 SELECT。',
  SQL_COMMENT_OR_HINT: 'SQL 中的注释和优化器提示不受支持。',
  SQL_CTE_DISABLED: '当前数据源禁用了公用表表达式（WITH）。',
  SQL_SUBQUERY_DISABLED: '当前数据源禁用了子查询。',
  SQL_WINDOW_DISABLED: '当前数据源禁用了窗口函数（OVER）。',
  SQL_SCHEMA_REQUIRED: '表和视图需要使用带 schema 的名称，例如 dbo.Orders。',
  SQL_OBJECT_BLOCKED: '该表或视图已被此数据源的黑名单拦截。',
  CALL_REASON_REQUIRED: '请填写 1–500 字的调用原因。',
  WRITE_DATABASE_ERROR: '数据库拒绝了写入或审计记录。请检查目标表、审计表和数据库权限。',
  WRITE_KEY_MISMATCH: '授权中填写的主键与数据库中的主键不一致。',
  WRITE_KEY_VALUE_MISMATCH: '数据库匹配到的主键值与请求中提供的值不同。',
  WRITE_TARGET_CHANGED: '提交后数据源连接或审计表配置发生了变化。',
  WRITE_NOT_ALLOWED: '提交客户端的当前策略不允许这个写操作。',
  WRITE_AUDIT_REQUIRED: '数据源需要先设置数据库审计表。',
  WRITE_COMMIT_UNKNOWN: '数据库尚未确认提交结果。可以在审批页核对审计。',
}

export function databaseMessage(code: string, fallback: string): string {
  return databaseMessages[code] ?? fallback
}

export function liveClient(token: string): WorkbenchClient & { datasources(): Promise<Datasource[]> } {
  async function request<T>(path: string, signal?: AbortSignal, query?: QueryRequest): Promise<T> {
    let response: Response
    try {
      response = await fetch(`/v1/${path}`, {
        method: query ? 'POST' : 'GET',
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: 'application/json',
          ...(query ? { 'Content-Type': 'application/json' } : {}),
        },
        body: query && JSON.stringify(query),
        signal,
        cache: 'no-store',
        credentials: 'omit',
        redirect: 'error',
      })
    } catch (error) {
      if (signal?.reason instanceof DOMException && signal.reason.name === 'TimeoutError') {
        throw new ApiError('GATEWAY_TIMEOUT', '连接 QueryGate 超时。')
      }
      if (signal?.aborted) throw error
      throw new ApiError('GATEWAY_UNAVAILABLE', '无法连接 QueryGate 服务。')
    }

    const value = await response.json().catch(() => null)
    if (!response.ok) {
      const code = typeof value?.diagnostic === 'string' ? value.diagnostic
        : typeof value?.code === 'string' ? value.code : `HTTP_${response.status}`
      throw new ApiError(
        code, databaseMessage(code, typeof value?.message === 'string' ? value.message : 'QueryGate 未能完成请求。'),
        typeof value?.query_id === 'string' ? value.query_id : undefined,
      )
    }
    if (value === null) throw new ApiError('INVALID_RESPONSE', 'QueryGate 返回了无法识别的响应。')
    return value as T
  }

  return {
    datasources: () => request<Datasource[]>('datasources', AbortSignal.timeout(10_000)),
    async catalog(id, signal) {
      const schema = await request<{ objects: CatalogObject[] }>(`schema?${new URLSearchParams({ datasource_id: id, include_metadata: 'true' })}`, signal)
      return schema.objects
    },
    validate: (query, signal) => request<QueryValidation>('query/validate', signal, query),
    execute: (query, signal) => request<QueryResult>('query', signal, query),
    history(filter, signal) {
      const search = new URLSearchParams({ limit: '25' })
      for (const [key, value] of Object.entries(filter)) if (value) search.set(key, value)
      return request<QueryHistory>(`queries?${search}`, signal)
    },
    queryDetail: (id, signal) => request<QueryDetail>(`queries/${encodeURIComponent(id)}`, signal),
  }
}

export function describeError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError('WORKBENCH_ERROR', '未能完成操作，请重试。')
}
