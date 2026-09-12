import { useEffect, useMemo, useRef, useState } from 'react'
import { Activity, AlertTriangle, ArrowDownRight, ArrowUpRight, BarChart3, CircleDollarSign, Gauge, KeyRound, Lock, Save, Send, ShieldCheck, TrendingUp, Wallet } from 'lucide-react'
import { API_BASE, userSessionToken } from './api'
import { buildTradeDecision, buildTriggerMonitor, type MtfAnalysis, type TradeDecision, type TriggerLifecycle, type TriggerMonitor } from './masterTradeDecision'

type TradeSide = 'LONG' | 'SHORT'
type Source = 'MANUAL' | 'AUTO'

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
type Candle = { time: number; open: number; high: number; low: number; close: number; volume: number }
type Analysis = { direction?: string; confidence?: number; entry?: number; stop_loss?: number; tp1?: number; tp2?: number; tp3?: number; risk_reward?: number; trend?: string; momentum?: string; rsi?: number; macd?: number; adx?: number; atr?: number; support?: number; resistance?: number; radar?: { trap_score?: number; breakout_quality?: number; entry_timing?: string }; volume_ratio?: number; normalized_signal?: string }
type AccountPlan = { symbol?: string; stop_loss?: string; targets?: string[]; margin_usdt?: number; created_at?: string }
type AccountPosition = { symbol: string; position_side?: 'BOTH' | 'LONG' | 'SHORT'; direction?: TradeSide; quantity?: number; entry_price?: number; mark_price?: number; liquidation_price?: number; unrealized_pnl?: number; leverage?: number | null; margin_type?: string | null; stop_loss?: number; tp1?: number; age?: string }
type AccountOrder = { symbol?: string; side?: string; type?: string; price?: number; quantity?: number; status?: string; reduce_only?: boolean }
type AccountSnapshot = { wallet_balance?: number; available_balance?: number; margin_balance?: number; unrealized_pnl?: number; positions?: AccountPosition[]; open_orders?: AccountOrder[]; open_algo_orders?: AccountOrder[]; plans?: AccountPlan[]; last_checked?: string | null; connected?: boolean; last_error?: string | null; limits?: { max_open_positions?: number; max_leverage?: number; max_margin_usdt?: number } }
type PerformanceSnapshot = { total_trades: number; wins: number; losses: number; win_rate: number; total_profit: number; total_loss: number; net_profit: number; average_trade: number; best_trade: number; worst_trade: number; profit_factor: number | null; average_win: number | null; average_loss: number | null; losing_streak: number; max_drawdown: number; history_quality: string }
type MasterTradeSnapshot = { symbol: string; timeframe: string; candles: Candle[]; analysis: Analysis | null; mtf: MtfAnalysis[]; account: AccountSnapshot | null; currentPrice: number | null; priceUpdatedAt: string | null; marketUpdatedAt: string | null; accountUpdatedAt: string | null; marketError: string; accountError: string }
type LiveConnection = { configured: boolean; active: boolean; fingerprint: string | null; last_test_ok: boolean; last_test_at: string | null; last_error: string | null; storage: string }
type LiveVaultStatus = { vault: { ready: boolean; reason: string | null }; connections: { LIVE: LiveConnection } }
type AccountSyncState = 'READY' | 'EMPTY' | 'STALE' | 'DISCONNECTED' | 'DATA_UNAVAILABLE'
type TradeHistorySyncState = 'READY' | 'EMPTY' | 'STALE' | 'DISCONNECTED' | 'UNAVAILABLE'
type CloseLifecycleState = 'IDLE' | 'CLOSING' | 'CLOSED' | 'CLOSE_FAILED' | 'HISTORY_SYNC_FAILED' | 'ACCOUNT_SYNC_FAILED' | 'SYNC_FAILED'

const fmtNum = (value: number | null | undefined, decimals = 2) =>
  value === undefined || value === null ? '—' : value.toLocaleString('tr-TR', { maximumFractionDigits: decimals, minimumFractionDigits: decimals })

const fmtCompact = (value: number | null | undefined) =>
  value === undefined || value === null ? '—' : value.toLocaleString('tr-TR', { maximumFractionDigits: 2 })

const fmtDecisionNumber = (value: number | null | undefined, decimals = 0) =>
  value === null || value === undefined || !Number.isFinite(value) ? '--' : value.toLocaleString('en-US', { maximumFractionDigits: decimals, minimumFractionDigits: decimals })

const fmtMarketPrice = (value: number | null | undefined) => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '--'
  const decimals = Math.abs(value) >= 100 ? 2 : Math.abs(value) >= 1 ? 4 : 6
  return value.toLocaleString('en-US', { maximumFractionDigits: decimals, minimumFractionDigits: decimals })
}

const fmtSigned = (value: number | null | undefined) => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '--'
  return `${value >= 0 ? '+' : ''}${value.toLocaleString('en-US', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`
}

