import {expect, test, type Page} from '@playwright/test'
test.use({baseURL: 'http://127.0.0.1:4177'})

async function fixture(page: Page, fail = false, malformed = false) {
  await page.routeWebSocket('**', socket => socket.close())
  await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'offline-risk-admin'))
  const reads: string[] = []
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const user = {id: 'owner', role: 'OWNER', display_name: 'Owner', active: true, email_verified: true, email: 'owner@example.test'}
    if (url.pathname === '/api/v22/admin/audit') {
      reads.push(url.search)
      if (fail) {
        await route.fulfill({status: 503, json: {detail: 'Audit storage unavailable'}})
        return
      }
      await route.fulfill({json: {total: 1, items: [{
        id: 1, created_at: malformed ? 'invalid date' : '2026-10-14T08:00:00Z', actor_user_id: 'owner',
        before: {risk_percent: 0.5, daily_loss_percent: 5, warning_level: 'NORMAL', daily_warning_level: 'NORMAL',
          risk_acknowledged: false, high_risk_confirmed: false, warning_balance_usdt: null},
        after: {risk_percent: 10, daily_loss_percent: 25, warning_level: 'RED', daily_warning_level: 'RED',
          risk_acknowledged: false, high_risk_confirmed: true, warning_balance_usdt: 1000},
      }]}})
      return
    }
    const bodies: Record<string, unknown> = {
      '/api/v22/session': {user, access: {isAdmin: true, isPremium: true}},
      '/api/v22/profile': {user, access: {isAdmin: true, isPremium: true}},
      '/api/v22/admin/overview': {customers: [], audit: [], total_users: 0, active_users: 0, verified_users: 0, admins: 1, new_users: 0, pro_users: 0, free_users: 0, active_subscriptions: 0, expired_subscriptions: 0},
      '/api/v22/admin/maintenance': {mode: 'NORMAL', reason: '', updated_at: null, updated_by: null},
      '/api/v22/admin/errors': {items: [], total: 0},
      '/api/v22/admin/system-health': {overall_status: 'OK', checks: [], counts: {}},
    }
    await route.fulfill({json: bodies[url.pathname] ?? {}})
  })
  await page.goto('/admin', {waitUntil: 'domcontentloaded'})
  await page.getByRole('button', {name: 'Trading Center', exact: true}).click()
  return reads
}

test('admin shows old and new risk, warning, acceptance, balance and actor from durable audit', async ({page}) => {
  const reads = await fixture(page)
  const panel = page.getByRole('region', {name: 'Original V2 risk değişiklikleri'})
  await expect(panel).toContainText('%0.5 → %10')
  await expect(panel).toContainText('%5 → %25')
  await expect(panel).toContainText('Kırmızı')
  await expect(panel).toContainText('Yüksek risk kabul yazısı doğrulandı')
  await expect(panel).toContainText('owner')
  expect(reads[0]).toContain('action=original_live.risk_changed')
})

test('admin audit failure is explicit rather than an empty success', async ({page}) => {
  await fixture(page, true)
  const panel = page.getByRole('region', {name: 'Original V2 risk değişiklikleri'})
  await expect(panel.getByRole('alert')).toContainText('kayıtları alınamadı')
  await expect(panel.getByText('Henüz risk değişikliği yok.')).toHaveCount(0)
})

test('admin rejects malformed audit records rather than displaying unverifiable approvals', async ({page}) => {
  await fixture(page, false, true)
  const panel = page.getByRole('region', {name: 'Original V2 risk değişiklikleri'})
  await expect(panel.getByRole('alert')).toContainText('biçimi doğrulanamadı')
  await expect(panel.getByRole('table')).toHaveCount(0)
})
