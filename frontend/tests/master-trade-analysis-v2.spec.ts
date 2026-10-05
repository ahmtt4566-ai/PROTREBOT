import {expect, test, type Page} from '@playwright/test'
import {join} from 'node:path'
import {mockAssistant} from './helpers/assistant-api'
import {previewAnalysis, previewCandles, previewMarkets, previewPrice, previewUniverse} from './helpers/master-trade-preview-data'
import {analysisPrice, analysisValue} from '../../master-trade-presentation'
import coinManifest from '../../src/assets/coins/manifest.json' with {type: 'json'}
import freshLogos from '../../src/assets/coins/logo-manifest.json' with {type: 'json'}

test.use({baseURL: 'http://127.0.0.1:4174'})
test.setTimeout(60000)
const NOW = new Date('2026-10-05T12:00:00Z')
const root = (page: Page) => page.locator('.masterReferenceAnalysis')
const panel = (page: Page) => root(page).getByRole('complementary', {name: 'Analysis decision panel'})

async function prepare(page: Page, options: {width?: number; height?: number; universe?: boolean; nativeFrames?: boolean; userId?: string; premium?: boolean; connected?: boolean; running?: boolean; ready?: boolean; unavailable?: boolean; legacy?: boolean; fields?: Record<string, unknown>; levelOverrides?: Partial<ReturnType<typeof previewAnalysis>>} = {}) {
  await page.setViewportSize({width: options.width ?? 1440, height: options.height ?? 900})
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  await page.route('**/api/**', route => route.fulfill({status: 501, json: {detail: 'Offline API fixture missing'}}))
  if (!options.nativeFrames) {
    await page.clock.install({time: NOW})
    await page.clock.pauseAt(NOW)
  }
  await page.emulateMedia({reducedMotion: 'reduce'})
  await mockAssistant(page)
  const premium = options.premium !== false
  const user = {id: options.userId ?? 'analysis-layout-member', role: premium ? 'OWNER' : 'CUSTOMER', active: true, email_verified: true}
  await page.route('**/api/v22/{profile,session}', route => route.fulfill({json: {user, access: {canAccessMasterTrade: true, isPremium: premium}}}))
  const live = {
    connected: options.ready || options.connected || false, real_trading_locked: !options.ready, armed: Boolean(options.ready),
    execution_state: options.ready ? 'ARMED' : 'LOCKED', live_auto_trade: options.running ?? false,
    recovery_ready: true, credentials: {configured: Boolean(options.ready || options.connected)},
    readiness: {ready: Boolean(options.ready), gates: [{key: 'risk', passed: Boolean(options.ready)}, {key: 'protection', passed: true}]},
    authorization: {valid: Boolean(options.ready)}, consent: {active: Boolean(options.ready)}, policy_acknowledged: Boolean(options.ready),
    account: {positions: [], open_orders: []}, plans: [], events: [], ...options.fields,
  }
  await page.route('**/api/v25/status', route => route.fulfill({json: live}))
  await page.route('**/api/exchange-connections/status', route => route.fulfill({json: {connections: {LIVE: {configured: live.connected, active: live.connected}}}}))
  await page.route('**/api/v25/risk/preview', route => route.fulfill({json: {estimated_stop_loss_usdt: 1, stop_distance_pct: 1}}))
  await page.route('**/api/markets**', route => route.fulfill({json: options.unavailable ? [] : options.universe && new URL(route.request().url()).searchParams.get('all') === 'true' ? previewUniverse() : previewMarkets}))
  await page.route('**/api/analysis-universe**', route => route.fulfill({json: {results: options.unavailable ? [] : previewMarkets.map((market, index) => ({...market, direction: 'LONG', confidence: 94 - index, final_decision_score: 94 - index, smart_score: 94 - index}))}}))
  await page.route('**/api/analysis/**', route => route.fulfill({status: options.unavailable ? 503 : 200, json: options.unavailable ? {detail: 'Unavailable'} : {...previewAnalysis(new URL(route.request().url()).pathname.split('/').pop()), ...options.levelOverrides}}))
  await page.route('**/api/klines/**', route => route.fulfill({json: options.unavailable ? [] : previewCandles(new URL(route.request().url()).pathname.split('/').pop(), options.nativeFrames ? Date.now() : NOW.getTime())}))
  const requests: string[] = []
  page.on('request', request => {if (new URL(request.url()).pathname.startsWith('/api/')) requests.push(`${request.method()} ${new URL(request.url()).pathname}`)})
  await page.goto(`/master-trade?tab=analiz${options.legacy ? '&masterLayoutV2=0' : ''}`, {waitUntil: 'domcontentloaded'})
  await expect.poll(async () => {if (!options.nativeFrames) await page.clock.runFor(100); return page.getByRole('tab', {name: 'Analiz', exact: true}).isVisible()}).toBe(true)
  if (!options.nativeFrames) await page.clock.runFor(1000)
  if (!options.unavailable && !options.legacy) await expect(root(page).locator('.refReportSymbol')).toContainText('89%')
  return {requests, live}
}

async function geometry(page: Page) {
  const values = await panel(page).evaluate(element => {
    const css = getComputedStyle(element), box = element.getBoundingClientRect()
    return {x: css.overflowX, y: css.overflowY, width: element.clientWidth, scrollWidth: element.scrollWidth,
      bottom: box.bottom, viewport: window.innerHeight,
      nested: Array.from(element.querySelectorAll<HTMLElement>('*')).filter(child => child.checkVisibility() && ['auto','scroll'].includes(getComputedStyle(child).overflowY) && child.scrollHeight > child.clientHeight).map(child => child.className),
      horizontal: Array.from(element.querySelectorAll<HTMLElement>('*')).filter(child => child.checkVisibility() && child.clientWidth > 0 && child.scrollWidth > child.clientWidth + 1).map(child => child.className)}
  })
  expect(values.x).toBe('hidden')
  expect(values.y).toBe('auto')
  expect(values.scrollWidth).toBeLessThanOrEqual(values.width)
  expect(values.bottom).toBeLessThanOrEqual(values.viewport - 16)
  expect(values.nested).toEqual([])
  expect(values.horizontal).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
}

