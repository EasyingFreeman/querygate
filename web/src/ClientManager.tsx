import { Clipboard, Plus, Save, ShieldCheck, Trash2, UserRound } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent } from 'react'
import type { ClientGrant, Source } from './Admin'
import { defaultSqlPolicy, sqlPolicyLabels } from './api'
import type { SqlPolicy, WriteOperation, WritePolicy } from './api'
import { Busy } from './panels'

interface ClientDraft {
  id: string
  previousId?: string
  datasourceIds: string[]
  sqlPolicy: SqlPolicy | null
  requiresApproval: boolean
  grants: GrantDraft[]
}

interface GrantDraft {
  id: string
  datasource: string
  table: string
  operations: WriteOperation[]
  primaryKey: string
  columns: string
}

interface ClientUpdate {
  id: string
  previousId?: string
  datasourceIds: string[]
  sqlPolicy: SqlPolicy | null
  writePolicy: WritePolicy
}

function draftFor(client?: ClientGrant): ClientDraft {
  if (!client) return { id: '', datasourceIds: [], sqlPolicy: defaultSqlPolicy, requiresApproval: true, grants: [] }
  const grants = Object.entries(client.write_policy.datasources).flatMap(([datasource, tables]) =>
    Object.entries(tables).map(([table, grant]) => ({
      id: crypto.randomUUID(), datasource, table, operations: grant.operations,
      primaryKey: grant.primary_key.join(', '), columns: grant.columns.join(', '),
    })))
  return {
    id: client.id, previousId: client.id, datasourceIds: client.datasource_ids, sqlPolicy: client.sql_policy,
    requiresApproval: client.write_policy.requires_approval, grants,
  }
}

