import {expect, test, type Page} from '@playwright/test'
import {join} from 'node:path'
import {mockAssistant} from './helpers/assistant-api'

test.use({baseURL: 'http://127.0.0.1:4174'})
test.setTimeout(60000)

const NOW = new Date('2026-10-05T12:00:00Z')
const PRICE = 85360.8
const analysis = {
  direction: 'LONG', confidence: 89, entry: PRICE, stop_loss: PRICE - .02,
  tp1: PRICE + .03, tp2: PRICE + .04, tp3: PRICE + .05,
  support: PRICE - .01, resistance: PRICE + .01,
  risk_reward: 2.5, trend: 'Güçlü yükseliş', momentum: 'POSITIVE',
  rsi: 58, macd: 12, adx: 24, atr: 400, volume_ratio: .5,
}

async function prepare(page: Page, options: {width?: number; flag?: string; connected?: boolean; running?: boolean; unavailable?: boolean; confirmed?: boolean} = {}) {
  await page.setViewportSize({width: options.width ?? 1440, height: 900})
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  await page.clock.install({time: NOW})
  await page.clock.pauseAt(NOW)
  await page.emulateMedia({reducedMotion: 'reduce'})
  await mockAssistant(page)
  const requests: string[] = []
  page.on('request', request => {
    if (new URL(request.url()).pathname.startsWith('/api/')) requests.push(`${request.method()} ${new URL(request.url()).pathname}`)
  })
  const user = {id: 'analysis-layout-owner', role: 'OWNER', active: true, email_verified: true}
  await page.route('**/api/v22/{profile,session}', route => route.fulfill({json: {user, access: {canAccessMasterTrade: true, isPremium: true}}}))
  const live = {
    connected: options.connected ?? false, real_trading_locked: true, armed: false,
    execution_state: 'LOCKED', live_auto_trade: options.running ?? false,
    recovery_ready: true, credentials: {configured: Boolean(options.connected)},
    readiness: {ready: false, gates: []}, authorization: {valid: false},
    account: {positions: [], open_orders: []}, plans: [], events: [],
  }
  await page.route('**/api/v25/status', route => route.fulfill({json: live}))
  await page.route('**/api/exchange-connections/status', route => route.fulfill({json: {connections: {LIVE: {configured: Boolean(options.connected), active: Boolean(options.connected)}}}}))
  await page.route('**/api/v25/risk/preview', route => route.fulfill({json: {estimated_stop_loss_usdt: 1, stop_distance_pct: 1}}))
  await page.route('**/api/markets**', route => route.fulfill({json: options.unavailable ? [] : [{symbol: 'BTCUSDT', display: 'BTC/USDT', price: PRICE, change: 1.2, volume: 1000000}]}))
  await page.route('**/api/analysis-universe**', route => route.fulfill({json: {results: options.unavailable ? [] : [{symbol: 'BTCUSDT', display: 'BTC/USDT', price: PRICE, direction: 'LONG', confidence: 94, final_decision_score: 94, smart_score: 94}]}}))
  await page.route('**/api/analysis/**', route => route.fulfill({status: options.unavailable ? 503 : 200, json: options.unavailable ? {detail: 'Unavailable'} : options.confirmed ? {...analysis, volume_ratio: 1.2, resistance: PRICE - .01} : analysis}))
  await page.route('**/api/klines/**', route => route.fulfill({json: options.unavailable ? [] : Array.from({length: 160}, (_, index) => ({
    time: NOW.getTime() / 1000 - (159 - index) * 900,
    open: PRICE + (index - 159) * .01 - .01,
    high: PRICE + (index - 159) * .01 + 1,
    low: PRICE + (index - 159) * .01 - 1,
    close: PRICE + (index - 159) * .01,
    volume: 1000 + index,
  }))}))
  await page.goto(`/master-trade${options.flag === '' ? '' : `?masterLayoutV2=${options.flag ?? '1'}`}`, {waitUntil: 'domcontentloaded'})
  await expect.poll(async () => {
    await page.clock.runFor(100)
    return page.getByRole('tab', {name: 'Analiz', exact: true}).isVisible()
  }).toBe(true)
  await page.clock.runFor(1000)
  await expect(page.locator('.masterTradeFocusConnection')).toContainText('LOCKED')
  if (!options.unavailable) await expect(page.locator('.masterTradeFinalCard')).toContainText('89%')
  return {requests, live}
}

const panel = (page: Page) => page.getByRole('complementary', {name: 'Analysis decision panel'})