for (const [width, height] of [[1440, 900], [1848, 900], [1891, 953]]) test(`Reference desktop layout, seven indicators and non-overlapping levels at ${width}px`, async ({page}) => {
  await prepare(page, {width, height})
  await expect(root(page)).toBeVisible()
  for (const card of ['.refReport', '.refAutoTrade', '.refTrigger']) {
    await expect(root(page).locator(card)).toHaveCSS('border-radius', '12px')
    await expect(root(page).locator(card)).toHaveCSS('padding', '16px')
    await expect(root(page).locator(card)).toHaveCSS('background-color', 'rgba(255, 255, 255, 0.03)')
  }
  expect(await root(page).locator('.refWorkspace').evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(3)
  await expect(root(page).locator('.refMetrics > article')).toHaveCount(5)
  await expect(root(page).locator('.refIndicatorGrid > article')).toHaveCount(7)
  await expect(root(page).locator('.decisionMtf')).toHaveCount(0)
  await expect(root(page).locator('.refMtfStrip')).toContainText('5 / 5')
  await expect(root(page).locator('.refMarketRow')).toHaveCount(10)
  await expect(root(page).locator('.refToolbar')).toBeVisible()
  const toolbar = await root(page).locator('.refToolbar > *').evaluateAll(elements => elements.map(element => {
    const box = element.getBoundingClientRect(); return {left: box.left, right: box.right}
  }))
  for (let index = 1; index < toolbar.length; index++) expect(toolbar[index].left).toBeGreaterThanOrEqual(toolbar[index - 1].right)
  await expect(root(page).locator('.refOhlc')).toContainText('O $')
  expect(await root(page).locator('.meter-confidence > i').evaluateAll(elements => elements.every(element => element.getBoundingClientRect().width > 0))).toBe(true)
  const labels = await root(page).locator('.masterTradeChartPills > span').evaluateAll(elements => elements.map(element => {
    const box = element.getBoundingClientRect(); return {top: box.top, bottom: box.bottom, text: element.textContent}
  }).sort((left, right) => left.top - right.top))
  expect(labels).toHaveLength(9)
  for (let index = 1; index < labels.length; index++) expect(labels[index].top).toBeGreaterThanOrEqual(labels[index - 1].bottom + 20)
  const drawing = await root(page).locator('.refChartCanvas').evaluate(element => {
    const svg = element.querySelector('svg')!
    const plotHeight = Number(svg.getAttribute('data-plot-height'))
    const wicks = Array.from(element.querySelectorAll<SVGLineElement>('.refCandle > line')).map(line => line.getBoundingClientRect())
    const pills = Array.from(element.querySelectorAll<HTMLElement>('.masterTradeChartPills > span'))
    return {
      low: Number(svg.getAttribute('data-axis-low')), high: Number(svg.getAttribute('data-axis-high')),
      count: wicks.length, occupancy: (Math.max(...wicks.map(box => box.bottom)) - Math.min(...wicks.map(box => box.top))) / plotHeight,
      bodies: Array.from(element.querySelectorAll('.refCandle > rect:not(.refVolume)')).map(body => body.getBoundingClientRect().width),
      strokes: Array.from(element.querySelectorAll('.refCandle > line')).map(line => getComputedStyle(line).strokeWidth),
      volumes: Array.from(element.querySelectorAll('.refVolume')).map(volume => ({height: volume.getBoundingClientRect().height / plotHeight, opacity: Number(getComputedStyle(volume).opacity)})),
      unclipped: pills.every(pill => pill.scrollWidth <= pill.clientWidth),
      above: pills.filter(pill => pill.dataset.edge === 'above').map(pill => pill.textContent),
      tickGaps: Array.from(element.querySelectorAll<SVGTextElement>('.refPriceTick')).flatMap(tick => pills.map(pill => Math.abs(Number(tick.dataset.priceY) - Number(pill.dataset.labelY)))),
    }
  })
  const candles = previewCandles('BTCUSDT', NOW.getTime()).slice(-drawing.count)
  const low = Math.min(...candles.map(candle => candle.low)), high = Math.max(...candles.map(candle => candle.high))
  expect(drawing.low).toBeCloseTo(low - (high - low) * .08, 7)
  expect(drawing.high).toBeCloseTo(high + (high - low) * .08, 7)
  expect(drawing.occupancy).toBeGreaterThanOrEqual(.65)
  expect(drawing.occupancy).toBeLessThanOrEqual(.70)
  expect(drawing.bodies.every(body => body >= 5 - .01)).toBe(true)
  expect(drawing.strokes.every(stroke => stroke === '1px')).toBe(true)
  expect(drawing.volumes.every(volume => volume.height <= .15 + .001 && volume.opacity < 1)).toBe(true)
  expect(drawing.unclipped).toBe(true)
  expect(drawing.above).toContain(`↑ TP3 ${analysisPrice(previewPrice * 1.015)}`)
  expect(drawing.tickGaps.every(gap => gap > 21)).toBe(true)
  const layout = await root(page).evaluate(element => {
    const box = (selector: string) => element.querySelector(selector)!.getBoundingClientRect()
    const summaries = Array.from(element.querySelectorAll('.refAccordions summary'))
    const fontTargets = Array.from(element.querySelectorAll('.refMetrics span, .refAutoTrade p, .refAutoTrade small, .refTriggerGrid small, .refTriggerGrid strong'))
    const targets = Array.from(element.querySelectorAll('.refTargets > div')).map(row => getComputedStyle(row).backgroundColor)
    return {workspace: box('.refWorkspace').toJSON(), left: box('.refMarkets').toJSON(), right: box('.refRight').toJSON(),
      center: box('.refCenter').toJSON(), chart: box('.refChart').height, mtf: box('.refMtfStrip').height,
      summaries: summaries.map(summary => ({height: summary.getBoundingClientRect().height, icons: summary.querySelectorAll('svg').length})),
      fonts: fontTargets.map(target => Number.parseFloat(getComputedStyle(target).fontSize)), targets,
      background: getComputedStyle(element.closest('main.v26App')!).backgroundColor}
  })
  expect(layout.chart).toBeGreaterThanOrEqual(460)
  expect(layout.mtf).toBe(36)
  for (const column of [layout.left, layout.right, layout.center]) expect(column.bottom).toBeCloseTo(layout.workspace.bottom, 0)
  expect(layout.workspace.bottom).toBeCloseTo(height - 16, 0)
  expect(layout.left.height).toBeCloseTo(layout.right.height, 0)
  expect(layout.summaries.every(summary => summary.height === 50 && summary.icons === 2)).toBe(true)
  expect(layout.fonts.every(font => font >= 12)).toBe(true)
  expect(layout.targets[0]).toBe(layout.targets[1])
  expect(layout.targets[1]).toBe(layout.targets[2])
  expect(layout.targets[3]).not.toBe(layout.targets[2])
  expect(layout.background).toBe('rgb(10, 16, 19)')
  await expect(root(page).locator('.refMtfPills > span')).toHaveCount(5)
  await expect(root(page).locator('.refMarkets')).toHaveCSS('overflow-y', 'hidden')
  await expect(root(page).locator('.refMarketViewport')).toHaveCSS('overflow-y', 'auto')
  await expect(root(page).locator('.refMarkets')).toHaveCSS('overflow-x', 'hidden')
  await geometry(page)
  expect(await panel(page).evaluate(element => element.firstElementChild?.classList.contains('refReport'))).toBe(true)
  await expect(root(page).locator('.refRightStatus')).toHaveCount(0)
  await expect.poll(() => root(page).locator('.refWatchlist img.coinIcon, .refReportSymbol img.coinIcon').count()).toBe(11)
  const icons = await root(page).locator('.refWatchlist .coinIcon, .refReportSymbol .coinIcon').evaluateAll(elements => elements.map(element => {
    const image = element as HTMLImageElement
    return {asset: image.dataset.asset, inline: image.src.startsWith('data:image/svg+xml'), loaded: image.complete && image.naturalWidth > 0, size: image.getBoundingClientRect().width}
  }))
  expect(icons).toHaveLength(11)
  expect(new Set(icons.map(icon => icon.asset)).size).toBe(10)
  expect(icons.every(icon => icon.inline && icon.loaded)).toBe(true)
  expect(icons.slice(0, 10).every(icon => icon.size === 32)).toBe(true)
  expect(icons[10].size).toBe(40)
  const contrast = await root(page).locator('.masterTradeChartPills > span').evaluateAll(elements => {
    const luminance = (color: string) => {
      const channels = color.match(/[\d.]+/g)!.slice(0, 3).map(Number).map(channel => {const value = channel / 255; return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4})
      return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722
    }
    return elements.map(element => {const css = getComputedStyle(element), foreground = luminance(css.color), background = luminance(css.backgroundColor); return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05)})
  })
  expect(contrast.every(ratio => ratio >= 4.5)).toBe(true)
  await expect(root(page).locator('.refTriggerGrid > div')).toHaveCount(4)
  await expect(root(page).locator('.refTriggerGrid > div').nth(2)).toContainText(`${(Math.abs(previewPrice * 1.003 - previewPrice) / previewPrice * 100).toFixed(2)}%`)
  await expect(panel(page).locator('details[open]')).toHaveCount(0)
  const screenshots = process.env.MASTER_ANALYSIS_SCREENSHOTS
  if (screenshots) {
    await page.screenshot({path: join(screenshots, `reference-${width}x${height}.png`), animations: 'disabled'})
    await panel(page).evaluate(element => {element.scrollTop = element.scrollHeight})
    await page.screenshot({path: join(screenshots, `reference-${width}x${height}-right-bottom.png`), animations: 'disabled'})
  }
  await panel(page).locator('details').evaluateAll(elements => elements.forEach(element => element.setAttribute('open', '')))
  await geometry(page)
})