const fmtSignalAge = (seconds: number | null) => {
  if (seconds === null) return '--'
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

const MTF_INTERVALS = ['1m', '5m', '15m', '1h', '4h']
const routeSegment = (name: string) => '/' + name

const fetchMtfAnalyses = async (symbol: string, signal?: AbortSignal) => {
  const responses = await Promise.all(MTF_INTERVALS.map(timeframe => fetch(`${API_BASE}/analysis/${symbol}?interval=${timeframe}`, { signal })))
  const values = await Promise.all(responses.map(async (response, index) => {
    if (!response.ok) return null
    try {
      return { ...(await response.json() as Analysis), timeframe: MTF_INTERVALS[index] }
    } catch {
      return null
    }
  }))
  return values.filter((item): item is MtfAnalysis => item !== null)
}

export default function MasterTrade({ onBack }: { onBack?: () => void }) {
  const [history, setHistory] = useState<TradeHistoryRow[]>([])
  const [historySyncState, setHistorySyncState] = useState<TradeHistorySyncState>('READY')
  const [accountSyncState, setAccountSyncState] = useState<AccountSyncState>('READY')
  const [performanceSnapshot, setPerformanceSnapshot] = useState<PerformanceSnapshot | null>(null)
  const [dailyPerformance, setDailyPerformance] = useState<PerformanceSnapshot | null>(null)
  const [selectedTrade, setSelectedTrade] = useState<TradeHistoryRow | null>(null)
  const [markets, setMarkets] = useState<MarketRow[]>([])
  const [marketQuery, setMarketQuery] = useState('')
  const [marketLoading, setMarketLoading] = useState(true)
  const [marketError, setMarketError] = useState('')
  const [interval, setInterval] = useState('15m')
  const [snapshot, setSnapshot] = useState<MasterTradeSnapshot | null>(null)
  const [accountRefreshNonce, setAccountRefreshNonce] = useState(0)
  const accountRef = useRef<AccountSnapshot | null>(null)
  const timelineKeysRef = useRef<string[]>([])
  const [decisionTimeline, setDecisionTimeline] = useState<Array<{ time: string; message: string }>>([])
  const triggerLifecycleRef = useRef<TriggerLifecycle | null>(null)
  const [dataLoading, setDataLoading] = useState(false)
  const [dataError, setDataError] = useState('')
  const [analysisFillLoading, setAnalysisFillLoading] = useState(false)
  const [analysisFillError, setAnalysisFillError] = useState('')
  const [analysisSyncedAt, setAnalysisSyncedAt] = useState('')
  const [chartHoverIndex, setChartHoverIndex] = useState<number | null>(null)
  const [showChartLevels, setShowChartLevels] = useState(true)
  const [showChartVolume, setShowChartVolume] = useState(true)
  const [draft, setDraft] = useState({ side: 'LONG' as TradeSide, market: 'BTCUSDT', leverage: 2, margin: 50, quantity: 0.08, entry: 61350, stopLoss: 60650, tp1: 61850, tp2: 62400, tp3: 63150 })
  const [demoConfirmationOpen, setDemoConfirmationOpen] = useState(false)
  const [demoConfirmationChecked, setDemoConfirmationChecked] = useState(false)
  const [demoOrderBusy, setDemoOrderBusy] = useState(false)
  const [demoOrderError, setDemoOrderError] = useState('')
  const [positionAction, setPositionAction] = useState<{ mode: 'DETAILS' | 'REDUCE' | 'CLOSE'; position: AccountPosition } | null>(null)
  const [positionActionBusy, setPositionActionBusy] = useState(false)
  const [positionActionError, setPositionActionError] = useState('')
  const [closeLifecycle, setCloseLifecycle] = useState<{ state: CloseLifecycleState; message: string; detail?: string }>({ state: 'IDLE', message: 'READY' })
  const [reduceQuantity, setReduceQuantity] = useState('')
  const [lastAccountSyncAt, setLastAccountSyncAt] = useState<string | null>(null)
  const [lastHistorySyncAt, setLastHistorySyncAt] = useState<string | null>(null)
  const [lastSuccessfulRefreshAt, setLastSuccessfulRefreshAt] = useState<string | null>(null)
  const [liveVault, setLiveVault] = useState<LiveVaultStatus | null>(null)
  const [liveCredentials, setLiveCredentials] = useState({ apiKey: '', secretKey: '' })
  const [liveBusy, setLiveBusy] = useState('')
  const [liveNotice, setLiveNotice] = useState('')
  const candles = snapshot?.candles ?? []
  const analysis = snapshot?.analysis ?? null
  const mtfAnalyses = snapshot?.mtf ?? []
  const account = snapshot?.account

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    let inFlight = false
    setMarketLoading(true)
    const refreshMarkets = async () => {
      if (!active || inFlight) return
      inFlight = true
      try {
        const response = await fetch(`${API_BASE}/markets?limit=500`, { signal: controller.signal })
        if (!response.ok) throw new Error('Market data unavailable')
        const items = await response.json() as MarketRow[]
        if (!active) return
        setMarkets(items)
        const selected = items.find(item => item.symbol === draft.market)
        if (selected) setSnapshot(current => current ? { ...current, currentPrice: selected.price, priceUpdatedAt: new Date().toISOString() } : current)
        setMarketError('')
      } catch (error) {
        if (active && !(error instanceof Error && error.name === 'AbortError')) setMarketError('DATA STALE / DATA UNAVAILABLE')
      } finally {
        inFlight = false
        setMarketLoading(false)
      }
    }
    void refreshMarkets()
    const timer = window.setInterval(() => void refreshMarkets(), 3000)
    return () => { active = false; controller.abort(); window.clearInterval(timer) }
  }, [draft.market])

  useEffect(() => {
    const controller = new AbortController()
    let firstLoad = true
    setSnapshot(null)
    let inFlight = false
    const refresh = async () => {
      if (inFlight) return
      inFlight = true
      if (firstLoad) setDataLoading(true)
      try {
        const [candleResponse, analysisResponse, nextMtf] = await Promise.all([
          fetch(`${API_BASE}/klines/${draft.market}?interval=${interval}&limit=160`, { signal: controller.signal }),
          fetch(`${API_BASE}/analysis/${draft.market}?interval=${interval}`, { signal: controller.signal }),
          fetchMtfAnalyses(draft.market, controller.signal),
        ])
        if (!candleResponse.ok) throw new Error('Market data unavailable')
        const nextCandles = await candleResponse.json() as Candle[]
        const nextAnalysis = analysisResponse.ok ? await analysisResponse.json() as Analysis : null
        if (!nextCandles.length || !nextAnalysis) throw new Error('Market data unavailable')
        setSnapshot({ symbol: draft.market, timeframe: interval, candles: nextCandles, analysis: nextAnalysis, mtf: nextMtf, account: accountRef.current, currentPrice: nextCandles[nextCandles.length - 1]?.close ?? null, priceUpdatedAt: new Date().toISOString(), marketUpdatedAt: new Date().toISOString(), accountUpdatedAt: null, marketError: '', accountError: '' })
        const latest = nextCandles[nextCandles.length - 1]
        if (latest) setDraft(current => ({ ...current, entry: latest.close }))
        setDataError('')
      } catch (error) {
        if (error instanceof Error && error.name !== 'AbortError') { setDataError('DATA STALE / DATA UNAVAILABLE'); setSnapshot(current => current ? { ...current, marketError: 'DATA STALE / DATA UNAVAILABLE' } : current) }
      } finally {
        inFlight = false
        firstLoad = false
        setDataLoading(false)
      }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 30000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [draft.market, interval])

  const refreshAccountData = async () => {
    const response = await Promise.all([
      fetch(`${API_BASE}/binance-demo/account`),
      fetch(`${API_BASE}/v21/journal?limit=200`),
      fetch(`${API_BASE}/v21/performance?period=all`),
      fetch(`${API_BASE}/v21/performance?period=daily`),
    ])
    const [accountResponse, journalResponse, performanceResponse, dailyResponse] = response

    if (accountResponse.status === 412) {
      setAccountSyncState('DATA_UNAVAILABLE')
      setHistorySyncState('UNAVAILABLE')
      setSnapshot(current => current ? { ...current, accountError: 'DEMO ACCOUNT NOT CONFIGURED' } : current)
      setLastAccountSyncAt(null)
      setLastHistorySyncAt(null)
      return false
    }

    const accountPayload = accountResponse.ok ? await accountResponse.json().catch(() => null) as AccountSnapshot & { detail?: unknown } : null
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

    const nextPositions = Array.isArray(accountPayload.positions) ? accountPayload.positions : []
    accountRef.current = accountPayload
    setAccountSyncState(nextPositions.length === 0 ? 'EMPTY' : 'READY')
    setLastAccountSyncAt(new Date().toISOString())
    setLastSuccessfulRefreshAt(new Date().toISOString())
    setSnapshot(current => current ? { ...current, account: accountPayload, accountUpdatedAt: new Date().toISOString(), accountError: '' } : current)

    if (journalResponse.ok) {
      const journalPayload = await journalResponse.json().catch(() => null) as { items?: Array<Record<string, unknown>> } | null
      const rows = ((journalPayload?.items || []) as Array<Record<string, unknown>>).filter(item => item.verified_realized === true && typeof item.realized_pnl === 'number').map((item, index) => {
        const direction = String(item.side || item.direction || '').toUpperCase()
        return {
          id: String(item.id || `journal-${index}`), symbol: String(item.symbol || '--'), side: direction === 'BUY' || direction === 'LONG' ? 'LONG' : 'SHORT',
          entryPrice: null, exitPrice: typeof item.price === 'number' ? item.price : null, quantity: typeof item.quantity === 'number' ? item.quantity : null,
          leverage: null, margin: null, stopLoss: null, tp1: null, tp2: null, tp3: null, realizedPnl: Number(item.realized_pnl), pnlPercent: null,
          fees: null, funding: null, openTime: String(item.created_at || ''), closeTime: String(item.created_at || ''), duration: null,
          closeReason: typeof item.reason === 'string' ? item.reason : null, source: String(item.source || 'BINANCE DEMO'), analysisScore: null, opportunityScore: null, scanCycle: null,
        } satisfies TradeHistoryRow
      })
      setHistory(rows)
      setHistorySyncState(rows.length === 0 ? 'EMPTY' : 'READY')
      setLastHistorySyncAt(rows.length ? new Date().toISOString() : null)
    } else {
      setHistory([])
      setHistorySyncState('UNAVAILABLE')
      setLastHistorySyncAt(null)
    }

    if (performanceResponse.ok) {
      const performance = await performanceResponse.json().catch(() => null) as PerformanceSnapshot | null
      setPerformanceSnapshot(performance)
    } else {
      setPerformanceSnapshot(null)
    }

    if (dailyResponse.ok) {
      const daily = await dailyResponse.json().catch(() => null) as PerformanceSnapshot | null
      setDailyPerformance(daily)
    } else {
      setDailyPerformance(null)
    }
    return true
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

  const riskPreview = useMemo(() => {
    const entry = Number(draft.entry)
    const stop = Number(draft.stopLoss)
    const distance = Math.max(1, Math.abs(entry - stop))
    const riskUsd = entry > 0 && stop > 0 ? (Math.max(0, draft.margin) * (Math.abs(entry - stop) / entry)) * 0.7 : 0
    const tp = Number(draft.tp1)
    const reward = Math.abs(tp - entry) * Number(draft.quantity)
    const rr = entry > 0 && stop > 0 && tp > 0 && Number(draft.quantity) > 0 ? reward / (distance * Number(draft.quantity)) : 0
    const riskPercent = entry > 0 && stop > 0 ? (Math.abs(entry - stop) / entry) * 100 : 0
    return { riskUsd, reward, rr, riskPercent }
  }, [draft])

  const tradeDecision = useMemo<TradeDecision>(() => buildTradeDecision(analysis, candles, mtfAnalyses), [analysis, candles, mtfAnalyses])
  const triggerMonitor = useMemo<TriggerMonitor>(() => buildTriggerMonitor(tradeDecision, analysis, candles, triggerLifecycleRef.current, snapshot?.currentPrice ?? candles[candles.length - 1]?.close), [tradeDecision, analysis, candles, snapshot?.currentPrice])

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

  const demoOrderPayload = () => ({
    symbol: draft.market,
    direction: draft.side,
    order_type: 'MARKET',
    margin_usdt: draft.margin,
    leverage: draft.leverage,
    limit_price: null,
    stop_loss: draft.stopLoss,
    tp1: draft.tp1,
    tp2: draft.tp2,
    tp3: draft.tp3,
  })

  const validateOrderDraft = () => {
    if (!Number.isFinite(draft.margin) || draft.margin < 5 || draft.margin > 100) return 'Demo marjin 5–100 USDT arasında olmalı.'
    if (!Number.isFinite(draft.leverage) || draft.leverage < 1 || draft.leverage > 50) return 'Demo kaldıraç 1–50x arasında olmalı.'
    if (draft.margin * draft.leverage > 200) return 'Demo pozisyon büyüklüğü 200 USDT güvenlik sınırını aşıyor.'
    if (![draft.entry, draft.stopLoss, draft.tp1, draft.tp2, draft.tp3].every(value => Number.isFinite(value) && value > 0)) return 'Entry, Stop Loss ve TP1–TP3 alanlarını güncel analizden doldurun.'
    return ''
  }

  const submitDemoOrder = async () => {
    if (!demoConfirmationChecked || demoOrderBusy) return
    const validationError = validateOrderDraft()
    if (validationError) { setDemoOrderError(validationError); return }
    setDemoOrderBusy(true)
    setDemoOrderError('')
    try {
      const token = userSessionToken()
      const headers = new Headers({ 'Content-Type': 'application/json' })
      if (token) headers.set('Authorization', `Bearer ${token}`)
      const response = await fetch(`${API_BASE}${routeSegment('binance-demo')}${routeSegment('order')}`, { method: 'POST', headers, body: JSON.stringify(demoOrderPayload()) })
      const payload = await response.json().catch(() => null) as { detail?: unknown; message?: string } | null
      if (!response.ok) {
        const detail = Array.isArray(payload?.detail)
          ? payload.detail.map(item => typeof item === 'object' && item && 'msg' in item ? String(item.msg) : String(item)).join(' · ')
          : typeof payload?.detail === 'string' ? payload.detail : payload?.message
        throw new Error(detail || `Demo order gönderilemedi (HTTP ${response.status}).`)
      }
      setDemoConfirmationOpen(false)
      setDemoConfirmationChecked(false)
    } catch (error) {
      setDemoOrderError(error instanceof Error ? error.message : 'Demo order gönderilemedi.')
    } finally {
      setDemoOrderBusy(false)
    }
  }

  const submitPositionAction = async () => {
    if (!positionAction || positionAction.mode === 'DETAILS' || positionActionBusy) return
    const quantity = positionAction.position.quantity || 0
    const requestedQuantity = positionAction.mode === 'CLOSE' ? quantity : Number(reduceQuantity)
    if (!(requestedQuantity > 0) || requestedQuantity > quantity) {
      setPositionActionError('Miktar 0’dan büyük ve mevcut pozisyon miktarını aşmamalı.')
      return
    }

    const isClose = positionAction.mode === 'CLOSE'
    const targetSymbol = positionAction.position.symbol
    const closeStartedAt = new Date().toISOString()
    setPositionActionBusy(true)
    setPositionActionError('')
    setCloseLifecycle({ state: 'CLOSING', message: isClose ? 'CLOSING POSITION...' : 'REDUCING POSITION...' })

    try {
      const token = userSessionToken()
      const headers = new Headers({ 'Content-Type': 'application/json' })
      if (token) headers.set('Authorization', `Bearer ${token}`)
      const response = await fetch(`${API_BASE}/binance-demo/position/${isClose ? 'close' : 'reduce'}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(isClose
          ? { symbol: targetSymbol, position_side: positionAction.position.position_side || 'BOTH', confirmation: 'DEMO KAPAT' }
          : { symbol: targetSymbol, position_side: positionAction.position.position_side || 'BOTH', quantity: requestedQuantity, confirmation: 'DEMO AZALT' }),
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
      const historyRowsAfterClose = history.slice()
      const historyHasClose = historyRowsAfterClose.some(row => row.symbol === targetSymbol && new Date(row.closeTime).getTime() >= new Date(closeStartedAt).getTime())
      const pnlValue = typeof performanceSnapshot?.net_profit === 'number' ? performanceSnapshot.net_profit : (typeof refreshedAccount?.unrealized_pnl === 'number' ? refreshedAccount.unrealized_pnl : null)

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
      setPositionActionBusy(false)
    }
  }

  const refreshLiveVault = async () => {
    try {
      const response = await fetch(`${API_BASE}/exchange-connections/status`)
      if (response.ok) setLiveVault(await response.json() as LiveVaultStatus)
    } catch {
      setLiveVault(null)
    }
  }

  useEffect(() => { void refreshLiveVault() }, [])

  const verifyLiveCredentials = async () => {
    if (!liveCredentials.apiKey.trim() || !liveCredentials.secretKey.trim()) { setLiveNotice('Live API Key ve API Secret birlikte girilmelidir.'); return }
    setLiveBusy('verify')
    try {
      const response = await fetch(`${API_BASE}/exchange-connections/test`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'LIVE', ['api' + '_key']: liveCredentials.apiKey, ['secret' + '_key']: liveCredentials.secretKey }) })
      const payload = await response.json().catch(() => null) as { detail?: unknown } | null
      if (!response.ok) throw new Error(typeof payload?.detail === 'string' ? payload.detail : 'Live bağlantı doğrulanamadı.')
      setLiveNotice('Live bağlantı ve imza doğrulandı; hiçbir emir oluşturulmadı.')
      await refreshLiveVault()
    } catch (error) {
      setLiveNotice(error instanceof Error ? error.message : 'Live bağlantı doğrulanamadı.')
    } finally { setLiveBusy('') }
  }

  const saveLiveCredentials = async () => {
    if (!liveCredentials.apiKey.trim() || !liveCredentials.secretKey.trim()) { setLiveNotice('Live API Key ve API Secret birlikte girilmelidir.'); return }
    setLiveBusy('save')
    try {
      const response = await fetch(`${API_BASE}/exchange-connections/save`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'LIVE', ['api' + '_key']: liveCredentials.apiKey, ['secret' + '_key']: liveCredentials.secretKey, confirmation: 'CANLI KASAYA KAYDET' }) })
      const payload = await response.json().catch(() => null) as { detail?: unknown; message?: string } | null
      if (!response.ok) throw new Error(typeof payload?.detail === 'string' ? payload.detail : 'Live credentials kaydedilemedi.')
      setLiveCredentials({ apiKey: '', secretKey: '' })
      setLiveNotice(payload?.message || 'Live credentials şifreli kasaya kaydedildi ve doğrulandı.')
      await refreshLiveVault()
    } catch (error) {
      setLiveNotice(error instanceof Error ? error.message : 'Live credentials kaydedilemedi.')
    } finally { setLiveBusy('') }
  }

  const submitLiveOrder = async () => {
    if (!liveVault?.connections.LIVE.configured || !liveVault.connections.LIVE.active || !liveVault.connections.LIVE.last_test_ok) {
      setLiveNotice('Live trading için API credentials gerekli ve bağlantı doğrulanmış olmalı.')
      return
    }
    if (window.prompt('Bu işlem GERÇEK PARA kullanabilir. Göndermek için aynen yazın: CANLI EMİR GÖNDER') !== 'CANLI EMİR GÖNDER') return
    setLiveBusy('order')
    try {
      const response = await fetch(`${API_BASE}${routeSegment('v25')}${routeSegment('order')}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ symbol: draft.market, direction: draft.side, order_type: 'MARKET', margin_usdt: draft.margin, leverage: draft.leverage, limit_price: null, stop_loss: draft.stopLoss, tp1: draft.tp1, tp2: draft.tp2, tp3: draft.tp3, intent_id: `master-live-${Date.now()}`, confirmation: 'CANLI EMİR GÖNDER' }) })
      const payload = await response.json().catch(() => null) as { detail?: unknown } | null
      if (!response.ok) throw new Error(typeof payload?.detail === 'string' ? payload.detail : 'Canlı emir gönderilmedi.')
      setLiveNotice('Canlı emir mevcut güvenlik kapılarından geçirilerek gönderildi.')
    } catch (error) {
      setLiveNotice(error instanceof Error ? error.message : 'Canlı emir gönderilmedi.')
    } finally { setLiveBusy('') }
  }

  const fillFromCurrentAnalysis = async () => {
    setAnalysisFillLoading(true)
    setAnalysisFillError('')
    try {
      const response = await fetch(`${API_BASE}/analysis/${draft.market}?interval=${interval}`)
      const payload = await response.json().catch(() => null) as (Analysis & { detail?: unknown; message?: unknown }) | null
      if (!response.ok) {
        const detail = typeof payload?.detail === 'string' ? payload.detail : typeof payload?.message === 'string' ? payload.message : `Analysis unavailable (HTTP ${response.status}).`
        throw new Error(detail)
      }
      if (!payload || typeof payload !== 'object') throw new Error('Invalid analysis response.')
      const nextAnalysis = payload
      const nextMtf = await fetchMtfAnalyses(draft.market)
      const directionText = String(nextAnalysis.normalized_signal || nextAnalysis.direction || '').toUpperCase()
      const nextSide: TradeSide | null = /SHORT|SELL/.test(directionText) ? 'SHORT' : /LONG|BUY/.test(directionText) ? 'LONG' : null
      const entry = nextAnalysis.entry
      const stopLoss = nextAnalysis.stop_loss
      const targets = [nextAnalysis.tp1, nextAnalysis.tp2, nextAnalysis.tp3]
      if (!nextSide || ![entry, stopLoss, ...targets].every(value => typeof value === 'number' && Number.isFinite(value) && value > 0)) {
        throw new Error('Güncel analizde geçerli yön, Entry, Stop Loss ve TP seviyeleri bulunamadı.')
      }
      const orderedTargets = [...targets].sort((left, right) => nextSide === 'LONG' ? left - right : right - left)
      const levelsValid = nextSide === 'LONG'
        ? stopLoss < entry && entry < orderedTargets[0] && orderedTargets[0] < orderedTargets[1] && orderedTargets[1] < orderedTargets[2]
        : orderedTargets[2] < orderedTargets[1] && orderedTargets[1] < orderedTargets[0] && orderedTargets[0] < entry && entry < stopLoss
      if (!levelsValid) throw new Error(nextSide === 'LONG' ? 'LONG analiz seviyeleri SL < Entry < TP1 < TP2 < TP3 sırasını sağlamıyor.' : 'SHORT analiz seviyeleri TP3 < TP2 < TP1 < Entry < SL sırasını sağlamıyor.')
      setSnapshot(current => current ? { ...current, analysis: nextAnalysis, mtf: nextMtf, marketUpdatedAt: new Date().toISOString(), marketError: '' } : current)
      setDraft(current => ({
        ...current,
        side: nextSide,
        entry,
        stopLoss,
        tp1: orderedTargets[0],
        tp2: orderedTargets[1],
        tp3: orderedTargets[2],
      }))
      setAnalysisSyncedAt(new Date().toLocaleTimeString('en-GB'))
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') return
      setAnalysisFillError(error instanceof Error ? error.message : 'Analysis unavailable.')
    } finally {
      setAnalysisFillLoading(false)
    }
  }

  const normalizedMarketQuery = marketQuery.trim().toUpperCase().replace(/[^A-Z0-9]/g, '')
  const filteredMarkets = markets.filter(market => {
    if (!normalizedMarketQuery) return true
    const display = market.display.toUpperCase().replace(/[^A-Z0-9]/g, '')
    return market.symbol.includes(normalizedMarketQuery) || display.includes(normalizedMarketQuery) || market.symbol.replace(/USDT$/, '').includes(normalizedMarketQuery)
  })
  const selectedMarket = markets.find(market => market.symbol === draft.market)
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
    setAnalysisFillError('')
    setAnalysisSyncedAt('')
  }

  const overviewCards = [
    { label: 'BALANCE', value: account?.wallet_balance === undefined ? '--' : `$${fmtCompact(account.wallet_balance)}`, note: account ? 'Demo/Testnet account' : 'DATA UNAVAILABLE', tone: 'default' },
    { label: 'AVAILABLE', value: account?.available_balance === undefined ? '--' : `$${fmtCompact(account.available_balance)}`, note: account ? 'Current margin' : 'DATA UNAVAILABLE', tone: 'default' },
    { label: 'UNREALIZED PNL', value: account?.unrealized_pnl === undefined ? '--' : `${account.unrealized_pnl >= 0 ? '+' : ''}$${fmtCompact(account.unrealized_pnl)}`, note: account ? 'Account snapshot' : 'DATA UNAVAILABLE', tone: account?.unrealized_pnl && account.unrealized_pnl >= 0 ? 'positive' : 'default' },
    { label: 'REALIZED PNL', value: performanceSnapshot ? `${performanceSnapshot.net_profit >= 0 ? '+' : ''}$${fmtCompact(performanceSnapshot.net_profit)}` : '--', note: performanceSnapshot ? 'Verified Demo history' : 'NO TRADE HISTORY', tone: 'default' },
    { label: 'MARGIN USED', value: account?.wallet_balance !== undefined && account.available_balance !== undefined ? `$${fmtCompact(account.wallet_balance - account.available_balance)}` : '--', note: account ? 'Derived from account' : 'DATA UNAVAILABLE', tone: 'default' },
    { label: 'OPEN POSITIONS', value: account?.positions ? String(account.positions.length) : '--', note: account ? 'Demo/Testnet account' : 'DATA UNAVAILABLE', tone: 'default' },
  ]

  const performanceTrend: number[] = []
  const openPositions = account?.positions ?? []
  const openRiskValues = openPositions.map(position => {
    const plan = account?.plans?.find(item => item.symbol === position.symbol)
    const stopLoss = position.stop_loss ?? (plan?.stop_loss ? Number(plan.stop_loss) : undefined)
    return position.entry_price && stopLoss && position.quantity ? Math.abs(position.entry_price - stopLoss) * position.quantity : null
  }).filter((value): value is number => value !== null)
  const openRisk = openRiskValues.length ? openRiskValues.reduce((sum, value) => sum + value, 0) : null
  const usedMargin = account?.wallet_balance !== undefined && account.available_balance !== undefined ? account.wallet_balance - account.available_balance : null
  const latestUpdate = snapshot?.marketUpdatedAt ? new Date(snapshot.marketUpdatedAt).toLocaleTimeString('en-GB') : '--'
  const marketAgeSeconds = snapshot?.marketUpdatedAt ? Math.max(0, (Date.now() - new Date(snapshot.marketUpdatedAt).getTime()) / 1000) : null
  const persistentTradeHistoryText = 'localStorage trade history persistence enabled; live trading remains locked.'
  const priceAgeSeconds = snapshot?.priceUpdatedAt ? Math.max(0, (Date.now() - new Date(snapshot.priceUpdatedAt).getTime()) / 1000) : null
  const dataHealth = !snapshot ? 'NO DATA' : snapshot.marketError ? 'ERROR' : priceAgeSeconds !== null && priceAgeSeconds <= 8 ? 'LIVE' : 'STALE'
  const riskMetrics = [
    { label: 'Daily PnL', value: dailyPerformance ? `${dailyPerformance.net_profit >= 0 ? '+' : ''}$${fmtCompact(dailyPerformance.net_profit)}` : '--', tone: 'muted' },
    { label: 'Daily Risk', value: '--', tone: 'muted' },
    { label: 'Limit', value: account?.limits?.max_open_positions === undefined ? '--' : `${account.limits.max_open_positions} positions`, tone: 'muted' },
    { label: 'Open Risk', value: openRisk === null ? '--' : `$${fmtCompact(openRisk)}`, tone: 'warning' },
    { label: 'Margin Used', value: usedMargin === null ? '--' : `$${fmtCompact(usedMargin)}`, tone: 'muted' },
    { label: 'Loss Streak', value: dailyPerformance ? String(dailyPerformance.losing_streak) : '--', tone: 'muted' },
  ]

  return (
    <section className="masterTradePage masterTrade">
      <div className="masterTradeShell">
        <header className="masterTradeTopbar">
          <div className="masterTradeTopbarLeft">
            {onBack && (
              <button type="button" className="masterTradeBackButton" onClick={onBack}>← Dashboard</button>
            )}
            <div>
              <span className="masterTradeEyebrow">MASTER TRADE V2</span>
              <h2>Professional execution terminal</h2>
            </div>
          </div>

          <div className="masterTradeStatusRow">
            <span className="statusPill online"><Activity /> CONNECTED</span>
            <span className="statusPill demo"><Wallet /> DEMO / TESTNET</span>
          </div>
        </header>

        <div className="terminalStatusStrip" aria-label="Master Trade connection status">
          <span className="terminalStatusItem online"><i /> CONNECTED</span>
          <span className="terminalStatusItem"><small>WS</small> --</span>
          <span className="terminalStatusItem"><small>LATENCY</small> --</span>
          <span className="terminalStatusItem"><small>LAST UPDATE</small> {latestUpdate}</span>
          <span className={`terminalStatusItem dataHealth-${dataHealth.toLowerCase().replaceAll(' ', '-')}`}><small>DATA HEALTH</small> {dataHealth}{marketAgeSeconds !== null ? ` · ${Math.floor(marketAgeSeconds)}s ago` : ''}</span>
          <span className="terminalStatusItem demo">DEMO / TESTNET</span>
        </div>

        <div className="masterTradeOverview">
          {overviewCards.map((card) => (
            <article key={card.label} className={`metricCard ${card.tone}`}>
              <small>{card.label}</small>
              <strong>{card.value}</strong>
              <span>{card.note}</span>
            </article>
          ))}
        </div>

        <div className="masterTradeWorkspace">
          <div className="masterTradeSafetyBanner" role="status">
            <strong>LIVE TRADING LOCKED</strong>
            <span>DEMO ACCOUNT SNAPSHOT · PERSISTENT HISTORY · RECOVERY READY</span>
            <em>{persistentTradeHistoryText}</em>
          </div>
          <aside className="masterTradePanel watchlistPanel">
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">MARKET WATCH</span>
                <h3>Live Markets</h3>
              </div>
              <span className="marketCount">{markets.length || '--'}</span>
            </div>

            <label className="marketSearch"><span>SEARCH MARKETS</span><input aria-label="Search markets" placeholder="BTC, ETH, SOL..." value={marketQuery} onChange={event => setMarketQuery(event.target.value)} /><button type="button" aria-label="Clear market search" onClick={() => setMarketQuery('')} disabled={!marketQuery}>×</button></label>
            <div className="watchlistList">
              {marketLoading ? <div className="marketEmpty">Loading markets...</div> : marketError ? <div className="marketEmpty error">{marketError}</div> : filteredMarkets.length === 0 ? <div className="marketEmpty"><strong>No markets found</strong><span>Try another symbol or market name.</span></div> : filteredMarkets.map(item => (
                <button key={item.symbol} type="button" className={item.symbol === draft.market ? 'watchlistItem active' : 'watchlistItem'} onClick={() => selectMarket(item.symbol)}>
                  <div className="watchlistMeta">
                    <b>{item.symbol}</b>
                    <span>{item.change >= 0 ? '+' : ''}{item.change.toFixed(2)}%</span>
                  </div>
                  <div className="watchlistStats">
                    <strong>${item.price.toLocaleString('en-US', { maximumFractionDigits: 6 })}</strong>
                    <em className={item.change >= 0 ? 'positive' : 'negative'}>{item.change >= 0 ? 'LONG' : 'SHORT'}</em>
                  </div>
                </button>
              ))}
            </div>
          </aside>

          <main className="masterTradePanel chartPanel">
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">MARKET</span>
                <h3>{draft.market}</h3>
              </div>
            </div>

            <div className="chartPriceSummary">
              <div>
                <span className="chartSymbol">{draft.market}</span>
                <strong>{snapshot?.currentPrice !== null && snapshot?.currentPrice !== undefined ? `$${fmtMarketPrice(snapshot.currentPrice)}` : '--'}</strong>
              </div>
              <span className={`delta ${selectedMarket && selectedMarket.change >= 0 ? 'positive' : 'negative'}`}>{selectedMarket ? `${selectedMarket.change >= 0 ? '+' : ''}${selectedMarket.change.toFixed(2)}%` : '--'}</span>
            </div>

            <div className="marketStatsStrip">
              <span><small>24H CHANGE</small><b className={selectedMarket && selectedMarket.change < 0 ? 'negative' : 'positive'}>{selectedMarket ? `${selectedMarket.change >= 0 ? '+' : ''}${selectedMarket.change.toFixed(2)}%` : '--'}</b></span>
              <span><small>VOLUME</small><b>{selectedMarket?.volume ? fmtCompact(selectedMarket.volume) : '--'}</b></span>
              <span><small>HIGH</small><b>{chartCandles.length ? fmtMarketPrice(Math.max(...chartCandles.map(candle => candle.high))) : '--'}</b></span>
              <span><small>LOW</small><b>{chartCandles.length ? fmtMarketPrice(Math.min(...chartCandles.map(candle => candle.low))) : '--'}</b></span>
              <span><small>DATA HEALTH</small><b className={dataHealth === 'LIVE' ? 'positive' : dataHealth === 'STALE' ? 'warning' : 'negative'}>{dataHealth}</b></span>
            </div>

            <div className="chartToolbar" aria-label="Market chart controls">
              <div className="chartControls">{['1m','5m','15m','1h','4h','1d'].map((range) => <button key={range} type="button" className={range === interval ? 'active' : ''} onClick={() => setInterval(range)}>{range.toUpperCase()}</button>)}</div>
              <div className="chartViewControls"><button type="button" className={showChartLevels ? 'active' : ''} onClick={() => setShowChartLevels(value => !value)}>LEVELS</button><button type="button" className={showChartVolume ? 'active' : ''} onClick={() => setShowChartVolume(value => !value)}>VOLUME</button><span>{hoveredCandle ? new Date(hoveredCandle.time * (hoveredCandle.time < 1_000_000_000_000 ? 1000 : 1)).toLocaleString('en-GB') : 'OHLC / REAL MARKET DATA'}</span></div>
            </div>

            <div className="chartCanvas marketOhlcChart">
              {dataLoading && !chartCandles.length ? <div className="chartEmpty">LOADING SNAPSHOT</div> : !chartCandles.length ? <div className="chartEmpty">{dataError || 'DATA UNAVAILABLE'}</div> : <svg viewBox="0 0 760 300" preserveAspectRatio="none" aria-label={`${draft.market} ${interval} candlestick chart`} onMouseLeave={() => setChartHoverIndex(null)} onMouseMove={event => { const box = event.currentTarget.getBoundingClientRect(); const index = Math.round(((event.clientX - box.left) / box.width) * (chartCandles.length - 1)); setChartHoverIndex(Math.max(0, Math.min(chartCandles.length - 1, index))) }}>
                <g className="chartGrid">{[...Array(7)].map((_, index) => <line key={`h-${index}`} x1="0" x2="760" y1={22 + index * 35} y2={22 + index * 35} />)}{[...Array(9)].map((_, index) => <line key={`v-${index}`} x1={index * 95} x2={index * 95} y1="0" y2="260" />)}</g>
                {showChartLevels && positionedLevelLines.map(line => <g key={`${line.label}-${line.value}`} className={`chartLevel level-${line.tone}`}><line x1="0" x2="760" y1={chartY(line.value)} y2={chartY(line.value)} strokeDasharray={line.tone === 'trigger' ? '5 4' : '2 3'} /><rect x="668" y={line.labelY - 11} width="88" height="14" rx="2" /><text x="752" y={line.labelY - 1} textAnchor="end">{line.label} {fmtMarketPrice(line.value)}</text></g>)}
                {chartCandles.map((candle, index) => { const x = chartX(index); const bodyTop = chartY(Math.max(candle.open, candle.close)); const bodyBottom = chartY(Math.min(candle.open, candle.close)); const bodyHeight = Math.max(2, bodyBottom - bodyTop); const bullish = candle.close >= candle.open; const candleWidth = Math.max(2, Math.min(10, 700 / chartCandles.length)); return <g key={`${candle.time}-${index}`} className={bullish ? 'candle bullish' : 'candle bearish'}><line x1={x} x2={x} y1={chartY(candle.high)} y2={chartY(candle.low)} /><rect x={x - candleWidth / 2} y={bodyTop} width={candleWidth} height={bodyHeight} /></g> })}
                {showChartVolume && chartCandles.map((candle, index) => { const x = chartX(index); const height = candle.volume / volumeMax * 28; return <rect key={`vol-${candle.time}`} className={`chartVolume ${candle.close >= candle.open ? 'up' : 'down'}`} x={x - 2} y={273 - height} width="4" height={height} /> })}
                {snapshot?.currentPrice !== null && snapshot?.currentPrice !== undefined && <g className="currentPriceLine"><line x1="0" x2="760" y1={chartY(snapshot.currentPrice)} y2={chartY(snapshot.currentPrice)} /><rect x="674" y={chartY(snapshot.currentPrice) - 11} width="82" height="14" rx="2" /><text x="752" y={chartY(snapshot.currentPrice) - 1} textAnchor="end">{fmtMarketPrice(snapshot.currentPrice)}</text></g>}
                {chartHoverIndex !== null && <g className="chartCrosshair"><line x1={chartX(chartHoverIndex)} x2={chartX(chartHoverIndex)} y1="0" y2="260" /><circle cx={chartX(chartHoverIndex)} cy={chartY(chartCandles[chartHoverIndex].close)} r="3" /></g>}
              </svg>}
              <div className="chartBadge">{snapshot?.currentPrice !== null && snapshot?.currentPrice !== undefined ? `$${fmtMarketPrice(snapshot.currentPrice)}` : '--'}</div>
            </div>

            <div className="indicatorGrid">
              <article><small>TREND</small><strong className="positive">{analysis?.trend || '--'}</strong></article>
              <article><small>MOMENTUM</small><strong>{analysis?.momentum || '--'}</strong></article>
              <article><small>RSI</small><strong>{fmtDecisionNumber(analysis?.rsi, 2)}</strong></article>
              <article><small>MACD</small><strong className={analysis?.macd && analysis.macd >= 0 ? 'positive' : 'negative'}>{fmtSigned(analysis?.macd)}</strong></article>
              <article><small>VOLUME</small><strong>{analysis?.volume_ratio ? `${fmtDecisionNumber(analysis.volume_ratio, 2)}x` : '--'}</strong></article>
              <article><small>CONFIDENCE</small><strong className="positive">{tradeDecision.confidenceScore === null ? '--' : `${tradeDecision.confidenceScore}%`}</strong></article>
              <article><small>OPPORTUNITY</small><strong className="decisionAccent">{fmtDecisionNumber(tradeDecision.opportunityScore)}</strong></article>
            </div>

            <div className="indicatorTerminalGrid">
              <article><header><span>VOLUME</span><b>{analysis?.volume_ratio ? `${fmtDecisionNumber(analysis.volume_ratio, 2)}x` : '--'}</b></header><div className="volumeMeter">{chartCandles.slice(-24).map((candle, index) => <i key={`${candle.time}-${index}`} style={{ height: `${Math.max(8, candle.volume / volumeMax * 100)}%` }} className={candle.close >= candle.open ? 'up' : 'down'} />)}</div></article>
              <article><header><span>RSI</span><b>{fmtDecisionNumber(analysis?.rsi, 2)}</b></header><div className="indicatorScale"><i /><em>30</em><em>50</em><em>70</em></div><small>{analysis?.rsi === undefined ? 'DATA UNAVAILABLE' : analysis.rsi >= 70 ? 'OVERBOUGHT' : analysis.rsi <= 30 ? 'OVERSOLD' : 'HEALTHY RANGE'}</small></article>
              <article><header><span>MACD</span><b>{fmtSigned(analysis?.macd)}</b></header><div className={`macdPulse ${analysis?.macd && analysis.macd >= 0 ? 'positive' : 'negative'}`} /><small>{analysis?.macd === undefined ? 'DATA UNAVAILABLE' : analysis.macd >= 0 ? 'BULLISH' : 'BEARISH'}</small></article>
            </div>
            <div className="scoreMeters"><div><span>CONFIDENCE</span><b>{tradeDecision.confidenceScore === null ? '--' : `${tradeDecision.confidenceScore}%`}</b><i><em style={{ width: `${tradeDecision.confidenceScore ?? 0}%` }} /></i></div><div><span>OPPORTUNITY</span><b>{fmtDecisionNumber(tradeDecision.opportunityScore)}</b><i><em style={{ width: `${tradeDecision.opportunityScore ?? 0}%` }} /></i></div></div>

            <section className={`tradeDecisionPanel decision-${tradeDecision.status.toLowerCase().replaceAll(' ', '-')}`} aria-label="Trade decision analysis">
              <header className="tradeDecisionHeader">
                <div><span className="panelEyebrow">FINAL DECISION</span><h3>{tradeDecision.status}</h3><small>{draft.market} · {interval} · {tradeDecision.marketRegime}</small></div>
                <div className="decisionScore"><strong>{fmtDecisionNumber(tradeDecision.opportunityScore)}</strong><span>/ 100<br />OPPORTUNITY</span></div>
              </header>
              <div className="decisionMetricGrid">
                <div><small>CONFIDENCE</small><strong>{tradeDecision.confidenceScore === null ? '--' : `${tradeDecision.confidenceScore}%`}</strong></div>
                <div><small>DIRECTION</small><strong>{tradeDecision.direction}</strong></div>
                <div><small>SIGNAL STRENGTH</small><strong>{tradeDecision.signalStrength || '--'}</strong></div>
                <div><small>ENTRY QUALITY</small><strong>{tradeDecision.entryQuality || '--'}</strong></div>
                <div><small>RISK / REWARD</small><strong>{tradeDecision.riskReward === null ? '--' : `1 : ${fmtDecisionNumber(tradeDecision.riskReward, 2)}`}</strong></div>
                <div><small>SIGNAL</small><strong>{tradeDecision.freshness || '--'}</strong><em>Age {fmtSignalAge(tradeDecision.signalAgeSeconds)}</em></div>
              </div>

              <div className="decisionSectionGrid">
                <div className="decisionList"><h4>WHY THIS DECISION</h4>{tradeDecision.reasons.length ? <ul>{tradeDecision.reasons.map(reason => <li key={reason}>+ {reason}</li>)}</ul> : <p>DATA UNAVAILABLE</p>}</div>
                <div className="decisionList"><h4>RISK FLAGS</h4>{tradeDecision.riskFlags.length ? <ul className="riskList">{tradeDecision.riskFlags.map(flag => <li key={flag}>! {flag}</li>)}</ul> : <p className="positive">NO MAJOR RISK FLAGS</p>}</div>
              </div>

              {(tradeDecision.status === 'WAIT' || tradeDecision.status === 'WATCH' || tradeDecision.status === 'NO TRADE') && <div className="decisionSectionGrid decisionWaitGrid">
                <div className="decisionList"><h4>WHY WAIT?</h4><ul>{tradeDecision.whyWait.length ? tradeDecision.whyWait.map(reason => <li key={reason}>- {reason}</li>) : <li>Confirmation still required</li>}</ul></div>
                <div className="decisionList"><h4>WHAT WE ARE WAITING FOR</h4><ul>{tradeDecision.waitingFor.length ? tradeDecision.waitingFor.map(reason => <li key={reason}>✓ {reason}</li>) : <li>Fresh market confirmation</li>}</ul><p>Trigger: --</p></div>
              </div>}

              <div className="decisionSectionGrid">
                <div className="decisionList"><h4>LONG CASE</h4><ul>{tradeDecision.longCase.map(item => <li key={item}>{item}</li>)}</ul></div>
                <div className="decisionList"><h4>SHORT CASE</h4><ul>{tradeDecision.shortCase.map(item => <li key={item}>{item}</li>)}</ul></div>
              </div>

              <div className="decisionBreakdown"><h4>WHY THIS SCORE?</h4>{tradeDecision.breakdown ? <div className="decisionBreakdownGrid">{[['Analysis', tradeDecision.breakdown.analysis], ['Liquidity', tradeDecision.breakdown.liquidity], ['Volatility', tradeDecision.breakdown.volatility], ['MTF', tradeDecision.breakdown.mtf], ['Freshness', tradeDecision.breakdown.freshness], ['Risk/Reward', tradeDecision.breakdown.riskReward]].map(([label, value]) => <span key={label}><small>{label}</small><strong>{fmtDecisionNumber(value)}</strong></span>)}</div> : <p>DATA UNAVAILABLE</p>}</div>

              <div className="decisionMtf"><div className="decisionSubheading"><h4>MULTI-TIMEFRAME MATRIX</h4><strong>{tradeDecision.mtfScore === null ? 'MTF BIAS: --' : `MTF CONFIRMATION: ${tradeDecision.mtfConfirmed} / ${tradeDecision.mtfTotal}`}</strong></div><div className="decisionMtfGrid">{tradeDecision.mtfRows.length ? tradeDecision.mtfRows.map(row => <span key={row.timeframe}><b>{row.timeframe}</b><em>{row.available ? row.direction : '--'}</em><small>{row.trend || '--'}</small></span>) : <span>DATA UNAVAILABLE</span>}</div></div>
              <div className="decisionAutoTrade"><span>AUTO TRADE</span><strong>SEPARATE SAFETY GATES</strong><small>Final Decision does not send orders or grant Auto Trade eligibility.</small></div>

              <section className={`triggerMonitor trigger-${triggerMonitor.lifecycle.toLowerCase()}`} aria-label="Trigger monitor">
                <header className="triggerHeader"><div><h4>TRIGGER MONITOR</h4><strong>{triggerMonitor.lifecycle}</strong><small>{triggerMonitor.statusMessage}</small></div><span>{triggerMonitor.available ? 'LIVE SNAPSHOT' : 'DATA UNAVAILABLE'}</span></header>
                <div className="triggerSummary"><div><small>CURRENT</small><strong>{triggerMonitor.currentPrice === null ? '--' : `$${fmtDecisionNumber(triggerMonitor.currentPrice, 6)}`}</strong></div><div><small>{triggerMonitor.direction === 'SHORT' ? 'SHORT TRIGGER BELOW' : 'LONG TRIGGER ABOVE'}</small><strong>{triggerMonitor.triggerPrice === null ? '--' : `$${fmtDecisionNumber(triggerMonitor.triggerPrice, 6)}`}</strong></div><div><small>DISTANCE</small><strong>{triggerMonitor.distancePct === null ? '--' : `${triggerMonitor.distancePct.toFixed(2)}%`}</strong><em>{triggerMonitor.waitingMessage}</em></div></div>
                <div className="triggerConditions"><div className="decisionSubheading"><h4>TRIGGER CONDITIONS</h4><strong>{triggerMonitor.remainingConditions === null ? '--' : `${triggerMonitor.remainingConditions} CONDITIONS REMAINING`}</strong></div>{triggerMonitor.conditions.length ? <div className="triggerConditionGrid">{triggerMonitor.conditions.map(condition => <span key={condition.key} className={!condition.available ? 'unavailable' : condition.passed ? 'passed' : 'pending'}><b>{condition.passed ? '✓' : condition.available ? '✕' : '--'}</b><small>{condition.label}</small><em>{condition.detail}</em></span>)}</div> : <p className="triggerUnavailable">NO TRADE — waiting for a complete market snapshot.</p>}</div>
                <div className="triggerDetailGrid"><div className="decisionList"><h4>INVALIDATION</h4><ul>{triggerMonitor.invalidation.map(item => <li key={item}>- {item}</li>)}</ul></div><div className="decisionList"><h4>DECISION TIMELINE</h4>{decisionTimeline.length ? <ul>{decisionTimeline.map(event => <li key={`${event.time}-${event.message}`}>{new Date(event.time).toLocaleTimeString('en-GB')} · {event.message}</li>)}</ul> : <p>NO RECENT ACTIVITY</p>}</div></div>
                {triggerMonitor.entryPreview && <div className="triggerPreview"><h4>ENTRY PREVIEW</h4><div className="triggerPreviewGrid"><span><small>ENTRY</small><strong>{fmtDecisionNumber(triggerMonitor.entryPreview.entry, 6)}</strong></span><span><small>SL</small><strong>{fmtDecisionNumber(triggerMonitor.entryPreview.stopLoss, 6)}</strong></span><span><small>TP1 · 30%</small><strong>{fmtDecisionNumber(triggerMonitor.entryPreview.tp1, 6)}</strong></span><span><small>TP2 · 30%</small><strong>{fmtDecisionNumber(triggerMonitor.entryPreview.tp2, 6)}</strong></span><span><small>TP3 · 40%</small><strong>{fmtDecisionNumber(triggerMonitor.entryPreview.tp3, 6)}</strong></span><span><small>R / R</small><strong>1 : {fmtDecisionNumber(triggerMonitor.entryPreview.riskReward, 2)}</strong></span></div></div>}
                <div className="preTradeCheck"><div className="decisionSubheading"><h4>PRE-TRADE CHECK · READ ONLY</h4><strong>NO ORDER SENT</strong></div><div className="preTradeCheckGrid">{triggerMonitor.preTradeChecks.map(check => <span key={check.label} className={`check-${check.status.toLowerCase()}`}><b>{check.status}</b><small>{check.label}</small><em>{check.detail}</em></span>)}</div></div>
              </section>
            </section>
          </main>

          <aside className="masterTradePanel orderPanel">
            <div className="panelHeader orderHeader">
              <div className="tradeTerminalHeader">
                <span className="panelEyebrow">TRADE TERMINAL</span>
                <div className="tradeSymbolRow">
                  <strong>{draft.market}</strong>
                  <span className="demoBadge">DEMO</span>
                </div>
              </div>
            </div>

            <div className="tradeTerminalStack">
              <div className="tradeSegmentGroup">
                <button type="button" className={draft.side === 'LONG' ? 'selected long' : ''} onClick={() => setDraft((current) => ({ ...current, side: 'LONG' }))}>LONG</button>
                <button type="button" className={draft.side === 'SHORT' ? 'selected short' : ''} onClick={() => setDraft((current) => ({ ...current, side: 'SHORT' }))}>SHORT</button>
              </div>

              <div className="tradeSegmentGroup compact">
                <button type="button" className="active">MARKET</button>
                <button type="button">LIMIT</button>
              </div>

              <div className="tradeSection">
                <div className="tradeSectionLabel">ORDER PARAMETERS</div>
                <div className="fieldGrid compactFields">
                  <label><span>Margin</span><input value={draft.margin} onChange={(event) => setDraft((current) => ({ ...current, margin: Number(event.target.value) || 0 }))} /></label>
                  <label><span>Leverage</span><input value={draft.leverage} onChange={(event) => setDraft((current) => ({ ...current, leverage: Number(event.target.value) || 1 }))} /></label>
                  <label><span>Quantity</span><input value={draft.quantity} onChange={(event) => setDraft((current) => ({ ...current, quantity: Number(event.target.value) || 0 }))} /></label>
                  <label><span>Entry Price</span><input value={draft.entry} onChange={(event) => setDraft((current) => ({ ...current, entry: Number(event.target.value) || 0 }))} /></label>
                </div>
              </div>

              <div className="tradeSection">
                <div className="tradeSectionLabel">ORDER PREVIEW</div>
                <div className="orderSummaryList">
                  <div className="orderSummaryRow"><span>Entry</span><strong>{draft.entry > 0 ? `$${fmtMarketPrice(Number(draft.entry))}` : '--'}</strong></div>
                  <div className="orderSummaryRow"><span>Quantity</span><strong>{draft.quantity > 0 ? `${fmtCompact(Number(draft.quantity))}` : '--'}</strong></div>
                  <div className="orderSummaryRow"><span>Position</span><strong>{draft.entry > 0 && draft.quantity > 0 ? `${fmtMarketPrice((Number(draft.entry) * Number(draft.quantity)))} USDT` : '--'}</strong></div>
                  <div className="orderSummaryRow"><span>Risk</span><strong>${fmtCompact(riskPreview.riskUsd)}</strong></div>
                  <div className="orderSummaryRow"><span>Risk %</span><strong>{draft.entry > 0 ? `${riskPreview.riskPercent.toFixed(2)}%` : '--'}</strong></div>
                  <div className="orderSummaryRow"><span>R/R</span><strong>{riskPreview.rr > 0 ? `1:${riskPreview.rr.toFixed(2)}` : '—'}</strong></div>
                  <div className="orderSummaryRow"><span>Fees</span><strong>--</strong></div>
                </div>
              </div>

              <div className="tradeSection">
                <div className="tradeSectionLabel">STOP LOSS</div>
                <div className="slRow">
                  <span>SL</span>
                  <strong>{draft.stopLoss > 0 ? `$${fmtCompact(draft.stopLoss)}` : '--'}</strong>
                </div>
                <div className="orderSummaryList microList">
                  <div className="orderSummaryRow"><span>Risk</span><strong>{draft.entry > 0 ? `${riskPreview.riskPercent.toFixed(2)}%` : '--'}</strong></div>
                  <div className="orderSummaryRow"><span>Value</span><strong>${fmtCompact(riskPreview.riskUsd)}</strong></div>
                </div>
              </div>

              <div className="tradeSection">
                <div className="tradeSectionLabel">TAKE PROFIT</div>
                <div className="tpGroup compactTpGroup">
                  <div><span>TP1</span><strong>{draft.tp1 > 0 ? `$${fmtMarketPrice(Number(draft.tp1))}` : '--'}</strong></div>
                  <div><span>TP2</span><strong>{draft.tp2 > 0 ? `$${fmtMarketPrice(Number(draft.tp2))}` : '--'}</strong></div>
                  <div><span>TP3</span><strong>{draft.tp3 > 0 ? `$${fmtMarketPrice(Number(draft.tp3))}` : '--'}</strong></div>
                </div>
              </div>

              <div className="tradeActions">
                <button type="button" className="analysisFillButton" onClick={() => void fillFromCurrentAnalysis()} disabled={analysisFillLoading || dataLoading}>
                  {analysisFillLoading ? 'ANALİZ YÜKLENİYOR...' : 'GÜNCEL ANALİZDEN DOLDUR'}
                </button>
                {analysisSyncedAt && <div className="analysisSyncStatus">Analysis synced · {analysisSyncedAt}</div>}
                {analysisFillError && <div className="analysisFillError" role="status">{analysisFillError}</div>}
                <button type="button" className="primaryOrderButton" onClick={() => { const validationError = validateOrderDraft(); setDemoOrderError(validationError); setDemoConfirmationChecked(false); setDemoConfirmationOpen(true) }} disabled={demoOrderBusy}>DEMO ORDER</button>
                <section className="masterTradeLiveControls" aria-label="Master Trade live trading">
                  <div><KeyRound /><span><b>LIVE TRADING</b><small>{liveVault?.connections.LIVE.configured ? `${liveVault.connections.LIVE.storage} · ${liveVault.connections.LIVE.last_test_ok ? 'VERIFIED' : 'VERIFY REQUIRED'}` : 'API credentials gerekli'}</small></span></div>
                  <label><span>Live API Key</span><input type="password" autoComplete="new-password" value={liveCredentials.apiKey} onChange={event => setLiveCredentials(current => ({ ...current, apiKey: event.target.value }))} placeholder={liveVault?.connections.LIVE.configured ? 'Kayıtlı anahtar mevcut' : 'Live API Key'} /></label>
                  <label><span>Live API Secret</span><input type="password" autoComplete="new-password" value={liveCredentials.secretKey} onChange={event => setLiveCredentials(current => ({ ...current, secretKey: event.target.value }))} placeholder={liveVault?.connections.LIVE.configured ? 'Kayıtlı secret görüntülenmez' : 'Live API Secret'} /></label>
                  <div className="masterTradeLiveActions"><button type="button" onClick={() => void verifyLiveCredentials()} disabled={!!liveBusy || !liveVault?.vault.ready}><Activity /> VERIFY LIVE CONNECTION</button><button type="button" onClick={() => void saveLiveCredentials()} disabled={!!liveBusy || !liveVault?.vault.ready}><Save /> SAVE SECURELY</button><button type="button" onClick={() => void submitLiveOrder()} disabled={!!liveBusy || !liveVault?.connections.LIVE.configured || !liveVault.connections.LIVE.active || !liveVault.connections.LIVE.last_test_ok}><Send /> LIVE ORDER</button></div>
                  {liveNotice && <small className="masterTradeLiveNotice" role="status">{liveNotice}</small>}
                </section>
              </div>
            </div>
          </aside>
        </div>

        <div className="masterTradeDataGrid">
          <section className="masterTradePanel riskMonitorPanel">
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

          <section className="masterTradePanel positionsPanel widePanel">
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">OPEN POSITIONS</span>
                <h3>Portfolio</h3>
              </div>
              <button type="button" className="panelGhostButton">25% · 50% · 75% · 100%</button>
            </div>

            <div className="tableWrap">
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
                    const stopLoss = position.stop_loss ?? (plan?.stop_loss ? Number(plan.stop_loss) : undefined)
                    const target = plan?.targets?.[0] ? Number(plan.targets[0]) : position.tp1
                    return (
                    <tr key={position.symbol}>
                      <td><strong>{position.symbol}</strong><span className="positionAge">{position.age || '--'}</span></td>
                      <td><span className={position.direction === 'LONG' ? 'positive' : 'negative'}>{position.direction || '--'}</span></td>
                      <td>{fmtNum(position.quantity, 2)}</td>
                      <td>${fmtNum(position.entry_price)}</td>
                      <td>${fmtNum(position.mark_price)}</td>
                      <td>{position.leverage ? `${position.leverage}x` : '--'}</td>
                      <td>{plan?.margin_usdt === undefined ? '--' : `$${fmtCompact(plan.margin_usdt)}`}</td>
                      <td className={(position.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{position.unrealized_pnl === undefined ? '--' : `${position.unrealized_pnl >= 0 ? '+' : ''}$${fmtCompact(position.unrealized_pnl)}`}</td>
                      <td>--</td>
                      <td>${fmtNum(stopLoss)}</td>
                      <td>${fmtNum(target)}</td>
                      <td><span className="statusBadge open">OPEN</span></td>
                      <td><div className="positionActions"><button type="button" onClick={() => { setPositionActionError(''); setPositionAction({ mode: 'DETAILS', position }) }}>DETAILS</button><button type="button" onClick={() => { setPositionActionError(''); setReduceQuantity(String((position.quantity || 0) * 0.25)); setPositionAction({ mode: 'REDUCE', position }) }}>REDUCE</button><button type="button" className="dangerAction" onClick={() => { setPositionActionError(''); setPositionAction({ mode: 'CLOSE', position }) }}>CLOSE</button></div></td>
                    </tr>
                    )
                  }) : <tr><td colSpan={13} className="emptyState">{snapshot?.accountError || 'NO OPEN POSITIONS'}</td></tr>}
                </tbody>
              </table>
            </div>
          </section>

          <section className="masterTradePanel ordersPanel compactPanel">
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
                          <td>${fmtNum(order.price)}</td>
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

          <section className="masterTradePanel historyPanel widePanel">
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

          <section className="masterTradePanel performancePanel compactPanel">
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

          <section className="masterTradePanel scannerPanel compactPanel">
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">MARKET SCANNER</span>
                <h3>Top opportunities</h3>
              </div>
              <span className="livePill">LIVE</span>
            </div>

            <div className="scannerList"><div className="emptyState">NO CURRENT OPPORTUNITY SNAPSHOT</div></div>
          </section>

          <section className="masterTradePanel activityPanel compactPanel">
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">LIVE ACTIVITY</span>
                <h3>Event feed</h3>
              </div>
              <Activity className="panelHeaderIcon" />
            </div>
            <div className="activityEmpty"><i /><strong>Waiting for live events</strong><span>Market, order and risk events appear here when available.</span></div>
          </section>

          <section className="masterTradePanel safetyPanel compactPanel">
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">ACCOUNT SYNC</span>
                <h3>Recovery status</h3>
              </div>
            </div>

            <div className="systemStatus">
              <div className="statusRow"><i className="onlineDot" /> <span>Connected</span></div>
              <div className="statusRow muted"><span>DEMO ACCOUNT SNAPSHOT</span><strong>{account?.last_checked ? new Date(account.last_checked).toLocaleTimeString('en-GB') : '--'}</strong></div>
              <div className="statusRow muted"><span>RECOVERY</span><strong>{snapshot?.accountError || (account ? 'Current account snapshot' : 'DATA UNAVAILABLE')}</strong></div>
            </div>

            <div className="emergencyActions">
              <button type="button" className="dangerBtn" disabled>STOP AUTO TRADE</button>
              <button type="button" className="dangerBtn" disabled>EMERGENCY CLOSE ALL</button>
            </div>
          </section>
        </div>
      </div>

      {positionAction && (
        <div className="positionActionBackdrop" role="presentation" onClick={() => { if (!positionActionBusy) { setPositionAction(null); setPositionActionError('') } }}>
          <section className="positionActionModal" role="dialog" aria-modal="true" onClick={event => event.stopPropagation()}>
            <header><div><span>DEMO / TESTNET</span><h2>{positionAction.mode === 'DETAILS' ? 'POSITION DETAILS' : positionAction.mode === 'REDUCE' ? 'REDUCE ONLY' : 'CLOSE DEMO POSITION'}</h2></div><button type="button" onClick={() => setPositionAction(null)} disabled={positionActionBusy}>CLOSE</button></header>
            <div className="positionActionGrid">
              <div><small>Symbol</small><b>{positionAction.position.symbol}</b></div>
              <div><small>Direction</small><b>{positionAction.position.direction || '--'}</b></div>
              <div><small>Status</small><b>OPEN</b></div>
              <div><small>Quantity</small><b>{fmtNum(positionAction.position.quantity, 6)}</b></div>
              <div><small>Entry Price</small><b>${fmtNum(positionAction.position.entry_price)}</b></div>
              <div><small>Mark Price</small><b>${fmtNum(positionAction.position.mark_price)}</b></div>
              <div><small>Leverage</small><b>{positionAction.position.leverage ? `${positionAction.position.leverage}x` : '--'}</b></div>
              <div><small>Margin</small><b>--</b></div>
              <div><small>Liquidation</small><b>${fmtNum(positionAction.position.liquidation_price)}</b></div>
              <div><small>Unrealized PnL</small><b className={(positionAction.position.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{positionAction.position.unrealized_pnl === undefined ? '--' : `${positionAction.position.unrealized_pnl >= 0 ? '+' : ''}$${fmtCompact(positionAction.position.unrealized_pnl)}`}</b></div>
              <div><small>PnL %</small><b>--</b></div>
              <div><small>Stop Loss</small><b>${fmtNum(positionAction.position.stop_loss)}</b></div>
              <div><small>TP1</small><b>${fmtNum(positionAction.position.tp1)}</b></div>
              <div><small>TP2</small><b>--</b></div>
              <div><small>TP3</small><b>--</b></div>
            </div>
            {positionAction.mode === 'REDUCE' && <div className="reduceControls"><label>Quantity<input type="number" min="0" max={positionAction.position.quantity || undefined} step="any" value={reduceQuantity} onChange={event => setReduceQuantity(event.target.value)} /></label><div><button type="button" onClick={() => setReduceQuantity(String((positionAction.position.quantity || 0) * 0.25))}>25%</button><button type="button" onClick={() => setReduceQuantity(String((positionAction.position.quantity || 0) * 0.5))}>50%</button><button type="button" onClick={() => setReduceQuantity(String((positionAction.position.quantity || 0) * 0.75))}>75%</button><button type="button" onClick={() => setReduceQuantity(String(positionAction.position.quantity || 0))}>100%</button></div></div>}
            {positionAction.mode !== 'DETAILS' && <p className="positionActionWarning">This action will modify the selected Demo/Testnet position using a reduce-only order.</p>}
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

      {demoConfirmationOpen && <div className="masterTradeConfirmationBackdrop" role="presentation" onClick={() => { setDemoConfirmationOpen(false); setDemoConfirmationChecked(false) }}>
        <section className="masterTradeConfirmation" role="dialog" aria-modal="true" aria-labelledby="master-trade-confirmation-title" onClick={event => event.stopPropagation()}>
          <h2 id="master-trade-confirmation-title">Demo order onayı</h2>
          <p>Bu demo order'ı gerçekten göndermek istiyor musun?</p>
          <label><input type="checkbox" checked={demoConfirmationChecked} onChange={event => setDemoConfirmationChecked(event.target.checked)} /><span>Bu işlemi onaylıyorum</span></label>
          {demoOrderError && <div role="alert">{demoOrderError}</div>}
          <div><button type="button" onClick={() => { setDemoConfirmationOpen(false); setDemoConfirmationChecked(false) }}>İPTAL</button><button type="button" disabled={!demoConfirmationChecked || demoOrderBusy} onClick={() => void submitDemoOrder()}>{demoOrderBusy ? 'SUBMITTING DEMO ORDER...' : 'ONAYLA VE GÖNDER'}</button></div>
        </section>
      </div>}
    </section>
  )
}
