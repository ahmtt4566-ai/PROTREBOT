import { useEffect, useMemo, useRef, useState } from 'react'
import { Activity, AlertTriangle, ArrowDownRight, ArrowUpRight, BarChart3, CircleDollarSign, Gauge, Lock, ShieldCheck, TrendingUp, Wallet } from 'lucide-react'
import { API_BASE } from './api'
import { buildTradeDecision, buildTriggerMonitor, type MtfAnalysis, type TradeDecision, type TriggerLifecycle, type TriggerMonitor } from './masterTradeDecision'

type TradeSide = 'LONG' | 'SHORT'
type Source = 'MANUAL' | 'AUTO'

type TradeHistoryRow = {
  id: string
  symbol: string
  side: TradeSide
  entryPrice: number
  exitPrice: number
  quantity: number
  leverage: number
  margin: number
  stopLoss: number
  tp1: number
  tp2: number
  tp3: number
  realizedPnl: number
  pnlPercent: number
  fees: number
  funding: number
  openTime: string
  closeTime: string
  duration: string
  closeReason: string
  source: Source
  analysisScore: number
  opportunityScore: number
  scanCycle: string
}

type MarketRow = { symbol: string; display: string; price: number; change: number; volume: number; status?: string; contractType?: string; quoteAsset?: string; filters?: unknown[] }
type Candle = { time: number; open: number; high: number; low: number; close: number; volume: number }
type Analysis = { direction?: string; confidence?: number; entry?: number; stop_loss?: number; tp1?: number; tp2?: number; tp3?: number; risk_reward?: number; trend?: string; momentum?: string; rsi?: number; macd?: number; adx?: number; atr?: number; support?: number; resistance?: number; radar?: { trap_score?: number; breakout_quality?: number; entry_timing?: string }; volume_ratio?: number; normalized_signal?: string }
type AccountPosition = { symbol: string; direction?: TradeSide; quantity?: number; entry_price?: number; mark_price?: number; unrealized_pnl?: number; leverage?: number | null; stop_loss?: number; tp1?: number; age?: string }
type AccountOrder = { symbol?: string; side?: string; type?: string; price?: number; quantity?: number; status?: string; reduce_only?: boolean }
type AccountSnapshot = { wallet_balance?: number; available_balance?: number; unrealized_pnl?: number; positions?: AccountPosition[]; open_orders?: AccountOrder[]; open_algo_orders?: AccountOrder[]; last_checked?: string | null; connected?: boolean; last_error?: string | null; limits?: { max_open_positions?: number; max_leverage?: number } }
type MasterTradeSnapshot = { symbol: string; timeframe: string; candles: Candle[]; analysis: Analysis | null; mtf: MtfAnalysis[]; account: AccountSnapshot | null; currentPrice: number | null; priceUpdatedAt: string | null; marketUpdatedAt: string | null; accountUpdatedAt: string | null; marketError: string; accountError: string }

const STORAGE_KEY = 'protrebot-master-trade-history-v3'

const fmtNum = (value: number | undefined, decimals = 2) =>
  value === undefined ? '—' : value.toLocaleString('tr-TR', { maximumFractionDigits: decimals, minimumFractionDigits: decimals })

const fmtCompact = (value: number | undefined) =>
  value === undefined ? '—' : value.toLocaleString('tr-TR', { maximumFractionDigits: 2 })

const fmtDecisionNumber = (value: number | null | undefined, decimals = 0) =>
  value === null || value === undefined || !Number.isFinite(value) ? '--' : value.toLocaleString('en-US', { maximumFractionDigits: decimals, minimumFractionDigits: decimals })