async function assertScrollGeometry(page: Page) {
  const geometry = await panel(page).evaluate(element => {
    const style = getComputedStyle(element)
    const box = element.getBoundingClientRect()
    return {
      overflowX: style.overflowX, overflowY: style.overflowY, overscroll: style.overscrollBehaviorY,
      width: element.clientWidth, scrollWidth: element.scrollWidth, height: element.clientHeight, scrollHeight: element.scrollHeight,
      bottom: box.bottom, viewport: window.innerHeight,
      nested: Array.from(element.querySelectorAll<HTMLElement>('*')).filter(child => {
        const css = getComputedStyle(child)
        return child.checkVisibility() && ['auto', 'scroll'].includes(css.overflowY) && child.scrollHeight > child.clientHeight
      }).map(child => child.className),
      horizontal: Array.from(element.querySelectorAll<HTMLElement>('*')).filter(child =>
        child.checkVisibility() && child.clientWidth > 0 && child.scrollWidth > child.clientWidth + 1).map(child => ({
          className: child.className, tag: child.tagName, text: child.textContent?.slice(0, 100),
          width: child.clientWidth, scrollWidth: child.scrollWidth, whiteSpace: getComputedStyle(child).whiteSpace,
        })),
    }
  })
  expect(geometry.overflowX).toBe('hidden')
  expect(geometry.overflowY).toBe('auto')
  expect(geometry.overscroll).toBe('contain')
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.width)
  expect(geometry.scrollHeight).toBeGreaterThan(geometry.height)
  expect(geometry.bottom).toBeLessThanOrEqual(geometry.viewport - 16)
  expect(geometry.nested).toEqual([])
  expect(geometry.horizontal).toEqual([])
}

