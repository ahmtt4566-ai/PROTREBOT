import {expect, test, type Page} from '@playwright/test'

const symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT']
async function fixture(page: Page, {owner = true, available = true, armed = true, enabled = false, liveAutoTrade = enabled, locked = false, dailyLossLocked = false, readinessFailure = false, rejectApproval = false, binanceBan = false} = {}) {
  const writes: Array<{path: string; body: unknown}> = []
  await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'original-live-ui'))
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const user = {id: owner ? 'owner' : 'member', role: owner ? 'OWNER' : 'CUSTOMER', email: 'user@example.com', active: true, email_verified: true}
    const fillLock = {id: 'fill-lock-1', active: locked, symbol: 'BTCUSDT',
      violations: ['Stop mesafesi %6 sınırını aştı'], evidence: {stop_percent: '7.84', risk_usdt: '41.02', risk_budget_usdt: '30', margin_usdt: '170'}}
    const original = {
      available, enabled, paused: locked, risk_percent: 0.5, daily_loss_percent: 5, symbols, fill_lock: fillLock,
      session: enabled ? {hours: 1, expires_at: new Date(Date.now() + 3600000).toISOString(), remaining_seconds: 3600, authority_limited: false, warning_sent: false} : null,
      daily: {day: '2026-10-13', loss: dailyLossLocked ? 50 : 0, limit: 50, locked: dailyLossLocked},
      universe: {
        requested_count: 40, selected_count: 40, eligible_count: 46,
        filter_counts: {
          active_contracts: 650, age_under_30_days: 20, market_data_missing: 0,
          invalid_market_data: 1, volume_below_minimum: 500, spread_too_wide: 83, eligible: 46,
        },
      },
      scanner: {
        stage: 'SCAN_COMPLETE', scanned_symbol_count: 40, candidate_count: 0,
        rejection_counts: {
          data_missing: 1, invalid_market_data: 0, market_volume: 0, market_move: 0,
          market_filter: 0, market_shortlist: 0, signal_conditions: 20, confidence_low: 19,
          spread_wide: 0, stop_too_wide: 0, minimum_margin: 0, minimum_trade_size: 1, position_limit: 0,
          daily_limit: 0, risk_or_account_limit: 0,
        },
        near_misses: [
          {symbol: 'LINKUSDT', reason_code: 'confidence_low', confidence: 78, confidence_threshold: 80, failed_checks: ['confidence']},
          {symbol: 'ARBUSDT', reason_code: 'minimum_trade_size', confidence: 76, failed_checks: [],
            total_quantity: '0.4', total_notional_usdt: '4', tp1_quantity: '0.2', tp1_notional_usdt: '2',
            remaining_quantity: '0.2', remaining_notional_usdt: '2', min_qty: '0.001', min_notional_usdt: '5'},
          {symbol: 'OPUSDT', reason_code: 'market_move', market_move_percent: 0.12},
        ],
        last_24h: {scans: 3, candidates: 2, trades: 1},
      },
    }
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
        recovery_ready: true, credentials: {configured: true, fingerprint: 'fixture-fingerprint'}, live_auto_trade: liveAutoTrade,
        auto_session_until: liveAutoTrade ? new Date(Date.now() + 3600000).toISOString() : null,
        readiness: {ready: !readinessFailure, gates: readinessFailure ? [{key: 'policy', label: 'Risk policy', passed: false}] : []}, authorization: {valid: true, scope_match: true}, consent: {active: true},
        policy: {allowed_symbols: symbols, interval: '15m', daily_trade_limit: 3, daily_loss_limit: 10, consecutive_loss_limit: 3},
        daily: {entries: 0, realized_pnl: 0, consecutive_losses: 0, unverified_closures: 0},
        emergency: {active: false}, reconciliation_required: false,
        account: {wallet_balance: 1000, available_balance: 1000, unrealized_pnl: 0, positions: [], open_orders: []},
        ...(owner && available ? {original_v2_live: original} : {}),
      },
      '/api/exchange-connections/status': {
        vault: {ready: true},
        connections: {LIVE: {configured: true, active: true, last_test_ok: true}},
        binance_ban: binanceBan ? {
          active: true,
          blocked_until: new Date(Date.now() + 4_881_000).toISOString(),
          remaining_seconds: 4881,
          source: 'original_warmer',
          egress_route: 'render_direct',
          status_code: 418,
          exchange_code: -1003,
          used_weight_1m: 1660,
        } : {active: false, remaining_seconds: 0},
      },
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

