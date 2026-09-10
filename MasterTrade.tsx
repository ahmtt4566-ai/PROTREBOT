import { useEffect, useMemo, useState } from 'react'
import { Activity, AlertTriangle, ArrowDownRight, ArrowUpRight, BarChart3, CircleDollarSign, Gauge, Lock, ShieldCheck, TrendingUp, Wallet } from 'lucide-react'

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

type PositionRow = {
  symbol: string
  side: TradeSide
  entry: number
  mark: number
  quantity: number
  leverage: number
  margin: number
  pnl: number
  pnlPercent: number
  liquidation: number
  stopLoss: number
  tp1: number
  tp2: number
  tp3: number
  age: string
}

type OrderRow = {
  symbol: string
  side: TradeSide
  type: 'Market' | 'Limit'
  price: number
  quantity: number
  status: 'Open' | 'Filled' | 'Cancelled'
  created: string
}

const STORAGE_KEY = 'protrebot-master-trade-history-v2'
const SNAPSHOT_KEY = 'protrebot-master-trade-demo-snapshot-v2'

const seedHistory: TradeHistoryRow[] = [
  {
    id: 'MT-1001',
    symbol: 'BTCUSDT',
    side: 'LONG',
    entryPrice: 61240,
    exitPrice: 62010,
    quantity: 0.14,
    leverage: 7,
    margin: 1400,
    stopLoss: 60600,
    tp1: 61850,
    tp2: 62350,
    tp3: 62900,
    realizedPnl: 108.2,
    pnlPercent: 7.73,
    fees: 3.6,
    funding: 0.2,
    openTime: '2026-09-08T09:10:00Z',
    closeTime: '2026-09-08T11:45:00Z',
    duration: '2h 35m',
    closeReason: 'TP2',
    source: 'AUTO',
    analysisScore: 88,
    opportunityScore: 91,
    scanCycle: '09:00',
  },
  {
    id: 'MT-1002',
    symbol: 'ETHUSDT',
    side: 'SHORT',
    entryPrice: 3312,
    exitPrice: 3278,
    quantity: 1.2,
    leverage: 5,
    margin: 990,
    stopLoss: 3355,
    tp1: 3288,
    tp2: 3260,
    tp3: 3235,
    realizedPnl: 40.8,
    pnlPercent: 4.12,
    fees: 2.4,
    funding: 0.8,
    openTime: '2026-09-09T13:20:00Z',
    closeTime: '2026-09-09T15:05:00Z',
    duration: '1h 45m',
    closeReason: 'TP1',
    source: 'MANUAL',
    analysisScore: 82,
    opportunityScore: 84,
    scanCycle: '13:15',
  },
]

const seedPositions: PositionRow[] = [
  {
    symbol: 'BTCUSDT',
    side: 'LONG',
    entry: 61280,
    mark: 61420,
    quantity: 0.11,
    leverage: 7,
    margin: 1250,
    pnl: 15.9,
    pnlPercent: 1.27,
    liquidation: 55820,
    stopLoss: 60680,
    tp1: 61880,
    tp2: 62680,
    tp3: 63480,
    age: '1h 42m',
  },
  {
    symbol: 'SOLUSDT',
    side: 'SHORT',
    entry: 148.4,
    mark: 146.7,
    quantity: 13,
    leverage: 6,
    margin: 920,
    pnl: 22.1,
    pnlPercent: 2.4,
    liquidation: 171.4,
    stopLoss: 152.8,
    tp1: 144.9,
    tp2: 142.1,
    tp3: 139.8,
    age: '54m',
  },
]

const seedOrders: OrderRow[] = [
  { symbol: 'ETHUSDT', side: 'LONG', type: 'Limit', price: 3320, quantity: 0.9, status: 'Open', created: '2026-09-10T12:15:00Z' },
  { symbol: 'BNBUSDT', side: 'SHORT', type: 'Market', price: 615, quantity: 2, status: 'Filled', created: '2026-09-10T12:22:00Z' },
]

const fmtNum = (value: number | undefined, decimals = 2) =>
  value === undefined ? '—' : value.toLocaleString('tr-TR', { maximumFractionDigits: decimals, minimumFractionDigits: decimals })

