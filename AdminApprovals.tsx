import {useEffect, useRef, useState} from 'react'
import {approvalRequest} from './approval-api'
import {APPROVAL_STATUSES, approvalLabels, parseApproval, parseApprovalDetail, parseApprovalPage, type ApprovalDetail, type ApprovalPage, type ApprovalStatus} from './approval-model'
import {ApprovalCards, ApprovalError, ApprovalOwnerDetail} from './approval-ui'
import './moderator-panel.css'

export default function AdminApprovals() {
  const [query, setQuery] = useState<{status: ApprovalStatus | ''; offset: number; version: number}>({status: 'pending', offset: 0, version: 0})
  const [page, setPage] = useState<ApprovalPage | null>(null)
  const [selected, setSelected] = useState('')
  const [detail, setDetail] = useState<ApprovalDetail | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const inFlight = useRef(false)
  const operation = useRef<AbortController | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setBusy(true); setError(null); setPage(null); setDetail(null)
    if (selected) {
      void approvalRequest(true, `/${encodeURIComponent(selected)}`, parseApprovalDetail, controller.signal)
        .then(value => {if (!controller.signal.aborted) {setDetail(value); setNote('')}})
        .catch(failure => {if (!controller.signal.aborted) setError(failure)})
        .finally(() => {if (!controller.signal.aborted) setBusy(false)})
    } else {
      const params = new URLSearchParams({limit: '25', offset: String(query.offset)})
      if (query.status) params.set('status', query.status)
      void approvalRequest(true, `?${params}`, parseApprovalPage, controller.signal)
        .then(value => {if (!controller.signal.aborted) setPage(value)})
        .catch(failure => {if (!controller.signal.aborted) setError(failure)})
        .finally(() => {if (!controller.signal.aborted) setBusy(false)})
    }
    return () => controller.abort()
  }, [selected, query])
  useEffect(() => () => operation.current?.abort(), [])
  const mutate = async (action: 'approve' | 'reject' | 'retry-agents') => {
    if (!detail || busy || inFlight.current || action === 'reject' && !note.trim()) return
    if (action === 'retry-agents' && !window.confirm('Hesap durumu yeniden değiştirilmez. Yalnız ajan iptalini yeniden denemek istiyor musunuz?')) return
    const controller = new AbortController()
    operation.current = controller; inFlight.current = true; setBusy(true); setError(null)
    try {
      await approvalRequest(true, `/${encodeURIComponent(selected)}/${action}`, parseApproval, controller.signal,
        {method: 'POST', ...(action === 'reject' ? {body: JSON.stringify({decision_note: note})} : {})})
      if (!controller.signal.aborted) {setDetail(null); setQuery(value => ({...value, version: value.version + 1}))}
    } catch (failure) {if (!controller.signal.aborted) {setDetail(null); setError(failure)}}
    finally {inFlight.current = false; if (!controller.signal.aborted) setBusy(false)}
  }
  return <section className="mod-approval-section mod-stack" aria-label="Onay talepleri">
    {selected ? <div className="mod-actions"><button disabled={busy} onClick={() => setSelected('')}>Listeye dön</button></div> :
      <label className="mod-support-priority">Durum<select value={query.status} disabled={busy} onChange={event => {
        const status = APPROVAL_STATUSES.find(value => value === event.target.value) ?? ''
        setQuery({...query, status, offset: 0})
      }}><option value="">Tümü</option>{APPROVAL_STATUSES.map(status => <option key={status} value={status}>{approvalLabels[status]}</option>)}</select></label>}
    {busy && <p role="status" className="mod-secondary">Onay bilgileri yükleniyor...</p>}
    {error !== null && <ApprovalError error={error} onRefresh={() => setQuery(value => ({...value, version: value.version + 1}))}/>}
    {detail && <ApprovalOwnerDetail detail={detail} busy={busy} note={note} onNote={setNote} onApprove={() => void mutate('approve')} onReject={() => void mutate('reject')} onRetry={() => void mutate('retry-agents')}/>}
    {page && <><p className="mod-secondary">Bekleyen talepler: {page.pending_count}</p><ApprovalCards items={page.items} owner busy={busy} onSelect={setSelected}/>
      <div className="mod-pagination"><button disabled={busy || page.offset === 0} onClick={() => setQuery({...query, offset: Math.max(0, page.offset - page.limit)})}>Önceki</button>
        <button disabled={busy || page.offset + page.limit >= page.total} onClick={() => setQuery({...query, offset: page.offset + page.limit})}>Sonraki</button></div></>}
  </section>
}
