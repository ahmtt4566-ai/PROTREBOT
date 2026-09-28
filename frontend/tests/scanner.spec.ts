import {expect, Page, test} from '@playwright/test'

type Candidate = {
  symbol: string
  direction: string
  score: number
  trend: string
  momentum: string
  rsi: number
  volume_ratio: number
  reasons: string[]
  macd_confirmation: boolean
}

const candles = Array.from({length: 20}, (_, index) => {
  const close = 84000 + index * 10
  return {time: 1790185500 + index * 900, open: close - 5, high: close + 15, low: close - 15, close, volume: 1000 + index}
})

const analysis = {
  direction: 'LONG', confidence: 82, entry: 84200, stop_loss: 83500, tp1: 85000, tp2: 86000, tp3: 87000,
  support: 83000, resistance: 85000, trend: 'BULLISH', momentum: 'POSITIVE', rsi: 58, adx: 24, atr: 300,
  volume_ratio: 1.4, explanation: 'fixture', series: {ema20: [], ema50: [], ema200: []},
}

const candidate: Candidate = {
  symbol: 'BTCUSDT', direction: 'LONG', score: 86, trend: 'BULLISH', momentum: 'POSITIVE', rsi: 58,
  volume_ratio: 1.4, reasons: ['breakout'], macd_confirmation: true,
}

const summary = (candidates: Candidate[] = [candidate]) => ({
  scanner: {
    last_scan_at: '2026-09-28T12:00:00Z', last_scan_timeframe: '15m', last_scan_universe: 'TOP 20',
    top_candidates: candidates.slice(0, 3), all_candidates: candidates, running: false, last_error: null,
  },
})

const openScanner = async (page: Page, scannerSummary = summary(), scanBodies: Record<string, unknown>[] = [], scanResponse = scannerSummary, scanFailure = false) => {
  await page.route('**/api/**', async route => {
    const request = route.request()
    const url = request.url()
    if (url.includes('/v21/scanner/scan')) {
      if (request.method() === 'POST') scanBodies.push(request.postDataJSON() as Record<string, unknown>)
      if (scanFailure) {
        await route.fulfill({status: 502, contentType: 'application/json', body: JSON.stringify({detail: 'Exchange scanner unavailable'})})
      } else {
        await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(scanResponse)})
      }
      return
    }
    if (url.includes('/v21/summary')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(scannerSummary)})
      return
    }
    if (url.includes('/markets')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify([
        {symbol: 'BTCUSDT', display: 'BTC/USDT', price: 84200, change: 1.2, volume: 1000000},
        {symbol: 'ETHUSDT', display: 'ETH/USDT', price: 3200, change: 0.8, volume: 900000},
        {symbol: 'SOLUSDT', display: 'SOL/USDT', price: 140, change: -0.4, volume: 800000},
      ])})
      return
    }
    if (url.includes('/health')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({status: 'ok'})})
      return
    }
    if (url.includes('/exchange-connections/status')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({})})
      return
    }
    if (url.includes('/klines/')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(candles)})
      return
    }
    if (url.includes('/analysis/')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(analysis)})
      return
    }
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({})})
  })
  await page.goto('/')
  await page.getByRole('button', {name: /TARAMA/}).click()
  await expect(page.getByRole('heading', {name: 'MARKET SCANNER', exact: true})).toBeVisible()
}

test('Scanner renders and exposes scan controls', async ({page}) => {
  await openScanner(page)
  await expect(page.getByLabel('TIMEFRAME')).toBeVisible()
  await expect(page.getByLabel('SYMBOL UNIVERSE')).toBeVisible()
  await expect(page.getByLabel('DIRECTION')).toBeVisible()
})

test('SCAN MARKET sends the selected timeframe', async ({page}) => {
  const scanBodies: Record<string, unknown>[] = []
  await openScanner(page, summary(), scanBodies)
  await page.getByLabel('TIMEFRAME').selectOption('1h')
  await page.getByRole('button', {name: 'SCAN MARKET'}).click()
  await expect.poll(() => scanBodies.length).toBe(1)
  expect(scanBodies[0]).toMatchObject({timeframe: '1h', universe: 'TOP 20', symbols: []})
})

test('selects the first real candidate when Scanner opens', async ({page}) => {
  const eth = {...candidate, symbol: 'ETHUSDT'}
  await openScanner(page, summary([eth]))
  await expect(page.getByRole('heading', {name: 'ETHUSDT', exact: true})).toBeVisible()
  await expect(page.getByRole('heading', {name: 'BTCUSDT', exact: true})).toHaveCount(0)
})

