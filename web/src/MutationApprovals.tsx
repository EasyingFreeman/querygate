import { Check, Clock3, RefreshCw, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { databaseMessage } from './api'
import type { Cell, MutationRecord, MutationState } from './api'
import { Busy, Empty } from './panels'

const labels: Record<MutationState, string> = {
  pending: '待审批', executing: '执行中', succeeded: '已执行', rejected: '已拒绝', failed: '失败', unknown: '结果待核对',
}
const badge: Record<MutationState, string> = {
  pending: 'warning', executing: 'running', succeeded: 'success', rejected: 'error', failed: 'error', unknown: 'warning',
}
const time = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium' })

function display(value: Cell | undefined) {
  return value === undefined ? '—' : JSON.stringify(value)
}

export default function MutationApprovals({ list, decide }: {
  list: (state: MutationState | '') => Promise<MutationRecord[]>
  decide: (id: string, action: 'approve' | 'reject' | 'reconcile') => Promise<MutationRecord>
}) {
  const [state, setState] = useState<MutationState | ''>('pending')
  const [revision, setRevision] = useState(0)
  const [records, setRecords] = useState<MutationRecord[]>([])
  const [selected, setSelected] = useState<MutationRecord | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    let active = true
    setLoading(true)
    setError('')
    void list(state).then((items) => {
      if (!active) return
      setRecords(items)
      setSelected((record) => record
        ? items.find((item) => item.operation_id === record.operation_id) ?? record
        : items[0] ?? null)
    }).catch((failure) => {
      if (active) setError(failure instanceof Error ? failure.message : '读取审批队列失败。')
    }).finally(() => {
      if (active) setLoading(false)
    })
    return () => { active = false }
  }, [list, state, revision])

  async function act(action: 'approve' | 'reject' | 'reconcile') {
    if (!selected || busy) return
    setBusy(true)
    setNotice('')
    setError('')
    try {
      const result = await decide(selected.operation_id, action)
      setSelected(result)
      setRecords((current) => current.flatMap((record) => {
        if (record.operation_id !== result.operation_id) return [record]
        return !state || state === result.status ? [result] : []
      }))
      if (action === 'reconcile') {
        setNotice(result.status === 'succeeded' ? '已从数据库确认提交。' : '暂未找到已提交的数据库审计记录。')
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '未能处理此操作。')
    } finally {
      setBusy(false)
    }
  }

  return <>
    <header className="history-page-heading">
      <h1>写入审批</h1>
      <button className="button secondary" type="button" disabled={busy || loading} onClick={() => setRevision((value) => value + 1)}><RefreshCw size={16} /> 刷新</button>
    </header>
    {error && <p className="notice error mutation-notice" role="alert">{error}</p>}
    {notice && <p className="admin-hint" role="status">{notice}</p>}
    <div className="history-layout mutation-layout">
      <section className="history-surface" aria-label="写入记录">
        <div className="history-filters"><label className="select-field">
          <select value={state} aria-label="写入状态" disabled={busy} onChange={(event) => {
            setSelected(null); setRecords([]); setNotice(''); setState(event.target.value as MutationState | '')
          }}>
            <option value="">全部状态</option>
            {Object.entries(labels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label></div>
        {loading && <div className="inline-status history-progress"><Busy /> 正在读取审批队列</div>}
        {!loading && records.length === 0 && <Empty title="此状态下暂无记录" icon={<Clock3 size={21} />} />}
        {records.map((record) => <button key={record.operation_id} className={`mutation-entry ${record.operation_id === selected?.operation_id ? 'selected' : ''}`}
          type="button" disabled={busy} onClick={() => { setSelected(record); setNotice(''); setError('') }} aria-pressed={record.operation_id === selected?.operation_id}>
          <code>{record.submitted.operation.toUpperCase()} {record.submitted.table}</code>
          <span className={`tag ${badge[record.status]}`}>{labels[record.status]}</span>
          <small>{record.client_id} · {time.format(new Date(record.submitted_at))}</small>
        </button>)}
      </section>
      <section className="history-surface history-detail" aria-label="写入详情">
        {selected ? <>
          <header className="history-detail-heading">
            <h2>{selected.submitted.operation.toUpperCase()} {selected.submitted.table}</h2>
            <span className={`tag ${badge[selected.status]}`}>{labels[selected.status]}</span>
          </header>
          <dl className="history-properties">
            <div><dt>Client ID</dt><dd>{selected.client_id}</dd></div>
            <div><dt>数据源</dt><dd>{selected.submitted.datasource_id}</dd></div>
            <div><dt>审批方式</dt><dd>{selected.approval === 'web_admin' ? 'Web 审批' : selected.approval === 'client_exempt' ? '客户端免审' : '—'}</dd></div>
            <div><dt>提交时间</dt><dd>{time.format(new Date(selected.submitted_at))}</dd></div>
            {selected.decided_at && <div><dt>处理时间</dt><dd>{time.format(new Date(selected.decided_at))}</dd></div>}
            {selected.audit && <div><dt>变更时间</dt><dd>{time.format(new Date(selected.audit.changed_at))}</dd></div>}
            <div className="history-query-id"><dt>操作 ID</dt><dd><code>{selected.operation_id}</code></dd></div>
            <div className="history-query-id"><dt>调用原因</dt><dd>{selected.submitted.reason}</dd></div>
            <div className="history-query-id"><dt>主键</dt><dd><code>{JSON.stringify(selected.submitted.key)}</code></dd></div>
          </dl>
          <div className="history-table-wrap">
            <table className="mutation-values">
              <thead><tr><th>列</th><th>提交值</th>{selected.audit && <><th>修改前</th><th>修改后</th></>}</tr></thead>
              <tbody>{Object.entries(selected.submitted.values).map(([column, value]) => <tr key={column}>
                <td>{column}</td><td>{display(value)}</td>
                {selected.audit && <><td>{display(selected.audit.before?.[column])}</td><td>{display(selected.audit.after[column])}</td></>}
              </tr>)}</tbody>
            </table>
          </div>
          {selected.error_code && <p className="notice error mutation-notice">{databaseMessage(selected.error_code, selected.error_code)}</p>}
          <div className="admin-form-actions">
            {selected.status === 'pending' && <>
              <button type="button" className="button secondary" disabled={busy} onClick={() => void act('reject')}><X size={16} /> 拒绝</button>
              <button type="button" className="button primary" disabled={busy} onClick={() => void act('approve')}>{busy ? <Busy /> : <Check size={16} />} 批准并执行</button>
            </>}
            {['unknown', 'executing'].includes(selected.status) && <button type="button" className="button secondary" disabled={busy} onClick={() => void act('reconcile')}>
              {busy ? <Busy /> : <RefreshCw size={16} />} 核对数据库审计
            </button>}
          </div>
        </> : <Empty title="选择一条写入记录" icon={<Clock3 size={21} />} />}
      </section>
    </div>
  </>
}
