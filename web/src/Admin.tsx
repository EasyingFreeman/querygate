import { ArrowLeft, Clipboard, Database, Filter, ListChecks, Plug, Plus, Save, ShieldCheck, Terminal, Users } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { databaseMessage } from './api'
import type { MutationRecord, MutationState, SqlPolicy, WritePolicy } from './api'
import ClientManager from './ClientManager'
import MutationApprovals from './MutationApprovals'
import { Busy } from './panels'

type ObjectPolicy = 'allowlist' | 'denylist'
interface Engine {
  id: string
  label: string
  fields: { key: string; label: string; masked: boolean }[]
}
export interface Source {
  id: string
  engine: string
  description: string
  enabled: boolean
  object_policy: ObjectPolicy
  write_audit_table: string | null
  objects: string[]
  configured_fields: string[]
  client_ids: string[]
}
export interface ClientGrant {
  id: string
  datasource_ids: string[]
  has_token: boolean
  sql_policy: SqlPolicy | null
  write_policy: WritePolicy
}
interface AdminConfig {
  engines: Engine[]
  clients: ClientGrant[]
  datasources: Source[]
}
interface Draft extends Omit<Source, 'objects'> {
  rules: string
  credentials: Record<string, string>
  existing: boolean
}

function draftFor(config: AdminConfig, source?: Source): Draft {
  return {
    id: source?.id ?? '', engine: source?.engine ?? config.engines[0].id,
    description: source?.description ?? '', enabled: source?.enabled ?? true,
    object_policy: source?.object_policy ?? 'allowlist', rules: source?.objects.join('\n') ?? '',
    write_audit_table: source?.write_audit_table ?? '',
    client_ids: source?.client_ids ?? (config.clients.length === 1 ? [config.clients[0].id] : []),
    configured_fields: source?.configured_fields ?? [], credentials: {}, existing: Boolean(source),
  }
}

const adminSessionKey = 'querygate-admin-session'

function rememberSession(token?: string) {
  try {
    if (token === undefined) return sessionStorage.getItem(adminSessionKey) ?? ''
    if (token) sessionStorage.setItem(adminSessionKey, token)
    else sessionStorage.removeItem(adminSessionKey)
  } catch { /* Management remains available in the current page. */ }
  return ''
}

async function adminRequest<T>(token: string, path: string, method: 'GET' | 'POST' | 'DELETE' = 'GET', data?: object): Promise<T> {
  const response = await fetch(`/admin/${path}`, {
    method, cache: 'no-store', credentials: 'omit', redirect: 'error',
    headers: { Authorization: `Bearer ${token}`, ...(data ? { 'Content-Type': 'application/json' } : {}) },
    body: data ? JSON.stringify(data) : undefined,
  })
  if (response.status === 204) return undefined as T
  const result = await response.json().catch(() => null)
  if (!response.ok || !result) {
    if (response.status === 401) throw new Error('管理密钥无效或已过期。请从当前启动终端复制管理密钥。')
    if (response.status === 404 && path === 'config') throw new Error('请通过服务电脑上的 QueryGate 查询工作台打开数据源管理。')
    const fallback = typeof result?.detail === 'string' ? result.detail : result?.message ?? '无法完成管理操作。'
    throw new Error(typeof result?.code === 'string' ? `${result.code} · ${databaseMessage(result.code, fallback)}` : fallback)
  }
  return result as T
}

