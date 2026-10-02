import {expect, test, type Page} from '@playwright/test'
import {join} from 'node:path'

test.use({baseURL: 'http://127.0.0.1:4174'})
test.setTimeout(60000)

const marketRow = {
  symbol: 'BTCUSDT', display: 'BTC/USDT', price: 60000, change: 1, volume: 1000000,
  volume_ratio: 1.3, volume_change_pct: 10, volatility_pct: 2, rsi: 58,
  ema20: 59900, ema50: 59800, ema200: 59000, trend: 'Yükseliş', direction: 'LONG',
  confidence: 85, smart_score: 85, opportunity_score: 85, final_decision_score: 85,
  mtf_direction: 'LONG', mtf_alignment: 100, mtf_timeframes: [{timeframe: '15m', direction: 'LONG', confidence: 85}], anomaly: null,
}

async function mockMember(page: Page, premium = false, initialRemaining = 87) {
  const requests: string[] = []
  let remaining = initialRemaining
  let consumed = false
  await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'member-ui-test-session'))
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    requests.push(`${request.method()} ${path}`)
    const user = {id: premium ? 'premium-member' : 'free-member', role: 'CUSTOMER', active: true, email_verified: true}
    const resetsAt = new Date(Date.now() + 24 * 3600000).toISOString()
    if (path === '/api/analyst/consume') {
      if (remaining === 0 && !consumed && !premium) {
        await route.fulfill({status: 429, json: {remaining: 0, total: 100, resetsAt, detail: 'Günlük analiz kredileri tükendi.'}})
        return
      }
      const cached = consumed
      if (!consumed && !premium) remaining--
      consumed = true
      await route.fulfill({json: {
        result: {...marketRow, entry: 987654.32, stop_loss: 876543.21, tp1: 999111.22, tp2: 1000222.33, tp3: 1000333.44, support: 59000, resistance: 61000, risk_reward: 2, risk_pct: 1, potential_tp3_pct: 5},
        remaining: premium ? null : remaining, total: 100, resetsAt: premium ? null : resetsAt,
        unlimited: premium, cached, cacheExpiresAt: new Date(Date.now() + 15 * 60000).toISOString(),
      }})
      return
    }
    const fixtures: Record<string, unknown> = {
      '/api/v22/profile': {user, access: {canAccessMasterTrade: true, isPremium: premium}},
      '/api/v22/session': {user},
      '/api/analyst/credits': {remaining: premium ? null : remaining, total: 100, resetsAt: premium ? null : resetsAt, unlimited: premium},
      '/api/markets': [{...marketRow, status: 'TRADING', contractType: 'PERPETUAL', quoteAsset: 'USDT'}],
      '/api/analysis-universe': {results: [marketRow]},
      '/api/v25/status': {connected: false, real_trading_locked: true, execution_state: 'LOCKED', armed: false, live_auto_trade: false, recovery_ready: false, credentials: {configured: false}, stream: {status: 'DISCONNECTED'}, policy: {allowed_symbols: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT']}, scanner: {scanned_symbol_count: 0, candidate_count: 0}, account: {}, plans: [], events: []},
      '/api/exchange-connections/status': {vault: {ready: true}, connections: {LIVE: {configured: false, active: false}}},
      '/api/v25/history': {external_trades: [], external_income: []},
      '/api/v21/journal': {items: []},
      '/api/v21/performance': {total_trades: 0, wins: 0, losses: 0, win_rate: 0, total_profit: 0, total_loss: 0, net_profit: 0, average_trade: 0, best_trade: 0, worst_trade: 0, profit_factor: null, average_win: null, average_loss: null, losing_streak: 0, max_drawdown: 0, history_quality: 'EMPTY'},
      '/api/scanner-alerts': {alerts: []},
    }
    const body = path.startsWith('/api/analysis/') ? marketRow
      : path.startsWith('/api/klines/') ? Array.from({length: 160}, (_, index) => ({time: 1790185500 + index * 900, open: 60000, high: 60100, low: 59900, close: 60000, volume: 1000}))
      : fixtures[path] ?? {}
    await route.fulfill({status: request.method() === 'GET' ? 200 : 403, json: body})
  })
  return requests
}

