import {expect, test} from '@playwright/test'
import {join} from 'node:path'

test.use({baseURL: 'http://127.0.0.1:4174'})
test.setTimeout(60000)

for (const width of [1440, 1024, 390]) {
  test(`Canlı İşlem presentation at ${width}px preserves locked actions`, async ({page}, testInfo) => {
    const mutations: string[] = []
    await page.setViewportSize({width, height: 900})
    await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'live-ui-test-session'))
    await page.route('**/api/**', async route => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      if (request.method() !== 'GET' && !path.endsWith('/risk/preview')) {
        mutations.push(`${request.method()} ${path}`)
        await route.fulfill({status: 405, json: {detail: 'Read-only UI fixture'}})
        return
      }
      const user = {id: 'live-ui-owner', role: 'OWNER', email: 'owner@example.com', display_name: 'UI Owner', active: true, email_verified: true}
      const fixtures: Record<string, unknown> = {
        '/api/v22/profile': {access: {canAccessMasterTrade: true}, user},
        '/api/v22/session': {user},
        '/api/markets': [{symbol: 'BTCUSDT', display: 'BTC/USDT', price: 87299.64377872791, change: 1.2, volume: 1000000, status: 'TRADING', contractType: 'PERPETUAL', quoteAsset: 'USDT'}],
        '/api/analysis-universe': {results: []},
        '/api/v25/status': {
          connected: false, real_trading_locked: true, execution_state: 'LOCKED', armed: false,
          live_auto_trade: false, recovery_ready: true, credentials: {configured: false},
          stream: {status: 'DISCONNECTED'}, readiness: {ready: false, gates: [
            {key: 'risk', passed: false}, {key: 'protection', passed: true},
          ]},
          policy: {allowed_symbols: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT']},
          scanner: {scanned_symbol_count: 0, candidate_count: 0, last_skip_reason: 'no_credentials'},
          events: [], account: {},
        },
        '/api/exchange-connections/status': {vault: {ready: true}, connections: {LIVE: {configured: false, active: false}}},
        '/api/v21/journal': {items: []},
        '/api/v21/performance': {total_trades: 0, wins: 0, losses: 0, win_rate: 0, total_profit: 0, total_loss: 0, net_profit: 0, average_trade: 0, best_trade: 0, worst_trade: 0, profit_factor: null, average_win: null, average_loss: null, losing_streak: 0, max_drawdown: 0, history_quality: 'EMPTY'},
        '/api/v25/history': {external_trades: [], external_income: []},
        '/api/v25/risk/preview': {estimated_stop_loss_usdt: 1, stop_distance_pct: 1},
      }
      const body = path.startsWith('/api/analysis/')
        ? {direction: 'SHORT', confidence: 80, entry: 87299.64377872791, stop_loss: 88000.12345, tp1: 86000.98765, tp2: 85000, tp3: 84000}
        : path.startsWith('/api/klines/')
          ? Array.from({length: 160}, (_, index) => ({time: 1790185500 + index * 900, open: 87200, high: 87400, low: 87100, close: 87299.64377872791, volume: 1000}))
          : fixtures[path] ?? {}
      await route.fulfill({json: body})
    })
    await page.goto('/master-trade?tab=canli')
    await expect(page.getByRole('heading', {name: 'LIVE AUTO TRADE', exact: true})).toBeVisible()
    const workspace = page.locator('#master-trade-live-terminal')
    await expect(workspace.locator('.masterTradeStepper button').nth(1)).toBeDisabled()
    await expect(workspace.locator('.masterTradeStepper button').nth(2)).toBeDisabled()
    await expect(workspace.locator('.masterTradeStepper button').nth(3)).toBeDisabled()
    await expect(workspace.locator('.masterTradeLiveAssistantActions').getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toBeDisabled()
    await workspace.locator('.masterTradeFlowCard:not([inert])').evaluateAll(cards => {
      for (const card of cards) (card as HTMLDetailsElement).open = true
    })
    await expect(workspace.locator('.masterTradeLiveAccountMetrics > span')).toHaveCount(10)
    const margin = workspace.locator('.liveUxManual input[type="number"]').first()
    await margin.fill('25')
    await expect(margin).toHaveValue('25')
    await workspace.locator('.liveChoice button').filter({hasText: 'SHORT'}).click()
    await expect(workspace.locator('.liveChoice .selectedShort')).toHaveText('SHORT')
    await expect(workspace.locator('.liveUxSummaryLead')).toContainText('SHORT')
    const gridColumns = (selector: string) => workspace.locator(selector).evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length)
    expect(await gridColumns('.masterTradeLiveAccountMetrics')).toBe(width >= 768 ? 5 : 2)
    expect(await gridColumns('.masterTradeLiveSignalGrid')).toBe(width >= 768 ? 3 : 2)
    expect(await gridColumns('.liveUxOrderGrid')).toBe(width >= 768 ? 2 : 1)
    expect(await gridColumns('.liveUxForm')).toBe(width >= 768 ? 4 : 2)
    expect(await workspace.evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(width >= 768 ? 2 : 1)
    const checkbox = workspace.locator('.masterTradeLiveCheck input')
    expect(await checkbox.evaluate(element => element.getBoundingClientRect().height)).toBe(16)
    expect(await checkbox.evaluate(element => element.getBoundingClientRect().width)).toBe(16)
    expect(await workspace.locator('.masterTradeStepIndex').first().evaluate(element => getComputedStyle(element).borderTopWidth)).toBe('2px')
    expect(await workspace.locator('.masterTradeStepper button').evaluateAll(elements => elements.every(element => element.getBoundingClientRect().height === 64))).toBe(true)
    expect(await workspace.locator('.masterTradeLiveApiGrid input, .liveUxForm input, .liveUxForm select, .masterTradeLiveSetupGrid input, .masterTradeLiveSetupGrid select').evaluateAll(elements => elements.every(element => element.getBoundingClientRect().height === 44))).toBe(true)
    const bounds = await workspace.evaluate(element => {
      const outer = element.getBoundingClientRect()
      return Array.from(element.querySelectorAll<HTMLInputElement | HTMLButtonElement>('input, select, button'))
        .filter(control => control.getClientRects().length && !control.closest('.masterTradeStepper'))
        .filter(control => control.getBoundingClientRect().right > outer.right + 1 || control.getBoundingClientRect().left < outer.left - 1)
        .map(control => ({
          text: control.textContent,
          className: control.className,
          left: control.getBoundingClientRect().left,
          right: control.getBoundingClientRect().right,
        }))
    })
    expect(bounds).toEqual([])
    await expect(workspace.locator('.masterTradeLiveSignalGrid > span').filter({hasText: 'ENTRY'}).locator('b')).toHaveText('87.299,64')
    await expect(workspace.locator('.masterTradeLiveAccountMetrics .masterTradeSkeleton')).toHaveCount(7)
    const accountHeader = await workspace.locator('.masterTradeLiveAccountHud > header').boundingBox()
    const firstMetric = await workspace.locator('.masterTradeLiveAccountMetrics > span').first().boundingBox()
    expect(accountHeader!.y + accountHeader!.height).toBeLessThanOrEqual(firstMetric!.y)
    const markets = workspace.locator('.masterTradeLiveMarketsScroll button')
    expect(await markets.first().evaluate(element => getComputedStyle(element).borderTopWidth)).toBe('1px')
    expect(await markets.first().evaluate(element => getComputedStyle(element).borderColor === getComputedStyle(element).color)).toBe(true)
    await markets.filter({hasText: 'BTC'}).click()
    await expect(markets.filter({hasText: 'BTC'})).toHaveAttribute('aria-pressed', 'false')
    await expect(workspace.locator('.masterTradeLiveMarkets > small')).toHaveText('3 approved')
    await markets.filter({hasText: 'BTC'}).click()
    const screenshotName = `${process.env.LIVE_UI_PHASE || 'after'}-${width === 1440 ? 'desktop' : width === 1024 ? 'tablet' : 'mobile'}.png`
    const screenshotPath = process.env.LIVE_UI_SCREENSHOTS
      ? join(process.env.LIVE_UI_SCREENSHOTS, screenshotName)
      : testInfo.outputPath(screenshotName)
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({path: screenshotPath, fullPage: true, animations: 'disabled'})
    await testInfo.attach(screenshotName, {path: screenshotPath, contentType: 'image/png'})
    if (width === 1440) {
      let armed = false
      let consent = true
      let running = false
      await page.route('**/api/exchange-connections/status', route => route.fulfill({json: {vault: {ready: true}, connections: {LIVE: {configured: true, active: true}}}}))
      await page.route('**/api/v25/status', route => route.fulfill({json: {
        connected: true, credentials: {configured: true}, real_trading_locked: !armed,
        armed, live_auto_trade: running, execution_state: armed ? 'ARMED' : 'LOCKED',
        recovery_ready: true, authorization: {valid: consent}, policy_acknowledged: consent,
        readiness: {ready: true, gates: [{key: 'risk', passed: true}, {key: 'protection', passed: true}]},
        account: {positions: [], open_orders: []}, events: [],
      }}))
      await page.reload()
      await expect(workspace.locator('.masterTradeStepper button').nth(2)).toHaveAttribute('aria-current', 'step')
      await expect(workspace.locator('.masterTradeLiveAssistantActions').getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toBeDisabled()
      armed = true
      consent = false
      await page.reload()
      await expect(workspace.locator('.masterTradeStepper button').nth(3)).toHaveAttribute('aria-current', 'step')
      await expect(workspace.locator('.masterTradeLiveAssistantActions').getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toBeDisabled()
      consent = true
      await page.reload()
      await expect(workspace.locator('.masterTradeLiveAssistantActions').getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toBeEnabled()
      running = true
      await page.reload()
      await expect(workspace.locator('.masterTradeLiveAssistantActions').getByRole('button', {name: 'STOP AUTO TRADE', exact: true})).toBeEnabled()
    }
    expect(mutations).toEqual([])
  })
}
