import { useCallback, useEffect, useRef, useState } from 'react'
import { LockKeyhole } from 'lucide-react'
import { API_BASE } from './api'
import { useMemberAccess } from './premium-access'
import './analyst-credit-ui.css'

export type AnalystBudget = {remaining: number | null; total: number; resetsAt: string | null; unlimited: boolean}
export type AnalysisPurchase<T> = AnalystBudget & {result: T; cached: boolean; cacheExpiresAt: string}

function validBudget(payload: AnalystBudget): boolean {
  return Number.isInteger(payload.total) && payload.total > 0
    && (payload.unlimited === true ? payload.remaining === null : Number.isInteger(payload.remaining) && payload.remaining !== null && payload.remaining >= 0 && payload.remaining <= payload.total)
    && (payload.resetsAt === null || typeof payload.resetsAt === 'string' && Number.isFinite(Date.parse(payload.resetsAt)))
}

export function useAnalystCredits() {
  const {premium, ready, userId} = useMemberAccess()
  const [budget, setBudget] = useState<AnalystBudget | null>(null)
  const [error, setError] = useState('')
  const [now, setNow] = useState(Date.now())
  const budgetController = useRef<AbortController | null>(null)
  const generation = useRef(0)
  const resetChecked = useRef<string | null>(null)
  const refresh = useCallback(async () => {
    if (!ready || premium || !userId) return
    budgetController.current?.abort()
    const controller = new AbortController()
    budgetController.current = controller
    const version = generation.current
    try {
      const response = await fetch(`${API_BASE}/analyst/credits`, {signal: controller.signal})
      const payload = await response.json() as AnalystBudget & {detail?: string}
      if (!response.ok) throw new Error(payload.detail || 'Kredi bilgisi alınamadı.')
      if (!validBudget(payload)) throw new Error('Sunucudan geçersiz kredi bilgisi döndü.')
      if (!controller.signal.aborted && version === generation.current) { setBudget(payload); setNow(Date.now()); setError('') }
    } catch (reason: unknown) {
      if (!controller.signal.aborted && version === generation.current) setError(reason instanceof Error ? reason.message : 'Kredi bilgisi alınamadı.')
    }
  }, [premium, ready, userId])
  useEffect(() => {
    generation.current++
    setBudget(null)
    void refresh()
    const update = () => { setNow(Date.now()); if (document.visibilityState === 'visible') void refresh() }
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    window.addEventListener('focus', update)
    document.addEventListener('visibilitychange', update)
    return () => {
      budgetController.current?.abort()
      window.clearInterval(timer)
      window.removeEventListener('focus', update)
      document.removeEventListener('visibilitychange', update)
    }
  }, [refresh])
  useEffect(() => {
    if (budget?.resetsAt && now >= Date.parse(budget.resetsAt) && resetChecked.current !== budget.resetsAt) {
      resetChecked.current = budget.resetsAt
      void refresh()
    }
  }, [budget?.resetsAt, now, refresh])
  const purchase = async <T,>(symbol: string, timeframe: string, signal: AbortSignal): Promise<AnalysisPurchase<T>> => {
    if (!ready || !userId) throw new Error('Üyelik yetkisi doğrulanmadan analiz açılamaz.')
    generation.current++
    const response = await fetch(`${API_BASE}/analyst/consume`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, signal,
      body: JSON.stringify({symbol, timeframe, idempotency_key: crypto.randomUUID()}),
    })
    const payload = await response.json() as AnalysisPurchase<T> & {detail?: string}
    if (signal.aborted) throw new DOMException('Analysis request cancelled.', 'AbortError')
    generation.current++
    if (validBudget(payload)) { setBudget(payload); setNow(Date.now()) }
    if (!response.ok) throw new Error(payload.detail || 'Analiz açılamadı.')
    if (!validBudget(payload) || !payload.result || !Number.isFinite(Date.parse(payload.cacheExpiresAt))) throw new Error('Sunucudan geçersiz analiz yanıtı döndü.')
    setError('')
    return payload
  }
  return {budget, error, now, refresh, purchase, exhausted: !premium && budget?.remaining === 0}
}

export function renewalText(resetsAt: string | null, now: number): string {
  if (!resetsAt) return 'İlk analizde 24 saatlik pencere başlar'
  const minutes = Math.max(0, Math.ceil((Date.parse(resetsAt) - now) / 60000))
  return `${Math.floor(minutes / 60)} sa ${minutes % 60} dk`
}

export function AnalystCreditBadge({budget, now}: {budget: AnalystBudget | null; now: number}) {
  const {premium, ready} = useMemberAccess()
  if (premium || !ready) return null
  if (budget?.unlimited) return <div className="analystCreditBadge" role="status"><strong>Kredi yükleniyor</strong></div>
  return <div className={`analystCreditBadge${budget?.remaining === 0 ? ' creditEmpty' : budget?.remaining !== null && budget?.remaining !== undefined && budget.remaining <= 10 ? ' creditLow' : ''}`} role="status">
    <strong>{budget ? `Kredi ${budget.remaining}/${budget.total}` : 'Kredi yükleniyor'}</strong>
    {budget && <small>{budget.resetsAt ? `Yenilenme: ${renewalText(budget.resetsAt, now)}` : renewalText(null, now)}</small>}
  </div>
}

export function AnalystCreditsExhausted({budget, now}: {budget: AnalystBudget; now: number}) {
  return <section className="analystCreditExhausted" role="status">
    <div className="analystCreditPreview" aria-hidden="true"><span/><span/><span/></div>
    <div className="analystCreditEmptyCard"><LockKeyhole aria-hidden="true"/><h3>Krediler {renewalText(budget.resetsAt, now)} sonra yenilenir</h3><p>Analyst sayfası açık kalır. Premium ile sınırsız analiz açabilirsiniz.</p><a className="premiumPrimary" href="/pricing">Premium'a geç</a></div>
  </section>
}