test('moves selected symbol to the first visible candidate after filtering', async ({page}) => {
  const eth = {...candidate, symbol: 'ETHUSDT'}
  const sol = {...candidate, symbol: 'SOLUSDT', direction: 'SHORT'}
  await openScanner(page, summary([eth, sol]))
  await page.getByLabel('DIRECTION').selectOption('SHORT')
  await expect(page.getByRole('heading', {name: 'SOLUSDT', exact: true})).toBeVisible()
  await expect(page.getByRole('heading', {name: 'ETHUSDT', exact: true})).toHaveCount(0)
})

test('clears selected symbol, detail, and chart when a scan returns no candidates', async ({page}) => {
  const eth = {...candidate, symbol: 'ETHUSDT'}
  await openScanner(page, summary([eth]), [], summary([]))
  await page.getByRole('button', {name: 'SCAN MARKET'}).click()
  await expect(page.getByRole('heading', {name: 'NO SYMBOL SELECTED', exact: true})).toBeVisible()
  await expect(page.getByText('NO SYMBOL SELECTED · 15M', {exact: true})).toBeVisible()
  await expect(page.getByRole('heading', {name: 'BTCUSDT', exact: true})).toHaveCount(0)
})

test('selecting another scanner row switches detail and chart symbol', async ({page}) => {
  const eth = {...candidate, symbol: 'ETHUSDT'}
  const sol = {...candidate, symbol: 'SOLUSDT'}
  await openScanner(page, summary([eth, sol]))
  await page.getByRole('row', {name: /SOLUSDT/}).click()
  await expect(page.getByRole('heading', {name: 'SOLUSDT', exact: true})).toBeVisible()
  await expect(page.getByText('SOLUSDT · 15M', {exact: true})).toBeVisible()
})

test('CUSTOM opens symbol selection and scans only selected symbols', async ({page}) => {
  const scanBodies: Record<string, unknown>[] = []
  await openScanner(page, summary(), scanBodies)
  await page.getByLabel('SYMBOL UNIVERSE').selectOption('CUSTOM')
  await expect(page.getByText('CUSTOM SYMBOLS')).toBeVisible()
  await expect(page.getByText('SELECT AT LEAST ONE SYMBOL')).toBeVisible()
  await page.getByRole('checkbox', {name: 'BTCUSDT', exact: true}).check()
  await page.getByRole('button', {name: 'SCAN MARKET'}).click()
  await expect.poll(() => scanBodies.length).toBe(1)
  expect(scanBodies[0]).toMatchObject({timeframe: '15m', universe: 'CUSTOM', symbols: ['BTCUSDT']})
})

test('shows the empty state when scanner returns no candidates', async ({page}) => {
  await openScanner(page, summary([]))
  await expect(page.getByText('NO SETUPS FOUND', {exact: true})).toBeVisible()
})

test('shows the generic error state when scanner API fails', async ({page}) => {
  await page.route('**/api/v21/summary', async route => {
    await route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({detail: 'internal error'})})
  })
  await page.route('**/api/markets', async route => {
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify([])})
  })
  await page.route('**/api/health', async route => {
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({status: 'ok'})})
  })
  await page.route('**/api/exchange-connections/status', async route => {
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({})})
  })
  await page.goto('/')
  await page.getByRole('button', {name: /TARAMA/}).click()
  await expect(page.getByText('MARKET DATA UNAVAILABLE', {exact: true})).toBeVisible()
  await expect(page.getByRole('button', {name: 'RETRY'})).toBeVisible()
})

test('shows the real controlled scan error and clears the previous result', async ({page}) => {
  const eth = {...candidate, symbol: 'ETHUSDT'}
  const scanBodies: Record<string, unknown>[] = []
  await openScanner(page, summary([eth]), scanBodies, summary([eth]), true)
  await page.getByRole('button', {name: 'SCAN MARKET'}).click()
  await expect(page.getByRole('alert')).toContainText('Exchange scanner unavailable')
  await expect(page.getByRole('heading', {name: 'NO SYMBOL SELECTED', exact: true})).toBeVisible()
  await expect(page.getByRole('row', {name: /ETHUSDT/})).toHaveCount(0)
})

test('does not issue duplicate requests during one scan', async ({page}) => {
  const scanBodies: Record<string, unknown>[] = []
  await openScanner(page, summary(), scanBodies)
  const scanButton = page.getByRole('button', {name: 'SCAN MARKET'})
  await Promise.all([scanButton.click(), scanButton.click()])
  await expect.poll(() => scanBodies.length).toBe(1)
})

test('Scanner keeps execution controls isolated', async ({page}) => {
  await openScanner(page)
  const body = await page.locator('body').innerText()
  expect(body).not.toMatch(/Demo Otopilot|Demo Emir|DEMO ARM|API Key|SECRET KEY|OPEN TRADE SETUP|auto trade|execute|order/i)
})
