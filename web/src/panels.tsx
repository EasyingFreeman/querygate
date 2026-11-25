import * as Dialog from '@radix-ui/react-dialog'
import { Check, ChevronDown, CircleAlert, Copy, Database, KeyRound, LoaderCircle, Search, Table2, X } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { describeError, liveClient, objectNameParts } from './api'
import type { ApiError, CatalogObject, Datasource, QueryResult, WorkbenchClient } from './api'

export const engineName = { sqlserver: 'SQL Server', oracle: 'Oracle' } as const

export interface Session {
  client: WorkbenchClient
  sources: Datasource[]
  mode: 'demo' | 'live'
  initialResult?: QueryResult
}

export function Busy() {
  return <LoaderCircle className="spin" size={16} aria-hidden="true" />
}

export function ErrorNotice({ error }: { error: ApiError }) {
  return <div className="notice error" role="alert">
    <CircleAlert size={17} aria-hidden="true" />
    <div>
      <code>{error.code}</code>
      <div>{error.message}</div>
      {error.queryId && <code className="quiet">{error.queryId}</code>}
    </div>
  </div>
}

export function Empty({ title, detail, icon }: { title: string; detail?: string; icon: ReactNode }) {
  return <div className="empty">
    <span className="empty-icon">{icon}</span>
    <strong>{title}</strong>
    {detail && <span>{detail}</span>}
  </div>
}

export function Catalog({
  sources, current, select, objects, loading, error, insert, disabled,
}: {
  sources: Datasource[]; current: Datasource; select: (id: string) => void; objects: CatalogObject[]
  loading: boolean; error: ApiError | null; insert: (text: string) => void; disabled: boolean
}) {
  const [search, setSearch] = useState('')
  const match = search.trim().toLowerCase()
  function quote(name: string) {
    return current.dialect === 'tsql' ? `[${name.replaceAll(']', ']]')}]` : `"${name.replaceAll('"', '""')}"`
  }
  const visible = objects.filter((item) => (
    [item.name, item.description, ...item.columns.flatMap((column) => [column.name, column.type, column.description])]
      .some((text) => text?.toLowerCase().includes(match))
  ))

  return <aside className="catalog" aria-label="数据目录">
    <h2>数据目录</h2>
    <label className="source-picker">
      <span className="source-icon"><Database size={17} aria-hidden="true" /></span>
      <span className="source-name"><strong>{current.id}</strong><small>{engineName[current.engine]}</small></span>
      <ChevronDown size={16} className="chevron" aria-hidden="true" />
      <select aria-label="当前数据源" value={current.id} onChange={(event) => select(event.target.value)} disabled={disabled}>
        {sources.map((item) => <option key={item.id} value={item.id}>{item.id} · {engineName[item.engine]}</option>)}
      </select>
    </label>
    <label className="search">
      <Search size={16} aria-hidden="true" />
      <input aria-label="搜索表、视图和字段" placeholder="搜索表、视图或字段" value={search} onChange={(event) => setSearch(event.target.value)} />
      {search && <button className="icon-button" aria-label="清除搜索" onClick={() => setSearch('')}><X size={14} /></button>}
    </label>
    <div className="catalog-label"><span>授权表和视图</span><span className="count">{loading || error ? '–' : visible.length}</span></div>
    <div className="catalog-scroll" aria-busy={loading}>
      {loading && <div className="inline-status"><Busy /> 正在读取目录</div>}
      {error && <>
        <ErrorNotice error={error} />
        <a className="button subtle" href={`?setup=1&source=${encodeURIComponent(current.id)}`}>检查此数据源</a>
      </>}
      {!loading && !error && visible.length === 0 && <p className="quiet">{search ? '未找到匹配的对象。' : '此数据源暂无可访问的表和视图。'}</p>}
      {visible.map((item, index) => <details className="catalog-object" key={item.name} open={index === 0 || Boolean(match)}>
        <summary title={item.name}><Table2 size={15} aria-hidden="true" /><code>{item.name}</code>
          {item.kind && item.kind !== 'unknown' && <small className="quiet">{item.kind === 'table' ? '表' : '视图'}</small>}
          <ChevronDown size={14} className="object-chevron" aria-hidden="true" /></summary>
        <div className="catalog-detail">
          {item.description && <p>{item.description}</p>}
          {item.columns.map((column) => <button
            className="column-item" key={column.name} disabled={disabled} onClick={() => insert(quote(column.name))}
            title={column.description ? `插入 ${column.name} · ${column.description}` : `插入 ${column.name}`}
          ><code>{column.name}</code><small>{column.type}</small></button>)}
          <button className="text-button" onClick={() => insert(objectNameParts(item.name).map(quote).join('.'))} disabled={disabled}>插入对象名</button>
        </div>
      </details>)}
    </div>
  </aside>
}