test('Extreme target and stop values remain honest edge pills and never expand the candle scale', async ({page}) => {
  await prepare(page, {levelOverrides: {tp3: previewPrice * 2, stop_loss: previewPrice / 2}})
  const canvas = root(page).locator('.refChartCanvas')
  await expect(canvas.locator('[data-edge="above"]', {hasText: 'TP3'})).toHaveText(`↑ TP3 ${analysisPrice(previewPrice * 2)}`)
  await expect(canvas.locator('[data-edge="below"]', {hasText: 'SL'})).toHaveText(`↓ SL ${analysisPrice(previewPrice / 2)}`)
  await expect(canvas.locator('.chartLevel[data-level="TP3"], .chartLevel[data-level="SL"]')).toHaveCount(0)
  const before = await canvas.locator('svg').first().evaluate(svg => [svg.getAttribute('data-axis-low'), svg.getAttribute('data-axis-high')])
  await root(page).locator('.refIndicators summary').click()
  await root(page).getByRole('button', {name: 'LEVELS', exact: true}).click()
  await expect(canvas.locator('.masterTradeChartPills > span')).toHaveCount(1)
  expect(await canvas.locator('.refPriceTick').count()).toBeGreaterThan(0)
  expect(await canvas.locator('svg').first().evaluate(svg => [svg.getAttribute('data-axis-low'), svg.getAttribute('data-axis-high')])).toEqual(before)
})

