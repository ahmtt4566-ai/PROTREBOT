import {useEffect, useRef, useState, type FormEvent} from 'react'
import {Copy, RefreshCw} from 'lucide-react'
import {API_BASE, userSessionToken} from './api'
import {isRecord as record, parseBinancePayment as payment, validAmount, type BinancePayment as Payment} from './binance-payment-model'

type Configuration = {enabled:false} | {enabled:true; amount:string; currency:'USDT'; pay_id:string}
function configuration(value:unknown):Configuration {
  if (record(value) && value.enabled === false) return {enabled:false}
  if (record(value) && value.enabled === true && validAmount(value.amount) && value.currency === 'USDT'
    && typeof value.pay_id === 'string' && value.pay_id.trim()) {
    return {enabled:true,amount:value.amount,currency:value.currency,pay_id:value.pay_id}
  }
  throw new Error('Binance ödeme bilgileri doğrulanamadı.')
}
async function request(path:string, options:RequestInit = {}):Promise<unknown> {
  const headers = new Headers(options.headers)
  const token = userSessionToken()
  if (token) headers.set('Authorization',`Bearer ${token}`)
  if (options.body) headers.set('Content-Type','application/json')
  const response = await fetch(`${API_BASE}/v22/payments/binance-manual${path}`,{...options,headers,cache:'no-store'})
  const value:unknown = await response.json()
  if (!response.ok) throw new Error(record(value) && typeof value.detail === 'string'
    ? value.detail : 'Binance ödeme işlemi tamamlanamadı.')
  return value
}