test('OWNER sees Original V2 rejection stages, nearest symbols and 24-hour conversions', async ({page}) => {
  await fixture(page)
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card.getByTestId('original-scan-24h')).toContainText('Son 24 saat: 3 tarama · 2 aday · 1 işleme dönüştü')
  await expect(card.getByTestId('original-scan-diagnostics')).toContainText('Trend / sinyal koşulu yok: 20')
  await expect(card.getByTestId('original-scan-diagnostics')).toContainText('Güven skoru düşük: 19')
  await expect(card.getByTestId('original-scan-diagnostics')).toContainText('Spread geniş 83')
  await expect(card.getByTestId('original-scan-near-misses')).toContainText('LINKUSDT')
  await expect(card.getByTestId('original-scan-near-misses')).toContainText('Güven %78 / eşik %80')
  await expect(card.getByTestId('original-scan-near-misses')).toContainText('ARBUSDT')
  await expect(card.getByTestId('original-scan-near-misses')).toContainText('Minimum işlem tutarı')
  await expect(card.getByTestId('original-scan-near-misses')).toContainText('Pozisyon 4 USDT (0.4)')
  await expect(card.getByTestId('original-scan-near-misses')).toContainText('TP1 %60 2 USDT (0.2)')
  await expect(card.getByTestId('original-scan-near-misses')).toContainText('Kalan 2 USDT (0.2)')
  await expect(card.getByTestId('original-scan-near-misses')).toContainText('Borsa minimumu 5 USDT / 0.001')
  await expect(card.getByTestId('original-scan-near-misses')).toContainText('OPUSDT')
})

test('active Original V2 reports entries open after its startup ARM is consumed', async ({page}) => {
  await fixture(page, {armed: false, enabled: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  const checks = card.getByTestId('original-entry-checks')
  await expect(card.getByTestId('original-entry-status')).toHaveText('Açık – sinyal bekleniyor')
  await expect(checks).toContainText('Giriş kontrolleri')
  await expect(checks).toContainText('GEÇTİ · Canlı otomasyon oturumu')
  await expect(checks).toContainText('GEÇTİ · Günlük işlem adedi')
  await expect(checks).not.toContainText('fixture-fingerprint')
  await expect(checks).not.toContainText(/fixture-secret|fixture-token|api-key-value/i)
})

test('Original V2 entry status names missing automation as the blocker', async ({page}) => {
  await fixture(page, {armed: true, enabled: true, liveAutoTrade: false})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card.getByTestId('original-entry-status')).toHaveText('Engelli – Canlı otomasyon oturumu etkin değil veya süresi dolmuş.')
  await expect(card.getByTestId('original-entry-checks')).toContainText('GEÇMEDİ · Canlı otomasyon oturumu')
})

test('Original V2 entry status names the fill lock', async ({page}) => {
  await fixture(page, {armed: false, enabled: true, locked: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card.getByTestId('original-entry-status')).toHaveText('Engelli – Dolum güvenlik kilidi aktif; OWNER incelemesi gerekiyor.')
  await expect(card.getByTestId('original-entry-checks')).toContainText('GEÇMEDİ · Dolum güvenlik kilidi')
})

test('Original V2 entry status names the daily loss lock', async ({page}) => {
  await fixture(page, {armed: false, enabled: true, dailyLossLocked: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card.getByTestId('original-entry-status')).toHaveText('Engelli – Original V2 günlük kayıp kilidi aktif.')
  await expect(card.getByTestId('original-entry-checks')).toContainText('GEÇMEDİ · Original V2 günlük kayıp sınırı')
})

test('Original V2 entry status translates failed policy readiness', async ({page}) => {
  await fixture(page, {armed: false, enabled: true, readinessFailure: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card.getByTestId('original-entry-status')).toHaveText('Engelli – Güncel risk politikası onayı yok.')
  await expect(card.getByTestId('original-entry-checks')).toContainText('GEÇMEDİ · Risk politikası onayı')
})

test('global Binance ban shows countdown and disables test and connect actions', async ({page}) => {
  await fixture(page, {binanceBan: true})
  const warning = page.getByTestId('binance-global-ban')
  await expect(warning).toContainText('Binance geçici olarak engelledi')
  await expect(warning).toContainText('Kalan süre: 01:21:')
  await page.getByTitle('Connect your trading account', {exact: true}).click()
  const connection = page.locator('.masterTradeLiveConnectionCard')
  await expect(connection.getByRole('button', {name: 'TEST CONNECTION'})).toBeDisabled()
  await expect(connection.getByRole('button', {name: 'CONNECT ACCOUNT'})).toBeDisabled()
})

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