for (const connected of [false, true]) test(`Locked navigation preserves all requests and authority, connected=${connected}`, async ({page}) => {
  const {requests, live} = await prepare(page, {connected})
  const toggle = root(page).getByRole('switch', {name: 'Auto Trade', exact: true})
  await expect(toggle).toBeDisabled()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await expect(root(page).locator('.refConnection')).toContainText(connected ? 'CONNECTED' : 'DISCONNECTED')
  if (!connected) await expect(root(page).locator('.refConnection i')).toHaveCSS('background-color', 'rgb(137, 154, 156)')
  const before = [...requests], serialized = JSON.stringify(live)
  await root(page).getByRole('button', {name: 'Auto Trade neden kilitli?'}).click()
  await expect(root(page).locator('.refNotice')).toBeVisible()
  await expect(root(page).locator('.refNotice')).toContainText(connected ? '24 saatlik izin sona erdi.' : 'LIVE account connection is required.')
  for (const label of ['LİMİT','PİYASA']) await expect(root(page).getByRole('button', {name: label, exact: true})).toBeDisabled()
  await root(page).getByRole('button', {name: 'LİMİT neden kilitli?'}).click()
  expect(requests).toEqual(before)
  await root(page).getByRole('button', {name: "Auto Trade'i aç", exact: true}).click()
  await expect(page).toHaveURL(/tab=canli/)
  await expect(page.locator('#master-trade-auto-trade')).toBeFocused()
  await expect(page.locator('#master-trade-auto-trade').getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toBeDisabled()
  await expect(page.locator('.liveConfirm')).toHaveCount(0)
  expect(requests).toEqual(before)
  expect(JSON.stringify(live)).toBe(serialized)
})

test('Running but locked automation never shows a green active switch', async ({page}) => {
  await prepare(page, {running: true})
  await expect(root(page).getByRole('switch')).toHaveAttribute('aria-checked', 'false')
  await expect(root(page).getByRole('switch')).toBeDisabled()
  await expect(root(page).locator('.refAutoTrade')).toContainText('SCANNING ONLY · LOCKED')
})

test('Ready toggle is navigation only even with all existing gates satisfied', async ({page}) => {
  const {requests, live} = await prepare(page, {ready: true})
  await expect(root(page).getByRole('switch')).toBeEnabled()
  const before = [...requests], serialized = JSON.stringify(live)
  await root(page).getByRole('switch').click()
  await expect(page.locator('#master-trade-auto-trade')).toBeFocused()
  expect(requests).toEqual(before)
  expect(JSON.stringify(live)).toBe(serialized)
  await expect(page.locator('.liveConfirm')).toHaveCount(0)
})

for (const fields of [{armed: false}, {consent: {active: false}}, {authorization: {valid: false}}, {policy_acknowledged: false}, {emergency: {active: true}}]) test(`Missing safety gate disables the presentation toggle: ${JSON.stringify(fields)}`, async ({page}) => {
  await prepare(page, {ready: true, running: true, fields})
  await expect(root(page).getByRole('switch')).toBeDisabled()
  await expect(root(page).getByRole('switch')).toHaveAttribute('aria-checked', 'false')
})

test('Non-premium levels remain blurred and open only the existing premium card', async ({page}) => {
  const {requests} = await prepare(page, {premium: false, ready: true})
  await expect(root(page).locator('.refAdvisoryAccordion, .refConditionRow, .refCheckRow, .refScoreGrid, .refCaseTabs')).toHaveCount(0)
  await expect(root(page).getByRole('switch')).toBeDisabled()
  await expect(root(page).locator('.refTargets')).toHaveAttribute('data-locked', 'true')
  await expect(root(page).locator('.refTargets strong').first()).toHaveCSS('filter', 'blur(3px)')
  await expect(root(page).locator('.masterTradeChartPills > span')).toHaveCount(1)
  const before = [...requests]
  await root(page).getByRole('button', {name: 'Olası hedefler · Premium'}).click()
  await expect(page.getByRole('dialog', {name: 'Daha fazlasını aç'})).toBeVisible()
  expect(requests).toEqual(before)
})

test('Actual targets, two-decimal prices and closed advisory accordions remain honest', async ({page}) => {
  await prepare(page)
  const analysis = previewAnalysis()
  expect(await root(page).locator('.refTargets strong').allTextContents()).toEqual([analysis.tp1, analysis.tp2, analysis.tp3, analysis.stop_loss].map(analysisPrice))
  await expect(panel(page).getByText('Waiting', {exact: true})).toHaveCount(1)
  await expect(panel(page).locator('details[open]')).toHaveCount(0)
  for (const title of ['Invalidation', 'Decision timeline', 'Pre-trade check', 'Why this score']) await expect(panel(page).locator('summary').filter({hasText: title})).toHaveCount(1)
  await expect(panel(page)).toContainText('Final Decision does not send orders')
  await expect(panel(page)).toContainText(analysisPrice(previewPrice))
})

const advisory = (page: Page, title: string) => panel(page).locator('.refAdvisoryAccordion').filter({has: page.locator('summary').filter({hasText: title})})

for (const [width, height] of [[1440, 900], [1891, 953]]) test(`Modern advisory components preserve data and keyboard exclusivity at ${width}px`, async ({page}) => {
  const {requests, live} = await prepare(page, {width, height})
  const before = [...requests], serialized = JSON.stringify(live)
  const sections = panel(page).locator('.refAdvisoryAccordion')
  await expect(sections).toHaveCount(6)
  await expect(sections.locator('summary[aria-expanded="false"]')).toHaveCount(6)
  const conditions = advisory(page, 'Trigger conditions')
  await conditions.locator('summary').focus()
  await page.keyboard.press('Enter')
  await expect(conditions).toHaveAttribute('open', '')
  await expect(conditions.locator('summary')).toHaveAttribute('aria-expanded', 'true')
  await expect(conditions.locator('[role="meter"]')).toHaveAttribute('aria-valuenow', '5')
  await expect(conditions.locator('[role="meter"]')).toHaveAttribute('aria-valuemax', '7')
  await expect(conditions.locator('.refConditionRow[data-tone="positive"]')).toHaveCount(5)
  await expect(conditions.locator('.refConditionRow[data-tone="negative"]')).toHaveCount(2)
  expect(await conditions.locator('.refConditionName').allTextContents()).toEqual(['15m Trend', 'Momentum', 'Volume', 'Breakout', 'MTF', 'R/R', 'Freshness'])
  await expect(conditions.locator('.refConditionValue', {hasText: '0.75x'})).toHaveCount(1)
  await expect(conditions.locator('.refConditionValue', {hasText: analysisPrice(previewAnalysis().resistance)})).toHaveCount(1)
  await expect(conditions.locator('summary')).toHaveCSS('outline-style', 'solid')
  await page.keyboard.press('Space')
  await expect(conditions).not.toHaveAttribute('open')
  await expect(conditions.locator('summary')).toHaveAttribute('aria-expanded', 'false')
  const screenshots = process.env.MASTER_ANALYSIS_SCREENSHOTS
  if (screenshots) {
    await panel(page).evaluate(element => {element.scrollTop = element.scrollHeight})
    await page.screenshot({path: join(screenshots, `accordions-${width}x${height}-closed.png`), animations: 'disabled'})
  }
  const titles = ['Trigger conditions', 'Invalidation', 'Decision timeline', 'Pre-trade check', 'Why this score', 'Long / Short case']
  for (const [index, title] of titles.entries()) {
    const section = advisory(page, title)
    await section.locator('summary').click()
    await expect(section.locator('summary')).toHaveAttribute('aria-expanded', 'true')
    await expect(sections.locator('summary[aria-expanded="true"]')).toHaveCount(1)
    await expect(panel(page).locator('details[open]')).toHaveCount(1)
    await expect(section.locator('.refAdvisoryBody')).toBeVisible()
    await expect(section.locator('.refAdvisoryBody p')).toHaveCount(0)
    await section.locator('summary').evaluate(element => {
      const container = element.closest('.refRight')!
      container.scrollTop += element.getBoundingClientRect().top - container.getBoundingClientRect().top - 12
    })
    await geometry(page)
    if (screenshots) await page.screenshot({path: join(screenshots, `accordions-${width}x${height}-open-${index + 1}.png`), animations: 'disabled'})
  }
  const invalidation = advisory(page, 'Invalidation')
  expect(await invalidation.locator('.refInvalidationGrid span').allTextContents()).toEqual(['Support lost', 'MTF conflict', 'Volume deterioration', 'Signal stale'])
  const preTrade = advisory(page, 'Pre-trade check')
  await preTrade.locator('summary').click()
  await expect(preTrade.locator('.refCheckRow')).toHaveCount(10)
  expect(await preTrade.locator('.refCheckCounts > span').allTextContents()).toEqual(['5 PASS', '1 FAIL', '3 N/A', '1 LOCKED'])
  await expect(preTrade.locator('.refCheckRow').filter({hasText: 'Live trading'}).locator('.refStatusChip')).toHaveText('LOCKED')
  await expect(preTrade.locator('.refStatusChip[data-tone="warning"] svg')).toHaveCount(1)
  await expect(preTrade.locator('.refNoOrder')).toHaveText('NO ORDER SENT')
  expect(await preTrade.locator('.refCheckDetail').evaluateAll(elements => elements.every(element => element.getAttribute('title') === element.textContent))).toBe(true)
  const score = advisory(page, 'Why this score')
  await score.locator('summary').click()
  await expect(score.locator('.refScoreTotal strong')).toHaveText('87')
  await expect(score.locator('.refScoreTotal strong')).toHaveCSS('font-size', '28px')
  await expect(score.locator('.refScoreGrid strong').first()).toHaveCSS('font-size', '18px')
  expect(await score.locator('.refScoreGrid small').allTextContents()).toEqual(['Analysis', 'Liquidity', 'Volatility', 'MTF', 'Freshness', 'Risk/Reward'])
  expect(await score.locator('.refScoreGrid strong').allTextContents()).toEqual(['89', '50', '100', '100', '100', '83'])
  await expect(score.locator('.refScoreGrid > div').nth(1)).toHaveAttribute('data-tone', 'warning')
  const cases = advisory(page, 'Long / Short case')
  await cases.locator('summary').click()
  await expect(cases.getByRole('tab', {name: 'LONG', exact: true})).toHaveAttribute('aria-selected', 'true')
  expect(await cases.getByRole('tabpanel').locator('.refCaseItem > span:last-child').allTextContents()).toEqual(['Trend aligned', 'MACD bullish', 'Volume not confirmed', 'MTF supportive'])
  await cases.getByRole('tab', {name: 'LONG', exact: true}).focus()
  await page.keyboard.press('ArrowRight')
  await expect(cases.getByRole('tab', {name: 'SHORT', exact: true})).toBeFocused()
  await expect(cases.getByRole('tab', {name: 'SHORT', exact: true})).toHaveAttribute('aria-selected', 'true')
  await expect(cases.getByRole('tabpanel')).toContainText('MACD not bearish')
  await expect(cases.locator('.refCaseRisks')).toContainText('Weak volume')
  await expect(cases.locator('.refCaseRisks')).toContainText('Volume insufficient')
  await expect(cases.locator('.refCasePanel[hidden]')).toBeHidden()
  expect(requests).toEqual(before)
  expect(JSON.stringify(live)).toBe(serialized)
})

test('Advisory animation is 180ms, reduced motion is instant and SHORT defaults to actual direction', async ({page}) => {
  await prepare(page, {levelOverrides: {direction: 'SHORT', rsi: 42, macd: -11.95, volume_ratio: .3}})
  await page.emulateMedia({reducedMotion: 'no-preference'})
  const section = advisory(page, 'Why this score')
  expect(await section.evaluate(element => getComputedStyle(element, '::details-content').transitionDuration)).toBe('0.18s, 0.18s')
  expect(await page.evaluate(() => CSS.supports('interpolate-size', 'allow-keywords'))).toBe(true)
  await page.clock.resume()
  const animation = await section.evaluate(async element => {
    const initialHeight = Number.parseFloat(getComputedStyle(element, '::details-content').height)
    const duration = Number.parseFloat(getComputedStyle(element, '::details-content').transitionDuration) * 1000
    const start = performance.now()
    element.querySelector('summary')!.click()
    const frames: Array<{time: number; height: number}> = []
    await new Promise<void>(resolve => {
      const sample = () => {
        const time = performance.now() - start
        frames.push({time, height: Number.parseFloat(getComputedStyle(element, '::details-content').height)})
        if (time >= 240) resolve()
        else requestAnimationFrame(sample)
      }
      requestAnimationFrame(sample)
    })
    return {duration, initialHeight, frames}
  })
  expect(animation.duration).toBeGreaterThanOrEqual(150)
  expect(animation.duration).toBeLessThanOrEqual(200)
  const finalHeight = animation.frames.at(-1)!.height
  expect(finalHeight).toBeGreaterThan(animation.initialHeight)
  expect(animation.frames.some(frame => frame.height > animation.initialHeight && frame.height < finalHeight)).toBe(true)
  expect(animation.frames.filter(frame => frame.time >= 210).every(frame => Math.abs(frame.height - finalHeight) < .1)).toBe(true)
  await section.locator('summary').click()
  await page.emulateMedia({reducedMotion: 'reduce'})
  expect(await section.evaluate(element => getComputedStyle(element, '::details-content').transitionDuration)).toBe('0s')
  await expect(section.locator('summary')).toHaveCSS('transition-duration', '0s')
  await expect(section.locator('.refAccordionChevron')).toHaveCSS('transition-duration', '0s')
  await section.locator('summary').click()
  await expect(section.locator('.refScoreGrid > div').nth(1)).toHaveAttribute('data-tone', 'negative')
  await expect(section.locator('.refScoreGrid > div').nth(1).locator('strong')).toHaveText('20')
  const cases = advisory(page, 'Long / Short case')
  await cases.locator('summary').click()
  await expect(cases.getByRole('tab', {name: 'SHORT', exact: true})).toHaveAttribute('aria-selected', 'true')
  await expect(cases.getByRole('tabpanel')).toContainText('MACD bearish')
})

test('Timeline shows multiple real snapshot events newest first without mutating their text', async ({page}) => {
  await prepare(page)
  await page.route('**/api/analysis/**', route => route.fulfill({json: {...previewAnalysis(new URL(route.request().url()).pathname.split('/').pop()), volume_ratio: 1.2}}))
  await page.clock.runFor(31000)
  const section = advisory(page, 'Decision timeline')
  await expect(section.locator('.refDecisionTimeline > li')).toHaveCount(2)
  await section.locator('summary').click()
  const events = await section.locator('.refDecisionTimeline > li').evaluateAll(elements => elements.map(element => ({time: Date.parse(element.querySelector('time')!.getAttribute('datetime')!), message: element.querySelector('span')!.textContent, latest: element.getAttribute('data-latest')})))
  expect(events[0].time).toBeGreaterThan(events[1].time)
  expect(events[0].latest).toBe('true')
  expect(events[1].latest).toBe('false')
  expect(events[0].message).toContain('armed')
  expect(events[1].message).toContain('waiting')
})

test('Empty advisory bodies use placeholders without inventing scores or conditions', async ({page}) => {
  await prepare(page, {unavailable: true})
  for (const title of ['Trigger conditions', 'Why this score', 'Long / Short case']) {
    const section = advisory(page, title)
    await section.locator('summary').click()
    await expect(section.getByRole('img', {name: 'Veri yok'})).toBeVisible()
    await expect(section.locator('[role="meter"], .refScoreTotal')).toHaveCount(0)
  }
})

test('Chart refresh reloads selected candles and all MTF analyses without changing trading authority', async ({page}) => {
  const {requests, live} = await prepare(page, {universe: true})
  await root(page).locator('[data-symbol="ETHUSDT"] .refMarketSelect').click()
  await expect(root(page).locator('.refReportSymbol')).toContainText('ETHUSDT89%')
  await root(page).getByRole('combobox', {name: 'Zaman dilimi'}).selectOption('1h')
  await expect(root(page).locator('.refOhlc')).toContainText('ETHUSDT · 1h')
  await expect(root(page).locator('.refReportSymbol')).toContainText('ETHUSDT89%')
  let respond = () => {}
  const responseReady = new Promise<void>(resolve => {respond = resolve})
  await page.route('**/api/analysis/ETHUSDT?**', async route => {
    await responseReady
    await route.fulfill({json: {...previewAnalysis('ETHUSDT'), confidence: 72}})
  })
  requests.length = 0
  const refresh = root(page).getByRole('button', {name: 'YENİLE', exact: true})
  try {
    await refresh.click()
    await expect.poll(async () => {
      await page.clock.runFor(100)
      return requests.filter(request => request === 'GET /api/klines/ETHUSDT').length
    }).toBe(1)
    await expect(refresh).toBeDisabled()
    await expect(root(page).locator('.refReportSymbol')).toContainText('ETHUSDT89%')
    await expect(root(page).locator('.refChartEmpty')).toHaveCount(0)
  } finally {
    respond()
  }
  await expect(root(page).locator('.refReportSymbol')).toContainText('ETHUSDT72%')
  expect(requests.filter(request => request === 'GET /api/analysis/ETHUSDT')).toHaveLength(6)
  expect(requests.filter(request => /\/api\/(?:analysis|klines)\/BTCUSDT/.test(request))).toEqual([])
  expect(requests.filter(request => /^(POST|PUT|PATCH|DELETE) /.test(request) && !['POST /api/v25/risk/preview', 'POST /api/assistant/proactive/check-in'].includes(request))).toEqual([])
  expect(live.armed).toBe(false)
  expect(live.live_auto_trade).toBe(false)
  expect(live.real_trading_locked).toBe(true)
})

test('Unavailable data explicitly shows no analysis without fictitious report bullets or meters', async ({page}) => {
  await prepare(page, {unavailable: true})
  await expect(root(page).locator('.refDecisionBadge')).toHaveText('Analiz yok')
  await expect(root(page).locator('.refChartEmpty')).toContainText('Bu sembol için yeterli veri yok')
  await expect(root(page).locator('.refChartCanvas svg[aria-label]')).toHaveCount(0)
  await expect(root(page).locator('.refReasons')).toHaveText('—')
  for (const label of ['CONFIDENCE', 'OPPORTUNITY']) {
    const tile = root(page).locator('.masterTradeMetricTile').filter({hasText: label})
    await expect(tile.locator('strong')).toHaveText('—')
    await expect(tile.locator('.masterTradeMetricVisual')).toHaveText('—')
    await expect(tile.locator('.masterTradeMiniMeter')).toHaveCount(0)
  }
})

for (const failure of [
  {name: 'short candle history', status: 422, detail: 'Analiz için yeterli mum verisi yok', message: 'Bu sembol için yeterli veri yok (Analiz için yeterli mum verisi yok).'},
  {name: 'inactive exchange symbol', status: 400, detail: 'Invalid symbol.', message: 'borsada aktif sembol bulunamadı'},
  {name: 'backend unavailable', status: 503, detail: 'Binance Futures sunucusuna ulaşılamadı', message: 'Binance Futures sunucusuna ulaşılamadı'},
  {name: 'preview without fixture', status: 422, detail: 'Önizleme: bu sembol için örnek veri yok', code: 'PREVIEW_DATA_UNAVAILABLE', message: 'Önizleme: bu sembol için örnek veri yok'},
]) test(`Missing data has an honest placeholder and no action badge: ${failure.name}`, async ({page}) => {
  const {requests, live} = await prepare(page, {universe: true})
  await page.route('**/api/analysis/1000PEPEUSDT?**', route => route.fulfill({status: failure.status, json: {detail: failure.detail, ...('code' in failure ? {code: failure.code} : {})}}))
  const watch = root(page).locator('.refMarketUniverse')
  await watch.getByRole('textbox', {name: 'Search markets'}).fill('1000PEPE')
  await page.clock.runFor(150)
  await watch.locator('[data-symbol="1000PEPEUSDT"] .refMarketSelect').click()
  await expect(root(page).locator('.refError')).toContainText(failure.message)
  await expect(root(page).locator('.refChartEmpty')).toContainText(failure.message)
  await expect(root(page).locator('.refDecisionBadge')).toHaveText('Analiz yok')
  await expect(root(page).locator('.refTargets strong')).toHaveText(['—', '—', '—', '—'])
  await expect(root(page).locator('.refChartCanvas svg[aria-label]')).toHaveCount(0)
  await expect(root(page).locator('.refReasons')).toHaveText('—')
  expect(live.armed).toBe(false)
  expect(live.real_trading_locked).toBe(true)
  expect(requests.filter(request => /^(POST|PUT|PATCH|DELETE) /.test(request) && !['POST /api/v25/risk/preview', 'POST /api/assistant/proactive/check-in'].includes(request))).toEqual([])
  await watch.getByRole('textbox', {name: 'Search markets'}).fill('ETHUSDT')
  await page.clock.runFor(150)
  await watch.locator('[data-symbol="ETHUSDT"] .refMarketSelect').click()
  await expect(root(page).locator('.refReportSymbol')).toContainText('ETHUSDT89%')
  await expect(root(page).locator('.refChartEmpty')).toHaveCount(0)
  await expect(root(page).locator('.refError')).toHaveCount(0)
})

test('Missing 4h history is an explicit partial MTF warning, not fabricated confirmation or missing main analysis', async ({page}) => {
  await prepare(page, {universe: true})
  await page.route('**/api/analysis/ETHUSDT?interval=4h', route => route.fulfill({status: 422, json: {detail: 'Analiz için yeterli mum verisi yok'}}))
  const watch = root(page).locator('.refMarketUniverse')
  await watch.locator('[data-symbol="ETHUSDT"] .refMarketSelect').click()
  await expect(root(page).locator('.refReportSymbol')).toContainText('ETHUSDT89%')
  await expect(root(page).locator('.refError')).toContainText('MTF 4h: Bu sembol için yeterli veri yok')
  await expect(root(page).locator('.refMtfPills > span').last()).toContainText('4h —')
  await expect(root(page).locator('.refMtfStrip')).toContainText('4 / 4')
  await expect(root(page).locator('.refMtfPills > span')).toHaveCount(5)
  await expect(root(page).locator('.refDecisionBadge')).not.toHaveText('Analiz yok')
  await expect(root(page).locator('.refChartCanvas svg[aria-label]')).toHaveCount(1)
  await expect(root(page).locator('.refTargets strong').first()).toHaveText(analysisPrice(previewAnalysis('ETHUSDT').tp1))
})

for (const width of [1024, 390]) test(`Responsive reference layout at ${width}px keeps controls and all data reachable`, async ({page}) => {
  await prepare(page, {width})
  await expect(root(page)).toBeVisible()
  await expect(root(page).locator('.refMarketSearch')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  if (width === 390) {
    await expect(root(page).locator('.refIndicatorGrid')).toHaveCSS('overflow-x', 'auto')
    await expect(root(page).locator('.refRight')).toHaveCSS('max-height', 'none')
  } else await expect(root(page).locator('.refMarkets')).toHaveCSS('display', 'flex')
})

test('Explicit masterLayoutV2=0 preserves the original presentation', async ({page}) => {
  await prepare(page, {legacy: true})
  await expect(root(page)).toHaveCount(0)
  await expect(page.locator('.masterTradePage')).toBeVisible()
  await expect(page.locator('.decisionMtf')).toBeVisible()
})

test.describe('Shipping market scroll performance', () => {
for (const [width, height] of [[1440, 900], [1891, 953]]) test(`650-symbol universe stays virtual, lazily loads logos and scrolls at ${width}px`, async ({page}, testInfo) => {
  test.skip(testInfo.project.name !== 'chromium-master-analysis-production', 'The native frame budget targets the shipping build, not development JSX stack instrumentation.')
  const assets = new Set<string>()
  const names = new Set([...coinManifest.map(coin => coin.symbol.toLowerCase()), ...Object.values(freshLogos.assets).map(entry => entry.file.split('/').pop()!.replace(/\.(svg|webp)$/, ''))])
  page.on('request', request => {
    const path = new URL(request.url()).pathname
    const chunk = path.split('/').pop()?.match(/^(.+)-[\w-]{8}\.js$/)?.[1]
    if (/\/assets\/coins\/.*\.svg/.test(path) || chunk && names.has(chunk)) assets.add(path)
  })
  await prepare(page, {width, height, universe: true, nativeFrames: true})
  const watch = root(page).locator('.refMarketUniverse')
  const viewport = watch.locator('.refMarketViewport')
  await expect(watch.locator('.refMarketSearch h2')).toContainText('Tümü · 650')
  const extent = await viewport.evaluate(element => element.clientHeight)
  const bound = Math.ceil(extent / 52) + 5
  expect(await watch.locator('.refMarketRow').count()).toBeLessThanOrEqual(bound)
  expect(await watch.locator('.refMarketRow').evaluateAll(elements => elements.every(element => element.getBoundingClientRect().height === 52))).toBe(true)
  await expect.poll(() => watch.locator('img.coinIcon').count()).toBeGreaterThanOrEqual(10)
  expect(assets.size).toBeGreaterThanOrEqual(10)
  expect(assets.size).toBeLessThanOrEqual(bound + 2)
  const metrics = await viewport.evaluate(async element => {
    const deltas: number[] = [], mounted: number[] = [], baseline: number[] = [], work: number[] = []
    let previous = performance.now()
    for (let index = 0; index < 20; index++) {
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
      const now = performance.now()
      baseline.push(now - previous); previous = now
    }
    const maxScroll = element.scrollHeight - element.clientHeight
    for (let index = 0; index < 60; index++) {
      const start = performance.now()
      element.scrollTop = index / 59 * maxScroll
      work.push(performance.now() - start)
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
      const now = performance.now()
      deltas.push(now - previous); previous = now
      mounted.push(element.querySelectorAll('.refMarketRow').length)
    }
    const sorted = deltas.slice(1).sort((a, b) => a - b)
    return {frames: deltas.length, p95FrameMs: sorted[Math.floor(sorted.length * .95)], maxFrameMs: Math.max(...sorted), maxMounted: Math.max(...mounted), total: 650, nativeClock: /\[native code\]/.test(performance.now.toString()), baselineP95Ms: baseline.sort((a, b) => a - b)[18], maxScrollWriteMs: Math.max(...work)}
  })
  await testInfo.attach('market-scroll-metrics', {body: Buffer.from(JSON.stringify(metrics)), contentType: 'application/json'})
  console.log(`MARKET_SCROLL_${width} ${JSON.stringify(metrics)}`)
  expect(metrics.nativeClock).toBe(true)
  expect(metrics.maxMounted).toBeLessThanOrEqual(bound)
  expect(metrics.p95FrameMs).toBeLessThan(50)
  await expect(watch.locator('[data-symbol="TOKEN0636USDT"]')).toBeVisible()
  await viewport.evaluate(element => {element.scrollTop = 0})
  await expect(watch.locator('[data-symbol="BTCUSDT"]')).toBeVisible()
  await geometry(page)
  const directory = process.env.MASTER_ANALYSIS_SCREENSHOTS
  const greeting = page.getByRole('button', {name: 'Kais AI karşılama balonunu kapat'})
  if (await greeting.isVisible()) await greeting.click()
  if (directory) await page.screenshot({path: join(directory, `master-market-universe-${width}.png`)})
})
})

test('Universe search is debounced, full names work, filters and sorts never invent scanner scores', async ({page}) => {
  await prepare(page, {universe: true})
  const watch = root(page).locator('.refMarketUniverse'), input = watch.getByRole('textbox', {name: 'Search markets'})
  expect(await watch.locator('.refMarketFilters').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
  await input.fill('btc')
  await expect(watch.locator('.refMarketSearch h2')).toContainText('650')
  await page.clock.runFor(149)
  await expect(watch.locator('.refMarketSearch h2')).toContainText('650')
  await page.clock.runFor(1)
  await expect(watch.locator('.refMarketRow')).toHaveCount(2)
  await expect(watch.locator('[data-symbol="BTCDOMUSDT"] .refMarketScore')).toHaveText('—')
  await input.fill('ethereum'); await page.clock.runFor(150)
  await expect(watch.locator('.refMarketRow')).toHaveCount(1)
  await expect(watch.locator('[data-symbol="ETHUSDT"]')).toBeVisible()
  await input.fill('1000'); await page.clock.runFor(150)
  await expect(watch.locator('[data-symbol="1000SHIBUSDT"] .coinIcon')).toHaveAttribute('data-asset', 'SHIB')
  await expect(watch.locator('[data-symbol="1000PEPEUSDT"] .coinIcon')).toHaveAttribute('data-asset', 'PEPE')
  await expect(watch.locator('[data-symbol="1000SHIBUSDT"] .refMarketPrice strong')).toHaveText('$0.01913')
  await input.fill('unknown-absent'); await page.clock.runFor(150)
  await expect(watch.getByText('Sonuç bulunamadı')).toBeVisible()
  await input.fill(''); await page.clock.runFor(150)
  await watch.getByRole('button', {name: 'Skorlu', exact: true}).click()
  await expect(watch.locator('.refMarketSearch h2')).toContainText('Skorlu · 10')
  await expect(watch.locator('.refMarketScore i')).toHaveCount(10)
  for (const [filter, predicate] of [['Yükselenler', (row: ReturnType<typeof previewUniverse>[number]) => row.change > 0], ['Düşenler', (row: ReturnType<typeof previewUniverse>[number]) => row.change < 0]] as const) {
    await watch.getByRole('button', {name: filter, exact: true}).click()
    await expect(watch.locator('.refMarketSearch h2')).toContainText(`${filter} · ${previewUniverse().filter(predicate).length}`)
  }
  await watch.getByRole('button', {name: 'Tümü', exact: true}).click()
  await watch.getByRole('button', {name: 'BTCUSDT favori', exact: true}).focus()
  await watch.locator('.refMarketViewport').evaluate(element => {element.scrollTop = 5000})
  await expect(watch.locator('.refMarketViewport')).toBeFocused()
  for (const sort of ['24s %', 'Hacim', 'A-Z']) {
    await watch.getByRole('combobox', {name: 'Piyasa sıralaması'}).selectOption(sort)
    const rows = previewUniverse().sort((a, b) => sort === 'A-Z' ? a.symbol.localeCompare(b.symbol, 'en') : (sort === '24s %' ? b.change - a.change : b.volume - a.volume) || a.symbol.localeCompare(b.symbol, 'en'))
    await watch.locator('.refMarketViewport').evaluate(element => {element.scrollTop = 0})
    await expect(watch.locator('.refMarketRow').first()).toHaveAttribute('data-symbol', rows[0].symbol)
  }
})

test('Bundled manifest resolves multiplier logos and explicit index proxy without changing the symbol', async ({page}) => {
  const {requests} = await prepare(page, {universe: true})
  const watch = root(page).locator('.refMarketUniverse')
  const input = watch.getByRole('textbox', {name: 'Search markets'})
  await input.fill('1000'); await page.clock.runFor(150)
  for (const [symbol, asset] of [['1000PEPEUSDT', 'PEPE'], ['1000SHIBUSDT', 'SHIB']]) {
    const image = watch.locator(`[data-symbol="${symbol}"] img.coinIcon`)
    await expect(image).toHaveAttribute('data-asset', asset)
    await expect(image).toHaveAttribute('data-logo-source', 'manifest')
    await expect.poll(() => image.evaluate(element => (element as HTMLImageElement).naturalWidth)).toBe(64)
    await expect(image).toHaveAttribute('loading', 'lazy')
    expect(await image.getAttribute('src')).toMatch(/^data:image\/svg\+xml/)
  }
  await input.fill('btc'); await page.clock.runFor(150)
  const index = watch.locator('[data-symbol="BTCDOMUSDT"] img.coinIcon')
  await expect(index).toHaveAttribute('data-asset', 'BTCDOM')
  await expect(index).toHaveAttribute('data-logo-asset', 'BTC')
  await expect(index).toHaveAttribute('title', /dominance index/)
  await watch.locator('[data-symbol="BTCDOMUSDT"] .refMarketSelect').click()
  await expect(root(page).locator('.refReportSymbol')).toContainText('BTCDOMUSDT')
  await expect.poll(() => requests.includes('GET /api/analysis/BTCDOMUSDT')).toBe(true)
  expect(requests.includes('POST /api/v25/auto/start')).toBe(false)
})

test('Failed images descend from generated to legacy to centered avatar without changing authority', async ({page}) => {
  const {requests, live} = await prepare(page)
  const row = root(page).locator('[data-symbol="BTCUSDT"]')
  await expect(row.locator('img.coinIcon')).toHaveAttribute('data-logo-source', 'manifest')
  await row.locator('img.coinIcon').evaluate(element => element.dispatchEvent(new Event('error')))
  await expect(row.locator('img.coinIcon')).toHaveAttribute('data-logo-source', 'legacy')
  await row.locator('img.coinIcon').evaluate(element => element.dispatchEvent(new Event('error')))
  await expect(row.locator('svg.coinIcon[data-fallback]')).toBeVisible()
  await expect(row.locator('svg.coinIcon text')).toHaveAttribute('dominant-baseline', 'central')
  await expect(row.locator('.refMarketSelect')).toHaveAttribute('aria-pressed', 'true')
  expect(live.real_trading_locked).toBe(true)
  expect(requests.filter(request => /^POST .*\/(?:auto|arm|orders|execute|consent)/.test(request))).toEqual([])
})

test('Synthetic performance symbols are labeled TEST and fixtures never enter the real logo manifest', async ({page}) => {
  await prepare(page, {universe: true})
  const watch = root(page).locator('.refMarketUniverse')
  await expect(watch.getByRole('note')).toContainText('OFFLINE TEST')
  await watch.getByRole('textbox', {name: 'Search markets'}).fill('TOKEN0000')
  await page.clock.runFor(150)
  await expect(watch.locator('[data-symbol="TOKEN0000USDT"] small').first()).toContainText('TEST ·')
  await expect(watch.locator('[data-symbol="TOKEN0000USDT"] svg.coinIcon[data-fallback]')).toBeVisible()
  expect(freshLogos.symbols.every(symbol => !/^TOKEN\d{4}USDT$/.test(symbol))).toBe(true)
})

test('Unavailable local logo module uses the same deterministic avatar in list and report', async ({page}) => {
  let failures = 0
  await page.route('**/src/assets/coins/generated/pepe.svg*', route => {failures++; return route.abort()})
  await prepare(page, {universe: true})
  const watch = root(page).locator('.refMarketUniverse')
  await watch.getByRole('textbox', {name: 'Search markets'}).fill('1000PEPE')
  await page.clock.runFor(150)
  await expect.poll(() => failures).toBeGreaterThan(0)
  const avatar = watch.locator('[data-symbol="1000PEPEUSDT"] svg.coinIcon[data-fallback]')
  await expect(avatar).toBeVisible()
  await expect(avatar.locator('text')).toHaveText('P')
  await watch.locator('[data-symbol="1000PEPEUSDT"] .refMarketSelect').click()
  const report = root(page).locator('.refReportSymbol svg.coinIcon[data-fallback]')
  await expect(report).toHaveAttribute('data-asset', 'PEPE')
  expect(await avatar.locator('circle').getAttribute('fill')).toBe(await report.locator('circle').getAttribute('fill'))
})

test('Market favorites persist only for the authenticated owner and denied storage is explicit', async ({page}) => {
  await prepare(page, {universe: true, userId: 'favorite-owner-a'})
  const watch = root(page).locator('.refMarketUniverse')
  await watch.getByRole('button', {name: 'BTCUSDT favori', exact: true}).click()
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('protrebot:master-market-favorites:favorite-owner-a')!))).toEqual(['BTCUSDT'])
  await watch.getByRole('button', {name: 'Favoriler', exact: true}).click()
  await expect(watch.locator('.refMarketRow')).toHaveCount(1)
  await page.reload({waitUntil: 'domcontentloaded'})
  await expect.poll(async () => {await page.clock.runFor(100); return watch.getByRole('button', {name: 'BTCUSDT favori', exact: true}).isVisible()}).toBe(true)
  await expect(watch.getByRole('button', {name: 'BTCUSDT favori', exact: true})).toHaveAttribute('aria-pressed', 'true')
  await page.route('**/api/v22/{profile,session}', route => route.fulfill({json: {user: {id: 'favorite-owner-b', role: 'OWNER', active: true, email_verified: true}, access: {canAccessMasterTrade: true, isPremium: true}}}))
  await page.reload({waitUntil: 'domcontentloaded'})
  await expect.poll(async () => {await page.clock.runFor(100); return watch.getByRole('button', {name: 'BTCUSDT favori', exact: true}).isVisible()}).toBe(true)
  await expect(watch.getByRole('button', {name: 'BTCUSDT favori', exact: true})).toHaveAttribute('aria-pressed', 'false')
  await page.evaluate(() => {Storage.prototype.setItem = () => {throw new DOMException('Storage denied', 'SecurityError')}})
  await watch.getByRole('button', {name: 'BTCUSDT favori', exact: true}).click()
  await expect(watch.getByRole('alert')).toContainText('Storage denied')
  await expect(watch.getByRole('button', {name: 'BTCUSDT favori', exact: true})).toHaveAttribute('aria-pressed', 'false')
})

