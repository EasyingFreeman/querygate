import '@fontsource-variable/geist'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'
import './styles.css'
import type { ReactCodeMirrorRef } from '@uiw/react-codemirror'
import { Braces, ChevronDown, CircleCheck, CodeXml, Database, GripHorizontal, History, Moon, Play, Plug, ShieldCheck, Sun, Table2, Terminal } from 'lucide-react'
import { lazy, StrictMode, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { Group, Panel, Separator } from 'react-resizable-panels'
import { ApiError, describeError } from './api'
import type { CatalogObject, Cell, Datasource, QueryRequest, QueryResult, QueryValidation } from './api'
import { demoSession, demoSql } from './demo'
import { Busy, Catalog, ConnectionDialog, Empty, ErrorNotice, ResultTable, engineName } from './panels'
import type { Session } from './panels'

type Theme = 'light' | 'dark'
type Operation = 'validate' | 'execute'
interface Draft { sql: string; reason: string; params: string; maxRows: string }

const emptyDraft: Draft = { sql: '', reason: '', params: '', maxRows: '100' }
const SqlEditor = lazy(() => import('./SqlEditor'))
const Admin = lazy(() => import('./Admin'))
const HistoryPage = lazy(() => import('./HistoryPage'))

function isHistoryPage() {
  return new URLSearchParams(location.search).get('view') === 'history'
}

function initialTheme(): Theme {
  try { if (localStorage.getItem('querygate-theme') === 'dark') return 'dark' } catch { /* Theme remains session-local. */ }
  return 'light'
}

function queryRequest(draft: Draft, source: Datasource): QueryRequest {
  if (!draft.sql.trim()) throw new ApiError('SQL_REQUIRED', '请输入 SQL 后再运行。')
  const reason = draft.reason.trim()
  if (!reason) throw new ApiError('REASON_REQUIRED', '请填写这次调用的原因。')
  const maxRows = Number(draft.maxRows)
  if (!Number.isSafeInteger(maxRows) || maxRows < 1) throw new ApiError('INVALID_MAX_ROWS', '最大行数须为正整数。')
  let params: QueryRequest['params'] = null
  if (draft.params.trim()) {
    try {
      const input: unknown = JSON.parse(draft.params)
      const positional = Array.isArray(input)
      if (input === null || typeof input !== 'object' || positional !== (source.dialect === 'tsql')) throw new Error()
      const items = Object.values(input)
      const isScalar = (item: unknown): item is Cell => item === null || typeof item === 'string'
        || typeof item === 'boolean' || (typeof item === 'number' && Number.isFinite(item)
          && (!Number.isInteger(item) || Number.isSafeInteger(item)))
      if (!items.every(isScalar)) throw new Error()
      params = input as QueryRequest['params']
    } catch {
      throw new ApiError('INVALID_PARAMS', source.dialect === 'tsql'
        ? 'SQL Server 参数须为 JSON 标量数组。大整数请用字符串。'
        : 'Oracle 参数须为 JSON 对象，值只能是标量。大整数请用字符串。')
    }
  }
  return { datasource_id: source.id, sql: draft.sql, reason, params, max_rows: maxRows }
}

function App() {
  const [session, setSession] = useState<Session | null>(() => new URLSearchParams(location.search).get('demo') === '1' ? demoSession() : null)
  const [revision, setRevision] = useState(0)
  const [theme, setTheme] = useState(initialTheme)
  const [connecting, setConnecting] = useState(false)
  const [showHistory, setShowHistory] = useState(isHistoryPage)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { localStorage.setItem('querygate-theme', theme) } catch { /* Theme remains session-local. */ }
  }, [theme])

  useEffect(() => {
    function applyLocation() { setShowHistory(isHistoryPage()) }
    window.addEventListener('popstate', applyLocation)
    return () => window.removeEventListener('popstate', applyLocation)
  }, [])

  function navigate(historyPage: boolean) {
    if (historyPage === showHistory) return
    const url = new URL(location.href)
    if (historyPage) url.searchParams.set('view', 'history')
    else url.searchParams.delete('view')
    window.history.pushState(null, '', url)
    setShowHistory(historyPage)
  }

  function changeSession(next: Session | null) {
    setSession(next)
    setRevision((current) => current + 1)
    setConnecting(false)
  }

  return <div className="app">
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark"><Terminal size={21} strokeWidth={2.4} aria-hidden="true" /></span>
        <strong>QueryGate</strong>
      </div>
      <nav className="workspace-nav" aria-label="工作台导航">
        <button className="button subtle" aria-current={!showHistory ? 'page' : undefined} aria-label="查询工作台"
          onClick={() => navigate(false)}><CodeXml size={16} /><span>查询工作台</span></button>
        <button className="button subtle" aria-current={showHistory ? 'page' : undefined} aria-label="查询历史"
          onClick={() => navigate(true)}><History size={16} /><span>查询历史</span></button>
      </nav>
      <div className="top-actions">
        {session?.mode === 'demo' && <span className="tag demo" title="示例模式使用模拟预检和数据">示例模式</span>}
        <a className="button subtle manage-button" href="?setup=1" aria-label="数据源管理" title="数据源管理"><Database size={16} /> <span>数据源管理</span></a>
        <button className="icon-button theme-button" aria-label={theme === 'dark' ? '切换浅色主题' : '切换深色主题'}
          title={theme === 'dark' ? '切换浅色主题' : '切换深色主题'} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>
          {theme === 'dark' ? <Sun size={18} /> : <Moon size={17} />}
        </button>
        <button className="button secondary connection-button" aria-label="管理连接" onClick={() => setConnecting(true)}>
          <Plug size={16} /> <span>{session?.mode === 'live' ? '已连接' : '连接服务'}</span>
        </button>
      </div>
    </header>
    {session && session.sources.length > 0 && <div hidden={showHistory}>
      <Workbench key={revision} session={session} theme={theme} shortcuts={!connecting && !showHistory} />
    </div>}
    {showHistory && session
      ? <Suspense fallback={<div className="inline-status history-progress"><Busy /> 正在打开查询历史</div>}>
        <HistoryPage key={revision} client={session.client} sources={session.sources} />
      </Suspense>
      : (!session || session.sources.length === 0) && <main className="welcome">
        <Empty icon={<CodeXml size={25} />} title={session ? '暂无可访问的数据源' : showHistory ? '连接后查看查询历史' : '连接到 QueryGate'}
          detail={session ? '使用已获数据源授权的客户端 token。' : undefined} />
        <button className="button primary" onClick={() => setConnecting(true)}><Plug size={16} />连接服务</button>
      </main>}
    <ConnectionDialog open={connecting} setOpen={setConnecting} setSession={changeSession}
      useDemo={() => changeSession(demoSession())} disconnect={() => changeSession(null)} connected={session?.mode === 'live'} />
  </div>
}

