import {expect, test, type Page} from '@playwright/test'

test.use({baseURL: 'http://127.0.0.1:4174'})
test.setTimeout(60000)

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
  events: [
    {kind: 'SCAN', message: 'Waiting for confirmation', created_at: '2026-10-02T12:00:00Z'},
    {kind: 'SCAN', message: 'Waiting for confirmation', created_at: '2026-10-02T12:00:01Z'},
    {kind: 'SCAN', message: 'Next scan', created_at: '2026-10-02T12:00:02Z'},
  ],
}

async function prepareReadOnly(page: Page) {
  const mutations: string[] = []
  await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'readonly-test-session'))
  await page.route('**/api/**', async route => {
    const request = route.request()
    const url = request.url()
    if (url.includes('/api/v25/risk/preview')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({estimated_stop_loss_usdt: 1, stop_distance_pct: 1})})
      return
    }
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method()) && url.includes('/api/')) {
      mutations.push(`${request.method()} ${url}`)
      await route.fulfill({status: 405, contentType: 'application/json', body: JSON.stringify({detail: 'Read-only smoke test'})})
      return
    }
    if (url.includes('/api/v22/profile') || url.includes('/api/v22/session')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({access: {canAccessMasterTrade: true}, user: {id: 'readonly-owner', role: 'OWNER', email: 'owner@example.com', display_name: 'Read-only Owner', active: true, email_verified: true}})})
      return
    }
    if (url.includes('/api/markets')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify([{symbol: 'BTCUSDT', display: 'BTC/USDT', price: 84300, change: 1.2, volume: 1000000, status: 'TRADING', contractType: 'PERPETUAL', quoteAsset: 'USDT'}, {symbol: 'ETHUSDT', display: 'ETH/USDT', price: 2800, change: 2, volume: 500000, status: 'TRADING', contractType: 'PERPETUAL', quoteAsset: 'USDT'}])})
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
  return mutations
}

test('Master Trade keeps account state read-only and locked', async ({page}) => {
  const mutations = await prepareReadOnly(page)
  await expect(page.getByRole('tab', {name: 'Analiz', exact: true})).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('.masterTradeAccordion[open]')).toHaveCount(0)
  await page.locator('.watchlistItem').filter({hasText: 'ETHUSDT'}).click()
  await expect(page.locator('.chartPanel h3')).toHaveText('ETHUSDT')
  await page.getByRole('tab', {name: 'Canlı İşlem', exact: true}).click()
  await expect(page).toHaveURL(/tab=canli/)
  await expect(page.getByRole('heading', {name: 'LIVE AUTO TRADE'})).toBeVisible({timeout: 15000})
  await expect(page.getByText('LOCKED', {exact: true}).first()).toBeVisible()
  await expect(page.locator('.masterTradeStepper button').nth(2)).toBeDisabled()
  await expect(page.locator('.masterTradeStepper button').nth(3)).toBeDisabled()
  await page.locator('.masterTradeLiveSlot').filter({has: page.locator('.liveUxManual')}).locator('summary').click()
  const marginInput = page.locator('.liveUxManual input[type="number"]').first()
  await marginInput.fill('25')
  await page.getByRole('tab', {name: 'Analiz', exact: true}).click()
  await page.goBack()
  await expect(page.getByRole('tab', {name: 'Canlı İşlem', exact: true})).toHaveAttribute('aria-selected', 'true')
  await expect(marginInput).toHaveValue('25')
  expect(mutations).toEqual([])
})