export default function Admin({ token: initialToken }: { token: string }) {
  const [token, setToken] = useState(() => initialToken || rememberSession())
  const [tokenInput, setTokenInput] = useState('')
  const [section, setSection] = useState<'sources' | 'clients' | 'mutations'>(() =>
    new URLSearchParams(location.search).get('section') === 'mutations' ? 'mutations' : 'sources')
  const [config, setConfig] = useState<AdminConfig | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ message: string; failed: boolean } | null>(null)
  const [copying, setCopying] = useState(false)
  const [showInput, setShowInput] = useState(false)
  const listMutations = useCallback((state: MutationState | '') =>
    adminRequest<MutationRecord[]>(token, state ? `mutations?state=${state}` : 'mutations'), [token])

  useEffect(() => {
    if (!token) return
    let active = true
    adminRequest<AdminConfig>(token, 'config').then((next) => {
      if (!active) return
      rememberSession(token)
      setTokenInput('')
      setConfig(next)
      const requested = new URLSearchParams(location.search).get('source')
      setDraft(draftFor(next, next.datasources.find((source) => source.id === requested) ?? next.datasources[0]))
    }).catch((reason: unknown) => {
      if (active) {
        rememberSession('')
        setError(reason instanceof Error ? reason.message : '无法读取管理配置。')
        setToken('')
      }
    })
    return () => { active = false }
  }, [token])

  function select(source?: Source) {
    if (config) setDraft(draftFor(config, source))
    setShowInput(false)
    setError('')
    setNotice('')
    setTestResult(null)
  }

  function change(values: Partial<Draft>) {
    setDraft((previous) => previous && { ...previous, ...values })
    setNotice('')
    setTestResult(null)
  }

  function switchSection(next: 'sources' | 'clients' | 'mutations') {
    setSection(next)
    setError('')
    setNotice('')
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!draft || saving) return
    setSaving(true)
    setError('')
    setNotice('')
    try {
      await adminRequest(token, 'datasources', 'POST', {
        id: draft.id.trim(), engine: draft.engine, description: draft.description.trim(), enabled: draft.enabled,
        object_policy: draft.object_policy, existing: draft.existing, client_ids: draft.client_ids,
        write_audit_table: draft.write_audit_table?.trim() ?? '',
        objects: draft.rules.split(/\r?\n/).map((line) => line.trim()).filter(Boolean),
        credentials: Object.fromEntries(Object.entries(draft.credentials).filter(([, value]) => value)),
      })
      change({ credentials: {}, existing: true })
      const next = await adminRequest<AdminConfig>(token, 'config')
      setConfig(next)
      setDraft(draftFor(next, next.datasources.find((source) => source.id === draft.id.trim())))
      setShowInput(false)
      setNotice(`已保存数据源 ${draft.id.trim()}。`)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '保存未完成。')
    } finally {
      setSaving(false)
    }
  }

  async function testConnection() {
    if (!draft || testing) return
    setTesting(true)
    setTestResult(null)
    try {
      const result = await adminRequest<{ object_count: number }>(token, 'test-connection', 'POST', {
        id: draft.existing ? draft.id : undefined, engine: draft.engine,
        credentials: Object.fromEntries(Object.entries(draft.credentials).filter(([, value]) => value)),
      })
      setTestResult({ message: `连接成功。已读取数据库目录，发现 ${result.object_count} 个表和视图。`, failed: false })
    } catch (failure) {
      setTestResult({ message: failure instanceof Error ? failure.message : '连接测试失败。', failed: true })
    } finally {
      setTesting(false)
    }
  }

  async function updateClients(path: string, method: 'POST' | 'DELETE', message: string, data?: object) {
    setNotice('')
    setError('')
    try {
      await adminRequest(token, path, method, data)
      const next = await adminRequest<AdminConfig>(token, 'config')
      setConfig(next)
      setDraft((previous) => {
        if (!previous) return draftFor(next, next.datasources[0])
        const source = previous.existing ? next.datasources.find((item) => item.id === previous.id) : undefined
        return { ...previous, client_ids: source?.client_ids ?? previous.client_ids.filter((id) => next.clients.some((client) => client.id === id)) }
      })
      setNotice(message)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '无法更新客户端。')
      throw failure
    }
  }

  async function copyToken(client: string) {
    setCopying(true)
    setNotice('')
    setError('')
    try {
      const value = await adminRequest<{ token: string }>(token, `clients/${encodeURIComponent(client)}/token`, 'POST')
      await navigator.clipboard.writeText(value.token)
      setNotice(`已复制 ${client} 的客户端 token。`)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '无法复制客户端 token。')
    } finally {
      setCopying(false)
    }
  }

  const profile = config?.engines.find((entry) => entry.id === draft?.engine)
  return <div className="app admin-app">
    <header className="topbar">
      <div className="brand"><span className="brand-mark"><Terminal size={21} aria-hidden="true" /></span><strong>QueryGate</strong>
        <span className="brand-divider" aria-hidden="true" /><span className="brand-context">数据源管理</span></div>
      <div className="top-actions">
        <a className="button subtle" href="./"><ArrowLeft size={16} /> 查询工作台</a>
      </div>
    </header>
    {!config || !draft ? <main className="admin-loading">
      {token ? <p className="inline-status" role="status"><Busy /> 正在读取配置</p> :
        <form className="admin-card admin-login" onSubmit={(event) => {
          event.preventDefault()
          setError('')
          setToken(tokenInput.trim())
        }}>
          <h1>数据源管理</h1>
          <p className="admin-hint">请使用 QueryGate 启动终端中的 <code>QueryGate admin code</code>。</p>
          <label>管理密钥<input type="password" required autoComplete="off" value={tokenInput} onChange={(event) => setTokenInput(event.target.value)} /></label>
          {error && <p className="notice error" role="alert">{error}</p>}
          <button className="button primary" type="submit" disabled={!tokenInput.trim()}>进入管理页</button>
        </form>}
    </main> : <div className="admin-layout">
      <aside className="catalog admin-catalog" aria-label="管理导航">
        <button type="button" className={`button ${section === 'sources' ? 'secondary' : 'subtle'} full`}
          onClick={() => switchSection('sources')} disabled={saving || testing}><Database size={16} /> 数据源</button>
        <button type="button" className={`button ${section === 'clients' ? 'secondary' : 'subtle'} full`}
          onClick={() => switchSection('clients')} disabled={saving || testing}><Users size={16} /> 授权客户端</button>
        <button type="button" className={`button ${section === 'mutations' ? 'secondary' : 'subtle'} full`}
          onClick={() => switchSection('mutations')} disabled={saving || testing}><ListChecks size={16} /> 写入审批</button>
        {section === 'sources' && <>
        <h2>数据源</h2>
        <button type="button" className="button secondary full" onClick={() => select()} disabled={saving || testing}><Plus size={16} /> 新增数据源</button>
        <div className="admin-sources">
          {config.datasources.map((source) => <button type="button" className={`admin-source ${draft.existing && draft.id === source.id ? 'selected' : ''}`}
            key={source.id} onClick={() => select(source)} disabled={saving || testing} aria-pressed={draft.existing && draft.id === source.id}>
            <Database size={17} aria-hidden="true" />
            <span><strong>{source.id}</strong><small>{config.engines.find((entry) => entry.id === source.engine)?.label ?? source.engine}</small></span>
            <span className={`tag ${source.enabled ? 'success' : ''}`}>{source.enabled ? '启用' : '停用'}</span>
          </button>)}
          {config.datasources.length === 0 && <p className="quiet">还没有配置数据源。</p>}
        </div>
        </>}
      </aside>
      <main className="admin-content">
        {section === 'mutations' ? <MutationApprovals list={listMutations}
          decide={(id, action) => adminRequest<MutationRecord>(token, `mutations/${encodeURIComponent(id)}/${action}`, 'POST')} />
          : section === 'clients' ? <>
          <h1>客户端管理</h1>
          <ClientManager clients={config.clients} sources={config.datasources}
            save={(client) => updateClients('clients', 'POST', `已保存客户端 ${client.id}。`, {
              id: client.id, previous_id: client.previousId, datasource_ids: client.datasourceIds,
              sql_policy: client.sqlPolicy, write_policy: client.writePolicy,
            })}
            remove={(id) => updateClients(`clients/${encodeURIComponent(id)}`, 'DELETE', `已删除客户端 ${id}。`)}
            copyToken={copyToken} />
          {error && <p className="notice error" role="alert">{error}</p>}
          {notice && <p className="admin-hint" role="status">{notice}</p>}
        </> : <>
        <h1>{draft.existing ? draft.id : '新增数据源'}</h1>
        <form onSubmit={(event) => void save(event)}>
          <fieldset disabled={saving || testing} className="admin-form">
            <section className="admin-card" aria-label="连接配置">
              <h2><Database size={17} /> 连接配置</h2>
              <div className="admin-fields">
                <label>数据源 ID<input required value={draft.id} readOnly={draft.existing} pattern="[A-Za-z][A-Za-z0-9_]{0,62}"
                  placeholder="finance" onChange={(event) => change({ id: event.target.value })} /></label>
                <label>数据库类型<select value={draft.engine} disabled={draft.existing}
                  onChange={(event) => change({ engine: event.target.value, credentials: {}, write_audit_table: '' })}>
                  {config.engines.map((engine) => <option key={engine.id} value={engine.id}>{engine.label}</option>)}
                </select></label>
                <label className="admin-wide">用途说明 <input value={draft.description} onChange={(event) => change({ description: event.target.value })} /></label>
                {profile?.fields.map((field) => <label className={profile.fields.length === 1 ? 'admin-wide' : ''} key={field.key}>
                  {field.label}<input type={field.masked && !showInput ? 'password' : 'text'} autoComplete="off" spellCheck={false}
                    value={draft.credentials[field.key] ?? ''} required={draft.enabled && !draft.configured_fields.includes(field.key)}
                    placeholder={draft.configured_fields.includes(field.key) ? '已配置，留空保留' : undefined}
                    onChange={(event) => change({ credentials: { ...draft.credentials, [field.key]: event.target.value } })} />
                </label>)}
              </div>
              <div className="admin-checks">
                <label><input type="checkbox" checked={draft.enabled} onChange={(event) => change({ enabled: event.target.checked })} /> 启用此数据源</label>
                <label><input type="checkbox" checked={showInput} onChange={(event) => setShowInput(event.target.checked)} /> 显示本次输入的连接值</label>
              </div>
              <button type="button" className="button secondary" onClick={() => void testConnection()}>
                {testing ? <Busy /> : <Plug size={16} />} 测试连接
              </button>
              {testResult && <p className={testResult.failed ? 'notice error' : 'admin-hint'} role={testResult.failed ? 'alert' : 'status'}>
                {testResult.message}
              </p>}
            </section>

            <section className="admin-card" aria-label="对象授权">
              <h2><Filter size={17} /> 表和视图</h2>
              <div className="policy-options" role="radiogroup" aria-label="对象授权模式">
                <label><input type="radio" name="policy" checked={draft.object_policy === 'allowlist'} onChange={() => change({ object_policy: 'allowlist' })} /> 白名单</label>
                <label><input type="radio" name="policy" checked={draft.object_policy === 'denylist'} onChange={() => change({ object_policy: 'denylist' })} /> 黑名单</label>
              </div>
              <label>{draft.object_policy === 'allowlist' ? '允许访问的对象' : '禁止访问的对象'}
                <textarea value={draft.rules} onChange={(event) => change({ rules: event.target.value })}
                  rows={5} spellCheck={false} placeholder="每行一个 Schema.对象，例如 dbo.v_orders" />
              </label>
              <p className="admin-hint">{draft.object_policy === 'allowlist' ? '有条目时，只开放列出的表和视图。' : '有条目时，排除列出的表和视图。'} 空清单开放数据库账号可读的表和视图。</p>
            </section>

            <section className="admin-card" aria-label="写入审计">
              <h2><ShieldCheck size={17} /> 写入审计</h2>
              <label>数据库审计表 <input value={draft.write_audit_table ?? ''} spellCheck={false}
                placeholder={draft.engine === 'oracle' ? 'QUERYGATE_AUDIT.MUTATIONS' : 'dbo.QueryGateMutationAudit'}
                onChange={(event) => change({ write_audit_table: event.target.value })} /></label>
              <p className="admin-hint">由 DBA 在目标数据库建立审计表。留空时，这个数据源只提供查询。</p>
            </section>

            <section className="admin-card" aria-label="授权客户端">
              <div className="admin-client">
                <h2>授权客户端</h2>
                <button type="button" className="button subtle" onClick={() => switchSection('clients')}><Users size={16} /> 管理客户端</button>
              </div>
              {config.clients.map(({ id, has_token }) => <div className="admin-client" key={id}>
                <label><input type="checkbox" checked={draft.client_ids.includes(id)}
                  onChange={(event) => change({ client_ids: event.target.checked ? [...draft.client_ids, id] : draft.client_ids.filter((client) => client !== id) })} /> {id}</label>
                <button type="button" className="button subtle" disabled={copying || !has_token} onClick={() => void copyToken(id)}><Clipboard size={15} /> 复制 token</button>
              </div>)}
            </section>
            {error && <div className="notice error" role="alert">{error}</div>}
            {notice && <p className="admin-hint" role="status">{notice}</p>}
            <button className="button primary" type="submit">
              {saving ? <Busy /> : <Save size={17} />} 保存数据源
            </button>
          </fieldset>
        </form>
        </>}
      </main>
    </div>}
  </div>
}
