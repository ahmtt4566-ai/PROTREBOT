import {useEffect, useRef, useState} from 'react'
import {Check, Sparkles} from 'lucide-react'
import {API_BASE, userSessionToken} from './api'
import BinanceManualPayment from './BinanceManualPayment'
import {PLAN_BY_CODE} from './subscription'
import './free-trial-pricing.css'

type Trial = {enabled:false} | {enabled:true; used:boolean; active:boolean; started_at:string|null; ends_at:string|null}
function parseTrial(value:unknown):Trial {
  if (typeof value !== 'object' || value === null) throw new Error('Deneme bilgileri doğrulanamadı.')
  const row = value as Record<string,unknown>
  if (row.enabled === false) return {enabled:false}
  if (row.enabled !== true || typeof row.used !== 'boolean' || typeof row.active !== 'boolean'
    || !(row.started_at === null || typeof row.started_at === 'string' && Number.isFinite(Date.parse(row.started_at)))
    || !(row.ends_at === null || typeof row.ends_at === 'string' && Number.isFinite(Date.parse(row.ends_at)))
    || row.active && (!row.used || row.started_at === null || row.ends_at === null)
    || row.started_at !== null && row.ends_at !== null && Date.parse(row.ends_at)-Date.parse(row.started_at) !== 7*86400000) {
    throw new Error('Deneme bilgileri doğrulanamadı.')
  }
  return {enabled:true,used:row.used,active:row.active,started_at:row.started_at,ends_at:row.ends_at}
}
async function trialRequest(path:string, signal?:AbortSignal):Promise<Trial> {
  const response = await fetch(`${API_BASE}/v22/free-trial${path}`, {
    method:path ? 'POST' : 'GET', signal, cache:'no-store',
  })
  const value:unknown = await response.json()
  if (!response.ok) {
    const detail = typeof value === 'object' && value !== null && 'detail' in value ? value.detail : null
    throw new Error(typeof detail === 'string' ? detail : 'Deneme işlemi tamamlanamadı.')
  }
  return parseTrial(value)
}

export default function FreeTrialPricing({trialEnabled,binanceEnabled,onBilling}:{
  trialEnabled:boolean;binanceEnabled:boolean;onBilling:()=>void
}) {
  const [trial,setTrial] = useState<Trial|null>(null)
  const [loading,setLoading] = useState(false)
  const [busy,setBusy] = useState(false)
  const [error,setError] = useState('')
  const [attempt,setAttempt] = useState(0)
  const [paymentOpen,setPaymentOpen] = useState(false)
  const [now,setNow] = useState(Date.now)
  const inFlight = useRef(false)
  const authenticated = Boolean(userSessionToken())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()),30000)
    return () => window.clearInterval(timer)
  },[])
  useEffect(() => {
    const controller = new AbortController()
    setTrial(null);setError('')
    if (!trialEnabled || !authenticated) return () => controller.abort()
    setLoading(true)
    void trialRequest('',controller.signal).then(value => {
      if (!controller.signal.aborted) setTrial(value)
    }).catch((exception:unknown) => {
      if (!controller.signal.aborted) setError(exception instanceof Error ? exception.message : 'Deneme bilgileri yüklenemedi.')
    }).finally(() => {if (!controller.signal.aborted) setLoading(false)})
    return () => controller.abort()
  },[trialEnabled,authenticated,attempt])
  const start = async () => {
    if (inFlight.current) return
    inFlight.current = true;setBusy(true);setError('')
    try {
      const value = await trialRequest('/start')
      setTrial(value);setNow(Date.now())
      window.dispatchEvent(new Event('protrebot-access-refresh'))
    } catch (exception) {
      setError(exception instanceof Error ? exception.message : 'Deneme başlatılamadı.')
    } finally {inFlight.current = false;setBusy(false)}
  }
  const used = trial?.enabled === true && trial.used
  const days = trial?.enabled === true && trial.ends_at ? Math.max(0,Math.ceil((Date.parse(trial.ends_at)-now)/86400000)) : 0
  const active = trial?.enabled === true && trial.active && days > 0
  const label = !trialEnabled ? 'Deneme yakında açılacak' : !authenticated ? 'Giriş yapın'
    : loading ? 'Yükleniyor…' : busy ? 'Başlatılıyor…' : active ? 'Deneme aktif' : used ? 'Deneme hakkı kullanıldı' : 'Ücretsiz denemeyi başlat'
  return <>
    <header className="subscriptionPageHeader"><div><span>MASTER TRADE</span><h1>Planını seç, çalışma alanını aç.</h1><p>Ödemesiz denemeyle başla veya MASTER MODE’a geç.</p></div><button type="button" onClick={onBilling}><Sparkles/>ABONELİK</button></header>
    <section className="subscriptionPlans freeTrialPlans" aria-label="Abonelik planları">
      <article className="subscriptionPlan">
        <header><span>ÖDEMESİZ DENEME</span><h2>1 Hafta Ücretsiz Deneme</h2><p>Ödeme bilgisi vermeden 7 günlük erişim.</p></header>
        <div className="subscriptionPrice"><strong>Ücretsiz</strong><span>/ 7 gün</span></div>
        <ul>{['Master Trade analiz, grafik ve piyasa izleme','AI Analist · mevcut kredi sınırı','Original V2, Demo Auto Trade ve pozisyon koruması','Kais sohbeti, asistan araçları ve proaktif mesajlar dahil değil'].map(feature => <li key={feature}><Check/>{feature}</li>)}</ul>
        <p>LIVE işlem MASTER MODE ile açılır</p>
        {active && <p role="status">Kalan süre: <strong>{days} gün</strong></p>}
        {used && !active && <p role="status">Deneme sona erdi. Erişimini sürdürmek için MASTER MODE’a geç.</p>}
        <small className="subscriptionTerms">Hesap başına yalnız bir deneme. E-posta doğrulaması zorunludur. Otomatik ücret alınmaz.</small>
        <button type="button" onClick={() => void start()} disabled={!trialEnabled || !authenticated || loading || busy || !trial || used}>{label}</button>
        {error && <p role="alert">{error} <button type="button" onClick={() => setAttempt(value => value + 1)}>Yeniden dene</button></p>}
      </article>
      <article className="subscriptionPlan subscriptionPlanFeatured">
        <header><span>TAM ERİŞİM</span><h2>MASTER MODE</h2><p>Yapay zekâ dahil premium çalışma alanı.</p></header>
        <div className="subscriptionPrice"><strong>{PLAN_BY_CODE.MASTER_MODE.monthlyPrice.toFixed(2)} USD</strong><span>/ ay</span></div>
        <ul>{['Kais sohbeti, asistan araçları ve proaktif mesajlar','AI Analist ve Master Trade analiz','Demo ve LIVE işlem · mevcut güvenlik kontrolleriyle','Original V2, Auto Trade ve pozisyon koruması'].map(feature => <li key={feature}><Check/>{feature}</li>)}</ul>
        <small className="subscriptionTerms">Binance ile elle onaylı 30 günlük dönem. Denemede ödeme onaylanırsa dönem deneme bitişinden başlar. Otomatik yenileme yoktur.</small>
        <button type="button" disabled={!binanceEnabled || !authenticated} onClick={() => setPaymentOpen(true)}>{binanceEnabled ? 'Binance ile öde' : 'Yakında'}</button>
      </article>
    </section>
    {paymentOpen && binanceEnabled && <div className="freeTrialPayment"><BinanceManualPayment/></div>}
  </>
}
