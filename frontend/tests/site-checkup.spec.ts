import {expect, test, type Page} from '@playwright/test'
import deployment from '../../vercel.json' with {type: 'json'}
import {mockAssistant} from './helpers/assistant-api'

const production = 'http://127.0.0.1:4176'
const root = production

async function mockSite(page: Page) {
  const state = await mockAssistant(page)
  await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'cookie-session:assistant-member'))
  return state
}

test('Production fonts load under the unchanged deployment CSP without data-font violations', async ({page}) => {
  const policy = deployment.headers[0].headers.find(header => header.key === 'Content-Security-Policy')?.value
  expect(policy).toBeTruthy()
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  await page.route(`${production}/terms`, async route => {
    const response = await route.fetch()
    await route.fulfill({response, headers: {...response.headers(), 'content-security-policy': String(policy)}})
  })
  await page.addInitScript(() => {
    const violations: Array<{directive: string; uri: string}> = []
    Object.defineProperty(window, 'siteCheckupCsp', {value: violations})
    document.addEventListener('securitypolicyviolation', event => violations.push({directive: event.effectiveDirective, uri: event.blockedURI}))
  })
  await page.goto(`${production}/terms`)
  const result = await page.evaluate(async () => {
    const fonts = await document.fonts.load('400 14px "Plus Jakarta Sans Variable"', '\u0462')
    return {loaded: fonts.length, sources: fonts.map(font => font.status), violations: Reflect.get(window, 'siteCheckupCsp') as Array<{directive: string; uri: string}>}
  })
  expect(result.loaded).toBeGreaterThan(0)
  expect(result.sources.every(status => status === 'loaded')).toBe(true)
  expect(result.violations.filter(event => event.directive === 'font-src')).toEqual([])
})

for (const path of ['/billing', '/pricing', '/master-trade']) {
  test(`Home button clears ${path} route and reload remains on dashboard without mutations`, async ({page}) => {
    await page.route('https://**', route => route.abort())
    await page.routeWebSocket('wss://**', socket => socket.close())
    const state = await mockSite(page)
    await page.goto(`${root}${path}`)
    await expect(page.locator('.homeBackButton')).toBeVisible()
    const mutations = state.requests.filter(request => !request.startsWith('GET '))
    await page.locator('.homeBackButton').click()
    await expect(page).toHaveURL(`${root}/`)
    await expect(page.locator('.v26DashboardChoices')).toBeVisible()
    expect(state.requests.filter(request => !request.startsWith('GET '))).toEqual(mutations)
    await page.reload()
    await expect(page.locator('.v26DashboardChoices')).toBeVisible()
  })
}

const activeSubscription = {
  status: 'ACTIVE', plan: 'MASTER_MODE', master_trade_access: true,
  current_period_end: '2027-01-01T00:00:00Z', next_payment_amount: 119.9,
}

test('Billing Master Trade shortcut reaches the correct protected workspace without mutations', async ({page}) => {
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  const state = await mockSite(page)
  await page.route('**/api/v22/subscription', route => route.fulfill({json: activeSubscription}))
  await page.goto(`${root}/billing`)
  await page.getByRole('button', {name: 'GO TO MASTER TRADE', exact: true}).click()
  await expect(page).toHaveURL(`${root}/master-trade`)
  await expect(page.locator('.v26ModeBar h1')).toHaveText('Master Trade')
  expect(state.requests.filter(request => !request.startsWith('GET '))).toEqual([])
})

test('Unavailable billing data is explicit, never fabricated as expired, and can be retried', async ({page}) => {
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  await mockSite(page)
  let unavailable = true
  await page.route('**/api/v22/subscription', route => route.fulfill(unavailable
    ? {status: 503, json: {detail: 'Subscription storage unavailable'}}
    : {json: activeSubscription}))
  await page.goto(`${root}/billing`)
  await expect(page.locator('.subscriptionFeedbackError')).toContainText('Subscription storage unavailable')
  await expect(page.locator('.subscriptionStatusCard')).not.toContainText('EXPIRED')
  await expect(page.locator('.subscriptionStatusCard')).not.toContainText('No active plan')
  unavailable = false
  await page.getByRole('button', {name: 'Retry subscription', exact: true}).click()
  await expect(page.locator('.subscriptionStatusCard')).toContainText('Master Mode')
  await expect(page.locator('.subscriptionStatusCard')).toContainText('ACTIVE')
  await expect(page.locator('.subscriptionFeedbackError')).toHaveCount(0)
  await expect(page.locator('.subscriptionHistoryPanel')).not.toContainText('No billing activity yet')
})