for (const width of [1440, 1280]) {
  test(`Restored desktop analysis has one scroll, spaced levels and readable metrics at ${width}px`, async ({page}) => {
    await prepare(page, {width})
    await expect(page.locator('.masterTradePage')).toHaveClass(/masterLayoutV2/)
    await assertScrollGeometry(page)
    const labels = await page.locator('.masterTradeChartPills > span').evaluateAll(elements =>
      elements.map(element => {
        const box = element.getBoundingClientRect()
        return {top: box.top, bottom: box.bottom, text: element.textContent}
      }).sort((left, right) => left.top - right.top))
    expect(labels).toHaveLength(9)
    for (let index = 1; index < labels.length; index++) expect(labels[index].top).toBeGreaterThanOrEqual(labels[index - 1].bottom + 2)
    for (const label of ['TP1', 'TP2', 'TP3', 'RESISTANCE', 'TRIGGER', 'ENTRY', 'SUPPORT', 'SL']) {
      expect(labels.some(item => item.text?.startsWith(label))).toBe(true)
    }
    await expect(page.locator('.watchlistStats')).toContainText('Skor 94%')
    await expect(page.locator('.masterTradeFocusMetric').first()).toContainText('Güven')
    await expect(page.locator('.indicatorGrid')).toContainText('Güçlü yükseliş')
    expect(await page.locator('.masterTradeMetricTile > strong').evaluateAll(elements => elements.every(element =>
      element.scrollWidth <= element.clientWidth && element.scrollHeight <= element.clientHeight))).toBe(true)
    expect(await page.locator('.meter-confidence > i').evaluateAll(elements => elements.every(element => element.getBoundingClientRect().width > 0))).toBe(true)
    const breakdown = panel(page).locator('.decisionBreakdownGrid')
    expect(await breakdown.evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(2)
    const ordered = ['.masterTradeFinalCard', '.decisionSectionGrid .decisionList:first-child', '.decisionSectionGrid .decisionList:nth-child(2)', '.masterTradeCases', '.decisionBreakdown', '.decisionAutoTrade', '.triggerMonitor']
    const positions = await panel(page).evaluate((element, selectors) => selectors.map(selector =>
      element.querySelector(selector)!.getBoundingClientRect().top), ordered)
    expect(positions.every((top, index) => index === 0 || top > positions[index - 1])).toBe(true)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

    const screenshots = process.env.MASTER_ANALYSIS_SCREENSHOTS
    if (screenshots) {
      await page.screenshot({path: join(screenshots, `analysis-${width}-top.png`), animations: 'disabled'})
      await panel(page).locator('details').evaluateAll(elements => elements.forEach(element => element.setAttribute('open', '')))
      await panel(page).evaluate(element => { element.scrollTop = element.scrollHeight })
      await assertScrollGeometry(page)
      await panel(page).locator('details').evaluateAll(elements => elements.forEach(element => element.removeAttribute('open')))
      await panel(page).evaluate(element => { element.scrollTop = element.scrollHeight })
      await page.screenshot({path: join(screenshots, `analysis-${width}-bottom.png`), animations: 'disabled'})
    }
  })
}

test('Trigger prices use two decimals, Waiting appears once and all read-only disclosures remain', async ({page}) => {
  await prepare(page)
  const trigger = panel(page).locator('.triggerMonitor')
  await expect(trigger.getByText('Waiting', {exact: true})).toHaveCount(1)
  await expect(trigger.locator('.triggerSummary strong').first()).toHaveText('$85,360.80')
  await expect(trigger.locator('.triggerSummary strong').nth(1)).toHaveText('$85,360.81')
  for (const title of ['TRIGGER CONDITIONS', 'INVALIDATION', 'DECISION TIMELINE', 'PRE-TRADE CHECK · READ ONLY']) {
    await expect(trigger.locator('summary').filter({hasText: title})).toHaveCount(1)
  }
  await expect(trigger.getByText('NO ORDER SENT', {exact: true})).toHaveCount(1)
  await trigger.locator('details').evaluateAll(elements => elements.forEach(element => element.setAttribute('open', '')))
  await assertScrollGeometry(page)
  await expect(trigger.locator('.triggerConditionGrid')).toContainText('Trigger $85,360.81')
  await expect(trigger.locator('.preTradeCheckGrid')).toContainText('$85,360.78')
})

for (const connected of [false, true]) {
  test(`Auto Trade shortcut navigates and focuses only while locked and connected=${connected}`, async ({page}) => {
    const {requests, live} = await prepare(page, {connected})
    await expect(page.locator('.analysisAutoStatus')).toHaveText('Kapalı')
    await expect(page.locator('.decisionAutoTrade')).toContainText('SEPARATE SAFETY GATES')
    const badge = page.locator('.masterTradeConnectionBadge')
    await expect(badge).toHaveCount(1)
    await expect(badge).toHaveText(`${connected ? 'CONNECTED' : 'DISCONNECTED'} · LOCKED`)
    if (!connected) await expect(badge.locator('i')).toHaveCSS('background-color', 'rgb(137, 152, 170)')
    const beforeRequests = [...requests]
    const beforeStatus = JSON.stringify(live)
    await page.getByRole('button', {name: "Auto Trade'i aç", exact: true}).click()
    await expect(page.getByRole('tab', {name: 'Canlı İşlem', exact: true})).toHaveAttribute('aria-selected', 'true')
    await expect(page).toHaveURL(/masterLayoutV2=1.*tab=canli/)
    const target = page.locator('#master-trade-auto-trade')
    await expect(target).toBeFocused()
    await expect(target).toBeInViewport()
    await expect(target).toHaveCSS('outline-style', 'solid')
    await expect(target.getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toBeDisabled()
    await expect(page.locator('.liveConfirm')).toHaveCount(0)
    expect(requests).toEqual(beforeRequests)
    expect(JSON.stringify(live)).toBe(beforeStatus)
  })
}

test('Already enabled LIVE automation is accurately labelled without changing navigation safety', async ({page}) => {
  const {requests} = await prepare(page, {running: true})
  await expect(page.locator('.analysisAutoStatus')).toHaveText("Live'da açık")
  const before = [...requests]
  await page.getByRole('button', {name: "Auto Trade'e git", exact: true}).click()
  await expect(page.locator('#master-trade-auto-trade')).toBeFocused()
  expect(requests).toEqual(before)
})

test('Confirmed entry preview keeps two-decimal prices and remains advisory only', async ({page}) => {
  const {requests} = await prepare(page, {confirmed: true})
  const trigger = panel(page).locator('.triggerMonitor')
  await expect(trigger.locator('.triggerHeader strong')).toHaveText('CONFIRMED')
  const prices = await trigger.locator('.triggerPreviewGrid strong').allTextContents()
  expect(prices.slice(0, 5)).toEqual(['$85,360.80', '$85,360.78', '$85,360.83', '$85,360.84', '$85,360.85'])
  await expect(trigger.locator('.triggerHeader')).toContainText('NO ORDER SENT')
  expect(requests.filter(request => /^(POST|PUT|PATCH|DELETE)/.test(request) && !request.endsWith('/risk/preview'))).toEqual([])
  await assertScrollGeometry(page)
})

test('Unavailable analysis renders dashes rather than invented values or empty score meters', async ({page}) => {
  await prepare(page, {unavailable: true})
  const meters = page.locator('.masterTradeMetricTile').filter({hasText: /Güven|OPPORTUNITY/})
  await expect(meters).toHaveCount(2)
  for (const meter of await meters.all()) {
    await expect(meter.locator('strong')).toHaveText('—')
    await expect(meter.locator('.masterTradeMetricVisual')).toHaveText('—')
    await expect(meter.locator('.masterTradeMiniMeter')).toHaveCount(0)
  }
  await expect(panel(page).locator('.triggerSummary strong').first()).toHaveText('—')
})

test('Desktop decision panel stays within a short viewport without adding nested scrolls', async ({page}) => {
  await prepare(page)
  await page.setViewportSize({width: 1440, height: 600})
  await assertScrollGeometry(page)
})

for (const options of [{flag: ''}, {flag: '0'}, {width: 1024}]) test(`Legacy layout remains unchanged for ${JSON.stringify(options)}`, async ({page}) => {
  await prepare(page, options)
  await expect(page.locator('.masterTradePage')).not.toHaveClass(/masterLayoutV2/)
  await expect(page.locator('.analysisAutoShortcut')).toHaveCount(0)
})
