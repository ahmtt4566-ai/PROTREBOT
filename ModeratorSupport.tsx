import {useEffect, useRef, useState} from 'react'
import {ChevronLeft, ChevronRight, MessageCircle} from 'lucide-react'
import {moderatorApiRequest, moderatorRequest} from './moderator-api'
import {moderatorDate, ModeratorRequestError} from './moderator-model'
import {ModCard, ModEmpty, ModError} from './moderator-ui'
import {SupportDetailCards, SupportPriorityBadge, SupportStatusBadge, SupportSummaryCards} from './support-ui'
import {
  parseSupportCase, parseSupportDetail, parseSupportPage, parseSupportSummary, PRIORITIES, priorityLabels,
  supportFilters, supportFilterQuery, type CaseStatus, type SupportDetail, type SupportFilter, type SupportPage, type SupportPriority, type SupportSummary,
} from './support-model'

export function SupportOverview() {
  const [summary, setSummary] = useState<SupportSummary | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [version, setVersion] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setSummary(null); setError(null)
    void moderatorRequest('/support/summary', parseSupportSummary, controller.signal)
      .then(value => {if (!controller.signal.aborted) setSummary(value)})
      .catch(reason => {if (!controller.signal.aborted) setError(reason)})
    return () => controller.abort()
  }, [version])
  return <div className="mod-stack"><SupportSummaryCards summary={summary}/>
    {error !== null && <ModError error={error} onRetry={() => setVersion(value => value + 1)}/>}</div>
}

function SupportDetailPanel({id, manage, onBack, onCustomer}: {id: string; manage: boolean; onBack: () => void; onCustomer?: (id: string) => void}) {
  const [detail, setDetail] = useState<SupportDetail | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [version, setVersion] = useState(0)
  const [note, setNote] = useState('')
  const [status, setStatus] = useState<CaseStatus>('NEW')
  const operation = useRef<AbortController | null>(null)
  const inFlight = useRef(false)
  const path = `/support/cases/${encodeURIComponent(id)}`
  useEffect(() => {
    const controller = new AbortController()
    setDetail(null); setError(null); setBusy(true)
    void moderatorRequest(path, parseSupportDetail, controller.signal)
      .then(value => {if (!controller.signal.aborted) {setDetail(value); setStatus(value.case_status)}})
      .catch(reason => {if (!controller.signal.aborted) setError(reason)})
      .finally(() => {if (!controller.signal.aborted) setBusy(false)})
    return () => controller.abort()
  }, [path, version])
  useEffect(() => () => operation.current?.abort(), [])
  const mutate = async (action: 'take' | 'release' | 'status' | 'notes') => {
    if (!detail || busy || inFlight.current) return
    const controller = new AbortController()
    operation.current = controller; inFlight.current = true; setBusy(true); setError(null)
    const body = action === 'status' ? {status, expected_version: detail.version} : action === 'notes' ? {body: note} : undefined
    try {
      await moderatorApiRequest(`${path}/${action}`, parseSupportCase, controller.signal, {method: 'POST', ...(body ? {body: JSON.stringify(body)} : {})})
      if (!controller.signal.aborted) {setDetail(null); if (action === 'notes') setNote(''); setVersion(value => value + 1)}
    } catch (reason) {
      if (!controller.signal.aborted) {
        setError(reason)
        if (reason instanceof ModeratorRequestError && [401, 403, 404, 409].includes(reason.status)) setDetail(null)
      }
    } finally {
      inFlight.current = false
      if (!controller.signal.aborted) setBusy(false)
    }
  }
  return <div className="mod-stack">
    <button className="mod-back" onClick={onBack} disabled={busy}><ChevronLeft aria-hidden="true"/>Taleplere dön</button>
    {busy && <p role="status" className="mod-secondary">Talep yükleniyor...</p>}
    {error !== null && <ModError error={error} onRetry={() => setVersion(value => value + 1)}/>}
    {detail && <SupportDetailCards detail={detail} manage={manage} busy={busy} note={note} status={status} onNoteChange={setNote} onStatusChange={setStatus}
      onTake={() => void mutate('take')} onRelease={() => void mutate('release')} onSaveStatus={() => void mutate('status')} onAddNote={event => {event.preventDefault(); void mutate('notes')}}
      onCustomer={onCustomer ? () => onCustomer(detail.user_id) : undefined}/>}
  </div>
}

export default function ModeratorSupport({manage, onCustomer}: {manage: boolean; onCustomer?: (id: string) => void}) {
  const [query, setQuery] = useState<{filter: SupportFilter; priority: SupportPriority | ''; offset: number; version: number}>({filter: 'all', priority: '', offset: 0, version: 0})
  const [page, setPage] = useState<SupportPage | null>(null)
  const [selected, setSelected] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(true)
  useEffect(() => {
    if (selected) return
    const controller = new AbortController()
    setPage(null); setError(null); setBusy(true)
    void moderatorRequest(`/support/cases?${supportFilterQuery(query.filter, query.priority, query.offset)}`, parseSupportPage, controller.signal)
      .then(value => {if (!controller.signal.aborted) setPage(value)})
      .catch(reason => {if (!controller.signal.aborted) setError(reason)})
      .finally(() => {if (!controller.signal.aborted) setBusy(false)})
    return () => controller.abort()
  }, [query, selected])
  if (selected) return <SupportDetailPanel id={selected} manage={manage} onBack={() => setSelected('')} onCustomer={onCustomer}/>
  return <div className="mod-stack">
    <div className="mod-support-filters" role="group" aria-label="Talep filtreleri">
      {supportFilters.map(filter => <button key={filter.id} aria-pressed={query.filter === filter.id} onClick={() => setQuery({...query, filter: filter.id, offset: 0})}>{filter.label}</button>)}
    </div>
    <label className="mod-support-priority">Öncelik<select value={query.priority} onChange={event => {
      const priority = PRIORITIES.find(value => value === event.target.value) ?? ''
      setQuery({...query, priority, offset: 0})
    }}><option value="">Tüm öncelikler</option>{PRIORITIES.map(value => <option key={value} value={value}>{priorityLabels[value]}</option>)}</select></label>
    {busy && <p role="status" className="mod-secondary">Talepler yükleniyor...</p>}
    {error !== null && <ModError error={error} onRetry={() => setQuery({...query, version: query.version + 1})}/>}
    {page && <>{!page.items.length ? <ModCard title="Destek talepleri"><ModEmpty text="Henüz destek talebi yok"/></ModCard> :
      <ul className="mod-results">{page.items.map(item => <li key={item.id}><button className="mod-customer-row mod-support-row" onClick={() => setSelected(item.id)}>
        <MessageCircle aria-hidden="true"/><span><strong>{item.subject}</strong><small>Müşteri {item.user_id} · {moderatorDate(item.created_at)}</small>
          <small>{item.assignee_user_id === null ? 'Atanmamış' : item.assigned_to_me ? 'Bana atanmış' : `Atanan: ${item.assignee_user_id}`}</small></span>
        <SupportPriorityBadge priority={item.priority}/><SupportStatusBadge status={item.case_status}/><ChevronRight aria-hidden="true"/>
      </button></li>)}</ul>}
      <div className="mod-pagination"><button disabled={busy || query.offset === 0} onClick={() => setQuery({...query, offset: Math.max(0, query.offset - page.limit)})}><ChevronLeft aria-hidden="true"/>Önceki</button>
        <button disabled={busy || page.offset + page.limit >= page.total} onClick={() => setQuery({...query, offset: query.offset + page.limit})}>Sonraki<ChevronRight aria-hidden="true"/></button></div>
    </>}
  </div>
}