export function ResultTable({ result, demo }: { result: QueryResult; demo: boolean }) {
  const [copyState, setCopyState] = useState<'ready' | 'copied' | 'error'>('ready')
  const isNumber = (type: string) => /number|decimal|numeric|float|double|int|real/i.test(type)

  async function copy() {
    try {
      await navigator.clipboard.writeText(JSON.stringify({ columns: result.columns, rows: result.rows }, null, 2))
      setCopyState('copied')
    } catch {
      setCopyState('error')
    }
  }

  return <>
    <div className="result-meta">
      <span className="result-success"><Check size={15} aria-hidden="true" />{demo ? '示例结果' : '执行成功'}</span>
      <span>{result.row_count.toLocaleString()} 行</span>
      <span>{result.execution_ms.toLocaleString()} ms</span>
      {result.truncated && <span className="tag warning" title="已达到行数或响应大小上限">结果已截断</span>}
      <button className="button subtle copy-result" onClick={() => void copy()} aria-live="polite">
        {copyState === 'copied' ? <Check size={15} /> : <Copy size={15} />}
        {copyState === 'copied' ? '已复制' : copyState === 'error' ? '复制失败，重试' : '复制 JSON'}
      </button>
    </div>
    <div className="table-scroll" tabIndex={0} aria-label="查询结果表格">
      <table>
        <thead><tr>{result.columns.map((column, index) => <th key={`${column.name}-${index}`} scope="col" className={isNumber(column.type) ? 'numeric' : ''}>
          <span>{column.name}</span><small>{column.type}</small>
        </th>)}</tr></thead>
        <tbody>
          {result.rows.map((row, index) => <tr key={index}>
            {row.map((value, cellIndex) => <td key={cellIndex} className={isNumber(result.columns[cellIndex]?.type ?? '') ? 'numeric' : ''}>
              {value === null ? <em>NULL</em> : String(value)}
            </td>)}
          </tr>)}
          {result.rows.length === 0 && <tr><td colSpan={Math.max(result.columns.length, 1)} className="table-empty">没有匹配的记录</td></tr>}
        </tbody>
      </table>
    </div>
    <div className="result-footer"><span>查询 ID</span><code>{result.query_id}</code></div>
  </>
}

export function ConnectionDialog({ open, setOpen, setSession, useDemo, disconnect, connected }: {
  open: boolean; setOpen: (value: boolean) => void; setSession: (session: Session) => void
  useDemo: () => void; disconnect: () => void; connected: boolean
}) {
  const [token, setToken] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  function changeOpen(value: boolean) {
    if (pending) return
    if (!value) { setToken(''); setError(null) }
    setOpen(value)
  }

  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (pending || !token.trim()) return
    setPending(true)
    setError(null)
    try {
      const client = liveClient(token.trim())
      const sources = await client.datasources()
      setSession({ mode: 'live', client, sources })
      setToken('')
      setOpen(false)
    } catch (failure) {
      setError(describeError(failure))
    } finally {
      setPending(false)
    }
  }

  return <Dialog.Root open={open} onOpenChange={changeOpen}>
    <Dialog.Portal>
      <Dialog.Overlay className="dialog-overlay" />
      <Dialog.Content className="dialog connection">
        <div className="dialog-heading">
          <div className="empty-icon"><KeyRound size={20} aria-hidden="true" /></div>
          <Dialog.Close className="icon-button" aria-label="关闭" disabled={pending}><X size={19} /></Dialog.Close>
        </div>
        <Dialog.Title>连接 QueryGate</Dialog.Title>
        <Dialog.Description>使用配置中的客户端 token 访问授权数据源。Token 仅保留在当前页面会话中。</Dialog.Description>
        <form onSubmit={(event) => void connect(event)}>
          <label htmlFor="client-token">客户端 token</label>
          <input id="client-token" type="password" autoComplete="off" spellCheck={false} disabled={pending}
            required value={token} placeholder="输入 QueryGate token" onChange={(event) => setToken(event.target.value)} />
          {error && <ErrorNotice error={error} />}
          <button className="button primary full" type="submit" disabled={pending || !token.trim()}>
            {pending && <Busy />}{pending ? '正在连接' : '连接服务'}
          </button>
        </form>
        <div className="connection-options">
          <button className="text-button" disabled={pending} onClick={() => { setToken(''); setError(null); useDemo() }}>使用示例数据</button>
          {connected && <button className="text-button quiet" disabled={pending} onClick={() => { setToken(''); setError(null); disconnect() }}>断开连接</button>}
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