const fmtSignalAge = (seconds: number | null) => {
  if (seconds === null) return '--'
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

const MTF_INTERVALS = ['1m', '5m', '15m', '1h', '4h']

const fetchMtfAnalyses = async (symbol: string, signal?: AbortSignal) => {
  const responses = await Promise.all(MTF_INTERVALS.map(timeframe => fetch(`${API_BASE}/analysis/${symbol}?interval=${timeframe}`, { signal })))
  const values = await Promise.all(responses.map(async (response, index) => response.ok ? { ...(await response.json() as Analysis), timeframe: MTF_INTERVALS[index] } : null))
  return values.filter((item): item is MtfAnalysis => item !== null)
}

export default function MasterTrade({ onBack }: { onBack?: () => void }) {
  const [history, setHistory] = useState<TradeHistoryRow[]>(() => {
    if (typeof window === 'undefined') return []
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    try {
      const parsed = JSON.parse(raw) as TradeHistoryRow[]
      return Array.isArray(parsed) ? parsed : []
    } catch {
      return []
    }
  })
  const [selectedTrade, setSelectedTrade] = useState<TradeHistoryRow | null>(null)
  const [markets, setMarkets] = useState<MarketRow[]>([])
  const [marketQuery, setMarketQuery] = useState('')
  const [marketLoading, setMarketLoading] = useState(true)
  const [marketError, setMarketError] = useState('')
  const [interval, setInterval] = useState('15m')
  const [snapshot, setSnapshot] = useState<MasterTradeSnapshot | null>(null)
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
  const [draft, setDraft] = useState({ side: 'LONG' as TradeSide, market: 'BTCUSDT', leverage: 7, margin: 1000, quantity: 0.08, entry: 61350, stopLoss: 60650, tp1: 61850, tp2: 62400, tp3: 63150 })
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

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    const refreshAccount = async () => {
      try {
        const response = await fetch(`${API_BASE}/account`, { signal: controller.signal })
        if (!response.ok) throw new Error('Account data unavailable')
        const nextAccount = await response.json() as AccountSnapshot
        if (!active) return
        accountRef.current = nextAccount
        setSnapshot(current => current ? { ...current, account: nextAccount, accountUpdatedAt: new Date().toISOString(), accountError: '' } : current)
      } catch (error) {
        if (active && !(error instanceof Error && error.name === 'AbortError')) setSnapshot(current => current ? { ...current, accountError: 'ACCOUNT DATA UNAVAILABLE' } : current)
      }
    }
    void refreshAccount()
    const timer = window.setInterval(() => void refreshAccount(), 5000)
    return () => { active = false; controller.abort(); window.clearInterval(timer) }
  }, [])

  useEffect(() => {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(history))
    }
  }, [history])

  const performance = useMemo(() => {
    const total = history.length
    const wins = history.filter((item) => item.realizedPnl > 0).length
    const losses = history.filter((item) => item.realizedPnl < 0).length
    const realized = history.reduce((sum, item) => sum + item.realizedPnl, 0)
    const avgWin = history.filter((item) => item.realizedPnl > 0).reduce((sum, item) => sum + item.realizedPnl, 0) / Math.max(1, wins)
    const avgLoss = Math.abs(history.filter((item) => item.realizedPnl < 0).reduce((sum, item) => sum + item.realizedPnl, 0) / Math.max(1, losses))
    const winRate = total ? (wins / total) * 100 : 0
    const best = history.reduce((best, item) => Math.max(best, item.realizedPnl), 0)
    const worst = history.reduce((worst, item) => Math.min(worst, item.realizedPnl), 0)
    return { total, wins, losses, realized, avgWin, avgLoss, winRate, best, worst }
  }, [history])

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

  const addTradeRecord = () => {
    const next: TradeHistoryRow = {
      id: `MT-${Date.now()}`,
      symbol: draft.market,
      side: draft.side,
      entryPrice: Number(draft.entry),
      exitPrice: Number(draft.tp1) + (draft.side === 'LONG' ? 8 : -8),
      quantity: Number(draft.quantity),
      leverage: Number(draft.leverage),
      margin: Number(draft.margin),
      stopLoss: Number(draft.stopLoss),
      tp1: Number(draft.tp1),
      tp2: Number(draft.tp2),
      tp3: Number(draft.tp3),
      realizedPnl: Number((draft.side === 'LONG' ? Number(draft.tp1) - Number(draft.entry) : Number(draft.entry) - Number(draft.tp1)) * Number(draft.quantity)),
      pnlPercent: Number(((Math.abs(Number(draft.tp1) - Number(draft.entry)) / Number(draft.entry)) * 100).toFixed(2)),
      fees: 1.4,
      funding: 0.1,
      openTime: new Date().toISOString(),
      closeTime: new Date(Date.now() + 3600000).toISOString(),
      duration: '1h 00m',
      closeReason: 'Risk Preview',
      source: 'MANUAL',
      analysisScore: 82,
      opportunityScore: 86,
      scanCycle: 'Now',
    }
    setHistory((current) => [next, ...current].slice(0, 12))
    setSelectedTrade(next)
  }

  const fillFromCurrentAnalysis = async () => {
    setAnalysisFillLoading(true)
    setAnalysisFillError('')
    try {
      const response = await fetch(`${API_BASE}/analysis/${draft.market}?interval=${interval}`)
      if (!response.ok) throw new Error('Analysis unavailable')
      const nextAnalysis = await response.json() as Analysis
      const nextMtf = await fetchMtfAnalyses(draft.market)
      setSnapshot(current => current ? { ...current, analysis: nextAnalysis, mtf: nextMtf, marketUpdatedAt: new Date().toISOString(), marketError: '' } : current)
      setDraft(current => ({
        ...current,
        side: /SHORT/i.test(nextAnalysis.normalized_signal || nextAnalysis.direction || '') ? 'SHORT' : /LONG/i.test(nextAnalysis.normalized_signal || nextAnalysis.direction || '') ? 'LONG' : current.side,
        entry: typeof nextAnalysis.entry === 'number' ? nextAnalysis.entry : current.entry,
        stopLoss: typeof nextAnalysis.stop_loss === 'number' ? nextAnalysis.stop_loss : current.stopLoss,
        tp1: typeof nextAnalysis.tp1 === 'number' ? nextAnalysis.tp1 : current.tp1,
        tp2: typeof nextAnalysis.tp2 === 'number' ? nextAnalysis.tp2 : current.tp2,
        tp3: typeof nextAnalysis.tp3 === 'number' ? nextAnalysis.tp3 : current.tp3,
      }))
      setAnalysisSyncedAt(new Date().toLocaleTimeString('en-GB'))
    } catch {
      setAnalysisFillError('Güncel analiz alınamadı. Lütfen tekrar deneyin.')
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
    { label: 'REALIZED PNL', value: '--', note: 'No real trade history loaded', tone: 'default' },
    { label: 'MARGIN USED', value: account?.wallet_balance !== undefined && account.available_balance !== undefined ? `$${fmtCompact(account.wallet_balance - account.available_balance)}` : '--', note: account ? 'Derived from account' : 'DATA UNAVAILABLE', tone: 'default' },
    { label: 'OPEN POSITIONS', value: account?.positions ? String(account.positions.length) : '--', note: account ? 'Demo/Testnet account' : 'DATA UNAVAILABLE', tone: 'default' },
  ]

  const performanceTrend: number[] = []
  const openPositions = account?.positions ?? []
  const openRiskValues = openPositions.map(position => position.entry_price && position.stop_loss && position.quantity ? Math.abs(position.entry_price - position.stop_loss) * position.quantity : null).filter((value): value is number => value !== null)
  const openRisk = openRiskValues.length ? openRiskValues.reduce((sum, value) => sum + value, 0) : null
  const usedMargin = account?.wallet_balance !== undefined && account.available_balance !== undefined ? account.wallet_balance - account.available_balance : null
  const latestUpdate = snapshot?.marketUpdatedAt ? new Date(snapshot.marketUpdatedAt).toLocaleTimeString('en-GB') : '--'
  const marketAgeSeconds = snapshot?.marketUpdatedAt ? Math.max(0, (Date.now() - new Date(snapshot.marketUpdatedAt).getTime()) / 1000) : null
  const priceAgeSeconds = snapshot?.priceUpdatedAt ? Math.max(0, (Date.now() - new Date(snapshot.priceUpdatedAt).getTime()) / 1000) : null
  const dataHealth = !snapshot ? 'NO DATA' : snapshot.marketError ? 'ERROR' : priceAgeSeconds !== null && priceAgeSeconds <= 8 ? 'LIVE' : 'STALE'
  const riskMetrics = [
    { label: 'Daily PnL', value: '--', tone: 'muted' },
    { label: 'Daily Risk', value: '--', tone: 'muted' },
    { label: 'Limit', value: '--', tone: 'muted' },
    { label: 'Open Risk', value: openRisk === null ? '--' : `$${fmtCompact(openRisk)}`, tone: 'warning' },
    { label: 'Margin Used', value: usedMargin === null ? '--' : `$${fmtCompact(usedMargin)}`, tone: 'muted' },
    { label: 'Loss Streak', value: '--', tone: 'muted' },
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
              <span className="masterTradeEyebrow">MASTER TRADE</span>
              <h2>Professional execution terminal</h2>
            </div>
          </div>

          <div className="masterTradeStatusRow">
            <span className="statusPill online"><Activity /> CONNECTED</span>
            <span className="statusPill demo"><Wallet /> DEMO / TESTNET</span>
            <span className="statusPill locked"><Lock /> LIVE TRADING LOCKED</span>
          </div>
        </header>

        <div className="terminalStatusStrip" aria-label="Master Trade connection status">
          <span className="terminalStatusItem online"><i /> CONNECTED</span>
          <span className="terminalStatusItem"><small>WS</small> --</span>
          <span className="terminalStatusItem"><small>LATENCY</small> --</span>
          <span className="terminalStatusItem"><small>LAST UPDATE</small> {latestUpdate}</span>
          <span className={`terminalStatusItem dataHealth-${dataHealth.toLowerCase().replaceAll(' ', '-')}`}><small>DATA HEALTH</small> {dataHealth}{marketAgeSeconds !== null ? ` · ${Math.floor(marketAgeSeconds)}s ago` : ''}</span>
          <span className="terminalStatusItem demo">DEMO / TESTNET</span>
          <span className="terminalStatusItem locked"><Lock /> LIVE TRADING LOCKED</span>
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
                <strong>{snapshot?.currentPrice !== null && snapshot?.currentPrice !== undefined ? `$${snapshot.currentPrice.toLocaleString('en-US', { maximumFractionDigits: 6 })}` : '--'}</strong>
              </div>
              <span className={`delta ${selectedMarket && selectedMarket.change >= 0 ? 'positive' : 'negative'}`}>{selectedMarket ? `${selectedMarket.change >= 0 ? '+' : ''}${selectedMarket.change.toFixed(2)}%` : '--'}</span>
            </div>

            <div className="marketStatsStrip">
              <span><small>24H CHANGE</small><b className={selectedMarket && selectedMarket.change < 0 ? 'negative' : 'positive'}>{selectedMarket ? `${selectedMarket.change >= 0 ? '+' : ''}${selectedMarket.change.toFixed(2)}%` : '--'}</b></span>
              <span><small>VOLUME</small><b>{selectedMarket?.volume ? fmtCompact(selectedMarket.volume) : '--'}</b></span>
              <span><small>HIGH</small><b>{chartCandles.length ? fmtDecisionNumber(Math.max(...chartCandles.map(candle => candle.high)), 6) : '--'}</b></span>
              <span><small>LOW</small><b>{chartCandles.length ? fmtDecisionNumber(Math.min(...chartCandles.map(candle => candle.low)), 6) : '--'}</b></span>
              <span><small>DATA HEALTH</small><b className={dataHealth === 'LIVE' ? 'positive' : dataHealth === 'STALE' ? 'warning' : 'negative'}>{dataHealth}</b></span>
            </div>

            <div className="chartToolbar" aria-label="Market chart controls">
              <div className="chartControls">{['1m','5m','15m','1h','4h','1d'].map((range) => <button key={range} type="button" className={range === interval ? 'active' : ''} onClick={() => setInterval(range)}>{range.toUpperCase()}</button>)}</div>
              <div className="chartViewControls"><button type="button" className={showChartLevels ? 'active' : ''} onClick={() => setShowChartLevels(value => !value)}>LEVELS</button><button type="button" className={showChartVolume ? 'active' : ''} onClick={() => setShowChartVolume(value => !value)}>VOLUME</button><span>{hoveredCandle ? new Date(hoveredCandle.time * (hoveredCandle.time < 1_000_000_000_000 ? 1000 : 1)).toLocaleString('en-GB') : 'OHLC / REAL MARKET DATA'}</span></div>
            </div>

            <div className="chartCanvas marketOhlcChart">
              {dataLoading && !chartCandles.length ? <div className="chartEmpty">LOADING SNAPSHOT</div> : !chartCandles.length ? <div className="chartEmpty">{dataError || 'DATA UNAVAILABLE'}</div> : <svg viewBox="0 0 760 300" preserveAspectRatio="none" aria-label={`${draft.market} ${interval} candlestick chart`} onMouseLeave={() => setChartHoverIndex(null)} onMouseMove={event => { const box = event.currentTarget.getBoundingClientRect(); const index = Math.round(((event.clientX - box.left) / box.width) * (chartCandles.length - 1)); setChartHoverIndex(Math.max(0, Math.min(chartCandles.length - 1, index))) }}>
                <g className="chartGrid">{[...Array(7)].map((_, index) => <line key={`h-${index}`} x1="0" x2="760" y1={22 + index * 35} y2={22 + index * 35} />)}{[...Array(9)].map((_, index) => <line key={`v-${index}`} x1={index * 95} x2={index * 95} y1="0" y2="260" />)}</g>
                {showChartLevels && levelLines.map(line => <g key={`${line.label}-${line.value}`} className={`chartLevel level-${line.tone}`}><line x1="0" x2="760" y1={chartY(line.value)} y2={chartY(line.value)} strokeDasharray={line.tone === 'trigger' ? '5 4' : '2 3'} /><text x="8" y={chartY(line.value) - 4}>{line.label} {fmtDecisionNumber(line.value, 6)}</text></g>)}
                {chartCandles.map((candle, index) => { const x = chartX(index); const bodyTop = chartY(Math.max(candle.open, candle.close)); const bodyBottom = chartY(Math.min(candle.open, candle.close)); const bodyHeight = Math.max(2, bodyBottom - bodyTop); const bullish = candle.close >= candle.open; const candleWidth = Math.max(2, Math.min(10, 700 / chartCandles.length)); return <g key={`${candle.time}-${index}`} className={bullish ? 'candle bullish' : 'candle bearish'}><line x1={x} x2={x} y1={chartY(candle.high)} y2={chartY(candle.low)} /><rect x={x - candleWidth / 2} y={bodyTop} width={candleWidth} height={bodyHeight} /></g> })}
                {showChartVolume && chartCandles.map((candle, index) => { const x = chartX(index); const height = candle.volume / volumeMax * 28; return <rect key={`vol-${candle.time}`} className={`chartVolume ${candle.close >= candle.open ? 'up' : 'down'}`} x={x - 2} y={273 - height} width="4" height={height} /> })}
                {snapshot?.currentPrice !== null && snapshot?.currentPrice !== undefined && <g className="currentPriceLine"><line x1="0" x2="760" y1={chartY(snapshot.currentPrice)} y2={chartY(snapshot.currentPrice)} /><text x="670" y={chartY(snapshot.currentPrice) - 5}>{fmtDecisionNumber(snapshot.currentPrice, 6)}</text></g>}
                {chartHoverIndex !== null && <g className="chartCrosshair"><line x1={chartX(chartHoverIndex)} x2={chartX(chartHoverIndex)} y1="0" y2="260" /><circle cx={chartX(chartHoverIndex)} cy={chartY(chartCandles[chartHoverIndex].close)} r="3" /></g>}
              </svg>}
              <div className="chartBadge">{snapshot?.currentPrice !== null && snapshot?.currentPrice !== undefined ? `$${snapshot.currentPrice.toLocaleString('en-US', { maximumFractionDigits: 6 })}` : '--'}</div>
            </div>

            <div className="indicatorGrid">
              <article><small>TREND</small><strong className="positive">{analysis?.trend || '--'}</strong></article>
              <article><small>MOMENTUM</small><strong>{analysis?.momentum || '--'}</strong></article>
              <article><small>RSI</small><strong>{analysis?.rsi ?? '--'}</strong></article>
              <article><small>MACD</small><strong className={analysis?.macd && analysis.macd >= 0 ? 'positive' : 'negative'}>{analysis?.macd ?? '--'}</strong></article>
              <article><small>VOLUME</small><strong>{analysis?.volume_ratio ? `${analysis.volume_ratio}x` : '--'}</strong></article>
              <article><small>CONFIDENCE</small><strong className="positive">{tradeDecision.confidenceScore === null ? '--' : `${tradeDecision.confidenceScore}%`}</strong></article>
              <article><small>OPPORTUNITY</small><strong className="decisionAccent">{fmtDecisionNumber(tradeDecision.opportunityScore)}</strong></article>
            </div>

            <div className="indicatorTerminalGrid">
              <article><header><span>VOLUME</span><b>{analysis?.volume_ratio ? `${analysis.volume_ratio}x` : '--'}</b></header><div className="volumeMeter">{chartCandles.slice(-24).map((candle, index) => <i key={`${candle.time}-${index}`} style={{ height: `${Math.max(8, candle.volume / volumeMax * 100)}%` }} className={candle.close >= candle.open ? 'up' : 'down'} />)}</div></article>
              <article><header><span>RSI</span><b>{analysis?.rsi ?? '--'}</b></header><div className="indicatorScale"><i /><em>30</em><em>50</em><em>70</em></div><small>{analysis?.rsi === undefined ? 'DATA UNAVAILABLE' : analysis.rsi >= 70 ? 'OVERBOUGHT' : analysis.rsi <= 30 ? 'OVERSOLD' : 'HEALTHY RANGE'}</small></article>
              <article><header><span>MACD</span><b>{analysis?.macd ?? '--'}</b></header><div className={`macdPulse ${analysis?.macd && analysis.macd >= 0 ? 'positive' : 'negative'}`} /><small>{analysis?.macd === undefined ? 'DATA UNAVAILABLE' : analysis.macd >= 0 ? 'BULLISH' : 'BEARISH'}</small></article>
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
            <div className="panelHeader">
              <div>
                <span className="panelEyebrow">TRADE</span>
                <h3>{draft.market}</h3>
              </div>
              <span className="premiumBadge">PREMIUM</span>
            </div>

            <div className="tradeTabs">
              <button type="button" className={draft.side === 'LONG' ? 'selected long' : ''} onClick={() => setDraft((current) => ({ ...current, side: 'LONG' }))}>LONG</button>
              <button type="button" className={draft.side === 'SHORT' ? 'selected short' : ''} onClick={() => setDraft((current) => ({ ...current, side: 'SHORT' }))}>SHORT</button>
            </div>

            <div className="modeTabs">
              <button type="button" className="active">MARKET</button>
              <button type="button">LIMIT</button>
            </div>

            <div className="fieldGrid">
              <label><span>Margin</span><input value={draft.margin} onChange={(event) => setDraft((current) => ({ ...current, margin: Number(event.target.value) || 0 }))} /></label>
              <label><span>Leverage</span><input value={draft.leverage} onChange={(event) => setDraft((current) => ({ ...current, leverage: Number(event.target.value) || 1 }))} /></label>
              <label><span>Quantity</span><input value={draft.quantity} onChange={(event) => setDraft((current) => ({ ...current, quantity: Number(event.target.value) || 0 }))} /></label>
              <label><span>Entry Price</span><input value={draft.entry} onChange={(event) => setDraft((current) => ({ ...current, entry: Number(event.target.value) || 0 }))} /></label>
            </div>

            <div className="riskSummary">
              <div className="summaryHeading">ORDER PREVIEW</div>
              <div><small>Stop Loss</small><strong>{draft.stopLoss > 0 ? `$${fmtCompact(draft.stopLoss)}` : '--'}</strong></div>
              <div><small>Risk $</small><strong>${fmtCompact(riskPreview.riskUsd)}</strong></div>
              <div><small>Risk %</small><strong>{draft.entry > 0 ? `${riskPreview.riskPercent.toFixed(2)}%` : '--'}</strong></div>
              <div><small>R / R</small><strong>{riskPreview.rr > 0 ? `1:${riskPreview.rr.toFixed(2)}` : '—'}</strong></div>
              <div><small>Position Size</small><strong>{fmtCompact(Number(draft.entry) * Number(draft.quantity))}</strong></div>
              <div><small>Est. Fees</small><strong>--</strong></div>
            </div>

            <div className="tpGroup">
              <div><span>TP1</span><strong>{draft.tp1 > 0 ? Number(draft.tp1).toLocaleString('en-US', { maximumFractionDigits: 2 }) : '--'}</strong></div>
              <div><span>TP2</span><strong>{draft.tp2 > 0 ? Number(draft.tp2).toLocaleString('en-US', { maximumFractionDigits: 2 }) : '--'}</strong></div>
              <div><span>TP3</span><strong>{draft.tp3 > 0 ? Number(draft.tp3).toLocaleString('en-US', { maximumFractionDigits: 2 }) : '--'}</strong></div>
            </div>

            <button type="button" className="analysisFillButton" onClick={() => void fillFromCurrentAnalysis()} disabled={analysisFillLoading || dataLoading}>
              {analysisFillLoading ? 'ANALİZ YÜKLENİYOR...' : 'GÜNCEL ANALİZDEN DOLDUR'}
            </button>
            {analysisSyncedAt && <div className="analysisSyncStatus">Analysis synced · {analysisSyncedAt}</div>}
            {analysisFillError && <div className="analysisFillError" role="status">{analysisFillError}</div>}
            <button type="button" className="primaryOrderButton" onClick={addTradeRecord} disabled>DEMO ORDER</button>
            <div className="lockNotice"><Lock /> LIVE TRADING LOCKED</div>
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
            <div className="riskMonitorFooter"><span>Open positions</span><strong>{account?.positions ? account.positions.length : '--'} / {account?.limits?.max_open_positions ?? '--'}</strong><span>Live trading</span><strong className="warning">LOCKED</strong></div>
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
                  </tr>
                </thead>
                <tbody>
                  {openPositions.length ? openPositions.map((position) => (
                    <tr key={position.symbol}>
                      <td><strong>{position.symbol}</strong><span className="positionAge">{position.age || '--'}</span></td>
                      <td><span className={position.direction === 'LONG' ? 'positive' : 'negative'}>{position.direction || '--'}</span></td>
                      <td>{fmtNum(position.quantity, 2)}</td>
                      <td>${fmtNum(position.entry_price)}</td>
                      <td>${fmtNum(position.mark_price)}</td>
                      <td>{position.leverage ? `${position.leverage}x` : '--'}</td>
                      <td>--</td>
                      <td className={(position.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{position.unrealized_pnl === undefined ? '--' : `${position.unrealized_pnl >= 0 ? '+' : ''}$${fmtCompact(position.unrealized_pnl)}`}</td>
                      <td>--</td>
                      <td>${fmtNum(position.stop_loss)}</td>
                      <td>${fmtNum(position.tp1)}</td>
                      <td><span className="statusBadge open">OPEN</span></td>
                    </tr>
                  )) : <tr><td colSpan={12} className="emptyState">{snapshot?.accountError || 'NO OPEN POSITIONS'}</td></tr>}
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
              {account?.open_orders?.length || account?.open_algo_orders?.length ? (
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
                          <td>{order.reduce_only === undefined ? '--' : order.reduce_only ? 'YES' : 'NO'}</td>
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
                <h3>Persistent trade log</h3>
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
                  {history.length ? history.map((trade) => (
                    <tr key={trade.id} onClick={() => setSelectedTrade(trade)} className="historyRow">
                      <td>{new Date(trade.closeTime).toLocaleDateString('en-GB')}</td>
                      <td><strong>{trade.symbol}</strong></td>
                      <td className={trade.side === 'LONG' ? 'positive' : 'negative'}>{trade.side}</td>
                      <td>${fmtNum(trade.entryPrice)}</td>
                      <td>${fmtNum(trade.exitPrice)}</td>
                      <td className={trade.realizedPnl >= 0 ? 'positive' : 'negative'}>{trade.realizedPnl >= 0 ? '+' : ''}${fmtCompact(trade.realizedPnl)}</td>
                      <td className={trade.realizedPnl >= 0 ? 'positive' : 'negative'}>{trade.pnlPercent >= 0 ? '+' : ''}{trade.pnlPercent.toFixed(2)}%</td>
                      <td>{trade.duration}</td>
                      <td>{trade.source}</td>
                      <td><span className={`statusBadge ${trade.realizedPnl >= 0 ? 'win' : 'loss'}`}>{trade.realizedPnl >= 0 ? 'WIN' : 'LOSS'}</span></td>
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
              <div><small>Total trades</small><strong>{history.length ? performance.total : '--'}</strong></div>
              <div><small>Wins</small><strong>{history.length ? performance.wins : '--'}</strong></div>
              <div><small>Losses</small><strong>{history.length ? performance.losses : '--'}</strong></div>
              <div><small>Win rate</small><strong>{history.length ? `${performance.winRate.toFixed(1)}%` : '--'}</strong></div>
              <div><small>Total PnL</small><strong className={performance.realized >= 0 ? 'positive' : 'negative'}>{history.length ? `$${fmtCompact(performance.realized)}` : '--'}</strong></div>
              <div><small>Avg win</small><strong>{history.length ? `$${fmtCompact(performance.avgWin)}` : '--'}</strong></div>
              <div><small>Avg loss</small><strong>{history.length ? `$${fmtCompact(performance.avgLoss)}` : '--'}</strong></div>
              <div><small>Profit factor</small><strong>--</strong></div>
              <div><small>Best trade</small><strong className="positive">{history.length ? `$${fmtCompact(performance.best)}` : '--'}</strong></div>
              <div><small>Worst trade</small><strong className={performance.worst < 0 ? 'negative' : 'muted'}>{history.length && performance.worst ? `$${fmtCompact(performance.worst)}` : '--'}</strong></div>
              <div><small>Max drawdown</small><strong>--</strong></div>
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
              <div className="statusRow muted"><span>Last synchronized</span><strong>{account?.last_checked ? new Date(account.last_checked).toLocaleTimeString('en-GB') : '--'}</strong></div>
              <div className="statusRow muted"><span>Recovery</span><strong>{snapshot?.accountError || (account ? 'Current account snapshot' : 'DATA UNAVAILABLE')}</strong></div>
            </div>

            <div className="emergencyActions">
              <button type="button" className="dangerBtn" disabled>STOP AUTO TRADE</button>
              <button type="button" className="dangerBtn" disabled>EMERGENCY CLOSE ALL</button>
            </div>
          </section>
        </div>
      </div>

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
              <div><small>ROI</small><b>{selectedTrade.pnlPercent.toFixed(2)}%</b></div>
              <div><small>Leverage</small><b>{selectedTrade.leverage}x</b></div>
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
                  ['TP1', String(selectedTrade.tp1)],
                  ['TP2', String(selectedTrade.tp2)],
                  ['TP3', String(selectedTrade.tp3)],
                  ['Closed', selectedTrade.closeReason],
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
  )
}
