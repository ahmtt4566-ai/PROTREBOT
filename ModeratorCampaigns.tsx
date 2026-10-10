import {useEffect, useRef, useState} from 'react'
import {moderatorApiRequest, moderatorRequest} from './moderator-api'
import {ModCard, ModEmpty, ModError} from './moderator-ui'
import {AUDIENCES, audienceLabels, parseCampaign, parseCampaignPage, parseCampaignPreview, type Campaign, type CampaignDraft, type CampaignPage, type CampaignPreview} from './campaign-model'
import {CampaignBadge, CampaignConfirmation, CampaignPreviewView, CampaignReport} from './campaign-ui'

const EMPTY: CampaignDraft = {title: '', subject: '', body: '', audience: 'all_users', scheduled_at: null}
function localDate(value: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}

export default function ModeratorCampaigns({owner, canRequest}: {owner: boolean; canRequest: boolean}) {
  const [query, setQuery] = useState({offset: 0, version: 0})
  const [page, setPage] = useState<CampaignPage | null>(null)
  const [selected, setSelected] = useState<Campaign | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<CampaignDraft>(EMPTY)
  const [preview, setPreview] = useState<CampaignPreview | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const [message, setMessage] = useState('')
  const inFlight = useRef(false)
  const active = useRef<AbortController | null>(null)
  const sendKey = useRef('')
  useEffect(() => {
    const controller = new AbortController()
    void moderatorRequest(`/campaigns?limit=25&offset=${query.offset}`, parseCampaignPage, controller.signal)
      .then(value => {if (!controller.signal.aborted) setPage(value)})
      .catch(failure => {if (!controller.signal.aborted) setError(failure)})
    return () => controller.abort()
  }, [query])
  useEffect(() => () => active.current?.abort(), [])
  useEffect(() => {
    if (!editing || draft.subject.trim().length < 3 || !draft.title.trim() || !draft.body.trim()) {setPreview(null); return}
    const controller = new AbortController()
    setPreview(null)
    const timer = setTimeout(() => {
      void moderatorApiRequest('/campaigns/preview', parseCampaignPreview, controller.signal, {method: 'POST', body: JSON.stringify(draft)})
        .then(value => {if (!controller.signal.aborted) {setPreview(value); setError(null)}})
        .catch(failure => {if (!controller.signal.aborted) setError(failure)})
    }, 2500)
    return () => {clearTimeout(timer); controller.abort()}
  }, [draft, editing])
  useEffect(() => {
    if (!selected || selected.status === 'draft') return
    const controller = new AbortController()
    const timer = setInterval(() => {
      void moderatorRequest(`/campaigns/${encodeURIComponent(selected.id)}`, parseCampaign, controller.signal)
        .then(value => {if (!controller.signal.aborted) setSelected(value)})
        .catch(failure => {if (!controller.signal.aborted) setError(failure)})
    }, 15000)
    return () => {clearInterval(timer); controller.abort()}
  }, [selected?.id, selected?.status])
  const choose = (row: Campaign | null) => {
    setSelected(row); setDraft(row ? {title: row.title, subject: row.subject, body: row.body, audience: row.audience, scheduled_at: row.scheduled_at} : EMPTY)
    setEditing(!row || row.status === 'draft'); setError(null); setPreview(null); setMessage(''); setConfirm(false); sendKey.current = ''
  }
  const run = async (operation: (signal: AbortSignal) => Promise<void>) => {
    if (inFlight.current) return
    inFlight.current = true; setBusy(true); setError(null); setMessage('')
    const controller = new AbortController(); active.current = controller
    try {await operation(controller.signal); setQuery(value => ({...value, version: value.version + 1}))}
    catch (failure) {if (!controller.signal.aborted) {setError(failure); setConfirm(false)}}
    finally {inFlight.current = false; if (!controller.signal.aborted) setBusy(false)}
  }
  const save = () => void run(async signal => {
    const row = await moderatorApiRequest(selected ? `/campaigns/${encodeURIComponent(selected.id)}` : '/campaigns', parseCampaign, signal,
      {method: selected ? 'PUT' : 'POST', body: JSON.stringify({...draft, ...(selected ? {version: selected.version} : {})})})
    setSelected(row); setMessage('Taslak kaydedildi.')
  })
  const showPreview = (toSend = false) => void run(async signal => {
    if (!selected) return
    const value = await moderatorApiRequest(`/campaigns/${encodeURIComponent(selected.id)}/preview`, parseCampaignPreview, signal, {method: 'POST'})
    setPreview(value)
    if (owner && toSend) setConfirm(true)
  })
  const send = () => void run(async signal => {
    if (!selected || !preview) return
    sendKey.current ||= crypto.randomUUID()
    const row = await moderatorApiRequest(`/campaigns/${encodeURIComponent(selected.id)}/send`, parseCampaign, signal,
      {method: 'POST', body: JSON.stringify({idempotency_key: sendKey.current, confirmed_recipient_count: preview.recipient_count, content_hash: preview.content_hash})})
    setSelected(row); setEditing(false); setConfirm(false); setMessage('Gönderim kuyruğu oluşturuldu. Raporu takip edebilirsiniz.')
  })
  const action = (name: 'request-send' | 'test' | 'cancel' | 'cancel?withdraw=true') => void run(async signal => {
    if (!selected) return
    if (name === 'test') {
      await moderatorApiRequest(`/campaigns/${encodeURIComponent(selected.id)}/test`, value => {
        if (!value || typeof value !== 'object' || !('queued' in value) || value.queued !== true) throw new Error('Deneme kuyruğu doğrulanamadı.')
        return true
      }, signal, {method: 'POST'})
      setMessage('Deneme yalnız kendi hesabınız için kuyruğa alındı.')
    } else {
      const row = await moderatorApiRequest(`/campaigns/${encodeURIComponent(selected.id)}/${name}`, parseCampaign, signal, {method: 'POST'})
      choose(row)
    }
  })
  const changed = !!selected && (['title', 'subject', 'body', 'audience'] as const).some(key => draft[key] !== selected[key]) ||
    !!selected && (draft.scheduled_at ? Date.parse(draft.scheduled_at) : null) !== (selected.scheduled_at ? Date.parse(selected.scheduled_at) : null)
  const refresh = () => void run(async signal => {
    if (selected) choose(await moderatorRequest(`/campaigns/${encodeURIComponent(selected.id)}`, parseCampaign, signal))
  })
  return <div className="mod-stack">
    <div className="mod-stack" inert={confirm}>
    <div className="mod-actions"><button disabled={busy} onClick={() => choose(null)}>Yeni duyuru</button>
      <button disabled={busy} onClick={() => {setEditing(false); setSelected(null); setQuery({...query, version: query.version + 1})}}>Listeyi yenile</button></div>
    {error !== null && <ModError error={error} onRetry={refresh}/>}
    {message && <p role="status" className="mod-secondary">{message}</p>}
    {editing && <ModCard title="Bilgilendirme taslağı"><form className="mod-support-note" onSubmit={event => {event.preventDefault(); save()}}>
      <label>İç ad<input required maxLength={120} disabled={busy} value={draft.title} onChange={event => setDraft({...draft, title: event.target.value})}/></label>
      <label>Konu<input required minLength={3} maxLength={120} disabled={busy} value={draft.subject} onChange={event => setDraft({...draft, subject: event.target.value})}/></label>
      <label>Metin<textarea required maxLength={5000} rows={10} disabled={busy} value={draft.body} onChange={event => setDraft({...draft, body: event.target.value})}/></label>
      <label>Alıcı grubu<select disabled={busy} value={draft.audience} onChange={event => {
        const audience = AUDIENCES.find(value => value === event.target.value)
        if (audience) setDraft({...draft, audience})
      }}>{AUDIENCES.map(value => <option key={value} value={value}>{audienceLabels[value]}</option>)}</select></label>
      <label>Yaklaşık gönderim tarihi (isteğe bağlı)<input type="datetime-local" disabled={busy} value={localDate(draft.scheduled_at)} onChange={event => setDraft({...draft, scheduled_at: event.target.value ? new Date(event.target.value).toISOString() : null})}/></label>
      <p className="mod-secondary">Yalnız düz metin ve https://kaistrade.com bağlantıları. İleri tarih 10 dakika–30 gün aralığında; servis uykusu nedeniyle gecikebilir.</p>
      <button disabled={busy}>{busy ? 'İşlem sürüyor...' : 'Taslağı kaydet'}</button>
    </form></ModCard>}
    {selected && <><CampaignReport campaign={selected}/><div className="mod-actions">
      {selected.status === 'draft' && <><button disabled={busy || changed} onClick={() => action('test')}>Kendime deneme gönder</button>
        {owner ? <button disabled={busy || changed} onClick={() => showPreview(true)}>Gönder</button> :
          <button disabled={busy || changed || !canRequest} onClick={() => action('request-send')}>Onaya gönder</button>}</>}
      {selected.status === 'pending_approval' && <button disabled={busy} onClick={() => action('cancel?withdraw=true')}>Talebi geri çek ve düzenle</button>}
      {selected.status !== 'draft' && <button disabled={busy} onClick={() => showPreview()}>E-postayı görüntüle</button>}
      {['draft', 'pending_approval'].includes(selected.status) || owner && selected.status === 'sending' ?
        <button disabled={busy} onClick={() => {if (window.confirm('Bekleyen duyuru gönderimleri iptal edilsin mi?')) action('cancel')}}>İptal et</button> : null}
    </div>{changed && <p className="mod-secondary">Göndermeden önce değişiklikleri kaydedin.</p>}{!owner && !canRequest && <p className="mod-secondary">Onaya göndermek için ayrıca onay talebi oluşturma izni gerekli.</p>}</>}
    {preview && <CampaignPreviewView preview={preview}/>}
    {!editing && !selected && (page?.items.length ? <ul className="mod-results">{page.items.map(row => <li key={row.id}><ModCard title={row.title}>
      <CampaignBadge status={row.status}/><p>{row.subject}</p><p className="mod-secondary">{audienceLabels[row.audience]}</p>
      <button disabled={busy} onClick={() => choose(row)}>Görüntüle</button></ModCard></li>)}</ul> : <ModCard title="Duyurular"><ModEmpty text="Henüz duyuru yok."/></ModCard>)}
    {page && !editing && !selected && <div className="mod-pagination"><button disabled={busy || query.offset === 0} onClick={() => setQuery({...query, offset: Math.max(0, query.offset - 25)})}>Önceki</button>
      <button disabled={busy || page.offset + page.limit >= page.total} onClick={() => setQuery({...query, offset: query.offset + 25})}>Sonraki</button></div>}
    </div>
    {confirm && preview && <CampaignConfirmation preview={preview} busy={busy} onConfirm={send} onCancel={() => setConfirm(false)}/>}
  </div>
}
