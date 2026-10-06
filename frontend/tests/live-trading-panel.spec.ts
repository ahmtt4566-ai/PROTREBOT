import {expect, Page, test} from '@playwright/test'

type LiveStatus = {
  live_auto_trade: boolean
  real_trading_locked: boolean
  armed: boolean
  readinessReady: boolean
  policyAcknowledged?: boolean
  authorizationValid?: boolean
  authorizationReason?: string
  interval?: string
}

const statusFixture = (state: LiveStatus) => ({
  live_auto_trade: state.live_auto_trade,
  real_trading_locked: state.real_trading_locked,
  armed: state.armed,
  connected: true,
  recovery_ready: true,
  recovery_error: null,
  reconciliation_required: false,
  execution_state: 'READY',
  emergency: {active: false},
  readiness: {
    ready: state.readinessReady,
    gates: [
      {key: 'policy', label: 'policy', passed: state.policyAcknowledged === true},
      {key: 'risk', label: 'risk', passed: state.readinessReady},
      {key: 'protection', label: 'protection', passed: state.readinessReady},
    ],
  },
  account: {positions: [], open_orders: []},
  plans: [],
  stream: {status: 'CANLI'},
  credentials: {configured: true},
  consent: {active: true, expires_at: '2099-01-01T00:00:00Z'},
  authorization: {valid: state.authorizationValid !== false, expires_at: '2099-01-01T00:00:00Z', reason: state.authorizationReason || 'NONE', scope_match: state.authorizationValid !== false},
  policy_acknowledged: state.policyAcknowledged === true,
  policy: {interval: state.interval || '15m'},
  auto: {enabled: state.live_auto_trade},
})

const openPanelWithStatus = async (page: Page, state: LiveStatus, mutations: string[] = []) => {
  await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'live-state-ui-test-session'))
  await page.routeWebSocket('**/*', socket => socket.close())
  await page.route('**/*', async route => {
    const url = new URL(route.request().url())
    if (!['127.0.0.1', 'localhost'].includes(url.hostname)) {
      await route.abort()
      return
    }
    const path = url.pathname
    if (route.request().method() !== 'GET' && path.startsWith('/api/')) mutations.push(path)
    const user = {id: 'live-state-ui-owner', role: 'OWNER', active: true, email_verified: true}
    if (path === '/api/v22/session' || path === '/api/v22/profile') {
      await route.fulfill({json: {user, access: {canAccessMasterTrade: true, isPremium: true}}})
      return
    }
    if (route.request().url().includes('/v25/status')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(statusFixture(state))})
      return
    }
    if (route.request().url().includes('/exchange-connections/status')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({connections: {LIVE: {configured: true, active: true}}, testnet: {configured: true}})})
      return
    }
    if (path.startsWith('/api/')) {
      await route.fulfill({json: path === '/api/markets' || path.startsWith('/api/klines/') ? [] : {}})
      return
    }
    await route.continue()
  })
  await page.goto('/master-trade')
  await page.getByRole('button', {name: /CANLI HAZIRLIK/}).click()
  await expect(page.getByRole('heading', {name: 'LIVE AUTO TRADE'})).toBeVisible()
}

test('shows SCANNING ONLY while Auto Trade is enabled but execution is locked', async ({page}) => {
  await openPanelWithStatus(page, {live_auto_trade: true, real_trading_locked: true, armed: false, readinessReady: true})

  await expect(page.getByText('SCANNING ONLY', {exact: true}).first()).toBeVisible()
  await expect(page.getByText('SCANNER ACTIVE · LIVE ORDERS LOCKED', {exact: true})).toBeVisible()
})

test('shows RUNNING only when Auto Trade is enabled and execution is unlocked', async ({page}) => {
  await openPanelWithStatus(page, {live_auto_trade: true, real_trading_locked: false, armed: true, readinessReady: true})

  await expect(page.getByText('RUNNING', {exact: true}).first()).toBeVisible()
  await expect(page.getByText('AUTO TRADE IS RUNNING', {exact: true})).toBeVisible()
})

test('shows ARMED before Auto Trade starts when the live lock is released', async ({page}) => {
  await openPanelWithStatus(page, {live_auto_trade: false, real_trading_locked: false, armed: true, readinessReady: true})

  await expect(page.locator('header.masterTradeLiveHeader strong').getByText('ARMED', {exact: true})).toBeVisible()
  await expect(page.getByText('AUTO TRADE IS OFF', {exact: true})).toBeVisible()
})