export default function BinanceManualPayment() {
  const [config,setConfig] = useState<Configuration|null>(null)
  const [current,setCurrent] = useState<Payment|null>(null)
  const [period,setPeriod] = useState<{start:string;end:string}|null>(null)
  const [number,setNumber] = useState('')
  const [loading,setLoading] = useState(true)
  const [busy,setBusy] = useState(false)
  const [error,setError] = useState('')
  const [notice,setNotice] = useState('')
  const [attempt,setAttempt] = useState(0)
  const inFlight = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    setLoading(true)
    setError('')
    void (async () => {
      const next = configuration(await request('/config',{signal:controller.signal}))
      if (controller.signal.aborted) return
      setConfig(next)
      if (next.enabled) {
        const value = await request('/requests/current',{signal:controller.signal})
        if (!record(value) || !('request' in value)) throw new Error('Binance ödeme geçmişi doğrulanamadı.')
        const row = value.request === null ? null : payment(value.request)
        const stored = value.period
        let nextPeriod:{start:string;end:string}|null = null
        if (stored != null) {
          if (!record(stored) || row?.status !== 'approved' || typeof stored.period_start !== 'string' || typeof stored.period_end !== 'string'
            || !Number.isFinite(Date.parse(stored.period_start)) || Date.parse(stored.period_end)-Date.parse(stored.period_start) !== 30*86400000) {
            throw new Error('Binance üyelik dönemi doğrulanamadı.')
          }
          nextPeriod = {start:stored.period_start,end:stored.period_end}
        }
        if (!controller.signal.aborted) {
          setCurrent(row)
          setPeriod(nextPeriod)
          if (row?.status === 'approved') window.dispatchEvent(new Event('protrebot-access-refresh'))
        }
      }
    })().catch(exception => {
      if (!controller.signal.aborted) setError(exception instanceof Error ? exception.message : 'Binance ödeme bilgileri yüklenemedi.')
    }).finally(() => {if (!controller.signal.aborted) setLoading(false)})
    return () => {mounted.current = false;controller.abort()}
  },[attempt])

  const update = async (path:string, body?:{transaction_number:string}) => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true);setError('');setNotice('')
    try {
      const row = payment(await request(path,{method:'POST',...(body ? {body:JSON.stringify(body)} : {})}))
      if (mounted.current) {setCurrent(row);setNumber('')}
    } catch (exception) {
      if (mounted.current) setError(exception instanceof Error ? exception.message : 'Binance ödeme işlemi tamamlanamadı.')
    } finally {
      inFlight.current = false
      if (mounted.current) setBusy(false)
    }
  }
  const submit = (event:FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (current?.status === 'created' && number.trim()) {
      void update(`/requests/${current.id}/submit`,{transaction_number:number.trim()})
    }
  }
  const copy = async () => {
    if (!current) return
    try {
      await navigator.clipboard.writeText(current.reference_code)
      if (mounted.current) {setNotice('Referans kodu kopyalandı.');setError('')}
    } catch {
      if (mounted.current) setError('Kod kopyalanamadı; referans kodunu seçip elle kopyalayın.')
    }
  }
  if (config?.enabled === false || loading && !current && !error) return null
  const status = current?.status
  return <section className="binanceManualPayment" aria-label="Binance ile ödeme">
    <header><div><small>ALTERNATİF ÖDEME</small><h3>Binance ile öde</h3></div><span>Elle onaylı</span></header>
    <p>30 günlük MASTER MODE · Ücretsiz deneme yok. Otomatik yenileme yok.</p>
    {error && <div className="binanceManualError" role="alert">{error}{' '}
      {!current && <button type="button" onClick={() => setAttempt(value => value + 1)} disabled={loading}>Yeniden dene</button>}
    </div>}
    {config?.enabled && <>
      {!current && !error && <><p>MASTER MODE için {config.amount} {config.currency} ile ödeme yapın. Erişim yalnız ödeme doğrulanıp onaylandıktan sonra açılır.</p>
        <button type="button" className="binanceManualPrimary" disabled={busy || loading} onClick={() => void update('/requests')}>
          {busy ? 'Hazırlanıyor…' : 'Binance ile öde'}
        </button></>}
      {current && <><div className="binanceManualDetails">
        <div><small>Ödenecek tutar</small><strong>{current.amount} {current.currency}</strong></div>
        <div><small>Binance Pay ID</small><strong>{config.pay_id}</strong></div>
        <div className="binanceManualReference"><small>Referans kodu</small><strong>{current.reference_code}</strong>
          <button type="button" onClick={() => void copy()} aria-label="Referans kodunu kopyala"><Copy size={16}/></button></div>
      </div>
        {status === 'approved' && period && <div className="binanceManualDetails">
          <div><small>Ödeme yöntemi</small><strong>Binance ile ödendi</strong></div>
          <div><small>Dönem başlangıcı</small><strong>{new Date(period.start).toLocaleDateString('tr-TR')}</strong></div>
          <div><small>Bitiş tarihi</small><strong>{new Date(period.end).toLocaleDateString('tr-TR')}</strong></div>
        </div>}
        {status === 'created' && <>
          <p className="binanceManualWarning"><b>Ödeme notuna bu kodu yaz:</b> {current.reference_code}</p>
          <p>Ödeme yaptıktan sonra Binance işlem/sipariş numarasını girin. Talep oluşturulduktan sonra 48 saat içinde göndermelisiniz.</p>
          <small>Son gönderim: {new Date(current.expires_at).toLocaleString('tr-TR',{dateStyle:'medium',timeStyle:'short'})} (yerel saat)</small>
          <form onSubmit={submit}>
            <label htmlFor={`binance-number-${current.id}`}>Binance işlem / sipariş numarası</label>
            <input id={`binance-number-${current.id}`} value={number} onChange={event => setNumber(event.target.value)}
              maxLength={128} required pattern="[A-Za-z0-9_-]+" autoComplete="off" disabled={busy}/>
            <button type="submit" className="binanceManualPrimary" disabled={busy || !number.trim()}>{busy ? 'Gönderiliyor…' : 'Ödemeyi bildirdim'}</button>
          </form>
        </>}
        {status === 'pending' && <div role="status"><strong>Bekliyor</strong><p>Ödeme talebiniz inceleme için alındı. Bu işlem erişim açmaz; onay bekleniyor.</p>
          <small>İşlem numarası: {current.transaction_number}</small></div>}
        {(status === 'expired' || status === 'rejected') && <div role="status"><strong>{status === 'expired' ? 'Süresi doldu' : 'Reddedildi'}</strong>
          <p>{status === 'expired' ? '48 saat içinde işlem numarası girilmedi. Yeni talep oluşturabilirsiniz.' : 'Ödeme talebiniz reddedildi.'}</p>
          <button type="button" className="binanceManualPrimary" disabled={busy} onClick={() => void update('/requests')}>Yeni ödeme talebi</button></div>}
        {status === 'approved' && <div role="status"><strong>Onaylandı</strong><p>Ödeme talebiniz onaylandı. Abonelik durumunu faturalama ekranından kontrol edin.</p></div>}
        <button type="button" className="binanceManualRefresh" onClick={() => setAttempt(value => value + 1)} disabled={loading || busy}><RefreshCw size={14}/> Durumu yenile</button>
      </>}
      {notice && <p role="status">{notice}</p>}
    </>}
  </section>
}
