import { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type RefCallback } from 'react'
import { Activity, BarChart3, Cable, Check, ChevronDown, Clock3, Crosshair, Gauge, History, ListChecks, LockKeyhole, ShieldX, UnlockKeyhole, Wallet, X, XCircle } from 'lucide-react'
import { API_BASE, userSessionToken } from './api'
import LiveTradingPanel, { type SharedConnectionStatus, type SharedLiveStatus } from './frontend/src/LiveTradingPanel'
import { buildTradeDecision, buildTriggerMonitor, type MtfAnalysis, type TradeDecision, type TriggerLifecycle, type TriggerMonitor } from './masterTradeDecision'
import { MasterTradeChartLabels, MasterTradeMetricTile, MasterTradeMetricVisual, MasterTradePresentation, MasterTradeValue, masterTradeTone } from './MasterTradeLayout'
import { PremiumWorkspace, useMemberAccess } from './premium-access'
import {useKaisWorkspaceReaction} from './useKaisPageReactions'
import { fetchWithTimeout } from './master-trade-request'
import { analysisPrice, analysisValue, autoTradePresentation, masterLayoutV2Enabled, MASTER_LAYOUT_DESKTOP_WIDTH, MASTER_MTF_INTERVALS } from './master-trade-presentation'
import './master-trade-analysis-v2.css'
import MasterTradeReference from './MasterTradeReference'
import {useMasterMarketQuotes} from './useMasterMarketQuotes'
import {useTradingPreferences} from './useTradingPreferences'
import {MarketDataFailure, marketDataFailure, marketDataResponseError} from './master-trade-data-error'
import {parseRequiredMarketQuotes} from './master-market-data'

type TradeSide = 'LONG' | 'SHORT'
type TradeHistoryRow = {
  id: string
  symbol: string
  side: TradeSide
  entryPrice: number | null
  exitPrice: number | null
  quantity: number | null
  leverage: number | null
  margin: number | null
  stopLoss: number | null
  tp1: number | null
  tp2: number | null
  tp3: number | null
  realizedPnl: number
  pnlPercent: number | null
  fees: number | null
  funding: number | null
  openTime: string
  closeTime: string
  duration: string | null
  closeReason: string | null
  source: string
  analysisScore: number | null
  opportunityScore: number | null
  scanCycle: string | null
}

type MarketRow = { symbol: string; display: string; price: number; change: number; volume: number; status?: string; contractType?: string; quoteAsset?: string; filters?: unknown[] }
type ScannerCandidate = { symbol: string; direction: string; confidence: number; final_decision_score: number; opportunity_score: number; smart_score: number; price?: number; change?: number; volume?: number; display?: string }
type Candle = { time: number; open: number; high: number; low: number; close: number; volume: number }
type Analysis = { direction?: string; confidence?: number; entry?: number; stop_loss?: number; tp1?: number; tp2?: number; tp3?: number; risk_reward?: number; trend?: string; momentum?: string; rsi?: number; macd?: number; adx?: number; atr?: number; support?: number; resistance?: number; radar?: { trap_score?: number; breakout_quality?: number; entry_timing?: string }; volume_ratio?: number; normalized_signal?: string }
type AccountPlan = { symbol?: string; stop_loss?: string; targets?: string[]; margin_usdt?: number; created_at?: string }
type AccountPosition = { symbol: string; position_side?: 'BOTH' | 'LONG' | 'SHORT'; direction?: TradeSide; quantity?: number; entry_price?: number; mark_price?: number; liquidation_price?: number; unrealized_pnl?: number; leverage?: number | null; margin_type?: string | null; stop_loss?: number; tp1?: number; tp2?: number; tp3?: number; age?: string }
type AccountOrder = { symbol?: string; side?: string; type?: string; price?: number; trigger_price?: number; quantity?: number; status?: string; reduce_only?: boolean }
type AccountSnapshot = { wallet_balance?: number; available_balance?: number; margin_balance?: number; unrealized_pnl?: number; positions?: AccountPosition[]; open_orders?: AccountOrder[]; open_algo_orders?: AccountOrder[]; plans?: AccountPlan[]; last_checked?: string | null; connected?: boolean; last_error?: string | null; limits?: { max_open_positions?: number; max_leverage?: number; max_margin_usdt?: number } }
type BackendRiskPreview = { estimated_stop_loss_usdt?: number; stop_distance_pct?: number; notional_usdt?: number; margin_usdt?: number; max_stop_distance_pct?: number; capped?: boolean }
type PerformanceSnapshot = { total_trades: number; wins: number; losses: number; win_rate: number; total_profit: number; total_loss: number; net_profit: number; average_trade: number; best_trade: number; worst_trade: number; profit_factor: number | null; average_win: number | null; average_loss: number | null; losing_streak: number; max_drawdown: number; history_quality: string }
type MasterTradeSnapshot = { symbol: string; timeframe: string; candles: Candle[]; analysis: Analysis | null; mtf: MtfAnalysis[]; mtfError?: string; account: AccountSnapshot | null; currentPrice: number | null; priceUpdatedAt: string | null; marketUpdatedAt: string | null; accountUpdatedAt: string | null; marketError: string; accountError: string }
type AccountSyncState = 'READY' | 'EMPTY' | 'STALE' | 'DISCONNECTED' | 'DATA_UNAVAILABLE'
type TradeHistorySyncState = 'READY' | 'EMPTY' | 'STALE' | 'DISCONNECTED' | 'UNAVAILABLE'
type CloseLifecycleState = 'IDLE' | 'CLOSING' | 'CLOSED' | 'CLOSE_FAILED' | 'HISTORY_SYNC_FAILED' | 'ACCOUNT_SYNC_FAILED' | 'SYNC_FAILED'

const fmtNum = (value: number | null | undefined, decimals = 2) =>
  value === undefined || value === null ? '—' : value.toLocaleString('tr-TR', { maximumFractionDigits: decimals, minimumFractionDigits: decimals })

const fmtCompact = (value: number | null | undefined) =>
  value === undefined || value === null ? '—' : value.toLocaleString('tr-TR', { maximumFractionDigits: 2, minimumFractionDigits: 2 })

const fmtPnl = (value: number | null | undefined) =>
  value === undefined || value === null || !Number.isFinite(value) ? '—' : value.toLocaleString('tr-TR', { maximumFractionDigits: 2, minimumFractionDigits: 2 })

const fmtDecisionNumber = (value: number | null | undefined, decimals = 0) =>
  value === null || value === undefined || !Number.isFinite(value) ? '--' : value.toLocaleString('en-US', { maximumFractionDigits: decimals, minimumFractionDigits: decimals })

const fmtMarketPrice = (value: number | null | undefined) => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '--'
  const decimals = Math.abs(value) >= 100 ? 2 : Math.abs(value) >= 1 ? 4 : 6
  return value.toLocaleString('en-US', { maximumFractionDigits: decimals, minimumFractionDigits: decimals })
}

const fmtSigned = (value: number | null | undefined) => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '--'
  return `${value >= 0 ? '+' : ''}${analysisValue(value)}`
}