export default function ClientManager({ clients, sources, save, remove, copyToken }: {
  clients: ClientGrant[]
  sources: Source[]
  save: (draft: ClientUpdate) => Promise<void>
  remove: (id: string) => Promise<void>
  copyToken: (id: string) => Promise<void>
}) {
  const [draft, setDraft] = useState<ClientDraft>(() => draftFor())
  const [busy, setBusy] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [formError, setFormError] = useState('')
  const selected = clients.find((client) => client.id === draft.previousId)
  const assigned = sources.filter((source) => draft.datasourceIds.includes(source.id))

  function select(client?: ClientGrant) {
    setDraft(draftFor(client))
    setConfirmDelete(false)
    setFormError('')
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setFormError('')
    const policy: WritePolicy = { requires_approval: draft.requiresApproval, datasources: Object.create(null) }
    for (const grant of draft.grants) {
      const table = grant.table.trim()
      const tables = policy.datasources[grant.datasource] ??= Object.create(null)
      if (Object.hasOwn(tables, table) || !grant.operations.length) {
        setFormError('每个数据源的表只需添加一次，并至少选择一种操作。')
        return
      }
      const names = (value: string) => value.split(',').map((name) => name.trim()).filter(Boolean)
      tables[table] = { operations: grant.operations, primary_key: names(grant.primaryKey), columns: names(grant.columns) }
    }
    setBusy(true)
    try {
      const next = { ...draft, id: draft.id.trim() }
      await save({
        id: next.id, previousId: next.previousId, datasourceIds: next.datasourceIds, sqlPolicy: next.sqlPolicy, writePolicy: policy,
      })
      setDraft({ ...next, previousId: next.id })
    } catch { /* The management page displays the error. */ }
    finally { setBusy(false) }
  }

  function changeGrant(id: string, update: Partial<GrantDraft>) {
    setDraft((current) => ({ ...current, grants: current.grants.map((grant) => grant.id === id ? { ...grant, ...update } : grant) }))
  }

  async function deleteSelected() {
    if (!selected) return
    setBusy(true)
    try {
      await remove(selected.id)
      select()
    } catch { /* The management page displays the error. */ }
    finally { setBusy(false) }
  }

  return <div className="admin-client-layout">
    <section className="admin-card" aria-label="客户端列表">
      <h2>授权客户端</h2>
      <button className="button secondary" type="button" onClick={() => select()} disabled={busy}><Plus size={16} /> 新增客户端</button>
      {clients.map((client) => <button key={client.id} type="button"
        className={`admin-source ${selected?.id === client.id ? 'selected' : ''}`}
        onClick={() => select(client)} disabled={busy} aria-pressed={selected?.id === client.id}>
        <UserRound size={16} aria-hidden="true" />
        <span><strong>{client.id}</strong><small>{client.datasource_ids.length} 个数据源</small></span>
      </button>)}
      {!clients.length && <p className="admin-hint">暂无客户端。</p>}
    </section>
    <form className="admin-card" onSubmit={(event) => void submit(event)}>
      <h2>{selected ? `编辑 ${selected.id}` : '新增客户端'}</h2>
      <fieldset className="admin-form" disabled={busy}>
        <label>客户端 ID<input required value={draft.id} pattern="[A-Za-z][A-Za-z0-9_]{0,62}" placeholder="例如 reporting_agent"
          onChange={(event) => setDraft({ ...draft, id: event.target.value })} /></label>
        <div className="admin-checks" role="group" aria-label="可访问的数据源">
          {sources.map((source) => <label key={source.id}>
            <input type="checkbox" checked={draft.datasourceIds.includes(source.id)} onChange={(event) => setDraft({
              ...draft, datasourceIds: event.target.checked
                ? [...draft.datasourceIds, source.id]
                : draft.datasourceIds.filter((id) => id !== source.id),
              grants: event.target.checked ? draft.grants : draft.grants.filter((grant) => grant.datasource !== source.id),
            })} /> {source.id}{source.enabled ? '' : '（停用）'}
          </label>)}
          {!sources.length && <p className="admin-hint">添加数据源后可在这里分配访问权限。</p>}
        </div>
        <section className="client-policy" aria-label="客户端 SQL 策略">
          <h3><ShieldCheck size={16} /> 查询策略</h3>
          <label className="policy-toggle"><input type="checkbox" checked={draft.sqlPolicy !== null} onChange={(event) =>
            setDraft({ ...draft, sqlPolicy: event.target.checked ? defaultSqlPolicy : null })} /> 使用客户端级 SQL 规则</label>
          {draft.sqlPolicy ? <div className="admin-checks">
            {(Object.keys(sqlPolicyLabels) as (keyof SqlPolicy)[]).map((key) => <label key={key}>
              <input type="checkbox" checked={draft.sqlPolicy![key]} onChange={(event) => setDraft({
                ...draft, sqlPolicy: { ...draft.sqlPolicy!, [key]: event.target.checked },
              })} /> {sqlPolicyLabels[key]}
            </label>)}
          </div> : <p className="admin-hint">继承已有数据源的 SQL 设置。</p>}
        </section>
        <section className="client-policy" aria-label="客户端写入策略">
          <h3>写入策略</h3>
          <label className="policy-toggle"><input type="checkbox" checked={draft.requiresApproval}
            onChange={(event) => setDraft({ ...draft, requiresApproval: event.target.checked })} /> UPDATE／INSERT 需要 Web 审批</label>
          {draft.grants.map((grant) => <div className="grant-row" key={grant.id}>
            <div className="admin-fields">
              <label>数据源<select value={grant.datasource} onChange={(event) => changeGrant(grant.id, { datasource: event.target.value })}>
                {assigned.map((source) => <option key={source.id} value={source.id}>{source.id}</option>)}
              </select></label>
              <label>表<input required value={grant.table} placeholder="dbo.Orders" onChange={(event) => changeGrant(grant.id, { table: event.target.value })} /></label>
              <label>主键列<input required value={grant.primaryKey} placeholder="OrderId" onChange={(event) => changeGrant(grant.id, { primaryKey: event.target.value })} /></label>
              <label>可写列（逗号分隔）<input required value={grant.columns} placeholder="Status, Remarks" onChange={(event) => changeGrant(grant.id, { columns: event.target.value })} /></label>
            </div>
            <div className="admin-client">
              <div className="admin-checks">{(['update', 'insert'] as const).map((operation) => <label key={operation}>
                <input type="checkbox" checked={grant.operations.includes(operation)} onChange={(event) => changeGrant(grant.id, {
                  operations: event.target.checked ? [...grant.operations, operation] : grant.operations.filter((item) => item !== operation),
                })} /> {operation.toUpperCase()}
              </label>)}</div>
              <button className="button subtle" type="button" onClick={() => setDraft({
                ...draft, grants: draft.grants.filter((item) => item.id !== grant.id),
              })}><Trash2 size={15} /> 移除</button>
            </div>
          </div>)}
          <button type="button" className="button secondary" disabled={!assigned.length} onClick={() => setDraft({
            ...draft, grants: [...draft.grants, {
              id: crypto.randomUUID(), datasource: assigned[0].id, table: '', operations: ['update'], primaryKey: '', columns: '',
            }],
          })}><Plus size={16} /> 添加写入授权</button>
        </section>
        {formError && <p className="notice error" role="alert">{formError}</p>}
        <div className="admin-form-actions">
          {selected?.has_token && <button type="button" className="button secondary" onClick={() => void copyToken(selected.id)}><Clipboard size={15} /> 复制 token</button>}
          {selected && <button type="button" className="button subtle" onClick={() => setConfirmDelete(!confirmDelete)}><Trash2 size={15} /> 删除客户端</button>}
          <button type="submit" className="button primary">{busy ? <Busy /> : <Save size={16} />} 保存客户端</button>
        </div>
        {confirmDelete && selected && <div className="notice error" role="alert">
          <span>删除 {selected.id} 后，已有 token 将失去访问权限。</span>
          <button type="button" className="button secondary" onClick={() => void deleteSelected()}>确认删除</button>
        </div>}
      </fieldset>
    </form>
  </div>
}
