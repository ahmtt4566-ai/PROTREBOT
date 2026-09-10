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
  const [selectedTrade, setSelectedTrade] = useState<TradeHistoryRow | null>(seedHistory[0])
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

  return (
    <section className="masterTrade">
      <div className="masterTradeShell">
        <header className="masterTradeTopbar">
          <div>
            <span className="masterTradeEyebrow">MASTER TRADE V2</span>
            <h2>Professional execution cockpit</h2>
          </div>
          {onBack && (
            <button type="button" className="masterTradeBackButton" onClick={onBack}>← BACK TO DASHBOARD</button>
          )}
          <div className="masterTradeStatusRow">
            <span className="statusPill online"><Activity /> CONNECTED</span>
            <span className="statusPill demo"><Wallet /> DEMO</span>
            <span className="statusPill locked"><Lock /> LIVE TRADING LOCKED</span>
          </div>
          <div className="masterTradeMetrics">
            <div><small>Balance</small><b>$ 12,450.00</b></div>
            <div><small>Margin</small><b>$ 3,820.00</b></div>
            <div><small>U PnL</small><b className="up">+$ 182.40</b></div>
            <div><small>Today</small><b className="up">+$ 94.20</b></div>
            <div><small>ROI</small><b>2.14%</b></div>
            <div><small>WSS</small><b>LIVE</b></div>
          </div>
        </header>

        <div className="masterTradeGrid">
          <aside className="masterTradePanel watchlistPanel">
            <div className="panelTitleRow">
              <span>MARKET WATCHLIST</span>
              <button type="button">ALL</button>
            </div>
            <div className="watchlistRows">
              {['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT'].map((symbol, index) => (
                <button key={symbol} type="button" className={index === 0 ? 'active' : ''}>
                  <span><b>{symbol}</b><small>{index === 0 ? 0.8 : index === 1 ? -0.4 : 1.3}%</small></span>
                  <strong>{index === 0 ? '61380.00' : index === 1 ? '3315.40' : index === 2 ? '148.32' : index === 3 ? '612.10' : '0.540'}</strong>
                </button>
              ))}
            </div>
          </aside>

          <main className="masterTradePanel chartPanel">
            <div className="panelTitleRow">
              <span>{draft.market}</span>
              <div className="chartControls">
                <button type="button">1m</button>
                <button type="button" className="active">15m</button>
                <button type="button">1h</button>
              </div>
            </div>
            <div className="chartCanvas">
              <div className="chartGrid">
                {Array.from({ length: 18 }).map((_, index) => <span key={`grid-${index}`} />)}
              </div>
              <svg viewBox="0 0 640 260" preserveAspectRatio="none" aria-label="Master trade chart preview">
                <path d="M0 170 L80 150 L150 162 L220 130 L300 110 L380 130 L450 85 L520 92 L640 40" fill="none" stroke="#36d98d" strokeWidth="3" strokeLinecap="round"/>
                <path d="M0 190 L80 175 L150 180 L220 156 L300 170 L380 168 L450 145 L520 155 L640 120" fill="none" stroke="#f26a5c" strokeWidth="2" strokeDasharray="6 5" strokeLinecap="round"/>
              </svg>
              <div className="priceBadge">$ {fmtCompact(Number(draft.entry))}</div>
            </div>
          </main>

          <aside className="masterTradePanel orderPanel">
            <div className="panelTitleRow">
              <span>ORDER PANEL</span>
              <span className="lockedLabel">LIVE LOCKED</span>
            </div>
            <div className="orderSideRow">
              <button type="button" className={draft.side === 'LONG' ? 'selected long' : ''} onClick={() => setDraft((current) => ({ ...current, side: 'LONG' }))}>LONG</button>
              <button type="button" className={draft.side === 'SHORT' ? 'selected short' : ''} onClick={() => setDraft((current) => ({ ...current, side: 'SHORT' }))}>SHORT</button>
              <button type="button">Market</button>
            </div>
            <div className="orderFormGrid">
              <label><span>Margin</span><input value={draft.margin} onChange={(event) => setDraft((current) => ({ ...current, margin: Number(event.target.value) || 0 }))} /></label>
              <label><span>Leverage</span><input value={draft.leverage} onChange={(event) => setDraft((current) => ({ ...current, leverage: Number(event.target.value) || 1 }))} /></label>
              <label><span>Quantity</span><input value={draft.quantity} onChange={(event) => setDraft((current) => ({ ...current, quantity: Number(event.target.value) || 0 }))} /></label>
              <label><span>Entry</span><input value={draft.entry} onChange={(event) => setDraft((current) => ({ ...current, entry: Number(event.target.value) || 0 }))} /></label>
              <label><span>SL</span><input value={draft.stopLoss} onChange={(event) => setDraft((current) => ({ ...current, stopLoss: Number(event.target.value) || 0 }))} /></label>
              <label><span>TP1</span><input value={draft.tp1} onChange={(event) => setDraft((current) => ({ ...current, tp1: Number(event.target.value) || 0 }))} /></label>
              <label><span>TP2</span><input value={draft.tp2} onChange={(event) => setDraft((current) => ({ ...current, tp2: Number(event.target.value) || 0 }))} /></label>
              <label><span>TP3</span><input value={draft.tp3} onChange={(event) => setDraft((current) => ({ ...current, tp3: Number(event.target.value) || 0 }))} /></label>
            </div>
            <div className="riskPreview">
              <div><small>MAX LOSS</small><b>${fmtCompact(riskPreview.riskUsd)}</b></div>
              <div><small>POTENTIAL TP</small><b>${fmtCompact(riskPreview.reward)}</b></div>
              <div><small>R/R</small><b>1:{fmtCompact(riskPreview.rr)}</b></div>
            </div>
            <button type="button" className="riskSubmit" onClick={addTradeRecord} disabled>Place order — LIVE LOCKED</button>
          </aside>
        </div>

        <div className="masterTradeBottomGrid">
          <section className="masterTradePanel positionsPanel">
            <div className="panelTitleRow">
              <span>OPEN POSITIONS</span>
              <button type="button">25%  50%  75%  100%</button>
            </div>
            <div className="positionsTable">
              {seedPositions.map((position) => (
                <div key={position.symbol} className="positionRow">
                  <div><b>{position.symbol}</b><small>{position.side}</small></div>
                  <div><small>Entry</small><b>{fmtNum(position.entry)}</b></div>
                  <div><small>Mark</small><b>{fmtNum(position.mark)}</b></div>
                  <div><small>Qty</small><b>{fmtNum(position.quantity, 2)}</b></div>
                  <div><small>Leverage</small><b>{position.leverage}x</b></div>
                  <div><small>Margin</small><b>${fmtCompact(position.margin)}</b></div>
                  <div><small>PnL</small><b className={position.pnl >= 0 ? 'up' : 'down'}>{position.pnl >= 0 ? '+' : ''}${fmtCompact(position.pnl)}</b></div>
                  <div><small>Liquidation</small><b>{fmtNum(position.liquidation)}</b></div>
                  <div><small>SL</small><b>{fmtNum(position.stopLoss)}</b></div>
                </div>
              ))}
            </div>
          </section>

          <section className="masterTradePanel ordersPanel">
            <div className="panelTitleRow">
              <span>ORDERS</span>
            </div>
            <div className="miniTable">
              {seedOrders.map((order) => (
                <div key={`${order.symbol}-${order.created}`} className="miniRow">
                  <span>{order.symbol}</span>
                  <span>{order.side}</span>
                  <span>{order.type}</span>
                  <span>{fmtNum(order.price)}</span>
                  <span>{fmtNum(order.quantity, 2)}</span>
                  <span>{order.status}</span>
                </div>
              ))}
            </div>
          </section>

          <section className="masterTradePanel performancePanel">
            <div className="panelTitleRow">
              <span>PERFORMANCE</span>
            </div>
            <div className="perfMetrics">
              <div><small>Total Trades</small><b>{performance.total}</b></div>
              <div><small>Wins</small><b>{performance.wins}</b></div>
              <div><small>Losses</small><b>{performance.losses}</b></div>
              <div><small>Win Rate</small><b>{performance.winRate.toFixed(1)}%</b></div>
              <div><small>Realized PnL</small><b className={performance.realized >= 0 ? 'up' : 'down'}>${fmtCompact(performance.realized)}</b></div>
              <div><small>Best</small><b>${fmtCompact(performance.best)}</b></div>
            </div>
          </section>

          <section className="masterTradePanel automationPanel">
            <div className="panelTitleRow">
              <span>AUTO TRADE</span>
              <span className="lockedLabel">MANUAL | AUTO</span>
            </div>
            <div className="autoGrid">
              {automation.map((candidate) => (
                <div key={candidate.symbol} className="autoCandidate">
                  <div><b>{candidate.symbol}</b><span>{candidate.side}</span></div>
                  <small>{candidate.reason}</small>
                  <div className="autoStats">
                    <span>Score {candidate.analysis}</span>
                    <span>Oppo {candidate.opportunity}</span>
                    <span>{candidate.confirmation}</span>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="masterTradePanel historyPanel">
            <div className="panelTitleRow">
              <span>PERSISTENT HISTORY · TRADE HISTORY</span>
            </div>
            <div className="historyRows">
              {history.map((trade) => (
                <button key={trade.id} type="button" className="historyItem" onClick={() => setSelectedTrade(trade)}>
                  <span>{trade.symbol}</span>
                  <span>{trade.side}</span>
                  <span>{trade.pnlPercent.toFixed(2)}%</span>
                  <strong className={trade.realizedPnl >= 0 ? 'up' : 'down'}>${fmtCompact(trade.realizedPnl)}</strong>
                </button>
              ))}
            </div>
          </section>

          <section className="masterTradePanel emergencyPanel">
            <div className="panelTitleRow">
              <span>DEMO ACCOUNT SNAPSHOT / RECOVERY</span>
            </div>
            <div className="securityInfo">
              <ShieldCheck /> <span>{snapshotState.account} · {snapshotState.recovery}</span>
            </div>
            <div className="emergencyActions">
              <button type="button" className="dangerBtn" disabled>STOP AUTO TRADE</button>
              <button type="button" className="dangerBtn" disabled>EMERGENCY CLOSE ALL</button>
            </div>
            <div className="securityInfo">
              <ShieldCheck /> <span>LIVE TRADING remains fail-closed. Demo/Testnet protections stay active.</span>
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
            <div className="drawerGrid">
              <div><small>LONG/SHORT</small><b>{selectedTrade.side}</b></div>
              <div><small>Entry</small><b>${fmtNum(selectedTrade.entryPrice)}</b></div>
              <div><small>Exit</small><b>${fmtNum(selectedTrade.exitPrice)}</b></div>
              <div><small>PnL</small><b className={selectedTrade.realizedPnl >= 0 ? 'up' : 'down'}>${fmtCompact(selectedTrade.realizedPnl)}</b></div>
              <div><small>ROI</small><b>{selectedTrade.pnlPercent.toFixed(2)}%</b></div>
              <div><small>Duration</small><b>{selectedTrade.duration}</b></div>
              <div><small>Analysis</small><b>{selectedTrade.analysisScore}</b></div>
              <div><small>Opportunity</small><b>{selectedTrade.opportunityScore}</b></div>
              <div><small>Source</small><b>{selectedTrade.source}</b></div>
            </div>
            <ul className="timelineList">
              <li><span>Signal generated</span><em>{selectedTrade.scanCycle}</em></li>
              <li><span>Order submitted</span><em>{selectedTrade.openTime}</em></li>
              <li><span>Entry filled</span><em>{selectedTrade.openTime}</em></li>
              <li><span>TP1</span><em>{selectedTrade.tp1}</em></li>
              <li><span>Break-even</span><em>{selectedTrade.stopLoss}</em></li>
              <li><span>TP2</span><em>{selectedTrade.tp2}</em></li>
              <li><span>TP3</span><em>{selectedTrade.tp3}</em></li>
              <li><span>Closed</span><em>{selectedTrade.closeReason}</em></li>
            </ul>
          </aside>
        </div>
      )}
    </section>
  )
}