test('Selecting a previously unscored symbol uses the existing locked premium analysis flow without orders', async ({page}) => {
  const {requests} = await prepare(page, {universe: true, premium: false})
  const watch = root(page).locator('.refMarketUniverse')
  await watch.getByRole('textbox', {name: 'Search markets'}).fill('TOKEN0636')
  await page.clock.runFor(150)
  await expect(watch.locator('[data-symbol="TOKEN0636USDT"] .refMarketScore')).toHaveText('—')
  await watch.locator('[data-symbol="TOKEN0636USDT"] .refMarketSelect').click()
  await expect(root(page).locator('.refReportSymbol')).toContainText('TOKEN0636USDT')
  await expect.poll(() => requests.includes('GET /api/analysis/TOKEN0636USDT')).toBe(true)
  await expect.poll(() => requests.includes('GET /api/klines/TOKEN0636USDT')).toBe(true)
  await expect(root(page).locator('.refMetrics > article').first()).toContainText('$64.60')
  await expect(watch.locator('.refWatchlist h2')).toContainText('TOKEN0636USDT')
  await expect(root(page).locator('.refAutoTrade')).toContainText('Kilitli')
  await expect(root(page).locator('.refTargets')).toHaveAttribute('data-locked', 'true')
  await expect(root(page).locator('.refTargets strong').first()).toHaveCSS('filter', 'blur(3px)')
  expect(requests.filter(request => /^(POST|PUT|PATCH|DELETE) /.test(request) && !['POST /api/v25/risk/preview', 'POST /api/assistant/proactive/check-in'].includes(request))).toEqual([])
})

