import {expect, Page, test} from '@playwright/test'

type LiveStatus = {
  live_auto_trade: boolean
  real_trading_locked: boolean
  armed: boolean
  readinessReady: boolean
  policyAcknowledged?: boolean
  authorizationValid?: boolean
  authorizationReason?: string
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
  policy: {},
  auto: {enabled: state.live_auto_trade},
})

const openPanelWithStatus = async (page: Page, state: LiveStatus) => {
  await page.route('**/*', async route => {
    if (route.request().url().includes('/v25/status')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(statusFixture(state))})
      return
    }
    if (route.request().url().includes('/exchange-connections/status')) {
      await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({connections: {LIVE: {configured: true, active: true}}, testnet: {configured: true}})})
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

  await expect(page.getByText('AUTO TRADE IS OFF', {exact: true})).toBeVisible()
  await expect(page.getByText('OFF', {exact: true}).first()).toBeVisible()
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