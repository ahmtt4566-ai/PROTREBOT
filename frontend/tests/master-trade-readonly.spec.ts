import {expect, test} from '@playwright/test'

const candles = Array.from({length: 160}, (_, index) => {
  const close = 84000 + index * 2
  return {time: 1790185500 + index * 900, open: close - 10, high: close + 20, low: close - 30, close, volume: 1000 + index}
})

const analysis = {
  direction: 'LONG',
  confidence: 80,
  entry: 84300,
  stop_loss: 83500,
  tp1: 85000,
  tp2: 86000,
  tp3: 87000,
  risk_reward: 2.1,
  trend: 'BULLISH',
  momentum: 'POSITIVE',
  rsi: 58,
  macd: 12,
  adx: 24,
  atr: 400,
}

const status = {
  connected: true,
  real_trading_locked: true,
  execution_state: 'LOCKED',
  armed: false,
  live_auto_trade: false,
  recovery_ready: true,
  recovery_error: null,
  reconciliation_required: false,
  readiness: {ready: false, gates: [{key: 'risk', label: 'risk', passed: false}, {key: 'protection', label: 'protection', passed: true}]},
  credentials: {configured: true},
  stream: {status: 'CANLI'},
  account: {
    wallet_balance: 1000,
    available_balance: 800,
    unrealized_pnl: 1.2,
    positions: [{symbol: 'MUBARAKUSDT', direction: 'SHORT', quantity: 437, entry_price: 0.04577, mark_price: 0.0433, leverage: 2}],
    open_orders: [],
    open_algo_orders: [
      {symbol: 'MUBARAKUSDT', side: 'BUY', type: 'TAKE_PROFIT_MARKET', trigger_price: 0.01516, status: 'NEW'},
      {symbol: 'MUBARAKUSDT', side: 'BUY', type: 'TAKE_PROFIT_MARKET', trigger_price: 0.02537, status: 'NEW'},
      {symbol: 'MUBARAKUSDT', side: 'BUY', type: 'TAKE_PROFIT_MARKET', trigger_price: 0.03559, status: 'NEW'},
      {symbol: 'MUBARAKUSDT', side: 'BUY', type: 'STOP_MARKET', trigger_price: 0.04786, status: 'NEW'},
    ],
  },
  plans: [],
}

test('Master Trade keeps account state read-only and locked', async ({page}) => {
  const mutations: string[] = []
  await page.route('**/*', async route => {
    const request = route.request()
    const url = request.url()
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method()) && url.includes('/api/')) {
      mutations.push(`${request.method()} ${url}`)
      await route.fulfill({status: 405, contentType: 'application/json', body: JSON.stringify({detail: 'Read-only smoke test'})})
      return
    }
    if (!url.includes('/api/')) {
      await route.continue()
      return
    }
    if (url.includes('/api/markets')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify([{symbol: 'BTCUSDT', display: 'BTC/USDT', price: 84300, change: 1.2, volume: 1000000, status: 'TRADING', contractType: 'PERPETUAL', quoteAsset: 'USDT'}])})
      return
    }
    if (url.includes('/api/analysis-universe')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({results: [{symbol: 'BTCUSDT', display: 'BTC/USDT', direction: 'LONG', confidence: 80, final_decision_score: 82, opportunity_score: 78, smart_score: 80, price: 84300, change: 1.2, volume: 1000000}]})})
      return
    }
    if (url.includes('/api/klines/')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(candles)})
      return
    }
    if (url.includes('/api/analysis/')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(analysis)})
      return
    }
    if (url.includes('/api/v25/status')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(status)})
      return
    }
    if (url.includes('/api/exchange-connections/status')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({connections: {LIVE: {configured: true, active: true}}, testnet: {configured: true}})})
      return
    }
    if (url.includes('/api/v21/journal')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({items: []})})
      return
    }
    if (url.includes('/api/v21/performance')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({total_trades: 0, wins: 0, losses: 0, win_rate: 0, total_profit: 0, total_loss: 0, net_profit: 0, average_trade: 0, best_trade: 0, worst_trade: 0, profit_factor: null, average_win: null, average_loss: null, losing_streak: 0, max_drawdown: 0, history_quality: 'EMPTY'})})
      return
    }
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({})})
  })

  await page.goto('/master-trade')
  await expect(page.getByRole('heading', {name: 'LIVE AUTO TRADE'})).toBeVisible({timeout: 15000})
  await expect(page.getByText('LOCKED', {exact: true}).first()).toBeVisible()
  await expect(page.getByText('MUBARAKUSDT', {exact: true}).first()).toBeVisible()
  await expect(page.getByText('SHORT', {exact: true}).first()).toBeVisible()
  expect(mutations).toEqual([])
})