test('Bulk quote polling respects stale errors, retry delay and unchanged scanner scope', async ({page}) => {
  const urls: URL[] = []
  page.on('request', request => {if (new URL(request.url()).pathname.startsWith('/api/')) urls.push(new URL(request.url()))})
  await prepare(page, {universe: true})
  const bulk = () => urls.filter(url => url.pathname === '/api/markets' && url.searchParams.get('all') === 'true').length
  const before = bulk()
  await page.route('**/api/markets?all=true', route => route.fulfill({status: 503, headers: {'Retry-After': '120'}, json: {detail: 'Offline outage'}}))
  await page.clock.runFor(3000)
  await expect(root(page).locator('.refMarketUniverse').getByRole('status')).toContainText('HTTP 503')
  await expect(root(page).locator('.refMarketSearch h2')).toContainText('650')
  expect(bulk()).toBe(before + 1)
  await page.clock.runFor(9000)
  expect(bulk()).toBe(before + 1)
  expect(urls.filter(url => url.pathname === '/api/analysis-universe').every(url => url.searchParams.get('limit') === '40')).toBe(true)
  expect(urls.filter(url => url.pathname.startsWith('/api/analysis/')).every(url => url.pathname.endsWith('/BTCUSDT'))).toBe(true)
  expect(urls.some(url => url.pathname === '/api/markets' && url.searchParams.get('limit') === '50')).toBe(true)
  expect(urls.filter(url => url.pathname === '/api/markets').every(url => !url.searchParams.has('symbol'))).toBe(true)
})

