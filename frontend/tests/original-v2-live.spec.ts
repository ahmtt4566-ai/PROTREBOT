import {expect, test, type Page} from '@playwright/test'

const symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT']
async function fixture(page: Page, {owner = true, available = true, armed = true, enabled = false, locked = false, rejectApproval = false} = {}) {
  const writes: Array<{path: string; body: unknown}> = []
  await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'original-live-ui'))
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const user = {id: owner ? 'owner' : 'member', role: owner ? 'OWNER' : 'CUSTOMER', email: 'user@example.com', active: true, email_verified: true}
    const fillLock = {id: 'fill-lock-1', active: locked, symbol: 'BTCUSDT',
      violations: ['Stop mesafesi %6 sınırını aştı'], evidence: {stop_percent: '7.84', risk_usdt: '41.02', risk_budget_usdt: '30', margin_usdt: '170'}}
    const original = {available, enabled, paused: locked, risk_percent: 0.5, daily_loss_percent: 5, symbols, fill_lock: fillLock}
    if (request.method() === 'POST' && path.startsWith('/api/v25/original-v2/')) {
      writes.push({path, body: request.postData() ? request.postDataJSON() : null})
      if (path.endsWith('/fill-lock/approve')) {
        if (rejectApproval) {
          await route.fulfill({status: 401, json: {detail: '2FA kodu geçersiz.'}})
          return
        }
        locked = false
        armed = false
        enabled = false
        await route.fulfill({json: {...original, enabled: false, fill_lock: {...fillLock, active: false}}})
        return
      }
      enabled = path.endsWith('/start')
      await route.fulfill({json: {...original, enabled}})
      return
    }
    const bodies: Record<string, unknown> = {
      '/api/v22/profile': {user, access: {canAccessMasterTrade: true, isPremium: true, canExecuteLive: true}},
      '/api/v22/session': {user},
      '/api/markets': [{symbol: 'BTCUSDT', display: 'BTC/USDT', price: 90000, change: 1, volume: 1000000, status: 'TRADING', contractType: 'PERPETUAL', quoteAsset: 'USDT'}],
      '/api/v25/status': {
        connected: true, real_trading_locked: false, execution_state: 'LOCKED', armed,
        recovery_ready: true, credentials: {configured: true}, live_auto_trade: enabled,
        readiness: {ready: true, gates: []}, authorization: {valid: true}, consent: {active: true},
        policy: {allowed_symbols: symbols, interval: '15m'}, auto: {enabled},
        account: {wallet_balance: 1000, available_balance: 1000, positions: [], open_orders: []},
        ...(owner && available ? {original_v2_live: original} : {}),
      },
      '/api/exchange-connections/status': {vault: {ready: true}, connections: {LIVE: {configured: true, active: true, last_test_ok: true}}},
      '/api/v21/journal': {items: []},
      '/api/analysis-universe': {results: []},
    }
    const body = path.startsWith('/api/analysis/')
      ? {direction: 'WAIT', confidence: 80, entry: 90000, stop_loss: 88000, tp1: 92000, tp2: 94000, tp3: 96000}
      : path.startsWith('/api/klines/') ? [] : bodies[path] ?? {}
    await route.fulfill({json: body})
  })
  await page.goto('/master-trade?tab=canli', {waitUntil: 'domcontentloaded'})
  await expect(page.getByRole('heading', {name: 'LIVE AUTO TRADE', exact: true})).toBeVisible()
  return writes
}

