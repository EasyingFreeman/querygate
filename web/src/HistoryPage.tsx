import { ChevronDown, Clock3, CodeXml, Copy, RefreshCw } from 'lucide-react'
import { useEffect, useState } from 'react'
import { databaseMessage, describeError } from './api'
import type { ApiError, Datasource, QueryDetail, QueryState, QueryStatus, WorkbenchClient } from './api'
import { Busy, Empty, ErrorNotice } from './panels'

const timeFormat = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium' })
const statusLabels: Record<QueryState, string> = { success: '成功', error: '失败', running: '执行中' }

function formatDuration(ms: number | null) {
  if (ms === null) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${ms} ms`
}

export default function HistoryPage({ client, sources }: { client: WorkbenchClient; sources: Datasource[] }) {
  const [source, setSource] = useState('')
  const [status, setStatus] = useState<QueryState | ''>('')
  const [cursor, setCursor] = useState<string | undefined>()
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const [records, setRecords] = useState<QueryStatus[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<ApiError | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<QueryDetail | null>(null)
  const [detailError, setDetailError] = useState<ApiError | null>(null)
  const [copyLabel, setCopyLabel] = useState('复制 SQL')

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    void client.history({ datasource_id: source || undefined, status: status || undefined, cursor }, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return
        setRecords((previous) => cursor ? [...previous, ...page.queries] : page.queries)
        setNextCursor(page.next_cursor)
        if (!cursor) {
          setSelected((id) => page.queries.some((entry) => entry.query_id === id) ? id : page.queries[0]?.query_id ?? null)
        }
      })
      .catch((failure) => {
        if (!controller.signal.aborted) {
          setError(describeError(failure))
          setNextCursor(null)
        }
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [client, source, status, cursor, revision])

  useEffect(() => {
    setDetail(null)
    setDetailError(null)
    setCopyLabel('复制 SQL')
    if (!selected) return
    const controller = new AbortController()
    void client.queryDetail(selected, controller.signal)
      .then((record) => { if (!controller.signal.aborted) setDetail(record) })
      .catch((failure) => { if (!controller.signal.aborted) setDetailError(describeError(failure)) })
    return () => controller.abort()
  }, [client, selected, revision])

  function resetPage() {
    setCursor(undefined)
    setNextCursor(null)
    setRecords([])
    setSelected(null)
  }

  async function copySql(sql: string) {
    try {
      await navigator.clipboard.writeText(sql)
      setCopyLabel('已复制')
    } catch {
      setCopyLabel('复制失败，请重试')
    }
  }

  return <main className="history-page">
    <header className="history-page-heading">
      <h1>查询历史</h1>
      <button type="button" className="button secondary" disabled={loading} onClick={() => {
        setCursor(undefined)
        setRevision((previous) => previous + 1)
      }}><RefreshCw size={16} /> 刷新</button>
    </header>
    <div className="history-layout">
      <section className="history-surface" aria-label="查询记录">
        <div className="history-filters">
          <label className="select-field">
            <select aria-label="按数据源筛选" value={source} onChange={(event) => { resetPage(); setSource(event.target.value) }}>
              <option value="">全部数据源</option>
              {sources.map((item) => <option key={item.id} value={item.id}>{item.id}</option>)}
            </select><ChevronDown size={15} aria-hidden="true" />
          </label>
          <label className="select-field">
            <select aria-label="按状态筛选" value={status} onChange={(event) => { resetPage(); setStatus(event.target.value as QueryState | '') }}>
              <option value="">全部状态</option>
              {Object.entries(statusLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select><ChevronDown size={15} aria-hidden="true" />
          </label>
        </div>
        <div className="history-table-wrap" aria-busy={loading}>
          {records.length > 0 && <table className="history-table">
            <thead><tr><th scope="col">提交的 SQL</th><th scope="col">状态</th><th scope="col">提交时间</th><th scope="col">耗时</th></tr></thead>
            <tbody>{records.map((record) => <tr key={record.query_id} className={record.query_id === selected ? 'selected' : ''}>
              <td>
                <button type="button" className="history-select" aria-current={record.query_id === selected ? 'true' : undefined}
                  onClick={() => {
                    setSelected(record.query_id)
                    if (matchMedia('(max-width: 900px)').matches) document.getElementById('query-detail')?.scrollIntoView()
                  }}>
                  <code>{record.sql_preview || '无 SQL 预览'}</code>
                  <span>{record.datasource_id}{record.client_id && ` · ${record.client_id}`}</span>
                </button>
              </td>
              <td><span className={`tag ${record.status}`} title={record.diagnostic ?? record.error_code ?? undefined}>{statusLabels[record.status]}</span></td>
              <td><time dateTime={record.started_at}>{timeFormat.format(new Date(record.started_at))}</time></td>
              <td>{formatDuration(record.execution_ms)}</td>
            </tr>)}</tbody>
          </table>}
          {error && <ErrorNotice error={error} />}
          {loading && <div className="inline-status history-progress"><Busy /> 正在读取记录</div>}
          {!loading && !error && records.length === 0 && <Empty title="暂无查询记录" icon={<Clock3 size={22} />} />}
        </div>
        {nextCursor && <footer className="history-footer">
          <button type="button" className="button subtle" disabled={loading} onClick={() => setCursor(nextCursor)}>加载更多</button>
        </footer>}
      </section>
      <section className="history-surface history-detail" id="query-detail" aria-label="查询详情">
        <header className="history-detail-heading"><h2>查询详情</h2>
          {detail?.sql && <button type="button" className="button secondary" onClick={() => void copySql(detail.sql!)} aria-live="polite">
            <Copy size={15} /> {copyLabel}
          </button>}
        </header>
        {detailError && <ErrorNotice error={detailError} />}
        {!selected && <Empty title="选择一条记录查看 SQL" icon={<CodeXml size={23} />} />}
        {selected && !detail && !detailError && <div className="inline-status history-progress"><Busy /> 正在读取详情</div>}
        {detail && <>
          <dl className="history-properties">
            <div><dt>数据源</dt><dd>{detail.datasource_id}</dd></div>
            <div><dt>Client ID</dt><dd>{detail.client_id ?? '—'}</dd></div>
            <div><dt>SQL 方言</dt><dd>{detail.dialect ?? '—'}</dd></div>
            <div><dt>耗时</dt><dd>{formatDuration(detail.execution_ms)}</dd></div>
            <div><dt>状态</dt><dd><span className={`tag ${detail.status}`}>{statusLabels[detail.status]}</span></dd></div>
            <div><dt>返回行数</dt><dd>{detail.row_count ?? '—'}{detail.truncated ? ' · 已截断' : ''}</dd></div>
            <div className="history-query-id"><dt>调用原因</dt><dd>{detail.reason ?? '—'}</dd></div>
            <div className="history-query-id"><dt>查询 ID</dt><dd><code>{detail.query_id}</code></dd></div>
          </dl>
          {detail.error_code && <div className="notice error history-failure">
            <code>{detail.diagnostic ?? detail.error_code}</code>
            {detail.error_message && <p>{databaseMessage(detail.diagnostic ?? detail.error_code, detail.error_message)}</p>}
          </div>}
          <h3>提交原文</h3>
          {detail.sql === null
            ? <p className="quiet">没有保留这次请求的 SQL。</p>
            : <pre className="history-sql" tabIndex={0}><code>{detail.sql}</code></pre>}
        </>}
      </section>
    </div>
  </main>
}