test('Bulk updates pause offscreen and never overlap a slow request', async ({page}) => {
  await prepare(page, {universe: true})
  let count = 0
  let release: (() => void) | undefined
  await page.route('**/api/markets?all=true', async route => {
    count++
    await new Promise<void>(resolve => {release = resolve})
    await route.fulfill({json: previewUniverse()})
  })
  await page.evaluate(() => Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}))
  await page.clock.runFor(9000)
  expect(count).toBe(0)
  await page.evaluate(() => {Object.defineProperty(document, 'hidden', {configurable: true, get: () => false}); document.dispatchEvent(new Event('visibilitychange'))})
  await expect.poll(() => count).toBe(1)
  await page.clock.runFor(9000)
  expect(count).toBe(1)
  expect(release).toBeDefined()
  release?.()
  await expect(root(page).locator('.refMarketSearch h2')).toContainText('650')
  await page.route('**/api/markets?all=true', route => route.fulfill({headers: {'X-Market-Stale': '1'}, json: previewUniverse()}))
  await page.clock.runFor(3000)
  await expect(root(page).locator('.refMarketUniverse').getByRole('status')).toContainText('güncel değil')
})

for (const symbol of ['1000PEPEUSDT', '1000SHIBUSDT', 'BTCDOMUSDT']) test(`Non-scanner ${symbol} shares its bulk quote, full report and guarded order symbol`, async ({page}) => {
  const urls: URL[] = []
  page.on('request', request => {if (new URL(request.url()).pathname.startsWith('/api/')) urls.push(new URL(request.url()))})
  const {requests, live} = await prepare(page, {universe: true})
  const rows = previewUniverse()
  const row = rows.find(item => item.symbol === symbol)!
  const price = row.price * 1.0123
  await page.route('**/api/markets?all=true', route => route.fulfill({json: rows.map(item => item.symbol === symbol ? {...item, price, change: -3.75, volume: 987654321} : item)}))
  await page.route(`**/api/analysis/${symbol}?**`, route => route.fulfill({json: {...previewAnalysis(symbol), confidence: 73}}))
  await page.clock.runFor(3000)
  const watch = root(page).locator('.refMarketUniverse')
  await watch.getByRole('textbox', {name: 'Search markets'}).fill(symbol)
  await page.clock.runFor(150)
  await expect(watch.locator(`[data-symbol="${symbol}"] .refMarketScore`)).toHaveText('—')
  await watch.locator(`[data-symbol="${symbol}"] .refMarketSelect`).click()
  await expect(root(page).locator('.refReportSymbol')).toContainText(`${symbol}73%`)
  const priceCard = root(page).locator('.refMetrics > article').first()
  await expect(priceCard).toContainText(analysisPrice(price))
  await expect(priceCard).toContainText('-3.75%')
  await expect(priceCard.getByLabel('24 saat hacim')).toHaveAttribute('title', '987,654,321.00 USDT · 24h')
  await expect(root(page).locator('.refOhlc')).toContainText(`${symbol} · 15m`)
  await expect(root(page).getByLabel(`${symbol} 15m candlestick chart`)).toBeVisible()
  await expect(root(page).locator('.refTargets strong').first()).toHaveText(analysisPrice(previewAnalysis(symbol).tp1))
  await expect(root(page).locator('.refIndicatorGrid')).toContainText('58.00')
  await expect(root(page).locator('.refIndicatorGrid > article').nth(3)).toContainText(analysisValue(previewAnalysis(symbol).macd))
  await expect(root(page).locator('.refMtfStrip')).toContainText('5 / 5')
  const intervals = urls.filter(url => url.pathname === `/api/analysis/${symbol}`).map(url => url.searchParams.get('interval'))
  expect(new Set(intervals)).toEqual(new Set(['1m', '5m', '15m', '1h', '4h']))
  expect(urls.filter(url => url.pathname === '/api/analysis-universe').every(url => url.searchParams.get('limit') === '40')).toBe(true)
  expect(urls.filter(url => url.pathname.startsWith('/api/analysis/')).every(url => ['/api/analysis/BTCUSDT', `/api/analysis/${symbol}`].includes(url.pathname))).toBe(true)
  await root(page).getByRole('tab', {name: 'Canlı İşlem', exact: true}).click()
  await expect(page.locator('.liveUxManual .masterTradeOrderField input').first()).toHaveValue(symbol)
  await expect(page.locator('.liveUxManual')).toHaveAttribute('data-locked', 'true')
  expect(live.armed).toBe(false)
  expect(live.real_trading_locked).toBe(true)
  expect(requests.filter(request => /^(POST|PUT|PATCH|DELETE) /.test(request) && !['POST /api/v25/risk/preview', 'POST /api/assistant/proactive/check-in'].includes(request))).toEqual([])
})