test('shows OFF when Auto Trade is disabled and the live lock is closed', async ({page}) => {
  await openPanelWithStatus(page, {live_auto_trade: false, real_trading_locked: true, armed: false, readinessReady: false})

  const headline = page.locator('header.masterTradeLiveHeader')
  await expect(headline.getByText('AUTO TRADE IS OFF', {exact: true})).toBeVisible()
  await expect(headline.locator('.masterTradeLiveHeadline > strong')).toHaveText('LOCKED')
  await expect(page.getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toBeDisabled()
})

test('keeps the consent action inactive while scoped authorization is valid', async ({page}) => {
  await openPanelWithStatus(page, {live_auto_trade: false, real_trading_locked: true, armed: false, readinessReady: false, authorizationValid: true})

  await expect(page.getByRole('button', {name: '24 HOUR CONSENT ACTIVE'})).toBeDisabled()
})

test('shows the expiry reason when scoped authorization is invalid', async ({page}) => {
  await openPanelWithStatus(page, {live_auto_trade: false, real_trading_locked: true, armed: false, readinessReady: false, authorizationValid: false, authorizationReason: 'EXPIRED'})

  await expect(page.getByText('24 saatlik izin sona erdi.', {exact: true}).first()).toBeVisible()
  await expect(page.getByRole('button', {name: '24 SAAT İZİN VER'})).toBeEnabled()
})

test('shows policy acknowledgement unchecked until backend state is true', async ({page}) => {
  await openPanelWithStatus(page, {live_auto_trade: false, real_trading_locked: true, armed: false, readinessReady: false, policyAcknowledged: false})

  await expect(page.getByRole('checkbox', {name: 'Acknowledge current risk policy'})).toBeVisible()
  await expect(page.getByRole('checkbox', {name: 'Acknowledge current risk policy'})).not.toBeChecked()
  await expect(page.getByText('POLICY ACKNOWLEDGEMENT', {exact: true})).toBeVisible()
})

test('shows backend-acknowledged policy checked and prevents acknowledging it again', async ({page}) => {
  await openPanelWithStatus(page, {live_auto_trade: false, real_trading_locked: true, armed: false, readinessReady: false, policyAcknowledged: true})
  const checkbox = page.getByRole('checkbox', {name: 'Acknowledge current risk policy'})
  await expect(checkbox).toBeChecked()
  await expect(checkbox).toBeDisabled()
  await expect(page.getByRole('button', {name: 'POLICY ACKNOWLEDGED', exact: true})).toBeDisabled()
  await expect(page.getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toBeDisabled()
})

for (const interval of ['1m', '5m', '1h', '4h']) {
  test(`warns and prevents LIVE Auto Trade start at ${interval}`, async ({page}) => {
    const mutations: string[] = []
    await openPanelWithStatus(page, {
      live_auto_trade: false, real_trading_locked: false, armed: true,
      readinessReady: true, policyAcknowledged: true, interval,
    }, mutations)
    const warning = page.getByTestId('live-auto-timeframe-warning')
    await expect(warning).toBeVisible()
    await expect(warning).toHaveAttribute('role', 'alert')
    await expect(warning).toContainText('yalnızca 15m')
    await expect(warning).toContainText(`Timeframe: ${interval}.`)
    const startButtons = page.getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})
    await expect(startButtons).toHaveCount(2)
    for (const button of await startButtons.all()) await expect(button).toBeDisabled()
    expect(mutations.filter(path => path.endsWith('/auto/start'))).toEqual([])
  })
}

test('15m keeps the existing Auto Trade confirmation flow', async ({page}) => {
  const mutations: string[] = []
  await openPanelWithStatus(page, {
    live_auto_trade: false, real_trading_locked: false, armed: true,
    readinessReady: true, policyAcknowledged: true, interval: '15m',
  }, mutations)
  await expect(page.getByTestId('live-auto-timeframe-warning')).toHaveCount(0)
  const start = page.getByLabel('Auto Trade controls').getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})
  await expect(start).toBeEnabled()
  await start.click()
  await expect(page.getByRole('dialog')).toContainText('REAL MONEY WILL BE USED')
  await expect(page.getByPlaceholder('CANLI OTOMATİK')).toBeVisible()
  await expect(page.getByRole('dialog').getByRole('button', {name: 'CONFIRM', exact: true})).toBeDisabled()
  expect(mutations.filter(path => path.endsWith('/auto/start'))).toEqual([])
})

test('unsaved timeframe changes warn without mutating the saved policy', async ({page}) => {
  const mutations: string[] = []
  await openPanelWithStatus(page, {
    live_auto_trade: false, real_trading_locked: false, armed: true,
    readinessReady: true, policyAcknowledged: true, interval: '15m',
  }, mutations)
  const setup = page.locator('.masterTradeLiveSetup')
  await setup.evaluate(element => {
    const details = element.closest('details')
    if (details) details.open = true
  })
  const timeframe = setup.getByRole('combobox', {name: 'Timeframe', exact: true})
  await timeframe.selectOption('1h')
  await expect(page.getByTestId('live-auto-timeframe-warning')).toBeVisible()
  await expect(page.getByLabel('Auto Trade controls').getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toBeDisabled()
  await timeframe.selectOption('15m')
  await expect(page.getByTestId('live-auto-timeframe-warning')).toHaveCount(0)
  await expect(page.getByLabel('Auto Trade controls').getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toBeEnabled()
  expect(mutations.filter(path => path.endsWith('/policy') || path.endsWith('/auto/start'))).toEqual([])
})

test('selecting 15m does not hide a saved unsupported timeframe before policy save', async ({page}) => {
  await openPanelWithStatus(page, {
    live_auto_trade: false, real_trading_locked: false, armed: true,
    readinessReady: true, policyAcknowledged: true, interval: '1h',
  })
  const setup = page.locator('.masterTradeLiveSetup')
  await setup.evaluate(element => {
    const details = element.closest('details')
    if (details) details.open = true
  })
  await setup.getByRole('combobox', {name: 'Timeframe', exact: true}).selectOption('15m')
  await expect(page.getByTestId('live-auto-timeframe-warning')).toContainText('Timeframe: 1h.')
  await expect(page.getByLabel('Auto Trade controls').getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toBeDisabled()
})

test('unsupported timeframe does not disable stopping an existing Auto Trade session', async ({page}) => {
  await openPanelWithStatus(page, {
    live_auto_trade: true, real_trading_locked: false, armed: true,
    readinessReady: true, policyAcknowledged: true, interval: '1h',
  })
  await expect(page.getByTestId('live-auto-timeframe-warning')).toBeVisible()
  const stopButtons = page.getByRole('button', {name: 'STOP AUTO TRADE', exact: true})
  await expect(stopButtons).toHaveCount(2)
  for (const button of await stopButtons.all()) await expect(button).toBeEnabled()
})