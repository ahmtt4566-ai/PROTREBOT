import {useEffect, useState} from 'react'
import {API_BASE} from './api'
import {fetchWithTimeout} from './master-trade-request'
import {parseMarketQuotes, type MarketQuote} from './master-market-data'
import {coinDisplayName} from './src/components/coin-symbol'

export type MasterMarketFeed = {rows: readonly MarketQuote[]; error: string; stale: boolean}
let lastQuotes: MarketQuote[] = []

export function useMasterMarketQuotes(enabled: boolean): MasterMarketFeed {
  const [feed, setFeed] = useState<MasterMarketFeed>({rows: lastQuotes, error: '', stale: lastQuotes.length > 0})
  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    let active = true, inFlight = false, retryAt = 0
    const refresh = async () => {
      if (inFlight || Date.now() < retryAt || document.hidden) return
      inFlight = true
      retryAt = Date.now() + 3000
      try {
        const response = await fetchWithTimeout(`${API_BASE}/markets?all=true`, {signal: controller.signal}, 10000)
        if (!response.ok) {
          const retry = Number(response.headers.get('Retry-After'))
          retryAt = Date.now() + Math.max(3000, Number.isFinite(retry) && retry > 0 ? retry * 1000 : 30000)
          throw new Error(`Piyasa verisi alınamadı (HTTP ${response.status}).`)
        }
        const rows = parseMarketQuotes(await response.json()).map(row => ({...row, name: coinDisplayName(row.symbol)}))
        if (!active) return
        lastQuotes = rows
        setFeed({rows, stale: response.headers.get('X-Market-Stale') === '1', error: ''})
      } catch (error: unknown) {
        if (active && !controller.signal.aborted) {
          retryAt = Math.max(retryAt, Date.now() + 30000)
          setFeed(current => ({...current, stale: true, error: error instanceof Error ? error.message : 'Piyasa verisi alınamadı.'}))
        }
      } finally {inFlight = false}
    }
    void refresh()
    const timer = globalThis.setInterval(() => void refresh(), 3000)
    const visible = () => {if (!document.hidden) void refresh()}
    document.addEventListener('visibilitychange', visible)
    return () => {active = false; controller.abort(); globalThis.clearInterval(timer); document.removeEventListener('visibilitychange', visible)}
  }, [enabled])
  return feed
}