test('Checkout failure is visible and cookie-session metadata never becomes an authorization token', async ({page}) => {
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  await mockSite(page)
  await page.route('**/api/v22/subscription', route => route.fulfill({json: activeSubscription}))
  const checkouts: Array<{headers: Record<string, string>; body: unknown}> = []
  await page.route('**/api/v22/subscription/checkout', async route => {
    checkouts.push({headers: route.request().headers(), body: route.request().postDataJSON()})
    await route.fulfill({status: 503, json: {detail: 'Payment provider unavailable'}})
  })
  await page.goto(`${root}/pricing`)
  await page.getByRole('button', {name: 'START MASTER MODE', exact: true}).click()
  await expect(page.locator('.subscriptionFeedbackError')).toHaveText('Payment provider unavailable')
  await expect(page).toHaveURL(`${root}/pricing`)
  expect(checkouts).toHaveLength(1)
  expect(checkouts[0].body).toEqual({plan: 'MASTER_MODE', billing_interval: 'monthly'})
  expect(checkouts[0].headers['x-requested-with']).toBe('XMLHttpRequest')
  expect(checkouts[0].headers.authorization).toBeUndefined()
})

test('Malformed subscription information is an explicit error, not a fabricated entitlement', async ({page}) => {
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  await mockSite(page)
  await page.route('**/api/v22/subscription', route => route.fulfill({json: {}}))
  await page.goto(`${root}/billing`)
  await expect(page.locator('.subscriptionFeedbackError')).toContainText('invalid subscription information')
  await expect(page.locator('.subscriptionStatusCard')).toContainText('Unavailable')
  await expect(page.locator('.subscriptionStatusCard')).not.toContainText('No active plan')
  await expect(page.getByRole('button', {name: 'GO TO MASTER TRADE', exact: true})).toHaveCount(0)
})

const workspaces = [
  {button: 'İŞLEM', heading: 'İşlem Masası', selector: '.binanceDemoDeck'},
  {button: 'ANALİST', heading: 'Analyst', selector: '.coinAnalysisCenter'},
  {button: 'TARAMA', heading: 'Scanner', selector: '.scannerCenter'},
  {button: 'PERFORMANS', heading: 'Performance', selector: '.binanceDemoDeck'},
  {button: 'RİSK', heading: 'Risk Kasası', selector: '.riskOnlyDeck'},
  {button: 'CANLI', heading: 'Gerçek Futures Hazırlık Merkezi', selector: '.liveTradingPanel'},
  {button: 'OPERASYON', heading: 'Bulut Operasyon ve Kanıt Merkezi', selector: '.v27Ops'},
  {button: 'AYARLAR', heading: 'Sunucu ve Anahtar Kapıları', selector: '.connectionCenter'},
]

for (const workspace of workspaces) {
  test(`${workspace.button} dashboard shortcut renders its workspace and returns safely`, async ({page}) => {
    await page.route('https://**', route => route.abort())
    await page.routeWebSocket('wss://**', socket => socket.close())
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    const state = await mockSite(page)
    await page.goto(`${root}/`)
    await page.locator('.v26DashboardChoices button').filter({has: page.locator('b', {hasText: new RegExp(`^${workspace.button}$`)})}).click()
    await expect(page.locator('.v26ModeBar h1')).toHaveText(workspace.heading)
    await expect(page.locator(workspace.selector)).toBeVisible()
    await expect(page.locator('.v26Loading')).toHaveCount(0)
    await page.locator('.homeBackButton').click()
    await expect(page.locator('.v26DashboardChoices')).toBeVisible()
    expect(errors).toEqual([])
    expect(state.requests.filter(request => !request.startsWith('GET '))).toEqual([])
  })
}
