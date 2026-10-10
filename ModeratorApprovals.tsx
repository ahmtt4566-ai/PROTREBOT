import {useEffect, useRef, useState} from 'react'
import {approvalRequest} from './approval-api'
import {APPROVAL_STATUSES, approvalLabels, parseApproval, parseApprovalPage, type ApprovalPage, type ApprovalStatus} from './approval-model'
import {ApprovalCards, ApprovalError, ApprovalReasonForm} from './approval-ui'
import {ModCard} from './moderator-ui'

export function ApprovalRequestForm({userId, active, onCreated}: {userId: string; active: boolean; onCreated?: () => void}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [created, setCreated] = useState(false)
  const operation = useRef<AbortController | null>(null)
  const inFlight = useRef(false)
  useEffect(() => () => operation.current?.abort(), [])
  const submit = async () => {
    if (inFlight.current || reason.trim().length < 10) return
    const controller = new AbortController()
    operation.current = controller; inFlight.current = true; setBusy(true); setError(null)
    try {
      await approvalRequest(false, '', parseApproval, controller.signal, {method: 'POST', body: JSON.stringify({
        action_type: active ? 'account.deactivate' : 'account.reactivate', target_user_id: userId, reason,
      })})
      if (!controller.signal.aborted) {setCreated(true); setOpen(false); setReason('')}
    } catch (failure) {if (!controller.signal.aborted) setError(failure)}
    finally {inFlight.current = false; if (!controller.signal.aborted) setBusy(false)}
  }
  return <div className="mod-stack"><ModCard title="Hesap durumu onayı">
    {created ? <><p role="status">Talep oluşturuldu. Hesap durumu değiştirilmedi.</p><div className="mod-actions"><button onClick={onCreated}>Taleplerime git</button></div></> :
      <><div className="mod-actions"><button disabled={busy} onClick={() => setOpen(value => !value)}>{active ? 'Hesabı pasifleştirme talebi' : 'Hesabı aktif etme talebi'}</button></div>
        {open && <ApprovalReasonForm busy={busy} reason={reason} onReason={setReason} onSubmit={event => {event.preventDefault(); void submit()}}/>}</>}
  </ModCard>{error !== null && <ApprovalError error={error} onRefresh={() => setError(null)}/>}</div>
}

export function ApprovalOverview() {
  const [page, setPage] = useState<ApprovalPage | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [version, setVersion] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setPage(null); setError(null)
    void approvalRequest(false, '?status=pending&limit=1', parseApprovalPage, controller.signal)
      .then(value => {if (!controller.signal.aborted) setPage(value)})
      .catch(failure => {if (!controller.signal.aborted) setError(failure)})
    return () => controller.abort()
  }, [version])
  return <div className="mod-stack"><ModCard title="Bekleyen taleplerim"><p className="mod-primary">{page?.pending_count ?? 'veri yok'}</p></ModCard>
    {error !== null && <ApprovalError error={error} onRefresh={() => setVersion(value => value + 1)}/>}</div>
}

export default function ModeratorApprovals() {
  const [query, setQuery] = useState<{status: ApprovalStatus | ''; offset: number; version: number}>({status: '', offset: 0, version: 0})
  const [page, setPage] = useState<ApprovalPage | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const operation = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setPage(null); setError(null); setBusy(true)
    const params = new URLSearchParams({limit: '25', offset: String(query.offset)})
    if (query.status) params.set('status', query.status)
    void approvalRequest(false, `?${params}`, parseApprovalPage, controller.signal)
      .then(value => {if (!controller.signal.aborted) setPage(value)})
      .catch(failure => {if (!controller.signal.aborted) setError(failure)})
      .finally(() => {if (!controller.signal.aborted) setBusy(false)})
    return () => controller.abort()
  }, [query])
  useEffect(() => () => operation.current?.abort(), [])
  const cancel = async (id: string) => {
    if (busy || inFlight.current) return
    const controller = new AbortController()
    inFlight.current = true; operation.current = controller; setBusy(true); setError(null)
    try {
      await approvalRequest(false, `/${encodeURIComponent(id)}/cancel`, parseApproval, controller.signal, {method: 'POST'})
      if (!controller.signal.aborted) {setPage(null); setQuery(value => ({...value, version: value.version + 1}))}
    } catch (failure) {if (!controller.signal.aborted) {setPage(null); setError(failure)}}
    finally {inFlight.current = false; if (!controller.signal.aborted) setBusy(false)}
  }
  return <div className="mod-stack">
    <label className="mod-support-priority">Durum<select value={query.status} disabled={busy} onChange={event => {
      const status = APPROVAL_STATUSES.find(value => value === event.target.value) ?? ''
      setQuery({...query, status, offset: 0})
    }}><option value="">Tümü</option>{APPROVAL_STATUSES.map(status => <option key={status} value={status}>{approvalLabels[status]}</option>)}</select></label>
    {busy && <p role="status" className="mod-secondary">Talepler yükleniyor...</p>}
    {error !== null && <ApprovalError error={error} onRefresh={() => setQuery(value => ({...value, version: value.version + 1}))}/>}
    {page && <><ApprovalCards items={page.items} busy={busy} onCancel={id => void cancel(id)}/>
      <div className="mod-pagination"><button disabled={busy || page.offset === 0} onClick={() => setQuery({...query, offset: Math.max(0, page.offset - page.limit)})}>Önceki</button>
        <button disabled={busy || page.offset + page.limit >= page.total} onClick={() => setQuery({...query, offset: page.offset + page.limit})}>Sonraki</button></div></>}
  </div>
}
