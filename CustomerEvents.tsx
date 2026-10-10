import {useEffect, useState} from 'react'
import {EVENT_KINDS, eventCodeLabel, eventFilterLabels, eventKindLabels, parseCustomerEvents, type CustomerEvent, type CustomerEventsPage} from './customer-event-model'
import {moderatorRequest} from './moderator-api'
import {moderatorDate} from './moderator-model'
import {ModBadge, ModCard, ModEmpty, ModError} from './moderator-ui'

export function EventTimeline({items}: {items: CustomerEvent[]}) {
  if (!items.length) return <ModEmpty text="veri yok"/>
  return <ul className="mod-event-timeline">{items.map((item, index) => <li key={`${item.source}:${item.first_at}:${index}`}>
    <div className="mod-badges"><ModBadge tone={item.severity === 'INFO' ? 'neutral' : 'warning'}>{item.code === 'event_limit' ? 'Ek olaylar' : eventKindLabels[item.kind]}</ModBadge>
      <span className="mod-secondary">×{item.count}</span>{item.source === 'system_error' && <ModBadge>Sistem hatası kaynağı</ModBadge>}
      {item.resolved !== null && <ModBadge>{item.resolved ? 'Çözüldü' : 'Açık'}</ModBadge>}</div>
    <strong>{eventCodeLabel(item.code)}</strong>
    <time dateTime={item.last_at}>{moderatorDate(item.last_at)}</time>
    {item.count > 1 && <p className="mod-secondary">{item.code === 'event_limit' ? `${item.count} ek olay sayaçta toplandı.` : `Bu zaman aralığında ${item.count} tekrar kaydedildi.`}</p>}
  </li>)}</ul>
}

export default function CustomerEvents({userId}: {userId: string}) {
  const [filters, setFilters] = useState({kind: '', start: '', end: ''})
  const [query, setQuery] = useState({...filters, offset: 0, version: 0})
  const [page, setPage] = useState<CustomerEventsPage | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    setBusy(true); setError(null); setPage(null)
    const params = new URLSearchParams({limit: '25', offset: String(query.offset)})
    if (query.kind) params.set('kind', query.kind)
    if (query.start) params.set('start', new Date(`${query.start}T00:00:00`).toISOString())
    if (query.end) params.set('end', new Date(`${query.end}T23:59:59.999`).toISOString())
    void moderatorRequest(`/customers/${encodeURIComponent(userId)}/events?${params}`, parseCustomerEvents, controller.signal)
      .then(value => {if (!controller.signal.aborted) setPage(value)})
      .catch(failure => {if (!controller.signal.aborted) setError(failure)})
      .finally(() => {if (!controller.signal.aborted) setBusy(false)})
    return () => controller.abort()
  }, [userId, query])
  return <ModCard title="Teknik Olaylar"><div className="mod-stack">
    <form className="mod-event-filters" onSubmit={event => {event.preventDefault(); setQuery({...filters, offset: 0, version: query.version + 1})}}>
      <label>Olay türü<select value={filters.kind} onChange={event => setFilters({...filters, kind: event.target.value})}><option value="">Tüm türler</option>
        {EVENT_KINDS.map(kind => <option key={kind} value={kind}>{eventFilterLabels[kind]}</option>)}</select></label>
      <label>Başlangıç<input type="date" value={filters.start} max={filters.end || undefined} onChange={event => setFilters({...filters, start: event.target.value})}/></label>
      <label>Bitiş<input type="date" value={filters.end} min={filters.start || undefined} onChange={event => setFilters({...filters, end: event.target.value})}/></label>
      <button disabled={busy}>Filtrele</button>
    </form>
    {busy && <p className="mod-secondary" role="status">Olaylar yükleniyor...</p>}
    {error !== null && <ModError error={error} onRetry={() => setQuery({...query, version: query.version + 1})}/>}
    {page && <><EventTimeline items={page.items}/><div className="mod-pagination">
      <button disabled={busy || page.offset === 0} onClick={() => setQuery({...query, offset: Math.max(0, page.offset - page.limit)})}>Önceki</button>
      <button disabled={busy || page.offset + page.limit >= page.total} onClick={() => setQuery({...query, offset: page.offset + page.limit})}>Sonraki</button>
    </div></>}
  </div></ModCard>
}

export function OwnerApprovalNotice() {
  return <ModCard title="Onay Talepleri"><p>Yönetici olarak işlemi doğrudan yapabilirsiniz.</p><a className="mod-back" href="/admin?section=users">Yönetici kullanıcı yönetimine git</a></ModCard>
}