function Workbench({ session, theme, shortcuts }: { session: Session; theme: Theme; shortcuts: boolean }) {
  const { sources, client, mode, initialResult } = session
  const [sourceId, setSourceId] = useState(sources[0].id)
  const source = sources.find((item) => item.id === sourceId) ?? sources[0]
  const [drafts, setDrafts] = useState(() => new Map<string, Draft>())
  const draft = drafts.get(source.id) ?? (mode === 'demo' ? { ...emptyDraft, sql: demoSql[source.id], reason: '查看示例数据摘要' } : emptyDraft)
  const [objects, setObjects] = useState<CatalogObject[]>([])
  const [catalogLoading, setCatalogLoading] = useState(true)
  const [catalogError, setCatalogError] = useState<ApiError | null>(null)
  const [showParams, setShowParams] = useState(false)
  const [busy, setBusy] = useState<Operation | null>(null)
  const [validation, setValidation] = useState<QueryValidation | null>(null)
  const [validationError, setValidationError] = useState<ApiError | null>(null)
  const [result, setResult] = useState<QueryResult | null>(initialResult ?? null)
  const [queryError, setQueryError] = useState<ApiError | null>(null)
  const editor = useRef<ReactCodeMirrorRef>(null)
  const inFlight = useRef<AbortController | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setObjects([])
    setCatalogLoading(true)
    setCatalogError(null)
    void client.catalog(source.id, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setObjects(value) })
      .catch((error) => { if (!controller.signal.aborted) setCatalogError(describeError(error)) })
      .finally(() => { if (!controller.signal.aborted) setCatalogLoading(false) })
    return () => controller.abort()
  }, [client, source.id])

  useEffect(() => () => inFlight.current?.abort(), [])

  function updateDraft(changes: Partial<Draft>) {
    setDrafts((previous) => new Map(previous).set(source.id, { ...(previous.get(source.id) ?? draft), ...changes }))
    setValidation(null)
    setValidationError(null)
  }

  function selectSource(id: string) {
    if (inFlight.current || id === source.id) return
    setSourceId(id)
    setObjects([])
    setCatalogLoading(true)
    setCatalogError(null)
    setValidation(null)
    setValidationError(null)
    setResult(null)
    setQueryError(null)
  }

  function insert(text: string) {
    const view = editor.current?.view
    if (!view || inFlight.current) return
    const { from, to } = view.state.selection.main
    view.dispatch({ changes: { from, to, insert: text }, selection: { anchor: from + text.length } })
    view.focus()
  }

  const submit = useCallback(async (operation: Operation) => {
    if (inFlight.current) return
    const controller = new AbortController()
    inFlight.current = controller
    setBusy(operation)
    setValidationError(null)
    if (operation === 'validate') setValidation(null)
    else { setResult(null); setQueryError(null) }
    try {
      const payload = queryRequest(draft, source)
      if (operation === 'validate') {
        const value = await client.validate(payload, controller.signal)
        if (!controller.signal.aborted) setValidation(value)
      } else {
        const value = await client.execute(payload, controller.signal)
        if (!controller.signal.aborted) setResult(value)
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        const notice = describeError(error)
        if (operation === 'validate') setValidationError(notice)
        else setQueryError(notice)
      }
    } finally {
      if (inFlight.current === controller) inFlight.current = null
      if (!controller.signal.aborted) setBusy(null)
    }
  }, [client, draft, source])

  useEffect(() => {
    if (!shortcuts) return
    function runShortcut(event: KeyboardEvent) {
      if (!event.isComposing && !event.repeat && event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey) {
        event.preventDefault()
        void submit('execute')
      }
    }
    window.addEventListener('keydown', runShortcut)
    return () => window.removeEventListener('keydown', runShortcut)
  }, [shortcuts, submit])

  return <div className="workbench">
    <Catalog key={source.id} sources={sources} current={source} select={selectSource} objects={objects}
      loading={catalogLoading} error={catalogError} insert={insert} disabled={Boolean(busy)} />
    <main className="workspace">
      <div className="workspace-heading">
        <div className="workspace-title">
          <h1>SQL 查询</h1>
          <div className="context"><code>{source.id}</code><span className="context-separator">/</span><span>{engineName[source.engine]}</span>
            <span className="tag readonly"><ShieldCheck size={14} />只读</span></div>
        </div>
        <div className="query-actions">
          <button className="button secondary" disabled={Boolean(busy) || !draft.sql.trim()} onClick={() => void submit('validate')}>
            {busy === 'validate' ? <Busy /> : <ShieldCheck size={17} />} 预检
          </button>
          <button className="button primary run-button" disabled={Boolean(busy) || !draft.sql.trim()} onClick={() => void submit('execute')}>
            {busy === 'execute' ? <Busy /> : <Play size={16} fill="currentColor" />} {busy === 'execute' ? '运行中' : '运行查询'} <kbd>Ctrl ↵</kbd>
          </button>
        </div>
      </div>
      <Group className="query-panes" orientation="vertical">
        <Panel id="query-editor" defaultSize="46%" minSize="210px">
          <section className="pane editor-pane" aria-label="查询编辑器">
            <div className="pane-header">
              <div><CodeXml size={18} /><h2>编辑器</h2></div>
              <button className={`button subtle params-button ${showParams ? 'active' : ''}`} onClick={() => setShowParams(!showParams)}
                aria-expanded={showParams} aria-controls="query-parameters"><Braces size={16} />参数<ChevronDown size={14} /></button>
            </div>
            <label className="query-reason">调用原因
              <input maxLength={500} required value={draft.reason} onChange={(event) => updateDraft({ reason: event.target.value })}
                disabled={Boolean(busy)} placeholder="例如，核对本月交易汇总" />
            </label>
            <div className="editor-window">
              <Suspense fallback={<div className="inline-status"><Busy /> 正在加载编辑器</div>}>
                <SqlEditor key={source.id} editorRef={editor} value={draft.sql} onChange={(value) => updateDraft({ sql: value })}
                  editable={!busy} dialect={source.dialect} catalog={objects} theme={theme} />
              </Suspense>
            </div>
            {showParams && <label className="parameters" id="query-parameters">
              <span>{source.dialect === 'tsql' ? '位置参数 · ?' : '命名参数 · :name'}</span>
              <textarea value={draft.params} onChange={(event) => updateDraft({ params: event.target.value })} disabled={Boolean(busy)}
                spellCheck={false} placeholder={source.dialect === 'tsql' ? '[100]' : '{"minimum": 100}'} rows={2} />
            </label>}
            {validationError && <div className="validation-notice"><ErrorNotice error={validationError} /></div>}
            <div className="editor-footer">
              <label className="row-limit">最大行数 <input type="number" min="1" step="1" value={draft.maxRows}
                onChange={(event) => updateDraft({ maxRows: event.target.value })} disabled={Boolean(busy)} /></label>
              {validation && <span className="validation-success" role="status" title={`响应上限 ${validation.max_response_bytes.toLocaleString()} 字节`}>
                <CircleCheck size={15} /> {mode === 'demo' ? '示例预检' : '预检通过'} · {validation.max_rows} 行 · {validation.timeout_seconds} 秒
              </span>}
            </div>
          </section>
        </Panel>
        <Separator className="pane-separator" aria-label="调整编辑器和结果区高度"><GripHorizontal size={21} aria-hidden="true" /></Separator>
        <Panel id="query-results" defaultSize="54%" minSize="180px">
          <section className="pane results-pane" aria-label="查询结果" aria-busy={busy === 'execute'}>
            <div className="pane-header"><div><Table2 size={18} /><h2>查询结果</h2></div></div>
            {busy === 'execute' && <Empty title="查询执行中" icon={<Busy />} />}
            {queryError && <div className="result-error"><ErrorNotice error={queryError} /></div>}
            {result && <ResultTable key={result.query_id} result={result} demo={mode === 'demo'} />}
            {busy !== 'execute' && !queryError && !result && <Empty title="暂无结果" detail="运行 SQL 后在此查看返回的记录。" icon={<Table2 size={24} />} />}
          </section>
        </Panel>
      </Group>
    </main>
  </div>
}

const setupMode = new URLSearchParams(location.search).get('setup') === '1'
const setupToken = setupMode ? new URLSearchParams(location.hash.slice(1)).get('session') ?? '' : ''
if (setupMode && location.hash) history.replaceState(null, '', location.pathname + location.search)
document.documentElement.dataset.theme = initialTheme()

createRoot(document.getElementById('root')!).render(<StrictMode>
  {setupMode ? <Suspense fallback={<p className="inline-status"><Busy /> 正在打开管理页</p>}><Admin token={setupToken} /></Suspense> : <App />}
</StrictMode>)