const fmtSignalAge = (seconds: number | null) => {
  if (seconds === null) return '--'
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

const MTF_INTERVALS = MASTER_MTF_INTERVALS
const MASTER_TRADE_TABS = [
  {id: 'analiz', label: 'Analiz', icon: BarChart3},
  {id: 'canli', label: 'Canlı İşlem', icon: Activity},
  {id: 'pozisyonlar', label: 'Pozisyonlar', icon: Wallet},
  {id: 'baglanti', label: 'Bağlantı', icon: Cable},
] as const
type MasterTradeTab = typeof MASTER_TRADE_TABS[number]['id']
const subscribeTab = (notify: () => void) => {
  window.addEventListener('popstate', notify)
  return () => window.removeEventListener('popstate', notify)
}
const readTab = (): MasterTradeTab => {
  const requested = new URLSearchParams(window.location.search).get('tab')
  return MASTER_TRADE_TABS.find(tab => tab.id === requested)?.id ?? 'analiz'
}
const subscribePresentation = (notify: () => void) => {
  const media = window.matchMedia(`(min-width: ${MASTER_LAYOUT_DESKTOP_WIDTH}px)`)
  media.addEventListener('change', notify)
  window.addEventListener('popstate', notify)
  return () => {
    media.removeEventListener('change', notify)
    window.removeEventListener('popstate', notify)
  }
}
const readPresentation = () => masterLayoutV2Enabled(window.location.search, window.innerWidth)
const navigateTab = (tab: MasterTradeTab) => {
  const url = new URL(window.location.href)
  url.searchParams.set('tab', tab)
  window.history.pushState(window.history.state, '', url)
  window.dispatchEvent(new PopStateEvent('popstate'))
}
const fetchMtfAnalyses = async (symbol: string, signal?: AbortSignal) => {
  const results = await Promise.all(MTF_INTERVALS.map(async timeframe => {
    try {
      const response = await fetchWithTimeout(`${API_BASE}/analysis/${symbol}?interval=${timeframe}`, { signal }, 10000)
      if (!response.ok) throw await marketDataResponseError(response)
      const analysis = await response.json() as Analysis
      if (!analysis || typeof analysis.direction !== 'string' || !Number.isFinite(analysis.confidence)) throw new MarketDataFailure('backend', 'Backend geçerli analiz verisi döndürmedi.')
      return {analysis: {...analysis, timeframe}, error: ''}
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError' && signal?.aborted) throw error
      return {analysis: null, error: `MTF ${timeframe}: ${marketDataFailure(error).message}`}
    }
  }))
  return {analyses: results.map(result => result.analysis).filter(item => item !== null), error: results.map(result => result.error).filter(Boolean).join(' · ')}
}

export default function MasterTrade({ onBack, assistantSlotRef }: { onBack?: () => void; assistantSlotRef?: RefCallback<HTMLDivElement> }) {
  const {premium} = useMemberAccess()
  const layoutTab = useSyncExternalStore(subscribeTab, readTab, () => 'analiz' as MasterTradeTab)
  const referenceEnabled = useSyncExternalStore(subscribePresentation, readPresentation, () => false)
  const masterLayoutV2 = referenceEnabled && layoutTab === 'analiz'
  const marketFeed = useMasterMarketQuotes(referenceEnabled)
  const tradingDefaults = useTradingPreferences()
  const defaultsApplied = useRef(false)
  const decisionColumn = useRef<HTMLElement>(null)
  const shortcutPending = useRef(false)
  const reactionArea = useRef<HTMLElement>(null)
  useKaisWorkspaceReaction(layoutTab, reactionArea)
  const [history, setHistory] = useState<TradeHistoryRow[]>([])
  const [historySyncState, setHistorySyncState] = useState<TradeHistorySyncState>('READY')
  const [accountSyncState, setAccountSyncState] = useState<AccountSyncState>('READY')
  const [performanceSnapshot, setPerformanceSnapshot] = useState<PerformanceSnapshot | null>(null)
  const [dailyPerformance, setDailyPerformance] = useState<PerformanceSnapshot | null>(null)
  const [liveStatus, setLiveStatus] = useState<SharedLiveStatus | null>(null)
  const [liveConnections, setLiveConnections] = useState<SharedConnectionStatus | null>(null)
  const [selectedTrade, setSelectedTrade] = useState<TradeHistoryRow | null>(null)
  const [markets, setMarkets] = useState<MarketRow[]>([])
  const [scannerCandidates, setScannerCandidates] = useState<ScannerCandidate[]>([])
  const [marketQuery, setMarketQuery] = useState('')
  const [marketLoading, setMarketLoading] = useState(true)
  const [marketError, setMarketError] = useState('')
  const [interval, setInterval] = useState('15m')
  const [snapshot, setSnapshot] = useState<MasterTradeSnapshot | null>(null)
  const [marketRefreshNonce, setMarketRefreshNonce] = useState(0)
  const [accountRefreshNonce, setAccountRefreshNonce] = useState(0)
  const accountRef = useRef<AccountSnapshot | null>(null)
  const historyRef = useRef<TradeHistoryRow[]>([])
  const positionActionInFlight = useRef(false)
  const timelineKeysRef = useRef<string[]>([])
  const [decisionTimeline, setDecisionTimeline] = useState<Array<{ time: string; message: string }>>([])
  const triggerLifecycleRef = useRef<TriggerLifecycle | null>(null)
  const [dataLoading, setDataLoading] = useState(false)
  const [dataError, setDataError] = useState('')
  const [chartHoverIndex, setChartHoverIndex] = useState<number | null>(null)
  const [showChartLevels, setShowChartLevels] = useState(true)
  const [showChartVolume, setShowChartVolume] = useState(true)
  const [draft, setDraft] = useState({ market: 'BTCUSDT', leverage: 2, margin: 10, quantity: 0.08, entry: 61350, stopLoss: 60800, tp1: 61850, tp2: 62400, tp3: 63150 })
  const [backendRiskPreview, setBackendRiskPreview] = useState<BackendRiskPreview | null>(null)
  const [backendRiskPreviewError, setBackendRiskPreviewError] = useState('')
  const [positionAction, setPositionAction] = useState<{ mode: 'DETAILS' | 'REDUCE' | 'CLOSE'; position: AccountPosition } | null>(null)
  const [positionActionBusy, setPositionActionBusy] = useState(false)
  const [positionActionError, setPositionActionError] = useState('')
  const [closeLifecycle, setCloseLifecycle] = useState<{ state: CloseLifecycleState; message: string; detail?: string }>({ state: 'IDLE', message: 'READY' })
  const [reduceQuantity, setReduceQuantity] = useState('')
  const [lastAccountSyncAt, setLastAccountSyncAt] = useState<string | null>(null)
  const [lastHistorySyncAt, setLastHistorySyncAt] = useState<string | null>(null)
  const [lastSuccessfulRefreshAt, setLastSuccessfulRefreshAt] = useState<string | null>(null)
  const accountRefreshInFlight = useRef(false)
  const activeSnapshot = snapshot?.symbol === draft.market && snapshot.timeframe === interval ? snapshot : null
  const selectedQuote = referenceEnabled ? marketFeed.rows.find(row => row.symbol === draft.market) : undefined
  const currentPrice = selectedQuote?.price ?? activeSnapshot?.currentPrice ?? null
  const candles = activeSnapshot?.candles ?? []
  const analysis = activeSnapshot?.marketError || dataError ? null : activeSnapshot?.analysis ?? null
  const mtfAnalyses = activeSnapshot?.mtf ?? []
  const account = snapshot?.account ?? accountRef.current

  useEffect(() => {
    if (defaultsApplied.current || !tradingDefaults.preferences) return
    defaultsApplied.current = true
    if (!tradingDefaults.untouched()) return
    const preferences = tradingDefaults.preferences
    if (preferences.timeframe) setInterval(preferences.timeframe)
    const symbol = preferences.symbols?.[0]
    if (symbol) setDraft(current => ({...current, market: symbol, entry: 0, stopLoss: 0, tp1: 0, tp2: 0, tp3: 0}))
    if (preferences.trading_mode === 'AUTO' && !new URLSearchParams(location.search).has('tab')) navigateTab('canli')
  }, [tradingDefaults.preferences, tradingDefaults.untouched])

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setMarketLoading(true)
    let inFlight = false
    const refreshMarkets = async () => {
      if (!active || inFlight) return
      inFlight = true
      try {
        const response = await fetchWithTimeout(`${API_BASE}/markets?limit=50`, { signal: controller.signal })
        if (!response.ok) throw await marketDataResponseError(response,'markets')
        const items = parseRequiredMarketQuotes(await response.json())
        if (!active) return
        setMarkets(items)
        const selected = items.find(item => item.symbol === draft.market)
        if (selected) setSnapshot(current => current?.symbol === draft.market ? { ...current, currentPrice: selected.price, priceUpdatedAt: new Date().toISOString() } : current)
        setMarketError('')
      } catch (error) {
        if (active && !(error instanceof Error && error.name === 'AbortError')) setMarketError(`DATA STALE / DATA UNAVAILABLE · ${marketDataFailure(error).message}`)
      } finally {
        inFlight = false
        if (active) setMarketLoading(false)
      }
    }
    void refreshMarkets()
    const timer = window.setInterval(() => void refreshMarkets(), 15000)
    return () => { active = false; controller.abort(); window.clearInterval(timer) }
  }, [draft.market])

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    let inFlight = false
    setScannerCandidates([])
    const refreshScanner = async () => {
      if (!active || inFlight) return
      inFlight = true
      try {
        const response = await fetchWithTimeout(`${API_BASE}/analysis-universe?interval=${interval}&limit=40`, { signal: controller.signal }, 15000)
        if (!response.ok) throw new Error('Scanner data unavailable')
        const payload = await response.json() as { results?: ScannerCandidate[] }
        if (active) setScannerCandidates(Array.isArray(payload.results) ? payload.results : [])
      } catch (error) {
        if (active && !(error instanceof Error && error.name === 'AbortError')) {
          setScannerCandidates([])
          setMarketError(current => current || 'MARKET ANALYSIS DELAYED · RAW MARKETS ACTIVE')
        }
      } finally {
        inFlight = false
      }
    }
    void refreshScanner()
    const timer = window.setInterval(() => void refreshScanner(), 60000)
    return () => { active = false; controller.abort(); window.clearInterval(timer) }
  }, [interval])

  useEffect(() => {
    const controller = new AbortController()
    let firstLoad = true
    let active = true
    setSnapshot(current => current?.symbol === draft.market && current.timeframe === interval && !current.marketError ? current : null)
    setDataError('')
    let inFlight = false
    const refresh = async () => {
      if (inFlight) return
      inFlight = true
      if (firstLoad) setDataLoading(true)
      try {
        const [candleResponse, analysisResponse, nextMtf] = await Promise.all([
          fetchWithTimeout(`${API_BASE}/klines/${draft.market}?interval=${interval}&limit=160`, { signal: controller.signal }, 10000),
          fetchWithTimeout(`${API_BASE}/analysis/${draft.market}?interval=${interval}`, { signal: controller.signal }, 10000),
          fetchMtfAnalyses(draft.market, controller.signal),
        ])
        if (!candleResponse.ok) throw await marketDataResponseError(candleResponse)
        if (!analysisResponse.ok) throw await marketDataResponseError(analysisResponse)
        const nextCandles = await candleResponse.json() as Candle[]
        const nextAnalysis = await analysisResponse.json() as Analysis
        if (!active) return
        if (!Array.isArray(nextCandles) || !nextCandles.length || !nextCandles.every(candle => candle && [candle.time, candle.open, candle.high, candle.low, candle.close, candle.volume].every(Number.isFinite))) throw new MarketDataFailure('backend', 'Bu sembol için yeterli veri yok (backend geçerli mum verisi döndürmedi).')
        if (!nextAnalysis || typeof nextAnalysis.direction !== 'string' || !Number.isFinite(nextAnalysis.confidence)) throw new MarketDataFailure('backend', 'Bu sembol için yeterli veri yok (backend geçerli analiz verisi döndürmedi).')
        setSnapshot({ symbol: draft.market, timeframe: interval, candles: nextCandles, analysis: nextAnalysis, mtf: nextMtf.analyses, mtfError: nextMtf.error, account: accountRef.current, currentPrice: nextCandles[nextCandles.length - 1]?.close ?? null, priceUpdatedAt: new Date().toISOString(), marketUpdatedAt: new Date().toISOString(), accountUpdatedAt: null, marketError: '', accountError: '' })
        setDataError('')
      } catch (error) {
        if (active && !controller.signal.aborted) {
          const failure = marketDataFailure(error)
          setDataError(failure.message)
          setSnapshot(current => current ? {...current, marketError: failure.message} : current)
        }
      } finally {
        inFlight = false
        firstLoad = false
        if (active) setDataLoading(false)
      }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 30000)
    return () => { active = false; controller.abort(); window.clearInterval(timer) }
  }, [draft.market, interval, marketRefreshNonce])

  const refreshAccountData = async (forceLiveSnapshot = false) => {
    if (accountRefreshInFlight.current) return false
    accountRefreshInFlight.current = true
    try {
      const response = await Promise.all([
        forceLiveSnapshot
          ? fetchWithTimeout(`${API_BASE}/v25/connect/read-only`, {method: 'POST'})
          : fetchWithTimeout(`${API_BASE}/v25/status`),
        fetchWithTimeout(`${API_BASE}/exchange-connections/status`),
      ])
      const [accountResponse, connectionResponse] = response
      setLiveStatus(null)
      setLiveConnections(null)
      accountRef.current = null
      setSnapshot(current => current ? {...current, account:null} : current)
      if (connectionResponse.ok) setLiveConnections(await connectionResponse.json() as SharedConnectionStatus)

      if (accountResponse.status === 412) {
        setAccountSyncState('DATA_UNAVAILABLE')
        setHistorySyncState('UNAVAILABLE')
        setSnapshot(current => current ? { ...current, accountError: 'LIVE ACCOUNT NOT CONFIGURED' } : current)
        setLastAccountSyncAt(null)
        setLastHistorySyncAt(null)
        return false
      }

      const accountPayload = accountResponse.ok ? await accountResponse.json() as SharedLiveStatus & {
        account?:AccountSnapshot; plans?:AccountPlan[]; detail?:unknown;
        journal?:Array<Record<string,unknown>>; performance?:PerformanceSnapshot; daily_performance?:PerformanceSnapshot;
      } : null
      if (!accountResponse.ok || !accountPayload) {
        const detail = accountPayload && typeof accountPayload.detail === 'string'
          ? accountPayload.detail
          : `Account data unavailable (HTTP ${accountResponse.status}).`
        setAccountSyncState('DISCONNECTED')
        setSnapshot(current => current ? { ...current, accountError: detail } : current)
        setHistorySyncState('UNAVAILABLE')
        setLastAccountSyncAt(null)
        return false
      }
      setLiveStatus(accountPayload as SharedLiveStatus)

    const nextPositions = Array.isArray(accountPayload.account?.positions) ? accountPayload.account.positions : []
    const liveAccount = accountPayload.account ? { ...accountPayload.account, plans: accountPayload.plans } : null
    accountRef.current = liveAccount
    setAccountSyncState(nextPositions.length === 0 ? 'EMPTY' : 'READY')
    setLastAccountSyncAt(new Date().toISOString())
    setLastSuccessfulRefreshAt(new Date().toISOString())
    setSnapshot(current => current ? { ...current, account: liveAccount, accountUpdatedAt: new Date().toISOString(), accountError: '' } : current)

    if (Array.isArray(accountPayload.journal)) {
      const rows = accountPayload.journal.filter(item => item.verified_realized === true && typeof item.realized_pnl === 'number' && Number.isFinite(item.realized_pnl)).map((item, index) => {
        const direction = String(item.side || item.direction || '').toUpperCase()
        return {
          id: String(item.id || `journal-${index}`), symbol: String(item.symbol || '--'), side: direction === 'BUY' || direction === 'LONG' ? 'LONG' : 'SHORT',
          entryPrice: null, exitPrice: typeof item.price === 'number' ? item.price : null, quantity: typeof item.quantity === 'number' ? item.quantity : null,
          leverage: null, margin: null, stopLoss: null, tp1: null, tp2: null, tp3: null, realizedPnl: Number(item.realized_pnl), pnlPercent: null,
          fees: null, funding: null, openTime: String(item.created_at || ''), closeTime: String(item.created_at || ''), duration: null,
          closeReason: typeof item.reason === 'string' ? item.reason : null, source: String(item.source || 'BINANCE LIVE'), analysisScore: null, opportunityScore: null, scanCycle: null,
        } satisfies TradeHistoryRow
      })
      historyRef.current = rows
      setHistory(rows)
      setHistorySyncState(rows.length === 0 ? 'EMPTY' : 'READY')
      setLastHistorySyncAt(rows.length ? new Date().toISOString() : null)
    } else {
      historyRef.current = []
      setHistory([])
      setHistorySyncState('UNAVAILABLE')
      setLastHistorySyncAt(null)
    }

    setPerformanceSnapshot(accountPayload.performance ?? null)
    setDailyPerformance(accountPayload.daily_performance ?? null)
      return connectionResponse.ok
    } catch (error) {
      setLiveStatus(null)
      setLiveConnections(null)
      accountRef.current = null
      setAccountSyncState('DISCONNECTED')
      setHistorySyncState('UNAVAILABLE')
      setSnapshot(current => current ? {...current, account:null, accountError:error instanceof Error ? error.message : 'ACCOUNT DISCONNECTED'} : current)
      throw error
    } finally {
      accountRefreshInFlight.current = false
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    const refreshAccount = async () => {
      try {
        const ok = await refreshAccountData()
        if (!ok && !(accountRef.current?.last_error)) {
          setSnapshot(current => current ? { ...current, accountError: 'ACCOUNT DISCONNECTED' } : current)
        }
      } catch (error) {
        if (error instanceof Error && error.name !== 'AbortError') {
          setAccountSyncState('DISCONNECTED')
          setHistorySyncState('UNAVAILABLE')
          setSnapshot(current => current ? { ...current, accountError: 'ACCOUNT DISCONNECTED' } : current)
        }
      }
    }
    void refreshAccount()
    const timer = window.setInterval(() => void refreshAccount(), 5000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [accountRefreshNonce])

  useEffect(() => {
    if (!premium) return
    const controller = new AbortController()
    const timer = window.setTimeout(async () => {
      if (!(draft.entry > 0) || !(draft.stopLoss > 0) || !(draft.margin >= 5) || !(draft.leverage >= 1)) {
        setBackendRiskPreview(null)
        return
      }
      try {
        const response = await fetchWithTimeout(`${API_BASE}/v25/risk/preview`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({entry: draft.entry, stop_loss: draft.stopLoss, margin_usdt: draft.margin, leverage: draft.leverage, atr: analysis?.atr}),
          signal: controller.signal,
        })
        const payload = await response.json().catch(() => ({})) as BackendRiskPreview & {detail?: string}
        if (!response.ok) throw new Error(payload.detail || 'Backend risk preview unavailable')
        setBackendRiskPreview(payload)
        setBackendRiskPreviewError('')
      } catch (error) {
        if (!(error instanceof Error && error.name === 'AbortError')) {
          setBackendRiskPreview(null)
          setBackendRiskPreviewError(error instanceof Error ? error.message : 'Backend risk preview unavailable')
        }
      }
    }, 250)
    return () => { window.clearTimeout(timer); controller.abort() }
  }, [premium, analysis?.atr, draft.entry, draft.stopLoss, draft.margin, draft.leverage])

  const riskPreview = useMemo(() => ({
    riskUsd: backendRiskPreview?.estimated_stop_loss_usdt ?? null,
    riskPercent: backendRiskPreview?.stop_distance_pct ?? 0,
    error: backendRiskPreviewError,
  }), [backendRiskPreview, backendRiskPreviewError])

  const tradeDecision = useMemo<TradeDecision>(() => buildTradeDecision(analysis, candles, mtfAnalyses), [analysis, candles, mtfAnalyses])
  const triggerMonitor = useMemo<TriggerMonitor>(() => buildTriggerMonitor(tradeDecision, analysis, candles, triggerLifecycleRef.current, currentPrice ?? candles[candles.length - 1]?.close), [tradeDecision, analysis, candles, currentPrice])

  useEffect(() => {
    triggerLifecycleRef.current = triggerMonitor.lifecycle
  }, [triggerMonitor.lifecycle])

  useEffect(() => {
    setDecisionTimeline([])
    timelineKeysRef.current = []
  }, [draft.market, interval])

  useEffect(() => {
    if (!snapshot?.marketUpdatedAt || !snapshot.analysis) return
    const key = `${snapshot.symbol}|${snapshot.timeframe}|${tradeDecision.direction}|${triggerMonitor.lifecycle}|${triggerMonitor.remainingConditions}`
    if (timelineKeysRef.current.includes(key)) return
    timelineKeysRef.current = [...timelineKeysRef.current.slice(-11), key]
    const message = triggerMonitor.lifecycle === 'CONFIRMED'
      ? `${tradeDecision.direction} trigger confirmed · no order sent`
      : `${tradeDecision.direction} bias · ${triggerMonitor.lifecycle.toLowerCase()} · ${triggerMonitor.remainingConditions ?? '--'} conditions remaining`
    setDecisionTimeline(current => [{ time: snapshot.marketUpdatedAt as string, message }, ...current].slice(0, 12))
  }, [snapshot?.marketUpdatedAt, snapshot?.symbol, snapshot?.timeframe, tradeDecision.direction, triggerMonitor.lifecycle, triggerMonitor.remainingConditions])

  const submitPositionAction = async () => {
    if (!positionAction || positionAction.mode === 'DETAILS' || positionActionInFlight.current) return
    const quantity = positionAction.position.quantity || 0
    const requestedQuantity = positionAction.mode === 'CLOSE' ? quantity : Number(reduceQuantity)
    if (!(requestedQuantity > 0) || requestedQuantity > quantity) {
      setPositionActionError('Miktar 0’dan büyük ve mevcut pozisyon miktarını aşmamalı.')
      return
    }

    const isClose = positionAction.mode === 'CLOSE'
    const targetSymbol = positionAction.position.symbol
    const closeStartedAt = new Date().toISOString()
    positionActionInFlight.current = true
    setPositionActionBusy(true)
    setPositionActionError('')
    setCloseLifecycle({ state: 'CLOSING', message: isClose ? 'CLOSING POSITION...' : 'REDUCING POSITION...' })

    try {
      const token = userSessionToken()
      const headers = new Headers({ 'Content-Type': 'application/json' })
      if (token) headers.set('Authorization', `Bearer ${token}`)
      const livePlan = accountRef.current?.plans?.find(plan => plan.symbol === targetSymbol) as (AccountPlan & { id?: string }) | undefined
      if (isClose && !livePlan?.id) throw new Error('LIVE tracked plan is unavailable for this position.')
      const response = await fetchWithTimeout(`${API_BASE}/v25/position/close`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ plan_id: livePlan?.id, confirmation: 'CANLI POZİSYONU KAPAT' }),
      })
      const payload = await response.json().catch(() => null) as { detail?: unknown; message?: string } | null
      if (!response.ok) {
        const detail = typeof payload?.detail === 'string' ? payload.detail : payload?.message
        throw new Error(detail || `Pozisyon işlemi başarısız (HTTP ${response.status}).`)
      }

      const refreshed = await refreshAccountData()
      const refreshedAccount = accountRef.current
      const positionsAfterClose = Array.isArray(refreshedAccount?.positions) ? refreshedAccount.positions : []
      const positionStillOpen = positionsAfterClose.some(item => item.symbol === targetSymbol && Number(item.quantity || 0) > 0)
      const closedTrade = historyRef.current.find(row => row.id === livePlan?.id && row.symbol === targetSymbol && new Date(row.closeTime).getTime() >= new Date(closeStartedAt).getTime())
      const historyHasClose = Boolean(closedTrade)
      const pnlValue = closedTrade?.realizedPnl ?? null

      if (!refreshed || !positionStillOpen && !historyHasClose) {
        setCloseLifecycle({ state: 'SYNC_FAILED', message: 'POSITION CLOSED · HISTORY SYNC FAILED', detail: 'Backend acknowledged the close request, but position and history verification did not complete.' })
        setPositionActionError('POSITION CLOSED · HISTORY SYNC FAILED')
        setPositionAction(null)
        setReduceQuantity('')
        setAccountRefreshNonce(value => value + 1)
        return
      }

      if (positionStillOpen) {
        setCloseLifecycle({ state: 'CLOSE_FAILED', message: 'POSITION STILL OPEN', detail: 'Backend did not remove the active position after the close request.' })
        setPositionActionError('POSITION STILL OPEN - close request did not remove the position from backend state.')
        setPositionAction(null)
        setReduceQuantity('')
        setAccountRefreshNonce(value => value + 1)
        return
      }

      if (!historyHasClose) {
        setCloseLifecycle({ state: 'HISTORY_SYNC_FAILED', message: 'POSITION CLOSED · HISTORY SYNC FAILED', detail: 'Close was accepted by the backend, but trade history refresh did not confirm the closed record.' })
        setPositionActionError('POSITION CLOSED · HISTORY SYNC FAILED')
        setPositionAction(null)
        setReduceQuantity('')
        setAccountRefreshNonce(value => value + 1)
        return
      }

      const acknowledgedPnl = pnlValue === null ? 'PnL unavailable' : `${pnlValue >= 0 ? '+' : ''}$${pnlValue.toFixed(2)}`
      setCloseLifecycle({ state: 'CLOSED', message: `POSITION CLOSED · ${pnlValue === null ? 'PNL UNAVAILABLE' : `PNL ${acknowledgedPnl}`}`, detail: 'Backend close accepted and state checks passed.' })
      setPositionAction(null)
      setReduceQuantity('')
      setAccountRefreshNonce(value => value + 1)
    } catch (error) {
      setCloseLifecycle({ state: 'CLOSE_FAILED', message: 'CLOSE FAILED', detail: error instanceof Error ? error.message : 'Position close failed.' })
      setPositionActionError(error instanceof Error ? error.message : 'Pozisyon işlemi başarısız.')
    } finally {
      positionActionInFlight.current = false
      setPositionActionBusy(false)
    }
  }

  const watchlistCandidates: ScannerCandidate[] = scannerCandidates.length ? scannerCandidates : markets.map(market => ({
    symbol: market.symbol,
    display: market.display,
    price: market.price,
    change: market.change,
    volume: market.volume,
    direction: 'WATCH',
    confidence: 0,
    final_decision_score: 0,
    opportunity_score: 0,
    smart_score: 0,
  }))
  const rankedWatchlistMarkets = watchlistCandidates
    .map(candidate => {
      const market = markets.find(item => item.symbol === candidate.symbol)
      return {
        symbol: candidate.symbol,
        display: candidate.display || market?.display || candidate.symbol.replace(/USDT$/, '/USDT'),
        price: candidate.price ?? market?.price ?? 0,
        change: candidate.change ?? market?.change ?? 0,
        volume: candidate.volume ?? market?.volume ?? 0,
        direction: candidate.direction,
        confidence: candidate.confidence,
        finalDecisionScore: candidate.final_decision_score,
        smartScore: candidate.smart_score,
      }
    })
    .sort((left, right) => right.finalDecisionScore - left.finalDecisionScore || right.confidence - left.confidence || right.smartScore - left.smartScore)
  const normalizedMarketQuery = marketQuery.trim().toUpperCase().replace(/[^A-Z0-9]/g, '')
  const filteredMarkets = rankedWatchlistMarkets.filter(market => {
    if (!normalizedMarketQuery) return true
    const display = market.display.toUpperCase().replace(/[^A-Z0-9]/g, '')
    return market.symbol.includes(normalizedMarketQuery) || display.includes(normalizedMarketQuery) || market.symbol.replace(/USDT$/, '').includes(normalizedMarketQuery)
  })
  const selectedMarket = selectedQuote ?? markets.find(market => market.symbol === draft.market)
  const selectedChange = selectedMarket?.change ?? null
  const latestCandle = candles[candles.length - 1]
  const chartCandles = candles.slice(-80)
  const chartValues = chartCandles.map(candle => candle.close)
  const chartMin = chartValues.length ? Math.min(...chartValues) : 0
  const chartMax = chartValues.length ? Math.max(...chartValues) : 1
  const chartRange = Math.max(chartMax - chartMin, chartMax * 0.001, 1)
  const chartHigh = chartCandles.length ? Math.max(...chartCandles.map(candle => candle.high), analysis?.resistance || 0, analysis?.tp3 || 0, analysis?.entry || 0) : 1
  const chartLow = chartCandles.length ? Math.min(...chartCandles.map(candle => candle.low), analysis?.support || Number.POSITIVE_INFINITY, analysis?.stop_loss || Number.POSITIVE_INFINITY) : 0
  const chartValueRange = Math.max(chartHigh - chartLow, chartHigh * 0.001, 1)
  const chartX = (index: number) => chartCandles.length > 1 ? 760 * index / (chartCandles.length - 1) : 380
  const chartY = (value: number) => 232 - ((value - chartLow) / chartValueRange) * 190
  const volumeMax = chartCandles.length ? Math.max(...chartCandles.map(candle => candle.volume), 1) : 1
  const levelLines = [
    { label: 'ENTRY', value: analysis?.entry, tone: 'entry' },
    { label: 'SL', value: analysis?.stop_loss, tone: 'stop' },
    { label: 'TP1', value: analysis?.tp1, tone: 'tp' },
    { label: 'TP2', value: analysis?.tp2, tone: 'tp' },
    { label: 'TP3', value: analysis?.tp3, tone: 'tp' },
    { label: 'SUPPORT', value: analysis?.support, tone: 'support' },
    { label: 'RESISTANCE', value: analysis?.resistance, tone: 'resistance' },
    { label: 'TRIGGER', value: triggerMonitor.triggerPrice ?? undefined, tone: 'trigger' },
  ].filter((line): line is { label: string; value: number; tone: string } => typeof line.value === 'number' && Number.isFinite(line.value))
  const positionedLevelLines = [...levelLines].sort((left, right) => chartY(left.value) - chartY(right.value)).reduce<Array<{ label: string; value: number; tone: string; labelY: number }>>((rows, line) => {
    const rawY = chartY(line.value)
    const labelY = rows.length ? Math.max(rawY, rows[rows.length - 1].labelY + 15) : rawY
    rows.push({ ...line, labelY: Math.min(250, labelY) })
    return rows
  }, [])
  const hoveredCandle = chartHoverIndex === null ? null : chartCandles[chartHoverIndex]
  const selectMarket = (symbol: string) => {
    setDraft(current => ({ ...current, market: symbol, entry: 0, stopLoss: 0, tp1: 0, tp2: 0, tp3: 0 }))
    setMarketQuery('')
  }

  const performanceTrend: number[] = []
  const openPositions = account?.positions ?? []
  const protectionOrders = [...(account?.open_orders ?? []), ...(account?.open_algo_orders ?? [])]
  const protectionLevel = (order: AccountOrder) => {
    const value = order.trigger_price ?? order.price
    return typeof value === 'number' && Number.isFinite(value) ? value : undefined
  }
  const liveProtectionLevels = (position: AccountPosition) => {
    const expectedSide = position.direction === 'LONG' ? 'SELL' : 'BUY'
    const symbolOrders = protectionOrders.filter(order => order.symbol === position.symbol && String(order.side || '').toUpperCase() === expectedSide)
    const stopOrder = symbolOrders.find(order => String(order.type || '').toUpperCase() === 'STOP_MARKET')
    const targetOrders = symbolOrders
      .filter(order => String(order.type || '').toUpperCase() === 'TAKE_PROFIT_MARKET')
      .map(order => ({ order, level: protectionLevel(order) }))
      .filter((item): item is { order: AccountOrder; level: number } => item.level !== undefined)
      .sort((left, right) => position.direction === 'LONG' ? left.level - right.level : right.level - left.level)
    return { stopLoss: protectionLevel(stopOrder || {}), targets: targetOrders.map(item => item.level) }
  }
  const positionWithProtection = (position: AccountPosition): AccountPosition => {
    const plan = account?.plans?.find(item => item.symbol === position.symbol)
    const liveLevels = liveProtectionLevels(position)
    return {
      ...position,
      stop_loss: position.stop_loss ?? (plan?.stop_loss ? Number(plan.stop_loss) : undefined) ?? liveLevels.stopLoss,
      tp1: position.tp1 ?? (plan?.targets?.[0] ? Number(plan.targets[0]) : undefined) ?? liveLevels.targets[0],
      tp2: position.tp2 ?? (plan?.targets?.[1] ? Number(plan.targets[1]) : undefined) ?? liveLevels.targets[1],
      tp3: position.tp3 ?? (plan?.targets?.[2] ? Number(plan.targets[2]) : undefined) ?? liveLevels.targets[2],
    }
  }
  const openRiskValues = openPositions.map(position => {
    const plan = account?.plans?.find(item => item.symbol === position.symbol)
    const stopLoss = position.stop_loss ?? (plan?.stop_loss ? Number(plan.stop_loss) : undefined)
    return position.entry_price && stopLoss && position.quantity ? Math.abs(position.entry_price - stopLoss) * position.quantity : null
  }).filter((value): value is number => value !== null)
  const openRisk = openRiskValues.length ? openRiskValues.reduce((sum, value) => sum + value, 0) : null
  const usedMargin = account?.wallet_balance !== undefined && account.available_balance !== undefined ? account.wallet_balance - account.available_balance : null
  const latestUpdate = snapshot?.marketUpdatedAt ? new Date(snapshot.marketUpdatedAt).toLocaleTimeString('en-GB') : '--'
  const marketAgeSeconds = snapshot?.marketUpdatedAt ? Math.max(0, (Date.now() - new Date(snapshot.marketUpdatedAt).getTime()) / 1000) : null
  const priceAgeSeconds = snapshot?.priceUpdatedAt ? Math.max(0, (Date.now() - new Date(snapshot.priceUpdatedAt).getTime()) / 1000) : null
  const dataHealth = !snapshot ? 'NO DATA' : snapshot.marketError ? 'ERROR' : priceAgeSeconds !== null && priceAgeSeconds <= 20 ? 'LIVE' : 'STALE'
  const riskMetrics = [
    { label: 'Daily PnL', value: dailyPerformance ? `${dailyPerformance.net_profit >= 0 ? '+' : ''}$${fmtCompact(dailyPerformance.net_profit)}` : '--', tone: 'muted' },
    { label: 'Daily Risk', value: '--', tone: 'muted' },
    { label: 'Limit', value: account?.limits?.max_open_positions === undefined ? '--' : `${account.limits.max_open_positions} positions`, tone: 'muted' },
    { label: 'Open Risk', value: openRisk === null ? '--' : `$${fmtCompact(openRisk)}`, tone: 'warning' },
    { label: 'Margin Used', value: usedMargin === null ? '--' : `$${fmtCompact(usedMargin)}`, tone: 'muted' },
    { label: 'Loss Streak', value: dailyPerformance ? String(dailyPerformance.losing_streak) : '--', tone: 'muted' },
  ]
  const masterTradeOffline = !snapshot && !markets.length && !marketLoading
  const autoTrade = autoTradePresentation('LIVE', liveStatus?.live_auto_trade)
  const scoreBreakdown: Array<[string, number]> = tradeDecision.breakdown ? [
    ['Analysis', tradeDecision.breakdown.analysis], ['Liquidity', tradeDecision.breakdown.liquidity],
    ['Volatility', tradeDecision.breakdown.volatility], ['MTF', tradeDecision.breakdown.mtf],
    ['Freshness', tradeDecision.breakdown.freshness], ['Risk/Reward', tradeDecision.breakdown.riskReward],
  ] : []
  const triggerPriceText = (value: number | null | undefined) => masterLayoutV2 ? analysisPrice(value) : value === null || value === undefined ? '--' : `$${fmtDecisionNumber(value, 6)}`

  useLayoutEffect(() => {
    if (!masterLayoutV2 || layoutTab !== 'analiz' || !decisionColumn.current) return
    const panel = decisionColumn.current
    const measure = () => panel.style.setProperty('--analysis-panel-top', `${Math.max(124, panel.getBoundingClientRect().top)}px`)
    measure()
    const observer = new ResizeObserver(measure)
    const header = reactionArea.current?.querySelector('.masterTradeSticky')
    if (header) observer.observe(header)
    window.addEventListener('resize', measure)
    window.addEventListener('scroll', measure, {passive: true})
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', measure)
      window.removeEventListener('scroll', measure)
      panel.style.removeProperty('--analysis-panel-top')
    }
  }, [masterLayoutV2, layoutTab])

  useLayoutEffect(() => {
    if (!shortcutPending.current || layoutTab !== 'canli') return
    const target = reactionArea.current?.querySelector<HTMLElement>('#master-trade-auto-trade')
    if (!target) return
    shortcutPending.current = false
    target.scrollIntoView({block: 'center', behavior: 'instant'})
    target.focus({preventScroll: true})
  }, [layoutTab])

  const referenceAnalysis = masterLayoutV2 && layoutTab === 'analiz'
  const referenceContent = referenceAnalysis ? <MasterTradeReference assistantSlotRef={assistantSlotRef}
      symbol={draft.market} interval={interval} query={marketQuery} onQuery={setMarketQuery}
      scores={scannerCandidates.map(candidate => ({symbol: candidate.symbol, direction: candidate.direction, score: candidate.final_decision_score}))}
      marketFeed={marketFeed}
      onMarket={selectMarket} onInterval={setInterval} onNavigate={navigateTab}
      onAutoTrade={() => {shortcutPending.current = true; navigateTab('canli')}}
      onRefresh={() => setMarketRefreshNonce(value => value + 1)} refreshing={dataLoading}
      candles={candles} analysis={analysis} decision={tradeDecision} trigger={triggerMonitor} live={liveStatus}
      price={currentPrice} change={selectedChange} volume={selectedMarket?.volume}
      levelsVisible={showChartLevels} volumeVisible={showChartVolume}
      onLevels={() => setShowChartLevels(value => !value)} onVolume={() => setShowChartVolume(value => !value)}
      timeline={decisionTimeline} dataIssue={dataError} error={dataError || activeSnapshot?.mtfError || marketError}/> : null

  return (
    <MasterTradePresentation restored={masterLayoutV2 && layoutTab === 'analiz'}>
    <PremiumWorkspace>
    <section ref={reactionArea} className="masterTradePresentationHost">
    {tradingDefaults.error && <p className="refError" role="alert">İşlem tercihleri uygulanamadı: {tradingDefaults.error}</p>}
    {referenceContent}
    <section hidden={referenceAnalysis} className={`masterTradePage masterTrade${masterTradeOffline ? ' masterTradeOffline' : ''}${masterLayoutV2 ? ' masterLayoutV2' : ''}`} data-layout-tab={layoutTab}>
      <div className="masterTradeShell">
        <header className="masterTradeTerminalHeader">
          <div className="masterTradeTerminalIdentity">
            <span>INSTITUTIONAL EXECUTION DESK</span>
            <strong>MASTER TRADE</strong>
          </div>
          <span className="masterTradeTerminalMode">LIVE OPERATIONS · CONTROLLED</span>
          {assistantSlotRef && !referenceAnalysis && <div className="assistantMasterSlot" ref={assistantSlotRef}/>}
        </header>
        <div className="terminalStatusStrip" aria-label="Master Trade connection status" hidden={layoutTab !== 'baglanti'}>
          <span className="terminalStatusItem online"><i /> CONNECTED</span>
          <span className="terminalStatusItem"><small>WS</small> --</span>
          <span className="terminalStatusItem"><small>LATENCY</small> --</span>
          <span className="terminalStatusItem"><small>LAST UPDATE</small> {latestUpdate}</span>
          <span className={`terminalStatusItem dataHealth-${dataHealth.toLowerCase().replaceAll(' ', '-')}`}><small>DATA HEALTH</small> {dataHealth}{marketAgeSeconds !== null ? ` · ${Math.floor(marketAgeSeconds)}s ago` : ''}</span>
          <span className="terminalStatusItem online">LIVE ACCOUNT</span>
        </div>

        <div className="masterTradeSticky">
        <section className={`masterTradeFocusPanel${masterTradeOffline ? ' offline' : ''}`} aria-label="Primary trade decision">
          <div className="masterTradeFocusIdentity">
            <span className="panelEyebrow">PRIMARY DECISION</span>
            <strong>{draft.market}</strong>
            <span>{interval} · {masterTradeOffline ? 'WAITING FOR MARKET DATA' : 'CONTROLLED EXECUTION'}</span>
          </div>
          <div className="masterTradeFocusPrice">
            <small>MARKET PRICE</small>
            <strong><MasterTradeValue>{currentPrice !== null ? `$${fmtMarketPrice(currentPrice)}` : '--'}</MasterTradeValue></strong>
          </div>
          <div className="masterTradeFocusDecision">
            <small>SIGNAL / STATUS</small>
            <strong>{!analysis ? 'Analiz yok' : tradeDecision.status}</strong>
          </div>
          <div className="masterTradeFocusMetric">
            <small>{masterLayoutV2 ? 'Güven' : 'CONFIDENCE'}</small>
            <strong><MasterTradeValue>{tradeDecision.confidenceScore === null ? '--' : `${tradeDecision.confidenceScore}%`}</MasterTradeValue></strong>
          </div>
          <div className="masterTradeFocusMetric">
            <small>RISK / REWARD</small>
            <strong><MasterTradeValue>{tradeDecision.riskReward === null ? '--' : `1 : ${fmtDecisionNumber(tradeDecision.riskReward, 2)}`}</MasterTradeValue></strong>
          </div>
          <div className="masterTradeFocusConnection">
            {masterLayoutV2 ? <span className="masterTradeConnectionBadge" role="status" data-connected={Boolean(liveStatus?.connected)}>
              <i aria-hidden="true"/>{liveStatus?.real_trading_locked === false ? <UnlockKeyhole aria-hidden="true"/> : <LockKeyhole aria-hidden="true"/>}
              {liveStatus?.connected ? 'CONNECTED' : 'DISCONNECTED'} · {liveStatus?.real_trading_locked === false ? 'UNLOCKED' : 'LOCKED'}
            </span> : <>
            <span className={liveStatus?.connected ? 'positive' : 'muted'}>{liveStatus?.connected ? 'CONNECTED' : 'DISCONNECTED'}</span>
            <strong className={liveStatus?.real_trading_locked === false ? 'positive' : 'warning'}>{liveStatus?.real_trading_locked === false ? <UnlockKeyhole aria-hidden="true"/> : <LockKeyhole aria-hidden="true"/>}{liveStatus?.real_trading_locked === false ? 'UNLOCKED' : 'LOCKED'}</strong>
            </>}
          </div>
          {masterTradeOffline && <p className="masterTradeFocusNotice">Market bağlantısı bekleniyor. İşlem kararı ve canlı metrikler veri gelene kadar pasif tutuluyor.</p>}
        </section>

        <nav className="masterTradeTabs" aria-label="Master Trade" role="tablist">{MASTER_TRADE_TABS.map(({id,label,icon:Icon}) => <button key={id} type="button" id={`master-tab-${id}`} role="tab" aria-selected={layoutTab === id} aria-controls={`master-panel-${id}`} onClick={() => navigateTab(id)}><Icon aria-hidden="true"/><span>{label}</span></button>)}</nav>
        </div>

        <div className="masterTradeWorkspace" id="master-panel-analiz" role="tabpanel" aria-labelledby="master-tab-analiz" hidden={layoutTab !== 'analiz'}>
          <div className="masterTradeSafetyBanner" role="status">
            <strong>LIVE TRADING LOCKED</strong>
            <span>LIVE ACCOUNT SNAPSHOT · PERSISTENT HISTORY · RECOVERY CONTROLLED</span>
          </div>
          <aside className="masterTradePanel watchlistPanel">
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">MARKET WATCH</span>
                <h3>Live Markets</h3>
              </div>
              <span className="marketCount">{rankedWatchlistMarkets.length || '--'}</span>
            </div>

            <label className="marketSearch"><span>SEARCH MARKETS</span><input aria-label="Search markets" placeholder="BTC, ETH, SOL..." value={marketQuery} onChange={event => setMarketQuery(event.target.value)} /><button type="button" aria-label="Clear market search" onClick={() => setMarketQuery('')} disabled={!marketQuery}>×</button></label>
            <div className="watchlistList">
              {marketLoading && !watchlistCandidates.length ? <div className="marketEmpty">Loading markets...</div> : filteredMarkets.length === 0 ? marketError ? <div className="marketEmpty error">{marketError}</div> : <div className="marketEmpty"><strong>No markets found</strong><span>Try another symbol or market name.</span></div> : <>
                {marketError && <div className="marketStaleNotice">{marketError}</div>}
                {filteredMarkets.map((item, index) => (
                  <button key={item.symbol} type="button" className={item.symbol === draft.market ? 'watchlistItem active' : 'watchlistItem'} onClick={() => selectMarket(item.symbol)}>
                    <div className="watchlistMeta">
                      <b>#{index + 1} {item.symbol}</b>
                      <span>{scannerCandidates.length ? `FINAL ${fmtDecisionNumber(item.finalDecisionScore)}/100` : 'FINAL DECISION PENDING'}</span>
                    </div>
                    <div className="watchlistStats">
                      <strong>{masterLayoutV2 ? analysisPrice(scannerCandidates.find(candidate => candidate.symbol === item.symbol)?.price ?? markets.find(market => market.symbol === item.symbol)?.price) : `$${item.price.toLocaleString('en-US', { maximumFractionDigits: 6 })}`}</strong>
                      <em className={item.direction === 'LONG' ? 'positive' : item.direction === 'SHORT' ? 'negative' : 'muted'}>{item.direction} {scannerCandidates.length ? `${masterLayoutV2 ? 'Skor ' : ''}${fmtDecisionNumber(item.confidence)}%` : 'RAW MARKET'}</em>
                    </div>
                  </button>
                ))}
              </>}
            <details className="masterTradePanel scannerPanel compactPanel masterTradeScannerDetails">
              <summary className="panelHeader">
                <div><span className="panelEyebrow">MARKET SCANNER</span><h3>Top opportunities</h3></div>
                <span className="livePill">LIVE</span>
              </summary>
              <div className="scannerList">{scannerCandidates.length ? scannerCandidates.map((candidate, index) => <button type="button" key={candidate.symbol} onClick={() => selectMarket(candidate.symbol)}><div className="scannerCandidateMeta"><strong>{candidate.symbol}</strong><div><span className="scannerScore">#{index + 1}</span><strong>{fmtDecisionNumber(candidate.final_decision_score)}<small> / 100 FINAL DECISION</small></strong></div></div><div className="scannerCandidateStats"><strong><MasterTradeValue>{candidate.price === undefined ? '--' : `$${fmtMarketPrice(candidate.price)}`}</MasterTradeValue></strong><small data-tone={masterTradeTone(candidate.direction)} title={`${candidate.direction} · ${masterLayoutV2 ? 'Skor' : 'Confidence'} ${fmtDecisionNumber(candidate.confidence)}%`}>{candidate.direction} · {masterLayoutV2 ? 'Skor' : 'Confidence'} {fmtDecisionNumber(candidate.confidence)}%</small></div></button>) : <div className="emptyState">NO CURRENT OPPORTUNITY SNAPSHOT</div>}</div>
            </details>
            </div>
          </aside>

          <main className="masterTradePanel chartPanel">
            <div className="masterTradeChartHeader">
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">MARKET</span>
                <h3>{draft.market}</h3>
              </div>
            </div>

            <div className="chartPriceSummary">
              <div>
                <span className="chartSymbol">{draft.market}</span>
                <strong><MasterTradeValue>{currentPrice !== null ? `$${fmtMarketPrice(currentPrice)}` : '--'}</MasterTradeValue></strong>
              </div>
              <span className={`delta ${selectedChange === null ? '' : selectedChange >= 0 ? 'positive' : 'negative'}`}>{selectedChange === null ? '--' : `${selectedChange >= 0 ? '+' : ''}${selectedChange.toFixed(2)}%`}</span>
            </div>

            <div className="marketStatsStrip">
              <span><small>24H CHANGE</small><b className={selectedChange === null ? '' : selectedChange < 0 ? 'negative' : 'positive'}>{selectedChange === null ? '--' : `${selectedChange >= 0 ? '+' : ''}${selectedChange.toFixed(2)}%`}</b></span>
              <span><small>VOLUME</small><b><MasterTradeValue>{fmtCompact(selectedMarket?.volume)}</MasterTradeValue></b></span>
              <span><small>HIGH</small><b><MasterTradeValue>{chartCandles.length ? fmtMarketPrice(Math.max(...chartCandles.map(candle => candle.high))) : '--'}</MasterTradeValue></b></span>
              <span><small>LOW</small><b><MasterTradeValue>{chartCandles.length ? fmtMarketPrice(Math.min(...chartCandles.map(candle => candle.low))) : '--'}</MasterTradeValue></b></span>
              <span><small>DATA HEALTH</small><b className={dataHealth === 'LIVE' ? 'positive' : dataHealth === 'STALE' ? 'warning' : 'negative'}>{dataHealth}</b></span>
            </div>
            </div>

            <div className="decisionMtf"><div className="decisionSubheading"><h4>MULTI-TIMEFRAME MATRIX</h4><strong>{tradeDecision.mtfScore === null ? 'MTF BIAS: --' : `MTF CONFIRMATION: ${tradeDecision.mtfConfirmed} / ${tradeDecision.mtfTotal}`}</strong></div><div className="decisionMtfGrid">{MTF_INTERVALS.map(timeframe => { const row = tradeDecision.mtfRows.find(item => item.timeframe === timeframe); return <span key={timeframe} data-tone={masterTradeTone(row?.available ? row.direction : undefined)}><b>{timeframe}</b><em><MasterTradeValue>{row?.available ? row.direction : '--'}</MasterTradeValue></em><small><MasterTradeValue>{row?.trend || '--'}</MasterTradeValue></small></span> })}</div>{!tradeDecision.mtfRows.length && <p>DATA UNAVAILABLE</p>}</div>

            <div className="chartToolbar" aria-label="Market chart controls">
              <div className="chartControls">{Array.from(new Set(['1m','5m','15m','1h','4h','1d',interval])).map((range) => <button key={range} type="button" className={range === interval ? 'active' : ''} onClick={() => setInterval(range)}>{range.toUpperCase()}</button>)}</div>
              <div className="chartViewControls"><button type="button" className={showChartLevels ? 'active' : ''} onClick={() => setShowChartLevels(value => !value)}>LEVELS</button><button type="button" className={showChartVolume ? 'active' : ''} onClick={() => setShowChartVolume(value => !value)}>VOLUME</button></div>
            </div>
            <span className="chartOhlcLabel">{hoveredCandle ? new Date(hoveredCandle.time * (hoveredCandle.time < 1_000_000_000_000 ? 1000 : 1)).toLocaleString('en-GB') : 'OHLC / REAL MARKET DATA'}</span>

            <div className="chartCanvas marketOhlcChart">
              {dataLoading && !chartCandles.length ? <div className="chartEmpty">LOADING SNAPSHOT</div> : !chartCandles.length ? <div className="chartEmpty">{dataError || 'DATA UNAVAILABLE'}</div> : <svg viewBox="0 0 760 300" preserveAspectRatio="none" aria-label={`${draft.market} ${interval} candlestick chart`} onMouseLeave={() => setChartHoverIndex(null)} onMouseMove={event => { const box = event.currentTarget.getBoundingClientRect(); const index = Math.round(((event.clientX - box.left) / box.width) * (chartCandles.length - 1)); setChartHoverIndex(Math.max(0, Math.min(chartCandles.length - 1, index))) }}>
                <g className="chartGrid">{[...Array(7)].map((_, index) => <line key={`h-${index}`} x1="0" x2="760" y1={22 + index * 35} y2={22 + index * 35} />)}{[...Array(9)].map((_, index) => <line key={`v-${index}`} x1={index * 95} x2={index * 95} y1="0" y2="260" />)}</g>
                {chartCandles.map((candle, index) => { const x = chartX(index); const bodyTop = chartY(Math.max(candle.open, candle.close)); const bodyBottom = chartY(Math.min(candle.open, candle.close)); const bodyHeight = Math.max(2, bodyBottom - bodyTop); const bullish = candle.close >= candle.open; const candleWidth = Math.max(2, Math.min(10, 700 / chartCandles.length)); return <g key={`${candle.time}-${index}`} className={bullish ? 'candle bullish' : 'candle bearish'}><line x1={x} x2={x} y1={chartY(candle.high)} y2={chartY(candle.low)} /><rect x={x - candleWidth / 2} y={bodyTop} width={candleWidth} height={bodyHeight} /></g> })}
                {showChartVolume && chartCandles.map((candle, index) => { const x = chartX(index); const height = candle.volume / volumeMax * 28; return <rect key={`vol-${candle.time}`} className={`chartVolume ${candle.close >= candle.open ? 'up' : 'down'}`} x={x - 2} y={273 - height} width="4" height={height} /> })}
                <MasterTradeChartLabels lines={premium && showChartLevels ? positionedLevelLines : []} currentPrice={currentPrice} toY={chartY} format={fmtMarketPrice}/>
                {chartHoverIndex !== null && <g className="chartCrosshair"><line x1={chartX(chartHoverIndex)} x2={chartX(chartHoverIndex)} y1="0" y2="260" /><circle cx={chartX(chartHoverIndex)} cy={chartY(chartCandles[chartHoverIndex].close)} r="3" /></g>}
              </svg>}
              {!!chartCandles.length && <MasterTradeChartLabels lines={premium && showChartLevels ? positionedLevelLines : []} currentPrice={currentPrice} toY={chartY} format={fmtMarketPrice} pills/>}
              <div className="chartBadge">{currentPrice !== null ? `$${fmtMarketPrice(currentPrice)}` : '--'}</div>
            </div>

            <div className="indicatorGrid">
              <MasterTradeMetricTile label="TREND" value={analysis?.trend || '--'} tone={masterTradeTone(analysis?.trend)}/>
              <MasterTradeMetricTile label="MOMENTUM" value={analysis?.momentum || '--'} tone={masterTradeTone(analysis?.momentum)}/>
              <MasterTradeMetricTile label="RSI" value={fmtDecisionNumber(analysis?.rsi, 2)} status={analysis?.rsi === undefined ? 'DATA UNAVAILABLE' : analysis.rsi >= 70 ? 'OVERBOUGHT' : analysis.rsi <= 30 ? 'OVERSOLD' : 'HEALTHY RANGE'}><MasterTradeMetricVisual kind="rsi" value={analysis?.rsi}/></MasterTradeMetricTile>
              <MasterTradeMetricTile label="MACD" value={fmtSigned(analysis?.macd)} tone={masterTradeTone(analysis?.macd)} status={analysis?.macd === undefined ? 'DATA UNAVAILABLE' : analysis.macd >= 0 ? 'BULLISH' : 'BEARISH'}><MasterTradeMetricVisual kind="macd" value={analysis?.macd}/></MasterTradeMetricTile>
              <MasterTradeMetricTile label="VOLUME" value={analysis?.volume_ratio ? `${fmtDecisionNumber(analysis.volume_ratio, 2)}x` : '--'}><MasterTradeMetricVisual kind="volume" volumes={chartCandles.slice(-24).map(candle => candle.volume)}/></MasterTradeMetricTile>
              <MasterTradeMetricTile label={masterLayoutV2 ? 'Güven' : 'CONFIDENCE'} value={tradeDecision.confidenceScore === null ? '--' : `${tradeDecision.confidenceScore}%`}><MasterTradeMetricVisual kind="confidence" value={tradeDecision.confidenceScore}/></MasterTradeMetricTile>
              <MasterTradeMetricTile label="OPPORTUNITY" value={fmtDecisionNumber(tradeDecision.opportunityScore)}><MasterTradeMetricVisual kind="confidence" value={tradeDecision.opportunityScore}/></MasterTradeMetricTile>
            </div>
          </main>

          <aside ref={decisionColumn} className="masterTradeDecisionColumn" aria-label="Analysis decision panel">
            <section className={`tradeDecisionPanel decision-${tradeDecision.status.toLowerCase().replaceAll(' ', '-')}`} aria-label="Trade decision analysis">
              <div className="masterTradeFinalCard">
              <header className="tradeDecisionHeader">
                <div><span className="panelEyebrow">FINAL DECISION</span><h3>{analysis ? tradeDecision.status : 'Analiz yok'}</h3><small>{draft.market} · {interval} · {tradeDecision.marketRegime}</small></div>
                <div className="decisionScore"><strong><MasterTradeValue>{fmtDecisionNumber(tradeDecision.opportunityScore)}</MasterTradeValue></strong><span>/ 100<br />OPPORTUNITY</span></div>
              </header>
              <small>Presentation score only — not an order decision.</small>
              <div className="decisionMetricGrid">
                <div><small>{masterLayoutV2 ? 'Güven' : 'CONFIDENCE'}</small><strong><MasterTradeValue>{tradeDecision.confidenceScore === null ? '--' : `${tradeDecision.confidenceScore}%`}</MasterTradeValue></strong></div>
                <div><small>DIRECTION</small><strong><MasterTradeValue>{analysis ? tradeDecision.direction : '--'}</MasterTradeValue></strong></div>
                <div><small>SIGNAL STRENGTH</small><strong><MasterTradeValue>{tradeDecision.signalStrength || '--'}</MasterTradeValue></strong></div>
                <div><small>ENTRY QUALITY</small><strong><MasterTradeValue>{tradeDecision.entryQuality || '--'}</MasterTradeValue></strong></div>
                <div><small>RISK / REWARD</small><strong><MasterTradeValue>{tradeDecision.riskReward === null ? '--' : `1 : ${fmtDecisionNumber(tradeDecision.riskReward, 2)}`}</MasterTradeValue></strong><em>Target R/R; LIVE: TP1 60%, remainder TP3.</em></div>
                <div><small>SIGNAL</small><strong><MasterTradeValue>{tradeDecision.freshness || '--'}</MasterTradeValue></strong><em>Age {fmtSignalAge(tradeDecision.signalAgeSeconds)}</em></div>
              </div>
              </div>

              <div className="decisionSectionGrid">
                <details className="decisionList masterTradeAccordion"><summary><BarChart3 aria-hidden="true"/><h4 title="WHY THIS DECISION">WHY THIS DECISION</h4><span title={tradeDecision.reasons[0]}>{tradeDecision.reasons[0] || 'DATA UNAVAILABLE'}</span><ChevronDown aria-hidden="true"/></summary>{tradeDecision.reasons.length ? <ul>{tradeDecision.reasons.map(reason => <li key={reason}>+ {reason}</li>)}</ul> : <p>DATA UNAVAILABLE</p>}
                  {masterLayoutV2 && ['WAIT', 'WATCH', 'NO TRADE'].includes(tradeDecision.status) && <div className="decisionWaitReasons">
                    <h5>WHY WAIT?</h5><ul>{tradeDecision.whyWait.map(reason => <li key={reason}>{reason}</li>)}</ul>
                    <h5>WHAT WE ARE WAITING FOR</h5><ul>{tradeDecision.waitingFor.map(reason => <li key={reason}>{reason}</li>)}</ul>
                  </div>}
                </details>
                <div className="decisionList"><h4>RISK FLAGS</h4>{tradeDecision.riskFlags.length ? <ul className="riskList">{tradeDecision.riskFlags.map(flag => <li key={flag}>! {flag}</li>)}</ul> : <p className="positive">NO MAJOR RISK FLAGS</p>}</div>
              </div>

              {!masterLayoutV2 && (tradeDecision.status === 'WAIT' || tradeDecision.status === 'WATCH' || tradeDecision.status === 'NO TRADE') && <div className="decisionSectionGrid decisionWaitGrid">
                <details className="decisionList masterTradeAccordion"><summary><Clock3 aria-hidden="true"/><h4>WHY WAIT?</h4><span title={tradeDecision.whyWait[0]}>{tradeDecision.whyWait[0] || 'Confirmation still required'}</span><ChevronDown aria-hidden="true"/></summary><ul>{tradeDecision.whyWait.length ? tradeDecision.whyWait.map(reason => <li key={reason}>- {reason}</li>) : <li>Confirmation still required</li>}</ul></details>
                <div className="decisionList"><h4>WHAT WE ARE WAITING FOR</h4><ul>{tradeDecision.waitingFor.length ? tradeDecision.waitingFor.map(reason => <li key={reason}>✓ {reason}</li>) : <li>Fresh market confirmation</li>}</ul><p>Trigger: --</p></div>
              </div>}

              <div className="decisionSectionGrid masterTradeCases">
                <div className="decisionList"><h4>LONG CASE</h4><ul>{tradeDecision.longCase.map(item => <li key={item}>{item.includes('not ') ? <X aria-hidden="true" className="negative"/> : <Check aria-hidden="true" className="positive"/>}<span>{item}</span></li>)}</ul></div>
                <div className="decisionList"><h4>SHORT CASE</h4><ul>{tradeDecision.shortCase.map(item => <li key={item}>{item.includes('not ') ? <X aria-hidden="true" className="negative"/> : <Check aria-hidden="true" className="positive"/>}<span>{item}</span></li>)}</ul></div>
              </div>

              <div className="decisionBreakdown"><h4>WHY THIS SCORE?</h4>{tradeDecision.breakdown ? <div className="decisionBreakdownGrid">{scoreBreakdown.map(([label, value]) => <span key={label}><small>{label}</small><strong>{fmtDecisionNumber(value)}</strong></span>)}</div> : <p>DATA UNAVAILABLE</p>}</div>

              <div className="decisionAutoTrade"><span>AUTO TRADE</span><strong>SEPARATE SAFETY GATES</strong><small>Final Decision does not send orders or grant Auto Trade eligibility.</small>
                {masterLayoutV2 && <><span className="analysisAutoStatus" aria-label="Auto Trade state">{autoTrade.label}</span>
                  <button type="button" className="analysisAutoShortcut" onClick={() => { shortcutPending.current = true; navigateTab('canli') }}>{autoTrade.action}<ChevronDown aria-hidden="true"/></button>
                </>}
              </div>

              <section className={`triggerMonitor trigger-${triggerMonitor.lifecycle.toLowerCase()}`} aria-label="Trigger monitor">
                <header className="triggerHeader"><div><h4>TRIGGER MONITOR</h4><strong>{masterLayoutV2 && triggerMonitor.lifecycle === 'WAITING' ? 'Waiting' : triggerMonitor.lifecycle}</strong>{(!masterLayoutV2 || triggerMonitor.statusMessage !== triggerMonitor.lifecycle) && <small>{triggerMonitor.statusMessage}</small>}</div><span>{triggerMonitor.available ? 'LIVE SNAPSHOT' : 'DATA UNAVAILABLE'}</span></header>
                <div className="triggerSummary"><div><small>CURRENT</small><strong>{triggerPriceText(triggerMonitor.currentPrice)}</strong></div><div><small>{triggerMonitor.direction === 'SHORT' ? 'SHORT TRIGGER BELOW' : 'LONG TRIGGER ABOVE'}</small><strong>{triggerPriceText(triggerMonitor.triggerPrice)}</strong></div><div><small>DISTANCE</small><strong>{triggerMonitor.distancePct === null ? masterLayoutV2 ? '—' : '--' : `${triggerMonitor.distancePct.toFixed(2)}%`}</strong><em>{masterLayoutV2 ? triggerMonitor.waitingMessage.replace(/^WAITING\s*—\s*/, '') : triggerMonitor.waitingMessage}</em></div></div>
                <details className="triggerConditions masterTradeAccordion"><summary><Crosshair aria-hidden="true"/><h4 title="TRIGGER CONDITIONS">TRIGGER CONDITIONS</h4><span>{triggerMonitor.remainingConditions === null ? '--' : `${triggerMonitor.remainingConditions} CONDITIONS REMAINING`}</span><ChevronDown aria-hidden="true"/></summary>{triggerMonitor.conditions.length ? <div className="triggerConditionGrid">{triggerMonitor.conditions.map(condition => <span key={condition.key} className={!condition.available ? 'unavailable' : condition.passed ? 'passed' : 'pending'}><b>{condition.passed ? '✓' : condition.available ? '✕' : '--'}</b><small>{condition.label}</small><em>{masterLayoutV2 && condition.key === 'breakout' ? `Trigger ${analysisPrice(triggerMonitor.triggerPrice)}` : condition.detail}</em></span>)}</div> : <p className="triggerUnavailable">NO TRADE — waiting for a complete market snapshot.</p>}</details>
                <div className="triggerDetailGrid"><details className="decisionList masterTradeAccordion"><summary><ShieldX aria-hidden="true"/><h4>INVALIDATION</h4><span title={triggerMonitor.invalidation[0]}>{triggerMonitor.invalidation[0] || '--'}</span><ChevronDown aria-hidden="true"/></summary><ul>{triggerMonitor.invalidation.map(item => <li key={item}>- {item}</li>)}</ul></details><details className="decisionList masterTradeAccordion"><summary><History aria-hidden="true"/><h4 title="DECISION TIMELINE">DECISION TIMELINE</h4><span title={decisionTimeline[0]?.message}>{decisionTimeline[0]?.message || 'NO RECENT ACTIVITY'}</span><ChevronDown aria-hidden="true"/></summary>{decisionTimeline.length ? <ul>{decisionTimeline.map(event => <li key={`${event.time}-${event.message}`}>{new Date(event.time).toLocaleTimeString('en-GB')} · {event.message}</li>)}</ul> : <p>NO RECENT ACTIVITY</p>}</details></div>
                {triggerMonitor.entryPreview && <div className="triggerPreview"><h4>ENTRY PREVIEW</h4><div className="triggerPreviewGrid"><span><small>ENTRY</small><strong>{masterLayoutV2 ? analysisPrice(triggerMonitor.entryPreview.entry) : fmtDecisionNumber(triggerMonitor.entryPreview.entry, 6)}</strong></span><span><small>SL</small><strong>{masterLayoutV2 ? analysisPrice(triggerMonitor.entryPreview.stopLoss) : fmtDecisionNumber(triggerMonitor.entryPreview.stopLoss, 6)}</strong></span><span><small>TP1 · 30%</small><strong>{masterLayoutV2 ? analysisPrice(triggerMonitor.entryPreview.tp1) : fmtDecisionNumber(triggerMonitor.entryPreview.tp1, 6)}</strong></span><span><small>TP2 · 30%</small><strong>{masterLayoutV2 ? analysisPrice(triggerMonitor.entryPreview.tp2) : fmtDecisionNumber(triggerMonitor.entryPreview.tp2, 6)}</strong></span><span><small>TP3 · 40%</small><strong>{masterLayoutV2 ? analysisPrice(triggerMonitor.entryPreview.tp3) : fmtDecisionNumber(triggerMonitor.entryPreview.tp3, 6)}</strong></span><span><small>R / R</small><strong>1 : {fmtDecisionNumber(triggerMonitor.entryPreview.riskReward, 2)}</strong></span></div></div>}
                <details className="preTradeCheck masterTradeAccordion"><summary><ListChecks aria-hidden="true"/><h4 title="PRE-TRADE CHECK · READ ONLY">PRE-TRADE CHECK · READ ONLY</h4><span>NO ORDER SENT</span><ChevronDown aria-hidden="true"/></summary><div className="preTradeCheckGrid">{triggerMonitor.preTradeChecks.map(check => <span key={check.label} className={`check-${check.status.toLowerCase()}`}><b>{check.status}</b><small>{check.label}</small><em>{masterLayoutV2 && check.label === 'Stop Loss' ? analysisPrice(analysis?.stop_loss) : check.detail}</em></span>)}</div></details>
              </section>
            </section>

          <details className="masterTradePanel marketAnalysisPanel">
            <summary className="panelHeader">
              <div>
                <span className="panelEyebrow">MARKET ANALYSIS</span>
                <h3>{draft.market}</h3>
              </div>
              <span className="marketAnalysisState">{analysis ? tradeDecision.status : 'Analiz yok'}</span>
            </summary>
            <div className="marketAnalysisDecision">
              <span>MARKET REGIME</span>
              <strong>{tradeDecision.marketRegime || '--'}</strong>
              <em>{analysis ? tradeDecision.direction : '--'} · {tradeDecision.confidenceScore === null ? '--' : `${tradeDecision.confidenceScore}%`} {masterLayoutV2 ? 'Güven' : 'CONFIDENCE'}</em>
            </div>
            <div className="marketAnalysisMetrics">
              <div><small>TREND</small><strong>{analysis?.trend || '--'}</strong></div>
              <div><small>MOMENTUM</small><strong>{analysis?.momentum || '--'}</strong></div>
              <div><small>RSI</small><strong>{fmtDecisionNumber(analysis?.rsi, 2)}</strong></div>
              <div><small>MACD</small><strong className={analysis?.macd === undefined ? '' : analysis.macd >= 0 ? 'positive' : 'negative'}>{fmtSigned(analysis?.macd)}</strong></div>
              <div><small>EMA 20</small><strong>--</strong></div>
              <div><small>EMA 50</small><strong>--</strong></div>
              <div><small>EMA 200</small><strong>--</strong></div>
              <div><small>ATR</small><strong>{fmtDecisionNumber(analysis?.atr, 4)}</strong></div>
              <div><small>ADX</small><strong>{fmtDecisionNumber(analysis?.adx, 2)}</strong></div>
              <div><small>VOLUME</small><strong>{analysis?.volume_ratio === undefined ? '--' : `${fmtDecisionNumber(analysis.volume_ratio, 2)}x`}</strong></div>
              <div><small>SUPPORT</small><strong>{fmtMarketPrice(analysis?.support)}</strong></div>
              <div><small>RESISTANCE</small><strong>{fmtMarketPrice(analysis?.resistance)}</strong></div>
            </div>
            <div className="marketAnalysisFooter">
              <span>DECISION</span>
              <strong>{analysis ? tradeDecision.status : 'Analiz yok'}</strong>
              <em>{tradeDecision.signalStrength || '--'}</em>
            </div>
          </details>
          </aside>

        </div>

        <div className="masterTradeOperationsPanel" id={`master-panel-${layoutTab === 'analiz' ? 'operations' : layoutTab}`} role="tabpanel" aria-labelledby={`master-tab-${layoutTab}`} hidden={layoutTab === 'analiz'}>
          <LiveTradingPanel active symbol={draft.market} analysis={analysis} masterTrade masterTradeTab={layoutTab} sharedStatus={liveStatus} sharedConnections={liveConnections} onRefreshStatus={async (force = false) => {
            if (!await refreshAccountData(force)) throw new Error('LIVE durum yenilemesi başarısız; işlem durumunu doğrulayın.')
          }} />

        <div className="masterTradeDataGrid" hidden={layoutTab === 'canli'}>
          <section className="masterTradePanel riskMonitorPanel" hidden={layoutTab !== 'baglanti'}>
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">RISK MONITOR</span>
                <h3>Account risk posture</h3>
              </div>
              <Gauge className="panelHeaderIcon" />
            </div>
            <div className="riskMonitorGrid">
              {riskMetrics.map((metric) => <div key={metric.label}><small>{metric.label}</small><strong className={metric.tone}>{metric.value}</strong></div>)}
            </div>
            <div className="riskMonitorFooter"><span>Open positions</span><strong>{account?.positions ? account.positions.length : '--'} / {account?.limits?.max_open_positions ?? '--'}</strong><span>Live trading</span><strong className="warning">CONTROLLED</strong></div>
          </section>

          <section className="masterTradePanel positionsPanel widePanel" hidden={layoutTab !== 'pozisyonlar'}>
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">OPEN POSITIONS</span>
                <h3>Portfolio</h3>
              </div>
              <button type="button" className="panelGhostButton">25% · 50% · 75% · 100%</button>
            </div>

            <div className="tableWrap portfolioScrollRegion">
              {closeLifecycle.state !== 'IDLE' && <p role="status">{closeLifecycle.message}{closeLifecycle.detail ? ` · ${closeLifecycle.detail}` : ''}</p>}
              <table>
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th>Size</th>
                    <th>Entry</th>
                    <th>Mark</th>
                    <th>Leverage</th>
                    <th>Margin</th>
                    <th>PnL</th>
                    <th>PnL%</th>
                    <th>SL</th>
                    <th>TP</th>
                    <th>Status</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {accountSyncState === 'DISCONNECTED' ? <tr><td colSpan={13} className="emptyState">ACCOUNT DISCONNECTED</td></tr> : accountSyncState === 'DATA_UNAVAILABLE' ? <tr><td colSpan={13} className="emptyState">POSITIONS UNAVAILABLE</td></tr> : accountSyncState === 'STALE' ? <tr><td colSpan={13} className="emptyState">STALE DATA</td></tr> : openPositions.length ? openPositions.map((position) => {
                    const plan = account?.plans?.find(item => item.symbol === position.symbol)
                    const liveLevels = liveProtectionLevels(position)
                    const stopLoss = position.stop_loss ?? (plan?.stop_loss ? Number(plan.stop_loss) : undefined) ?? liveLevels.stopLoss
                    const target = plan?.targets?.[0] ? Number(plan.targets[0]) : position.tp1 ?? liveLevels.targets[0]
                    return (
                    <tr key={position.symbol}>
                      <td><strong>{position.symbol}</strong><span className="positionAge">{position.age || '--'}</span></td>
                      <td><span className={position.direction === 'LONG' ? 'positive' : 'negative'}>{position.direction || '--'}</span></td>
                      <td>{fmtNum(position.quantity, 2)}</td>
                      <td>${fmtMarketPrice(position.entry_price)}</td>
                      <td>${fmtMarketPrice(position.mark_price)}</td>
                      <td>{position.leverage ? `${position.leverage}x` : '--'}</td>
                      <td>{plan?.margin_usdt === undefined ? '--' : `$${fmtCompact(plan.margin_usdt)}`}</td>
                      <td className={(position.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{position.unrealized_pnl === undefined ? '--' : `${position.unrealized_pnl >= 0 ? '+' : ''}$${fmtPnl(position.unrealized_pnl)}`}</td>
                      <td>--</td>
                      <td>${fmtMarketPrice(stopLoss)}</td>
                      <td>${fmtMarketPrice(target)}</td>
                      <td><span className="statusBadge open">OPEN</span></td>
                      <td><div className="positionActions"><button type="button" onClick={() => { setPositionActionError(''); setPositionAction({ mode: 'DETAILS', position: positionWithProtection(position) }) }}>DETAILS</button><button type="button" className="dangerAction positionCloseButton" aria-label={`Close ${position.symbol} position`} onClick={() => { setPositionActionError(''); setPositionAction({ mode: 'CLOSE', position }) }}><XCircle size={14} aria-hidden="true" /> CLOSE POSITION</button></div></td>
                    </tr>
                    )
                  }) : <tr><td colSpan={13} className="emptyState">{snapshot?.accountError || 'NO OPEN POSITIONS'}</td></tr>}
                </tbody>
              </table>
            </div>
          </section>

          <section className="masterTradePanel ordersPanel compactPanel" hidden={layoutTab !== 'pozisyonlar'}>
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">ACTIVE ORDERS</span>
                <h3>Orders</h3>
              </div>
            </div>

            <div className="tableWrap compactTable">
              {accountSyncState === 'DISCONNECTED' ? <div className="emptyState">ACCOUNT DISCONNECTED</div> : accountSyncState === 'DATA_UNAVAILABLE' ? <div className="emptyState">POSITIONS UNAVAILABLE</div> : accountSyncState === 'STALE' ? <div className="emptyState">STALE DATA</div> : account?.open_orders?.length || account?.open_algo_orders?.length ? (
                <table>
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Side</th>
                      <th>Type</th>
                      <th>Price</th>
                      <th>Qty</th>
                      <th>Status</th>
                      <th>Reduce Only</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...(account?.open_orders || []), ...(account?.open_algo_orders || [])].map((order, index) => (
                        <tr key={`${order.symbol || 'ORDER'}-${index}`}>
                          <td>{order.symbol || '--'}</td>
                          <td className={order.side === 'BUY' || order.side === 'LONG' ? 'positive' : 'negative'}>{order.side || '--'}</td>
                          <td>{order.type || '--'}</td>
                          <td>${fmtMarketPrice(order.price || order.trigger_price)}</td>
                          <td>{fmtNum(order.quantity)}</td>
                          <td><span className="statusBadge open">{order.status || '--'}</span></td>
                          <td>{order.reduce_only === undefined ? '--' : order.reduce_only ? 'REDUCE ONLY' : 'NO'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="emptyState">{snapshot?.accountError || 'NO ACTIVE ORDERS'}</div>
              )}
            </div>
          </section>

          <section className="masterTradePanel historyPanel widePanel" hidden={layoutTab !== 'pozisyonlar'}>
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">TRADE HISTORY</span>
                <h3>PERSISTENT HISTORY</h3>
              </div>
              <div className="historyControls">
                <button type="button" className="chip active">All</button>
                <button type="button" className="chip">Manual</button>
                <button type="button" className="chip">Auto</button>
              </div>
            </div>

            <div className="tableWrap">
              <table>
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>PnL</th>
                    <th>PnL%</th>
                    <th>Duration</th>
                    <th>Source</th>
                    <th>Result</th>
                  </tr>
                </thead>
                <tbody>
                  {historySyncState === 'UNAVAILABLE' ? <tr><td colSpan={10} className="emptyState">TRADE HISTORY UNAVAILABLE</td></tr> : historySyncState === 'DISCONNECTED' ? <tr><td colSpan={10} className="emptyState">ACCOUNT DISCONNECTED</td></tr> : history.length ? history.map((trade) => (
                    <tr key={trade.id} onClick={() => setSelectedTrade(trade)} className="historyRow">
                      <td>{new Date(trade.closeTime).toLocaleDateString('en-GB')}</td>
                      <td><strong>{trade.symbol}</strong></td>
                      <td className={trade.side === 'LONG' ? 'positive' : 'negative'}>{trade.side}</td>
                      <td>${fmtNum(trade.entryPrice)}</td>
                      <td>${fmtNum(trade.exitPrice)}</td>
                      <td className={trade.realizedPnl > 0 ? 'positive' : trade.realizedPnl < 0 ? 'negative' : 'muted'}>{trade.realizedPnl > 0 ? '+' : ''}${fmtCompact(trade.realizedPnl)}</td>
                      <td className={trade.pnlPercent === null ? 'muted' : trade.pnlPercent >= 0 ? 'positive' : 'negative'}>{trade.pnlPercent === null ? '--' : `${trade.pnlPercent >= 0 ? '+' : ''}${trade.pnlPercent.toFixed(2)}%`}</td>
                      <td>{trade.duration || '--'}</td>
                      <td>{trade.source}</td>
                      <td><span className={`statusBadge ${trade.realizedPnl > 0 ? 'win' : trade.realizedPnl < 0 ? 'loss' : 'open'}`}>{trade.realizedPnl > 0 ? 'WIN' : trade.realizedPnl < 0 ? 'LOSS' : '--'}</span></td>
                    </tr>
                  )) : <tr><td colSpan={10} className="emptyState">NO TRADE HISTORY</td></tr>}
                </tbody>
              </table>
            </div>
          </section>

          <section className="masterTradePanel performancePanel compactPanel" hidden={layoutTab !== 'pozisyonlar'}>
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">PERFORMANCE</span>
                <h3>Execution summary</h3>
              </div>
            </div>

            <div className="performanceMetrics">
              <div><small>Total trades</small><strong>{performanceSnapshot?.total_trades ?? '--'}</strong></div>
              <div><small>Wins</small><strong>{performanceSnapshot?.wins ?? '--'}</strong></div>
              <div><small>Losses</small><strong>{performanceSnapshot?.losses ?? '--'}</strong></div>
              <div><small>Win rate</small><strong>{performanceSnapshot ? `${performanceSnapshot.win_rate.toFixed(1)}%` : '--'}</strong></div>
              <div><small>Total PnL</small><strong className={(performanceSnapshot?.net_profit ?? 0) >= 0 ? 'positive' : 'negative'}>{performanceSnapshot ? `$${fmtCompact(performanceSnapshot.net_profit)}` : '--'}</strong></div>
              <div><small>Avg win</small><strong>{performanceSnapshot?.average_win === null || performanceSnapshot?.average_win === undefined ? '--' : `$${fmtCompact(performanceSnapshot.average_win)}`}</strong></div>
              <div><small>Avg loss</small><strong>{performanceSnapshot?.average_loss === null || performanceSnapshot?.average_loss === undefined ? '--' : `$${fmtCompact(performanceSnapshot.average_loss)}`}</strong></div>
              <div><small>Profit factor</small><strong>{performanceSnapshot?.profit_factor === null || performanceSnapshot?.profit_factor === undefined ? '--' : performanceSnapshot.profit_factor.toFixed(2)}</strong></div>
              <div><small>Best trade</small><strong className="positive">{performanceSnapshot ? `$${fmtCompact(performanceSnapshot.best_trade)}` : '--'}</strong></div>
              <div><small>Worst trade</small><strong className={(performanceSnapshot?.worst_trade ?? 0) < 0 ? 'negative' : 'muted'}>{performanceSnapshot ? `$${fmtCompact(performanceSnapshot.worst_trade)}` : '--'}</strong></div>
              <div><small>Max drawdown</small><strong>{performanceSnapshot ? `$${fmtCompact(performanceSnapshot.max_drawdown)}` : '--'}</strong></div>
            </div>

            <div className="miniSparkline" aria-label="Performance trend">
              {performanceTrend.map((point, index) => (
                <span key={index} style={{ height: `${point}%` }} />
              ))}
            </div>
          </section>

        </div>
        </div>
      </div>

      {positionAction && (
        <div className="positionActionBackdrop" role="presentation" onClick={() => { if (!positionActionBusy) { setPositionAction(null); setPositionActionError('') } }}>
          <section className="positionActionModal" role="dialog" aria-modal="true" onClick={event => event.stopPropagation()}>
            <header><div><span>LIVE POSITION</span><h2>{positionAction.mode === 'DETAILS' ? 'POSITION DETAILS' : positionAction.mode === 'REDUCE' ? 'REDUCE ONLY' : 'CLOSE LIVE POSITION'}</h2></div><button type="button" onClick={() => setPositionAction(null)} disabled={positionActionBusy}>CLOSE</button></header>
            <div className="positionActionGrid">
              <div><small>Symbol</small><b>{positionAction.position.symbol}</b></div>
              <div><small>Direction</small><b>{positionAction.position.direction || '--'}</b></div>
              <div><small>Status</small><b>OPEN</b></div>
              <div><small>Quantity</small><b>{fmtNum(positionAction.position.quantity, 6)}</b></div>
              <div><small>Entry Price</small><b>${fmtMarketPrice(positionAction.position.entry_price)}</b></div>
              <div><small>Mark Price</small><b>${fmtMarketPrice(positionAction.position.mark_price)}</b></div>
              <div><small>Leverage</small><b>{positionAction.position.leverage ? `${positionAction.position.leverage}x` : '--'}</b></div>
              <div><small>Margin</small><b>--</b></div>
              <div><small>Liquidation</small><b>${fmtNum(positionAction.position.liquidation_price)}</b></div>
              <div><small>Unrealized PnL</small><b className={(positionAction.position.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{positionAction.position.unrealized_pnl === undefined ? '--' : `${positionAction.position.unrealized_pnl >= 0 ? '+' : ''}$${fmtPnl(positionAction.position.unrealized_pnl)}`}</b></div>
              <div><small>PnL %</small><b>--</b></div>
              <div><small>Stop Loss</small><b>${fmtMarketPrice(positionAction.position.stop_loss)}</b></div>
              <div><small>TP1</small><b>${fmtMarketPrice(positionAction.position.tp1)}</b></div>
              <div><small>TP2</small><b>--</b></div>
              <div><small>TP3</small><b>--</b></div>
            </div>
            {positionAction.mode === 'REDUCE' && <div className="reduceControls"><label>Quantity<input type="number" min="0" max={positionAction.position.quantity || undefined} step="any" value={reduceQuantity} onChange={event => setReduceQuantity(event.target.value)} /></label><div><button type="button" onClick={() => setReduceQuantity(String((positionAction.position.quantity || 0) * 0.25))}>25%</button><button type="button" onClick={() => setReduceQuantity(String((positionAction.position.quantity || 0) * 0.5))}>50%</button><button type="button" onClick={() => setReduceQuantity(String((positionAction.position.quantity || 0) * 0.75))}>75%</button><button type="button" onClick={() => setReduceQuantity(String(positionAction.position.quantity || 0))}>100%</button></div></div>}
            {positionAction.mode !== 'DETAILS' && <p className="positionActionWarning">This action uses the existing V25 live ownership and confirmation checks.</p>}
            {positionActionError && <div className="positionActionError" role="alert">{positionActionError}</div>}
            {positionAction.mode !== 'DETAILS' && <footer><button type="button" onClick={() => { setPositionAction(null); setPositionActionError('') }} disabled={positionActionBusy}>CANCEL</button><button type="button" className="dangerAction" onClick={() => void submitPositionAction()} disabled={positionActionBusy}>{positionActionBusy ? positionAction.mode === 'CLOSE' ? 'CLOSING POSITION...' : 'REDUCING POSITION...' : positionAction.mode === 'CLOSE' ? 'CLOSE POSITION' : 'REDUCE ONLY'}</button></footer>}
          </section>
        </div>
      )}

      {selectedTrade && (
        <div className="masterTradeDrawerBackdrop" onClick={() => setSelectedTrade(null)}>
          <aside className="masterTradeDrawer" onClick={(event) => event.stopPropagation()}>
            <header>
              <div>
                <span>TRADE DETAIL</span>
                <h3>{selectedTrade.symbol}</h3>
              </div>
              <button type="button" onClick={() => setSelectedTrade(null)}>Close</button>
            </header>
            <div className="drawerMeta">
              <div><small>Direction</small><b>{selectedTrade.side}</b></div>
              <div><small>Result</small><b className={selectedTrade.realizedPnl >= 0 ? 'positive' : 'negative'}>{selectedTrade.realizedPnl >= 0 ? 'WIN' : 'LOSS'}</b></div>
              <div><small>Entry</small><b>${fmtNum(selectedTrade.entryPrice)}</b></div>
              <div><small>Exit</small><b>${fmtNum(selectedTrade.exitPrice)}</b></div>
              <div><small>PnL</small><b className={selectedTrade.realizedPnl >= 0 ? 'positive' : 'negative'}>${fmtCompact(selectedTrade.realizedPnl)}</b></div>
              <div><small>ROI</small><b>{selectedTrade.pnlPercent === null ? '--' : `${selectedTrade.pnlPercent.toFixed(2)}%`}</b></div>
              <div><small>Leverage</small><b>{selectedTrade.leverage === null ? '--' : `${selectedTrade.leverage}x`}</b></div>
              <div><small>Margin</small><b>${fmtCompact(selectedTrade.margin)}</b></div>
              <div><small>Risk %</small><b>--</b></div>
              <div><small>R / R</small><b>--</b></div>
              <div><small>Opportunity</small><b>{selectedTrade.opportunityScore}</b></div>
              <div><small>Source</small><b>{selectedTrade.source}</b></div>
              <div><small>Close reason</small><b>{selectedTrade.closeReason}</b></div>
            </div>

            <div className="drawerSection">
              <h4>TRADE LIFECYCLE</h4>
              <div className="timelineList">
                {[
                  ['Signal Generated', selectedTrade.scanCycle],
                  ['Order Submitted', selectedTrade.openTime],
                  ['Entry Filled', selectedTrade.openTime],
                  ['TP1', fmtNum(selectedTrade.tp1)],
                  ['TP2', fmtNum(selectedTrade.tp2)],
                  ['TP3', fmtNum(selectedTrade.tp3)],
                  ['Closed', selectedTrade.closeReason || '--'],
                ].map(([event, detail], index) => (
                  <div key={`${event}-${index}`} className="timelineItem">
                    <span className="timelineDot" />
                    <div>
                      <strong>{event}</strong>
                      <small>{detail}</small>
                    </div>
                  </div>
                ))}

              </div>
            </div>
          </aside>
        </div>
      )}

    </section>
    </section>
    </PremiumWorkspace>
    </MasterTradePresentation>
  )
}