test('Changing coins hides the old report immediately and ignores its late response', async ({page}) => {
  await prepare(page, {universe: true})
  let release: (() => void) | undefined
  let pending = false
  await page.route('**/api/analysis/1000PEPEUSDT?**', async route => {
    if (new URL(route.request().url()).searchParams.get('interval') === '15m') {
      pending = true
      await new Promise<void>(resolve => {release = resolve})
    }
    await route.fulfill({json: {...previewAnalysis('1000PEPEUSDT'), confidence: 61}})
  })
  const watch = root(page).locator('.refMarketUniverse')
  await watch.getByRole('textbox', {name: 'Search markets'}).fill('1000')
  await page.clock.runFor(150)
  await watch.locator('[data-symbol="1000PEPEUSDT"] .refMarketSelect').click()
  await expect.poll(() => pending).toBe(true)
  await expect(root(page).locator('.refReportSymbol')).toContainText('1000PEPEUSDT—')
  await expect(root(page).locator('.refTargets strong').first()).toHaveText('—')
  await expect(root(page).locator('.refOhlc')).not.toContainText('BTCUSDT')
  await expect(root(page).locator('.refMtfStrip')).not.toContainText('5 / 5')
  await watch.getByRole('textbox', {name: 'Search markets'}).fill('1000SHIB')
  await page.clock.runFor(150)
  await watch.locator('[data-symbol="1000SHIBUSDT"] .refMarketSelect').click()
  await expect(root(page).locator('.refReportSymbol')).toContainText('1000SHIBUSDT89%')
  expect(release).toBeDefined()
  release?.()
  await page.clock.runFor(1000)
  await expect(root(page).locator('.refReportSymbol')).toContainText('1000SHIBUSDT89%')
  await expect(root(page).locator('.refTargets strong').first()).toHaveText(analysisPrice(previewAnalysis('1000SHIBUSDT').tp1))
})

for (const width of [1024, 390]) test(`650-symbol universe uses a bounded horizontal strip at ${width}px`, async ({page}) => {
  await prepare(page, {width, universe: true})
  const watch = root(page).locator('.refMarketUniverse'), viewport = watch.locator('.refMarketViewport')
  await expect(viewport).toHaveCSS('overflow-x', 'auto')
  await expect(viewport).toHaveCSS('overflow-y', 'hidden')
  expect(await watch.locator('.refMarketRow').count()).toBeLessThanOrEqual(Math.ceil(await viewport.evaluate(element => element.clientWidth) / 260) + 5)
  await viewport.evaluate(element => {element.scrollLeft = element.scrollWidth})
  await expect(watch.locator('[data-symbol="TOKEN0636USDT"]')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  const positions = await watch.evaluate(element => ({search: element.querySelector('.refMarketSearch')!.getBoundingClientRect().bottom, strip: element.querySelector('.refVirtualWatchlist')!.getBoundingClientRect().top}))
  expect(positions.search).toBeLessThanOrEqual(positions.strip)
})
