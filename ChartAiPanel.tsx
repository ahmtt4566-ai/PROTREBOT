import {useEffect, useRef, useState} from 'react'
import {createPortal} from 'react-dom'
import {Camera, Sparkles, X} from 'lucide-react'
import {ChartClientError, chartRequest, chartResult, chartUsage, captureChart, CHART_AI_DISCLAIMER, CHART_AI_PRIVACY,
  imageBase64, validateChartUpload, type ChartResult, type ChartSnapshot, type ChartUsage} from './chart-ai-client'
import './chart-ai.css'

type Pending = {request_key: string; symbol: string; timeframe: string}
type Preview = {blob: Blob; url: string; context: ChartSnapshot; source: 'screenshot' | 'upload'}
type Props = {userId: string; symbol: string; timeframe: string; snapshot: () => ChartSnapshot; chart: () => SVGSVGElement | null}
const QUESTIONS = ['Bu grafiği yorumla', 'Riskler neler?', 'Yukarı ve aşağı senaryolar']
const message = (error: unknown) => error instanceof Error ? error.message : 'Grafik AI işlemi tamamlanamadı.'

export default function ChartAiPanel(props: Props) {
  const [usage, setUsage] = useState<ChartUsage | null>(null)
  const [hidden, setHidden] = useState(false)
  const [loading, setLoading] = useState(true)
  const [pilotLocked, setPilotLocked] = useState(false)
  const [open, setOpen] = useState(false)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [question, setQuestion] = useState(QUESTIONS[0])
  const [consent, setConsent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [preparing, setPreparing] = useState(false)
  const [result, setResult] = useState<ChartResult | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)
  const [identity, setIdentity] = useState<Pending | null>(null)
  const [queryRevision, setQueryRevision] = useState(0)
  const dialog = useRef<HTMLDialogElement>(null)
  const controller = useRef<AbortController | null>(null)
  const locked = useRef(false)
  const preparation = useRef(0)
  const alive = useRef(false)
  const storageKey = `kaistrade-chart-ai-request:${props.userId}`

  const refresh = async (signal?: AbortSignal) => {
    const next = await chartUsage(signal)
    if (alive.current && !signal?.aborted) { setUsage(next); setHidden(false) }
    return next
  }
  const receive = (next: ChartResult, expected: Pending) => {
    if (next.request_key !== expected.request_key) throw new Error('Analiz istek kimliği uyuşmuyor.')
    if (!alive.current) return
    setResult(next)
    if (next.status !== 'processing') {
      setPending(null)
      sessionStorage.removeItem(storageKey)
      setBusy(false)
      locked.current = false
      if (next.error) setError(next.error.message)
    }
  }
  useEffect(() => {
    alive.current = true
    const abort = new AbortController()
    controller.current = abort
    void refresh(abort.signal).then(() => {
      if (abort.signal.aborted) return
      const raw = sessionStorage.getItem(storageKey)
      if (!raw) return
      const saved: unknown = JSON.parse(raw)
      if (typeof saved !== 'object' || saved === null || !('request_key' in saved) || typeof saved.request_key !== 'string'
          || !/^[0-9a-f-]{36}$/i.test(saved.request_key) || !('symbol' in saved) || typeof saved.symbol !== 'string'
          || !('timeframe' in saved) || typeof saved.timeframe !== 'string') throw new Error('Kayıtlı analiz kimliği geçersiz.')
      const restored = {request_key: saved.request_key, symbol: saved.symbol, timeframe: saved.timeframe}
      setPending(restored)
      setIdentity(restored)
      setBusy(true)
      locked.current = false
    }).catch((failure: unknown) => {
      if (!alive.current || abort.signal.aborted) return
      if (failure instanceof ChartClientError && (failure.code === 'feature_disabled' || failure.status === 404)) setHidden(true)
      else if (failure instanceof ChartClientError && failure.code === 'owner_only') setPilotLocked(true)
      else setError(message(failure))
    }).finally(() => { if (alive.current && !abort.signal.aborted) setLoading(false) })
    return () => { alive.current = false; abort.abort(); preparation.current++ }
  }, [storageKey])

  useEffect(() => {
    if (!preview) return
    return () => URL.revokeObjectURL(preview.url)
  }, [preview])
  useEffect(() => {
    preparation.current++
    setPreparing(false)
    setPreview(null)
  }, [props.symbol, props.timeframe])
  useEffect(() => {
    if (!open || !dialog.current) return
    const previous = document.activeElement
    const element = dialog.current
    element.showModal()
    return () => { element.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus() }
  }, [open])

  useEffect(() => {
    if (!pending) return
    const abort = new AbortController()
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const poll = async () => {
      try {
        const next = chartResult(await chartRequest(`/requests/${pending.request_key}`, {signal: abort.signal}))
        if (!active) return
        if (next.status !== 'processing') await refresh(abort.signal)
        if (!active) return
        receive(next, pending)
        if (next.status === 'processing') timer = setTimeout(() => void poll(), 2000)
      } catch (failure: unknown) {
        if (active && !abort.signal.aborted) { setError(`${message(failure)} Yeni üretim başlatılmadı; sonucu yeniden sorgulayabilirsin.`); setBusy(false) }
      }
    }
    // The initial POST owns its HTTP wait; polling starts only on a processing/unknown result.
    if (!locked.current || result?.status === 'processing') void poll()
    return () => { active = false; abort.abort(); if (timer) clearTimeout(timer) }
  }, [pending, result?.status, queryRevision])

  const prepare = async (file?: File) => {
    const revision = ++preparation.current
    setPreparing(true); setError(''); setPreview(null)
    try {
      const context = props.snapshot()
      const blob = file ?? await captureChart(props.chart())
      await validateChartUpload(blob)
      if (!alive.current || revision !== preparation.current) return
      setPreview({blob, url: URL.createObjectURL(blob), context, source: file ? 'upload' : 'screenshot'})
      setResult(null)
    } catch (failure: unknown) {
      if (alive.current && revision === preparation.current) setError(message(failure))
    } finally {
      if (alive.current && revision === preparation.current) setPreparing(false)
    }
  }
  const send = async () => {
    if (locked.current || !preview || !usage || !usage.analysis_enabled) return
    locked.current = true
    setBusy(true); setError(''); setResult(null)
    let started = false
    try {
      if (usage.acknowledgement.required) {
        if (!consent) throw new Error('Önce kullanım uyarısını okuyup onayla.')
        await chartRequest('/acknowledgement', {method: 'POST', signal: controller.current?.signal,
          headers: {'Content-Type': 'application/json'}, body: JSON.stringify({policy_version: usage.acknowledgement.policy_version, accepted: true})})
        const confirmed = await refresh(controller.current?.signal)
        if (confirmed.acknowledgement.required) throw new Error('Kullanım onayı sunucuda doğrulanamadı; analiz gönderilmedi.')
      }
      if (!alive.current || controller.current?.signal.aborted) return
      const data = await imageBase64(preview.blob)
      if (!alive.current || controller.current?.signal.aborted) return
      const job = {request_key: crypto.randomUUID(), symbol: preview.context.symbol, timeframe: preview.context.timeframe}
      sessionStorage.setItem(storageKey, JSON.stringify(job))
      setPending(job)
      setIdentity(job)
      started = true
      const next = chartResult(await chartRequest('/analyze', {
        method: 'POST', signal: controller.current?.signal, headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...job, question: question.trim(), image: {
          source: preview.source, media_type: preview.blob.type, data_base64: data,
        }, context: preview.context}),
      }))
      receive(next, job)
      if (alive.current) await refresh(controller.current?.signal)
    } catch (failure: unknown) {
      if (!alive.current) return
      setError(message(failure))
      setBusy(false)
      locked.current = false
      if (!started) setPending(null)
    }
  }
  if (hidden || loading) return null
  return <>
    <button type="button" className="chartAiButton" disabled={loading} onClick={() => setOpen(true)} title="Grafiği açıkla; işlem veya emir göndermez">
      <Sparkles aria-hidden="true"/>Yapay Zekaya Sor
    </button>
    {open && createPortal(<dialog ref={dialog} className="chartAiDialog" aria-labelledby="chart-ai-title" onCancel={event => {event.preventDefault(); setOpen(false)}}>
      <header><div><small>OWNER PİLOTU · SALT OKUNUR</small><h2 id="chart-ai-title">Grafik AI</h2></div><button type="button" autoFocus aria-label="Grafik AI panelini kapat" onClick={() => setOpen(false)}><X/></button></header>
      <div className="chartAiBody">
        <p>{CHART_AI_PRIVACY}</p>
        <p>Yalnız grafiğin görseli ve gösterilen piyasa verileri gönderilir. Hesap, bakiye ve anahtar bilgileri gönderilmez.</p>
        {usage && <p>Kalan hak: <strong>{usage.quota.unlimited ? 'Sınırsız (OWNER)' : usage.quota.remaining}</strong> · Aylık bütçe sınırı geçerlidir.</p>}
        {error && <p role="alert">{error}</p>}
        {pilotLocked && <p role="status">Grafik AI pilotu henüz hesabına açılmadı. Şu anda yalnız OWNER analiz yapabilir.</p>}
        {!pilotLocked && <>
        {!usage && <button type="button" onClick={() => {setError(''); void refresh().catch(failure => setError(message(failure)))}}>Durumu yeniden oku</button>}
        {usage && !usage.analysis_enabled && <p role="status">{usage.availability.message}</p>}
        <div className="chartAiInputs">
          <button type="button" disabled={busy || Boolean(pending) || preparing} onClick={() => void prepare()}><Camera/>Grafiği yakala</button>
          <label>Görsel yükle<input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy || Boolean(pending) || preparing}
            onChange={event => {const file = event.target.files?.[0]; event.target.value = ''; if (file) void prepare(file)}}/></label>
        </div>
        {preparing && <p role="status">Görsel hazırlanıyor…</p>}
        {preview && <figure><img src={preview.url} alt="Gönderilecek grafik önizlemesi"/><figcaption>{preview.context.symbol} · {preview.context.timeframe} · {new Date(preview.context.captured_at).toLocaleString('tr-TR')}</figcaption></figure>}
        <div className="chartAiPresets">{QUESTIONS.map(text => <button type="button" key={text} disabled={busy || Boolean(pending)} onClick={() => setQuestion(text)}>{text}</button>)}</div>
        <label className="chartAiQuestion">Sorun<textarea value={question} maxLength={800} disabled={busy || Boolean(pending)} onChange={event => setQuestion(event.target.value)}/></label>
        {usage?.acknowledgement.required && <label className="chartAiConsent"><input type="checkbox" checked={consent} disabled={busy} onChange={event => setConsent(event.target.checked)}/>Kullanım uyarısını okudum, anladım.</label>}
        <button type="button" className="chartAiSend" disabled={!preview || !question.trim() || !usage?.analysis_enabled || preparing || busy || Boolean(pending)
          || usage.acknowledgement.required && !consent || !usage.quota.unlimited && (usage.quota.remaining ?? 0) <= 0} onClick={() => void send()}>Grafiği analiz et</button>
        {pending && <p role="status">{pending.symbol} · {pending.timeframe}: {busy ? 'Yanıt hazırlanıyor; en fazla iki üretim, toplam analiz sınırı 60 saniye.' : 'Analiz durumu doğrulanmalı.'}</p>}
        {pending && !busy && <button type="button" onClick={() => {locked.current = false; setBusy(true); setError(''); setQueryRevision(value => value + 1)}}>Sonucu yeniden sorgula</button>}
        {pending && !busy && <button type="button" onClick={() => {
          if (!window.confirm('Bu işlem yalnız tarayıcıdaki kayıt bilgisini bırakır; başlamış analizi iptal etmez. Devam edilsin mi?')) return
          sessionStorage.removeItem(storageKey); setPending(null); setResult(null); locked.current = false
        }}>Bekleyen kayıt bilgisini bırak</button>}
        {result?.result_text && <section className="chartAiResult" aria-label="Grafik AI sonucu">
          {identity && <h3>{identity.symbol} · {identity.timeframe}</h3>}{result.result_text}
        </section>}
        </>}
      </div>
      <footer>{usage?.acknowledgement.text ?? `${CHART_AI_PRIVACY}\n${CHART_AI_DISCLAIMER}`}</footer>
    </dialog>, document.body)}
  </>
}