test('fill latch survives reload; OWNER approval only unlocks latch and never starts trading', async ({page}) => {
    const writes = await fixture(page, {locked: true})
    await page.reload()
    const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
    await expect(card.getByText(/Dolum güvenlik kilidi · BTCUSDT/)).toBeVisible()
    const start = card.getByRole('button', {name: 'Original V2 LIVE aç', exact: true})
    await card.getByLabel('ORIGINAL V2 CANLI yazın').fill('ORIGINAL V2 CANLI')
    await card.getByLabel('2FA kodu').fill('123456')
    await expect(start).toBeDisabled()
    const approve = card.getByRole('button', {name: 'Dolum kilidini onayımla kaldır'})
    await expect(approve).toBeDisabled()
    await card.getByLabel('DOLUM RİSKİNİ İNCELEDİM yazın').fill('DOLUM RİSKİNİ İNCELEDİM')
    await approve.click()
    await expect(card.getByText(/Dolum kilidi onayınla kaldırıldı; otomasyon başlamadı/)).toBeVisible()
    await expect(card.getByText(/Dolum güvenlik kilidi · BTCUSDT/)).toHaveCount(0)
    await expect(card.getByLabel('2FA kodu')).toHaveValue('')
    await expect(start).toBeDisabled()
    expect(writes).toEqual([{path: '/api/v25/original-v2/fill-lock/approve', body: {
      lock_id: 'fill-lock-1', confirmation: 'DOLUM RİSKİNİ İNCELEDİM', totp_code: '123456',
    }}])
  })

test('invalid fill approval shows Turkish error and keeps new entries disabled', async ({page}) => {
    const writes = await fixture(page, {locked: true, rejectApproval: true})
    const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
    await card.getByLabel('DOLUM RİSKİNİ İNCELEDİM yazın').fill('DOLUM RİSKİNİ İNCELEDİM')
    await card.getByLabel('2FA kodu').fill('wrongcode')
    await card.getByRole('button', {name: 'Dolum kilidini onayımla kaldır'}).click()
    await expect(card.getByRole('status')).toHaveText('LIVE HTTP 401: 2FA kodu geçersiz.')
    await expect(card.getByText(/Dolum güvenlik kilidi · BTCUSDT/)).toBeVisible()
    await expect(card.getByRole('button', {name: 'Original V2 LIVE aç', exact: true})).toBeDisabled()
    expect(writes.map(write => write.path)).toEqual(['/api/v25/original-v2/fill-lock/approve'])
  })

test('OWNER can start only after explicit confirmation and 2FA; emergency only stops entries', async ({page}) => {
  const writes = await fixture(page)
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card).toBeVisible()
  await expect(card.getByLabel('İşlem riski (%)')).toHaveValue('0.5')
  await expect(card.getByLabel('Günlük kayıp sınırı (%)')).toHaveValue('5')
  const start = card.getByRole('button', {name: 'Original V2 LIVE aç', exact: true})
  await expect(start).toBeDisabled()
  await card.getByLabel('ORIGINAL V2 CANLI yazın').fill('ORIGINAL V2 CANLI')
  await start.click()
  expect(writes).toEqual([])
  await card.getByLabel('2FA kodu').fill('123456')
  await start.click()
  await expect(card.getByText('Original V2 LIVE gözetimli oturumu açıldı.')).toBeVisible()
  expect(writes[0]).toEqual({path: '/api/v25/original-v2/start', body: {
    confirmation: 'ORIGINAL V2 CANLI', totp_code: '123456', risk_percent: 0.5, daily_loss_percent: 5,
  }})
  await expect(card.getByRole('button', {name: 'Acil durdur · Yeni girişleri kapat'})).toBeEnabled()
  await card.getByRole('button', {name: 'Acil durdur · Yeni girişleri kapat'}).click()
  await expect(card.getByText('Yeni girişler durduruldu; açık pozisyon ve Stop/TP korumaları korundu.')).toBeVisible()
  expect(writes.map(write => write.path)).toEqual(['/api/v25/original-v2/start', '/api/v25/original-v2/stop'])
})

test('OWNER without ARM cannot start Original V2', async ({page}) => {
  const writes = await fixture(page, {armed: false})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await card.getByLabel('ORIGINAL V2 CANLI yazın').fill('ORIGINAL V2 CANLI')
  await card.getByLabel('2FA kodu').fill('123456')
  await expect(card.getByRole('button', {name: 'Original V2 LIVE aç', exact: true})).toBeDisabled()
  expect(writes).toEqual([])
})

for (const options of [{owner: false}, {available: false}]) {
  test(`Original V2 is hidden when ${JSON.stringify(options)}`, async ({page}) => {
    const writes = await fixture(page, options)
    await expect(page.getByRole('region', {name: 'Kais Original V2 LIVE'})).toHaveCount(0)
    expect(writes).toEqual([])
  })
}