test('Master Trade positions and connections preserve the read-only account snapshot', async ({page}) => {
  const mutations = await prepareReadOnly(page)
  await page.locator('.watchlistItem').filter({hasText: 'ETHUSDT'}).click()
  await expect(page.locator('.chartPanel h3')).toHaveText('ETHUSDT')
  await page.getByRole('tab', {name: 'Pozisyonlar', exact: true}).click()
  await expect(page.locator('.masterTradeLiveAccountTable')).toHaveCount(0)
  await expect(page.locator('.positionsPanel').getByText('MUBARAKUSDT', {exact: true})).toBeVisible()
  await expect(page.locator('.positionsPanel').getByText('SHORT', {exact: true})).toBeVisible()
  expect(await page.locator('.positionsPanel th').evaluateAll(elements => elements.every(element => getComputedStyle(element, '::before').content === 'none' && getComputedStyle(element, '::after').content === 'none'))).toBe(true)
  await page.getByRole('tab', {name: 'Bağlantı', exact: true}).click()
  await expect(page.getByRole('heading', {name: 'Connect your trading account'})).toBeVisible()
  await expect(page.locator('.masterTradeLiveActivity .masterTradeLogCount')).toHaveText('×2')
  expect(await page.locator('.masterTradeLiveActivity ol').evaluate(element => getComputedStyle(element).height)).toBe('200px')
  expect(mutations).toEqual([])
})

test('Master Trade responsive geometry stays read-only at desktop, tablet and mobile sizes', async ({page}) => {
  const mutations = await prepareReadOnly(page)
  await page.locator('.watchlistItem').filter({hasText: 'ETHUSDT'}).click()
  await expect(page.locator('.chartPanel h3')).toHaveText('ETHUSDT')
  await page.getByRole('tab', {name: 'Analiz', exact: true}).click()
  await page.setViewportSize({width: 1440, height: 900})
  await expect(page.locator('.masterTradeMetricTile')).toHaveCount(7)
  await expect(page.locator('.decisionMtfGrid > span')).toHaveCount(5)
  expect(await page.locator('.chartCanvas').evaluate(element => element.getBoundingClientRect().height)).toBeGreaterThanOrEqual(420)
  expect(await page.locator('.masterTradeFocusPanel').evaluate(element => Math.round(element.getBoundingClientRect().height))).toBe(64)
  expect(await page.locator('.masterTradeDecisionColumn').evaluate(element => getComputedStyle(element).overflowY)).toBe('auto')
  const labelPositions = await page.locator('.masterTradeChartLabels [data-label-y]').evaluateAll(elements => elements.map(element => Number(element.getAttribute('data-label-y'))))
  expect(labelPositions.length).toBeGreaterThan(1)
  expect(labelPositions.slice(1).every((position, index) => position - labelPositions[index] >= 18)).toBe(true)
  expect(await page.locator('.masterTradeWorkspace').evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(3)
  expect(await page.locator('.watchlistList').evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(1)
  await page.evaluate(() => window.scrollTo(0, 600))
  await expect.poll(() => page.locator('.masterTradeFocusPanel').evaluate(element => Math.round(element.getBoundingClientRect().top))).toBe(0)
  await page.setViewportSize({width: 1024, height: 900})
  expect(await page.locator('.masterTradeWorkspace').evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(2)
  expect(await page.locator('.masterTradeScannerDetails').evaluate(element => getComputedStyle(element).flexShrink)).toBe('0')
  expect(await page.locator('.watchlistList').evaluate(element => element.getBoundingClientRect().height)).toBeLessThanOrEqual(240)
  await page.setViewportSize({width: 390, height: 844})
  expect(await page.locator('.masterTradeWorkspace').evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(1)
  expect(await page.locator('.watchlistList').evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(1)
  expect(await page.locator('.masterTradeSticky').evaluate(element => getComputedStyle(element).position)).toBe('sticky')
  expect(await page.locator('.masterTradeTabs').evaluate(element => getComputedStyle(element).position)).toBe('static')
  expect(await page.locator('.indicatorGrid').evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(2)
  expect(await page.locator('.chartToolbar').evaluate(element => getComputedStyle(element).flexDirection)).toBe('row')
  const mobilePills = await page.locator('.masterTradeChartPills > span').evaluateAll(elements => elements.map(element => ({top: element.getBoundingClientRect().top, font: getComputedStyle(element).fontSize})))
  expect(mobilePills.every(pill => pill.font === '12px')).toBe(true)
  expect(mobilePills.slice(1).every((pill, index) => pill.top - mobilePills[index].top >= 16)).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.emulateMedia({reducedMotion: 'reduce'})
  expect(await page.locator('.masterTradeSkeleton').first().evaluate(element => getComputedStyle(element).animationName)).toBe('none')
  expect(mutations).toEqual([])
})