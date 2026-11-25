import { ApiError } from './api'
import type { CatalogObject, Datasource, QueryDetail, QueryResult, WorkbenchClient } from './api'

export const demoSources: Datasource[] = [
  { id: 'finance', engine: 'sqlserver', dialect: 'tsql', description: '已审核的财务报表视图' },
  { id: 'erp', engine: 'oracle', dialect: 'oracle', description: '已审核的订单汇总视图' },
]

const catalog: Record<string, CatalogObject[]> = {
  finance: [{
    name: 'ai_public.v_transactions', description: '已过账的交易及其报告币种金额。',
    columns: [
      { name: 'transaction_id', type: 'bigint', description: '交易编号' },
      { name: 'posted_at', type: 'datetime2', description: '过账时间' },
      { name: 'amount', type: 'decimal(19,4)', description: '报告币种金额' },
    ],
  }, {
    name: 'ai_public.v_accounts', description: '财务报表可用的账户。',
    columns: [
      { name: 'account_id', type: 'bigint', description: '账户编号' },
      { name: 'account_name', type: 'nvarchar(120)', description: '账户名称' },
      { name: 'currency_code', type: 'char(3)', description: '币种代码' },
    ],
  }],
  erp: [{
    name: 'AI_PUBLIC.V_ORDERS', description: '按订单编号汇总的订单金额。',
    columns: [
      { name: 'ORDER_ID', type: 'NUMBER', description: '订单编号' },
      { name: 'TOTAL_AMOUNT', type: 'NUMBER(19,4)', description: '订单总金额' },
    ],
  }],
}

export const demoSql: Record<string, string> = {
  finance: 'SELECT TOP (100)\n    transaction_id,\n    posted_at,\n    amount\nFROM ai_public.v_transactions\nORDER BY posted_at DESC',
  erp: 'SELECT\n    ORDER_ID,\n    TOTAL_AMOUNT\nFROM AI_PUBLIC.V_ORDERS\nORDER BY ORDER_ID DESC\nFETCH FIRST 100 ROWS ONLY',
}

const resultRows: Record<string, QueryResult['rows']> = {
  finance: [
    [84291, '2026-11-24 09:42:16', '1280.5000'],
    [84290, '2026-11-24 09:38:05', '485.0000'],
    [84289, '2026-11-24 09:31:38', '9042.7500'],
    [84288, '2026-11-24 09:22:51', '215.4000'],
    [84287, '2026-11-24 09:17:20', '3247.0000'],
    [84286, '2026-11-24 09:08:04', '760.2500'],
    [84285, '2026-11-24 08:56:29', '148.9900'],
    [84284, '2026-11-24 08:42:03', '6218.3000'],
  ],
  erp: [[31026, '320.0000'], [31025, '1257.5000'], [31024, '87.2500'], [31023, '6740.0200']],
}

const makeResult = (source: string, limit: number, queryId: string): QueryResult => ({
  query_id: queryId,
  status: 'success',
  columns: catalog[source][0].columns,
  rows: resultRows[source].slice(0, limit),
  row_count: Math.min(resultRows[source].length, limit),
  truncated: resultRows[source].length > limit,
  execution_ms: 42,
})

export function demoSession() {
  const initialResult = makeResult('finance', 100, 'q_00000000000000000000000000000001')
  const history: QueryDetail[] = [
    {
      query_id: initialResult.query_id, datasource_id: 'finance', status: 'success',
      started_at: '2026-11-24T14:42:16Z', execution_ms: 42,
      error_code: null, row_count: initialResult.row_count, truncated: false,
      client_id: 'demo-agent', dialect: 'tsql', reason: '核对最近已过账的交易金额',
      sql: demoSql.finance, sql_preview: demoSql.finance.replace(/\s+/g, ' '), diagnostic: null, error_message: null,
    },
    {
      query_id: 'q_00000000000000000000000000000002', datasource_id: 'erp', status: 'error',
      started_at: '2026-11-24T14:38:05Z', execution_ms: 4,
      error_code: 'OBJECT_NOT_ALLOWED', row_count: null, truncated: null,
      client_id: 'demo-agent', dialect: 'oracle', reason: '检查客户目录的读取权限',
      sql: 'SELECT *\nFROM AI_PUBLIC.V_CUSTOMERS', sql_preview: 'SELECT * FROM AI_PUBLIC.V_CUSTOMERS',
      diagnostic: null, error_message: '此对象不在数据源授权范围内。',
    },
  ]

  const client: WorkbenchClient = {
    async catalog(id) { return catalog[id] },
    async validate(query) {
      return { valid: true, max_rows: Math.min(query.max_rows, 1000), max_response_bytes: 1048576, timeout_seconds: 10 }
    },
    async execute(query) {
      const result = makeResult(query.datasource_id, query.max_rows, `q_${crypto.randomUUID().replaceAll('-', '')}`)
      history.unshift({
        query_id: result.query_id, datasource_id: query.datasource_id, status: 'success',
        started_at: new Date().toISOString(), execution_ms: result.execution_ms, error_code: null,
        row_count: result.row_count, truncated: result.truncated,
        client_id: 'demo-agent', dialect: demoSources.find((item) => item.id === query.datasource_id)!.dialect, reason: query.reason,
        sql: query.sql, sql_preview: query.sql.trim().replace(/\s+/g, ' ').slice(0, 180), diagnostic: null, error_message: null,
      })
      return result
    },
    async history(filter) {
      const matching = history.filter((item) => (
        (!filter.datasource_id || item.datasource_id === filter.datasource_id)
        && (!filter.status || item.status === filter.status)
      ))
      const start = filter.cursor ? matching.findIndex((item) => item.query_id === filter.cursor) + 1 : 0
      const queries = matching.slice(start, start + 25)
      return {
        queries: queries.map(({ sql: _sql, error_message: _message, reason: _reason, ...summary }) => summary),
        next_cursor: start + queries.length < matching.length ? queries[queries.length - 1].query_id : null,
      }
    },
    async queryDetail(id) {
      const record = history.find((item) => item.query_id === id)
      if (!record) throw new ApiError('QUERY_NOT_FOUND', '查不到这条记录。')
      return record
    },
  }
  return { client, initialResult, sources: demoSources, mode: 'demo' as const }
}