for (const width of [1440, 390]) {
  test(`Free Master Trade premium card and safe previews at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 900})
    const requests = await mockMember(page)
    await page.goto('/master-trade?tab=canli')
    await expect(page.getByRole('heading', {name: 'LIVE AUTO TRADE', exact: true})).toBeVisible()
    const locked = page.locator('[data-premium-locked]').filter({hasText: 'START LIVE AUTO TRADE'}).getByRole('button').first()
    await expect(locked).toBeEnabled()
    await locked.click()
    const modal = page.getByRole('dialog')
    await expect(modal).toBeVisible()
    await expect(modal.getByRole('listitem')).toHaveCount(3)
    await expect(modal.getByRole('link', {name: "Premium'a geç"})).toHaveAttribute('href', '/pricing')
    const bounds = await modal.boundingBox()
    expect(bounds).not.toBeNull()
    if (width < 768) {
      expect(bounds!.y + bounds!.height).toBeCloseTo(900, 0)
      expect(bounds!.width).toBeCloseTo(width, 0)
    }
    else expect(bounds!.x + bounds!.width / 2).toBeCloseTo(width / 2, 0)
    await page.screenshot({path: join(process.env.MEMBER_UI_SCREENSHOTS || testInfo.outputDir, `premium-${width}.png`)})
    await modal.getByRole('button', {name: 'Şimdi değil'}).click()
    await expect(modal).not.toBeVisible()
    await expect(locked).toBeFocused()
    await locked.click()
    await expect(page.locator('.premiumToast')).toContainText('Premium üyelik gerekli.')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page.locator('.liveUxManual input')).toHaveCount(0)
    await expect(page.locator('.triggerMonitor')).toHaveCount(0)
    const html = await page.locator('.masterTradePage').innerHTML()
    expect(html).not.toContain('987654')
    expect(requests.filter(request => request.includes('/analyst/'))).toEqual([])
    expect(requests.filter(request => !request.startsWith('GET '))).toEqual([])
  })
}

async function openAnalyst(page: Page) {
  await page.goto('/')
  await page.getByRole('button', {name: /^ANALİST Piyasa zekâsı/}).click()
  await expect(page.locator('.analystWorkspaceHeader')).toBeVisible()
  await expect(page.locator('.analystSelectorList > button').first()).toBeVisible()
}

test('Analyst purchase updates the member budget and cache reopening is free', async ({page}, testInfo) => {
  await page.setViewportSize({width: 1440, height: 1000})
  const requests = await mockMember(page)
  await openAnalyst(page)
  await expect(page.locator('.analystCreditBadge')).toContainText('Kredi 87/100')
  await page.locator('.analystSelectorList > button').first().click()
  await expect(page.locator('.analystCreditBadge')).toContainText('Kredi 86/100')
  await page.locator('.analystPromptChips button').filter({hasText: 'desteği nerede'}).click()
  await expect(page.locator('.analystCached')).toHaveText('Ücretsiz (son 15 dk)')
  await expect(page.locator('.analystCreditBadge')).toContainText('Kredi 86/100')
  await expect(page.locator('.analystResponseText')).toContainText('59.000')
  await page.locator('.analystWorkspaceHeader').scrollIntoViewIfNeeded()
  await page.screenshot({path: join(process.env.MEMBER_UI_SCREENSHOTS || testInfo.outputDir, 'analyst-credits.png')})
  expect(requests.filter(request => request === 'POST /api/analyst/consume')).toHaveLength(2)
})

for (const width of [1280, 390]) {
test(`Using the last Analyst credit removes private content at ${width}px`, async ({page}, testInfo) => {
  await page.setViewportSize({width, height: 900})
  const requests = await mockMember(page, false, 1)
  await openAnalyst(page)
  await expect(page.locator('.analystCreditBadge')).toHaveClass(/creditLow/)
  await page.locator('.analystSelectorList > button').first().click()
  await expect(page.locator('.analystCreditBadge')).toContainText('Kredi 0/100')
  await expect(page.locator('.analystCreditBadge')).toHaveClass(/creditEmpty/)
  expect(await page.locator('.analystCreditBadge strong').evaluate(element => element.getBoundingClientRect().height)).toBeLessThanOrEqual(22)
  expect(await page.locator('.analystAutoScan span').evaluate(element => element.getBoundingClientRect().height)).toBeLessThanOrEqual(20)
  await expect(page.locator('.analystCreditExhausted').first()).toContainText('Krediler')
  await expect(page.locator('.analystCreditExhausted').getByRole('link', {name: "Premium'a geç"}).first()).toBeVisible()
  await expect(page.locator('.analystSelectorList')).toBeVisible()
  const html = await page.locator('.coinAnalysisCenter').innerHTML()
  for (const secret of ['987654.32', '987.654,32', '876543.21', '876.543,21', '999.111,22']) expect(html).not.toContain(secret)
  await page.locator('.analystWorkspaceHeader').scrollIntoViewIfNeeded()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1)
  await page.screenshot({path: join(process.env.MEMBER_UI_SCREENSHOTS || testInfo.outputDir, `analyst-exhausted-${width}.png`)})
  expect(requests.filter(request => request === 'POST /api/analyst/consume')).toHaveLength(1)
})
}

test('Premium Analyst has no credit badge or budget polling', async ({page}) => {
  const requests = await mockMember(page, true)
  await openAnalyst(page)
  await page.locator('.analystSelectorList > button').first().click()
  await expect.poll(() => requests.filter(request => request === 'POST /api/analyst/consume').length).toBe(1)
  await expect(page.locator('.analystCreditBadge')).toHaveCount(0)
  await expect(page.locator('.analystCreditExhausted')).toHaveCount(0)
  expect(requests.filter(request => request.includes('/analyst/credits'))).toEqual([])
})

test('Server entitlement refresh removes premium controls without reloading Master Trade', async ({page}) => {
  const requests = await mockMember(page, true)
  await page.goto('/master-trade?tab=canli')
  await expect.poll(() => page.locator('.liveUxManual input').count()).toBeGreaterThan(0)
  await page.route('**/api/v22/profile', route => route.fulfill({json: {
    user: {id: 'premium-member', role: 'CUSTOMER', active: true, email_verified: true},
    access: {canAccessMasterTrade: true, isPremium: false},
  }}))
  await page.evaluate(() => window.dispatchEvent(new Event('protrebot-access-refresh')))
  await expect(page.locator('.liveUxManual input')).toHaveCount(0)
  await expect(page.locator('[data-premium-locked]').filter({hasText: 'START LIVE AUTO TRADE'}).first()).toBeVisible()
  expect(requests.filter(request => request.includes('/analyst/'))).toEqual([])
})

test('Expired Analyst cache is removed from the DOM without an automatic credit charge', async ({page}) => {
  await page.clock.install()
  const requests = await mockMember(page)
  await openAnalyst(page)
  await page.locator('.analystSelectorList > button').first().click()
  await expect(page.locator('.analystCreditBadge')).toContainText('Kredi 86/100')
  expect(await page.locator('.coinAnalysisCenter').innerHTML()).toContain('987.654,32')
  await page.clock.fastForward(16 * 60000)
  await expect.poll(async () => (await page.locator('.coinAnalysisCenter').innerHTML()).includes('987.654,32')).toBe(false)
  expect(requests.filter(request => request === 'POST /api/analyst/consume')).toHaveLength(1)
})