const fmtCompact = (value: number | undefined) =>
  value === undefined ? '—' : value.toLocaleString('tr-TR', { maximumFractionDigits: 2 })

export default function MasterTrade({ onBack }: { onBack?: () => void }) {
  const [history, setHistory] = useState<TradeHistoryRow[]>(() => {
    if (typeof window === 'undefined') return seedHistory
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return seedHistory
    try {
      const parsed = JSON.parse(raw) as TradeHistoryRow[]
      return Array.isArray(parsed) && parsed.length ? parsed : seedHistory
    } catch {
      return seedHistory
    }
  })
  const [snapshotState] = useState(() => {
    if (typeof window === 'undefined') return { mode: 'DEMO', account: 'Demo account snapshot ready', lastUpdated: new Date().toISOString(), recovery: 'Local cache restored' }
    const raw = window.localStorage.getItem(SNAPSHOT_KEY)
    if (!raw) return { mode: 'DEMO', account: 'Demo account snapshot ready', lastUpdated: new Date().toISOString(), recovery: 'Local cache restored' }
    try {
      return JSON.parse(raw) as { mode: string; account: string; lastUpdated: string; recovery: string }
    } catch {
      return { mode: 'DEMO', account: 'Demo account snapshot ready', lastUpdated: new Date().toISOString(), recovery: 'Local cache restored' }
    }
  })
  const [selectedTrade, setSelectedTrade] = useState<TradeHistoryRow | null>(null)
  const [draft, setDraft] = useState({ side: 'LONG' as TradeSide, market: 'BTCUSDT', leverage: 7, margin: 1000, quantity: 0.08, entry: 61350, stopLoss: 60650, tp1: 61850, tp2: 62400, tp3: 63150 })
  const [automation] = useState([
    { symbol: 'BTCUSDT', side: 'LONG', analysis: 92, opportunity: 88, liquidity: 'HIGH', volatility: 'GOOD', confirmation: 'MTF ✓', reason: 'Strong trend + risk aligned' },
    { symbol: 'ETHUSDT', side: 'SHORT', analysis: 84, opportunity: 79, liquidity: 'MED', volatility: 'GOOD', confirmation: 'MTF ✓', reason: 'Fade after sweep resistance' },
    { symbol: 'SOLUSDT', side: 'LONG', analysis: 80, opportunity: 76, liquidity: 'HIGH', volatility: 'HIGH', confirmation: 'MTF ✓', reason: 'Ema stack + impulse breakout' },
  ])

  useEffect(() => {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(history))
      window.localStorage.setItem(SNAPSHOT_KEY, JSON.stringify({
        mode: 'DEMO',
        account: 'Demo account snapshot ready',
        lastUpdated: new Date().toISOString(),
        recovery: 'Recovered from local persistent cache',
      }))
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
    const riskUsd = (Math.max(0, draft.margin) * (Math.abs(entry - stop) / entry)) * 0.7
    const tp = Number(draft.tp1)
    const reward = Math.abs(tp - entry) * Number(draft.quantity)
    const rr = distance > 0 ? reward / (distance * Number(draft.quantity)) : 0
    return { riskUsd, reward, rr }
  }, [draft])

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

  const marketWatch = [
    { symbol: 'BTCUSDT', price: 61380.12, change: 2.31, signal: 'LONG', score: 91 },
    { symbol: 'ETHUSDT', price: 3315.42, change: -0.84, signal: 'SHORT', score: 88 },
    { symbol: 'SOLUSDT', price: 148.32, change: 1.56, signal: 'LONG', score: 84 },
    { symbol: 'BNBUSDT', price: 612.1, change: 0.72, signal: 'LONG', score: 80 },
    { symbol: 'XRPUSDT', price: 0.54, change: -1.2, signal: 'SHORT', score: 76 },
  ]

  const opportunityBreakdown = [
    { label: 'Trend', value: 92 },
    { label: 'Momentum', value: 89 },
    { label: 'Volume', value: 94 },
    { label: 'MTF Confirmation', value: 91 },
    { label: 'Signal Freshness', value: 87 },
    { label: 'Risk / Reward', value: 90 },
  ]

  const overviewCards = [
    { label: 'BALANCE', value: '$4,995.63', note: 'Demo Futures Wallet', tone: 'default' },
    { label: 'AVAILABLE', value: '$4,974.92', note: 'Ready Margin', tone: 'default' },
    { label: 'UNREALIZED PNL', value: '+$182.40', note: 'Net Open PnL', tone: 'positive' },
    { label: 'REALIZED PNL', value: '+$94.20', note: 'Today', tone: 'positive' },
    { label: 'MARGIN USED', value: '$1,840.00', note: '7.2% Utilized', tone: 'default' },
    { label: 'OPEN POSITIONS', value: '2 / 3', note: 'Active Exposure', tone: 'default' },
  ]

  const performanceTrend = [35, 40, 38, 48, 52, 46, 57, 64, 60, 68, 72, 76]

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
              <button type="button" className="panelGhostButton">ALL</button>
            </div>

            <div className="watchlistList">
              {marketWatch.map((item, index) => (
                <button key={item.symbol} type="button" className={index === 0 ? 'watchlistItem active' : 'watchlistItem'}>
                  <div className="watchlistMeta">
                    <b>{item.symbol}</b>
                    <span>{item.change >= 0 ? '+' : ''}{item.change.toFixed(2)}%</span>
                  </div>
                  <div className="watchlistStats">
                    <strong>${item.price.toLocaleString('en-US', { maximumFractionDigits: 2 })}</strong>
                    <em className={item.signal === 'LONG' ? 'positive' : 'negative'}>{item.signal}</em>
                    <small>{item.score}</small>
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
              <div className="chartControls">
                {['1m','5m','15m','1h','4h','1D'].map((range) => (
                  <button key={range} type="button" className={range === '15m' ? 'active' : ''}>{range}</button>
                ))}
              </div>
            </div>

            <div className="chartPriceSummary">
              <div>
                <span className="chartSymbol">{draft.market}</span>
                <strong>${Number(draft.entry).toLocaleString('en-US', { maximumFractionDigits: 2 })}</strong>
              </div>
              <span className="delta positive">+2.31%</span>
            </div>

            <div className="chartCanvas">
              <svg viewBox="0 0 760 300" preserveAspectRatio="none" aria-label="Master Trade chart preview">
                <defs>
                  <linearGradient id="masterTradeChartGlow" x1="0" x2="1" y1="0" y2="0">
                    <stop offset="0%" stopColor="#22c55e" stopOpacity="0.32" />
                    <stop offset="100%" stopColor="#22c55e" stopOpacity="0.08" />
                  </linearGradient>
                </defs>
                <g opacity="0.18" stroke="#334155" strokeWidth="1">
                  {[...Array(11)].map((_, index) => (
                    <line key={`v-${index}`} x1="0" x2="760" y1={30 + index * 24} y2={30 + index * 24} />
                  ))}
                </g>
                <path d="M0 188 L68 170 L128 160 L194 138 L264 148 L332 122 L404 116 L470 92 L542 98 L610 72 L686 48 L760 32" fill="none" stroke="#34d399" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
                <path d="M0 188 L68 170 L128 160 L194 138 L264 148 L332 122 L404 116 L470 92 L542 98 L610 72 L686 48 L760 32 L760 300 L0 300 Z" fill="url(#masterTradeChartGlow)" opacity="0.7" />
              </svg>
              <div className="chartBadge">$61,380.00</div>
            </div>

            <div className="indicatorGrid">
              <article><small>TREND</small><strong className="positive">BULLISH</strong></article>
              <article><small>MOMENTUM</small><strong>STRONG</strong></article>
              <article><small>RSI</small><strong>62.4</strong></article>
              <article><small>MACD</small><strong className="positive">+0.92</strong></article>
              <article><small>VOLUME</small><strong>1.3x</strong></article>
              <article><small>MTF</small><strong className="positive">CONFIRMED</strong></article>
            </div>
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
              <div><small>Stop Loss</small><strong>${fmtCompact(draft.stopLoss)}</strong></div>
              <div><small>Risk $</small><strong>${fmtCompact(riskPreview.riskUsd)}</strong></div>
              <div><small>Risk %</small><strong>{((Math.abs(Number(draft.entry) - Number(draft.stopLoss)) / Number(draft.entry) * 100) || 0).toFixed(2)}%</strong></div>
              <div><small>R / R</small><strong>{riskPreview.rr > 0 ? `1:${riskPreview.rr.toFixed(2)}` : '—'}</strong></div>
            </div>

            <div className="tpGroup">
              <div><span>TP1</span><strong>{Number(draft.tp1).toLocaleString('en-US', { maximumFractionDigits: 2 })}</strong></div>
              <div><span>TP2</span><strong>{Number(draft.tp2).toLocaleString('en-US', { maximumFractionDigits: 2 })}</strong></div>
              <div><span>TP3</span><strong>{Number(draft.tp3).toLocaleString('en-US', { maximumFractionDigits: 2 })}</strong></div>
            </div>

            <button type="button" className="primaryOrderButton" onClick={addTradeRecord} disabled>DEMO ORDER</button>
            <div className="lockNotice"><Lock /> LIVE TRADING LOCKED</div>
          </aside>
        </div>

        <div className="masterTradeDataGrid">
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
                  {seedPositions.map((position) => (
                    <tr key={position.symbol}>
                      <td><strong>{position.symbol}</strong></td>
                      <td><span className={position.side === 'LONG' ? 'positive' : 'negative'}>{position.side}</span></td>
                      <td>{fmtNum(position.quantity, 2)}</td>
                      <td>${fmtNum(position.entry)}</td>
                      <td>${fmtNum(position.mark)}</td>
                      <td>{position.leverage}x</td>
                      <td>${fmtCompact(position.margin)}</td>
                      <td className={position.pnl >= 0 ? 'positive' : 'negative'}>{position.pnl >= 0 ? '+' : ''}${fmtCompact(position.pnl)}</td>
                      <td className={position.pnl >= 0 ? 'positive' : 'negative'}>{position.pnlPercent >= 0 ? '+' : ''}{position.pnlPercent.toFixed(2)}%</td>
                      <td>${fmtNum(position.stopLoss)}</td>
                      <td>${fmtNum(position.tp1)}</td>
                      <td><span className="statusBadge open">OPEN</span></td>
                    </tr>
                  ))}
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
              {seedOrders.length ? (
                <table>
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Side</th>
                      <th>Type</th>
                      <th>Price</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {seedOrders.map((order) => (
                      <tr key={`${order.symbol}-${order.created}`}>
                        <td>{order.symbol}</td>
                        <td className={order.side === 'LONG' ? 'positive' : 'negative'}>{order.side}</td>
                        <td>{order.type}</td>
                        <td>${fmtNum(order.price)}</td>
                        <td><span className={`statusBadge ${order.status === 'Open' ? 'open' : order.status === 'Filled' ? 'filled' : 'cancelled'}`}>{order.status}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <div className="emptyState">No active orders</div>
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
                  {history.map((trade) => (
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
                  ))}
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
              <div><small>Total trades</small><strong>{performance.total}</strong></div>
              <div><small>Wins</small><strong>{performance.wins}</strong></div>
              <div><small>Losses</small><strong>{performance.losses}</strong></div>
              <div><small>Win rate</small><strong>{performance.winRate.toFixed(1)}%</strong></div>
              <div><small>Total PnL</small><strong className={performance.realized >= 0 ? 'positive' : 'negative'}>${fmtCompact(performance.realized)}</strong></div>
              <div><small>Avg win</small><strong>${fmtCompact(performance.avgWin)}</strong></div>
              <div><small>Avg loss</small><strong>${fmtCompact(performance.avgLoss)}</strong></div>
              <div><small>Profit factor</small><strong>{performance.total ? '1.74' : '—'}</strong></div>
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

            <div className="scannerList">
              {automation.map((candidate) => (
                <article key={candidate.symbol}>
                  <div>
                    <strong>{candidate.symbol}</strong>
                    <small>{candidate.side}</small>
                  </div>
                  <div className="scannerScore"><span>{candidate.analysis}</span></div>
                  <small>{candidate.reason}</small>
                </article>
              ))}
            </div>
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
              <div className="statusRow muted"><span>Last synchronized</span><strong>{new Date(snapshotState.lastUpdated).toLocaleTimeString('en-GB')}</strong></div>
              <div className="statusRow muted"><span>Recovery</span><strong>{snapshotState.recovery}</strong></div>
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
