import {expect, test, type Page} from '@playwright/test'

test.use({baseURL: 'http://127.0.0.1:4174'})

const symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT']
async function fixture(page: Page, {owner = true, available = true, armed = true, enabled = false, liveAutoTrade = enabled, locked = false, dailyLossLocked = false, readinessFailure = false, rejectApproval = false, binanceBan = false, pauseReason = '', expired = false, missingBalance = false, rejectRiskSave = false, consentActive = true, policyAcknowledged = true, withPosition = false, missingTp = false, pendingAccountCheck = false, externalPosition = false, sharedDailyLimit = 10, statusFingerprintMismatch = false, statusUnavailable = false} = {}) {
  const writes: Array<{path: string; body: unknown}> = []
  let riskPercent = 0.5, dailyPercent = 5, riskRevision = 0
  let vaultFingerprint = 'saved-fixture-fingerprint'
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('**', socket => socket.close())
  await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'original-live-ui'))
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const user = {id: owner ? 'owner' : 'member', role: owner ? 'OWNER' : 'CUSTOMER', email: 'user@example.com', active: true, email_verified: true}
    const connectionStatus = {
      vault: {ready: true}, connections: {LIVE: {
        configured: true, active: true, last_test_ok: true, fingerprint: vaultFingerprint,
        last_test_at: new Date().toISOString(),
        account: {api_permissions: {withdrawal_check: 'VERIFIED', enableWithdrawals: false}},
      }},
    }
    const fillLock = {id: 'fill-lock-1', active: locked, symbol: 'BTCUSDT',
      violations: ['Stop mesafesi %6 sınırını aştı'], evidence: {stop_percent: '7.84', risk_usdt: '41.02', risk_budget_usdt: '30', margin_usdt: '170'}}
    const original = {
      available, enabled, paused: locked || !!pauseReason, pause_reason: pauseReason, risk_percent: riskPercent, daily_loss_percent: dailyPercent, risk_revision: riskRevision, symbols, fill_lock: fillLock,
      risk_limits: {max_loss_per_trade_usdt: 3, daily_loss_limit_usdt: 10, max_margin_usdt: 25, min_margin_usdt: 5, leverage: 3},
      session: enabled || pauseReason || expired ? {hours: 1, expires_at: new Date(Date.now() + (expired ? -3600000 : 3600000)).toISOString(), remaining_seconds: enabled && !expired ? 3600 : 0, authority_limited: false, warning_sent: false} : null,
      daily: {day: new Date().toISOString().slice(0, 10), loss: dailyLossLocked ? 50 : 0, limit: 50, locked: dailyLossLocked},
      universe: {
        requested_count: 40, selected_count: 40, eligible_count: 46, minimum_age_days: 45,
        filter_counts: {
          active_contracts: 650, age_below_required_history: 20, market_data_missing: 0,
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
      if (path.endsWith('/risk-settings')) {
        if (rejectRiskSave) {
          await route.fulfill({status: 503, json: {detail: 'Risk ayarı ve onay kaydı birlikte kaydedilemedi; eski ayarlar korundu.'}})
          return
        }
        const body = request.postDataJSON()
        riskPercent = body.risk_percent
        dailyPercent = body.daily_loss_percent
        riskRevision += 1
        await route.fulfill({json: {...original, risk_percent: riskPercent, daily_loss_percent: dailyPercent, risk_revision: riskRevision}})
        return
      }
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
      liveAutoTrade = enabled
      await route.fulfill({json: {...original, enabled}})
      return
    }
    if (request.method() === 'POST' && path.startsWith('/api/v25/')) {
      if (!path.endsWith('/risk/preview')) writes.push({path, body: request.postData() ? request.postDataJSON() : null})
      if (path.endsWith('/arm')) armed = true
      if (path.endsWith('/consent')) consentActive = true
      if (path.endsWith('/policy/acknowledge')) policyAcknowledged = true
      if (!['/api/v25/arm', '/api/v25/consent', '/api/v25/policy/acknowledge', '/api/v25/risk/preview'].includes(path)) {
        await route.fulfill({status: 405, json: {detail: 'Unexpected LIVE mutation in an offline fixture.'}})
        return
      }
      await route.fulfill({json: {ok: true}})
      return
    }
    if (request.method() === 'POST' && path.startsWith('/api/exchange-connections/')) {
      writes.push({path, body: request.postDataJSON()})
      if (path.endsWith('/test')) {
        await route.fulfill({json: {ok: true, message: 'Salt-okunur test başarılı.', account: {wallet_balance: 1000}, fingerprint: 'tested-unsaved-fingerprint'}})
      } else {
        if (path.endsWith('/save')) vaultFingerprint = 'replacement-fixture-fingerprint'
        await route.fulfill({json: {...connectionStatus, connections: {LIVE: {...connectionStatus.connections.LIVE, fingerprint: vaultFingerprint}}}})
      }
      return
    }
    if (path === '/api/v25/status' && statusUnavailable) {
      await route.abort('failed')
      return
    }
    const bodies: Record<string, unknown> = {
      '/api/v22/profile': {user, access: {canAccessMasterTrade: true, isPremium: true, canExecuteLive: true}},
      '/api/v22/session': {user},
      '/api/markets': [{symbol: 'BTCUSDT', display: 'BTC/USDT', price: 90000, change: 1, volume: 1000000, status: 'TRADING', contractType: 'PERPETUAL', quoteAsset: 'USDT'}],
      '/api/v25/status': {
        connected: true, real_trading_locked: false, execution_state: 'LOCKED', armed,
        recovery_ready: true, credentials: {configured: true, fingerprint: statusFingerprintMismatch ? 'stale-fixture-fingerprint' : vaultFingerprint}, live_auto_trade: liveAutoTrade,
        auto_session_until: liveAutoTrade ? new Date(Date.now() + 3600000).toISOString() : null,
        readiness: {ready: !readinessFailure, gates: readinessFailure ? [{key: 'policy', label: 'Risk policy', passed: false}] : []}, authorization: {valid: consentActive, scope_match: true}, consent: {active: consentActive}, policy_acknowledged: policyAcknowledged,
        policy: {allowed_symbols: symbols, interval: '15m', daily_trade_limit: 3, daily_loss_limit: sharedDailyLimit, consecutive_loss_limit: 3, max_positions: 5},
        account_rate_limit: {pending_verification: pendingAccountCheck},
        daily: {entries: 0, realized_pnl: 0, consecutive_losses: 0, unverified_closures: 0},
        emergency: {active: false}, reconciliation_required: false,
        account: {wallet_balance: missingBalance ? null : 1000, available_balance: 1000, unrealized_pnl: 0, positions: withPosition ? [{symbol: 'BTCUSDT', direction: 'LONG', entry_price: '100', quantity: '1', unrealized_pnl: 2}] : [], open_orders: []},
        plans: withPosition ? [{symbol: 'BTCUSDT', direction: 'LONG', status: 'AÇIK', provenance_state: externalPosition ? 'READ_ONLY_EXTERNAL' : 'CONFIRMED', stop_loss: '90', targets: ['110', '115', '120'], exchange_protection: {stop: true, tp1: !missingTp, tp3: !missingTp}}] : [],
        ...(owner && available ? {original_v2_live: original} : {}),
      },
      '/api/exchange-connections/status': {
        ...connectionStatus,
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
  await expect(page.locator('.masterTradePage').first()).toBeVisible()
  if (owner && available && !statusFingerprintMismatch && !statusUnavailable) await expect(page.getByRole('region', {name: 'Kais Original V2 LIVE'})).toBeVisible()
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
  await expect(card).toContainText('liste yaşı en az 45 gün')
})

test('paused session with future expiry shows safety stop rather than expired', async ({page}) => {
  const writes = await fixture(page, {pauseReason: 'INCOMPLETE_ORIGINAL_LIVE_HISTORY'})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card.getByTestId('original-session-remaining')).toHaveText('Güvenlik nedeniyle durduruldu / giriş kapalı')
  await expect(card).toContainText('INCOMPLETE_ORIGINAL_LIVE_HISTORY')
  expect(writes).toEqual([])
})

test('expired session still reports expiry rather than safety stop', async ({page}) => {
  const writes = await fixture(page, {pauseReason: 'Oturum süresi doldu', expired: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card.getByTestId('original-session-remaining')).toHaveText('Süre doldu / giriş kapalı')
  expect(writes).toEqual([])
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
  await expect(page.getByTestId('original-entry-status')).toContainText('Binance geçici olarak engelledi')
  await expect(page.getByTestId('original-entry-checks')).toContainText('bitiş saati')
  await page.getByRole('tab', {name: 'Bağlantı', exact: true}).click()
  const connection = page.locator('.masterTradeLiveConnectionCard')
  await expect(connection.getByRole('button', {name: 'Bağlantıyı test et'})).toBeDisabled()
  await expect(connection.getByRole('button', {name: 'Kaydet ve bağla'})).toBeDisabled()
})

test('fill latch survives reload; OWNER approval only unlocks latch and never starts trading', async ({page}) => {
    const writes = await fixture(page, {locked: true})
    await page.reload()
    const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
    await expect(card.getByText(/Dolum güvenlik kilidi · BTCUSDT/)).toBeVisible()
    const start = card.getByRole('button', {name: 'Kais Original V2’yi başlat', exact: true})
    await card.getByLabel('Kurtarma için 2FA kodu').fill('123456')
    await expect(start).toBeDisabled()
    const approve = card.getByRole('button', {name: 'Dolum kilidini onayımla kaldır'})
    await expect(approve).toBeDisabled()
    await card.getByLabel('DOLUM RİSKİNİ İNCELEDİM yazın').fill('DOLUM RİSKİNİ İNCELEDİM')
    await approve.click()
    await expect(card.getByText(/Dolum kilidi onayınla kaldırıldı; otomasyon başlamadı/)).toBeVisible()
    await expect(card.getByText(/Dolum güvenlik kilidi · BTCUSDT/)).toHaveCount(0)
    await expect(card.getByLabel('Kurtarma için 2FA kodu')).toHaveCount(0)
    await expect(start).toBeDisabled()
    expect(writes).toEqual([{path: '/api/v25/original-v2/fill-lock/approve', body: {
      lock_id: 'fill-lock-1', confirmation: 'DOLUM RİSKİNİ İNCELEDİM', totp_code: '123456',
    }}])
  })

test('invalid fill approval shows Turkish error and keeps new entries disabled', async ({page}) => {
    const writes = await fixture(page, {locked: true, rejectApproval: true})
    const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
    await card.getByLabel('DOLUM RİSKİNİ İNCELEDİM yazın').fill('DOLUM RİSKİNİ İNCELEDİM')
    await card.getByLabel('Kurtarma için 2FA kodu').fill('wrongcode')
    await card.getByRole('button', {name: 'Dolum kilidini onayımla kaldır'}).click()
    await expect(card.getByRole('status')).toHaveText('LIVE HTTP 401: 2FA kodu geçersiz.')
    await expect(card.getByText(/Dolum güvenlik kilidi · BTCUSDT/)).toBeVisible()
    await expect(card.getByRole('button', {name: 'Kais Original V2’yi başlat', exact: true})).toBeDisabled()
    expect(writes.map(write => write.path)).toEqual(['/api/v25/original-v2/fill-lock/approve'])
  })

test('three-step setup requires an audited checkbox, not repeated MFA; emergency only stops entries', async ({page}) => {
  const writes = await fixture(page)
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card).toBeVisible()
  await expect(card.getByLabel('İşlem riski (%)')).toHaveValue('0.5')
  await expect(card.getByLabel('Günlük kayıp sınırı (%)')).toHaveValue('5')
  const start = card.getByRole('button', {name: 'Kais Original V2’yi başlat', exact: true})
  await expect(start).toBeDisabled()
  await expect(card.getByLabel('ORIGINAL V2 CANLI yazın')).toHaveCount(0)
  await expect(card.getByLabel('2FA kodu', {exact: true})).toHaveCount(0)
  expect(writes).toEqual([])
  await card.getByLabel('Gerçek parayla işlem açılacağını ve kayıp olabileceğini anladım').check()
  await card.getByRole('button', {name: 'Onayla ve devam et · Risk ayarını kaydet'}).click()
  await expect(start).toBeEnabled()
  await start.click()
  await expect(card.getByText('Original V2 LIVE gözetimli oturumu açıldı.')).toBeVisible()
  expect(writes[1]).toEqual({path: '/api/v25/original-v2/start', body: {
    understood: true, risk_percent: 0.5, daily_loss_percent: 5,
    symbol_count: 40, session_hours: 1, risk_acknowledged: false, high_risk_confirmation: '',
    warning_balance_usdt: 1000, expected_risk_revision: 1,
  }})
  await expect(card.getByRole('button', {name: 'Acil durdur · Yeni girişleri kapat'})).toBeEnabled()
  await card.getByRole('button', {name: 'Acil durdur · Yeni girişleri kapat'}).click()
  await expect(card.getByText('Yeni girişler durduruldu; açık pozisyon ve Stop/TP korumaları korundu.')).toBeVisible()
  expect(writes.map(write => write.path)).toEqual(['/api/v25/original-v2/risk-settings', '/api/v25/original-v2/start', '/api/v25/original-v2/stop'])
})

for (const [percent, level] of [[1, 'NORMAL'], [2, 'YELLOW'], [5, 'ORANGE'], [10, 'RED']] as const) {
  test(`risk ${percent} percent shows ${level} warning and exact approval gate`, async ({page}) => {
    const writes = await fixture(page)
    const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
    await card.getByLabel('Günlük kayıp sınırı (%)').fill('25')
    await card.getByLabel('İşlem riski (%)').fill(String(percent))
    if (!(percent > 2 && percent <= 5)) await card.getByLabel('Gerçek parayla işlem açılacağını ve kayıp olabileceğini anladım').check()
    await expect(card.getByTestId('original-risk-warning')).toHaveAttribute('data-level', level)
    const save = card.getByRole('button', {name: 'Onayla ve devam et · Risk ayarını kaydet'})
    if (percent > 5) {
      await expect(save).toBeDisabled()
      await card.getByLabel('YÜKSEK RİSKİ KABUL EDİYORUM yazın').fill('YANLIŞ')
      await expect(save).toBeDisabled()
      await card.getByLabel('YÜKSEK RİSKİ KABUL EDİYORUM yazın').fill('YÜKSEK RİSKİ KABUL EDİYORUM')
    } else if (percent > 2) {
      await expect(save).toBeDisabled()
      await card.getByLabel('Bu riski anladım').check()
    } else {
      await expect(card.getByLabel('Bu riski anladım')).toHaveCount(0)
      await expect(card.getByLabel('YÜKSEK RİSKİ KABUL EDİYORUM yazın')).toHaveCount(0)
    }
    await expect(save).toBeEnabled()
    await expect(card.getByRole('checkbox')).toHaveCount(1)
    await save.click()
    await expect(card).toContainText('Yalnızca yeni işlemlere uygulanır; açık pozisyon, Stop ve TP değişmedi.')
    expect(writes).toHaveLength(1)
    expect(writes[0].path).toBe('/api/v25/original-v2/risk-settings')
    expect(writes[0].body).toMatchObject({risk_percent: percent, daily_loss_percent: 25, warning_balance_usdt: 1000, expected_risk_revision: 0})
  })
}

test('loss bars use compound real-balance amounts and show preserved risk caps', async ({page}) => {
  await fixture(page)
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await card.getByLabel('İşlem riski (%)').fill('5')
  await expect(card.getByTestId('original-risk-warning')).toContainText('50,00 USDT')
  await expect(card.getByTestId('original-risk-warning')).toContainText('773,78 USDT')
  await expect(card.getByTestId('original-risk-warning')).toContainText('598,74 USDT')
  expect(Number(await card.getByRole('meter', {name: 'Seçilen risk 5 kayıp sonrası bakiye', exact: true}).getAttribute('aria-valuenow'))).toBeCloseTo(773.7809375, 5)
  expect(Number(await card.getByRole('meter', {name: 'Yüzde 1 risk 5 kayıp sonrası bakiye', exact: true}).getAttribute('aria-valuenow'))).toBeCloseTo(950.9900499, 5)
  await expect(card.getByTestId('original-risk-cap')).toContainText('%0,3 (3,00 USDT)')
  await expect(card).toContainText('üst üste 5–10 kayıp olağan dışı değildir')
  await page.setViewportSize({width: 390, height: 844})
  await expect(card.getByTestId('original-risk-warning')).toBeVisible()
})

test('changing risk clears acknowledgement and daily limit cannot be below trade risk', async ({page}) => {
  await fixture(page, {enabled: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await card.locator('.originalSettings > summary').click()
  await card.getByLabel('İşlem riski (%)').fill('3')
  await card.getByLabel('Bu riski anladım').check()
  await card.getByLabel('İşlem riski (%)').fill('4')
  await expect(card.getByLabel('Bu riski anladım')).not.toBeChecked()
  await card.getByLabel('Günlük kayıp sınırı (%)').fill('1')
  await expect(card).toContainText('Günlük sınır işlem riskinden küçük olamaz.')
  await expect(card.getByRole('button', {name: 'Onayla ve devam et · Risk ayarını kaydet'})).toBeDisabled()
})

test('unverified balance blocks risk consent without inventing loss estimates', async ({page}) => {
  const writes = await fixture(page, {missingBalance: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card).toContainText('Güncel Binance bakiyesi doğrulanamadı.')
  await expect(card.getByTestId('original-risk-warning')).toHaveCount(0)
  await expect(card.getByRole('button', {name: 'Onayla ve devam et · Risk ayarını kaydet'})).toBeDisabled()
  expect(writes).toEqual([])
})

test('audit failure cannot report a successful risk save', async ({page}) => {
  await fixture(page, {rejectRiskSave: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await card.getByLabel('İşlem riski (%)').fill('2')
  await card.getByLabel('Gerçek parayla işlem açılacağını ve kayıp olabileceğini anladım').check()
  await card.getByRole('button', {name: 'Onayla ve devam et · Risk ayarını kaydet'}).click()
  await expect(card).toContainText('eski ayarlar korundu')
  await expect(card.getByText('Risk ayarı ve onayın kaydedildi.', {exact: false})).toHaveCount(0)
})

test('saving risk while running never starts or stops automation', async ({page}) => {
  const writes = await fixture(page, {enabled: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await card.locator('.originalSettings > summary').click()
  await card.getByLabel('İşlem riski (%)').fill('2')
  await card.getByLabel('Gerçek parayla işlem açılacağını ve kayıp olabileceğini anladım').check()
  await card.getByRole('button', {name: 'Onayla ve devam et · Risk ayarını kaydet'}).click()
  await expect(card).toContainText('Yalnızca yeni işlemlere uygulanır')
  await expect(card.getByRole('heading', {name: 'Çalışıyor – sinyal bekleniyor'})).toBeVisible()
  await expect(card.getByTestId('original-runtime-status')).toContainText('%2')
  expect(writes.map(write => write.path)).toEqual(['/api/v25/original-v2/risk-settings'])
})

test('setup completes consent, policy acknowledgement and ARM before Original start', async ({page}) => {
  const writes = await fixture(page, {armed: false, consentActive: false, policyAcknowledged: false})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await card.getByLabel('Gerçek parayla işlem açılacağını ve kayıp olabileceğini anladım').check()
  await card.getByRole('button', {name: 'Onayla ve devam et · Risk ayarını kaydet'}).click()
  await card.getByRole('button', {name: 'Kais Original V2’yi başlat', exact: true}).click()
  await expect(card.getByText('Original V2 LIVE gözetimli oturumu açıldı.')).toBeVisible()
  expect(writes.map(write => write.path)).toEqual([
    '/api/v25/original-v2/risk-settings', '/api/v25/consent',
    '/api/v25/policy/acknowledge', '/api/v25/arm', '/api/v25/original-v2/start',
  ])
  for (const write of writes) {
    expect(write.body).toMatchObject({understood: true})
    expect(write.body).not.toHaveProperty('totp_code')
  }
})

test('running dashboard hides keys and technical controls, and confirms Stop independently of TP', async ({page}) => {
  await fixture(page, {enabled: true, withPosition: true, missingTp: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card.getByRole('heading', {name: 'Çalışıyor – sinyal bekleniyor'})).toBeVisible()
  await expect(page.getByLabel('API KEY', {exact: true})).toHaveCount(0)
  await expect(page.getByLabel('SECRET KEY', {exact: true})).toHaveCount(0)
  await expect(page.getByRole('button', {name: 'START LIVE AUTO TRADE', exact: true})).toHaveCount(0)
  await expect(card.getByTestId('original-details')).not.toHaveAttribute('open', '')
  const position = card.locator('.originalPosition')
  await expect(position.getByText('Binance’te ✓', {exact: true})).toHaveCount(1)
  await expect(position.getByText('Doğrulanamadı', {exact: true})).toHaveCount(2)
  const progress = card.getByRole('progressbar', {name: 'Günlük kayıp sınırı kullanımı'})
  await expect(progress).toHaveAttribute('aria-valuemax', '10')
  await expect(progress).toHaveAttribute('aria-valuenow', '0')
})

test('mobile dashboard has no horizontal overflow and emergency remains visible after scrolling', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844})
  await fixture(page, {enabled: true, withPosition: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card.locator('.originalPosition').getByText('Binance’te ✓', {exact: true})).toHaveCount(3)
  const emergency = card.getByRole('button', {name: 'Acil durdur · Yeni girişleri kapat'})
  const unobscured = () => emergency.evaluate(element => {
    const bounds = element.getBoundingClientRect()
    return element.contains(document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2))
  })
  await expect(emergency).toBeInViewport()
  expect(await unobscured()).toBe(true)
  await card.getByTestId('original-details').locator('summary').first().click()
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight))
  await expect(card.getByRole('button', {name: 'Acil durdur · Yeni girişleri kapat'})).toBeInViewport()
  const button = await card.getByRole('button', {name: 'Acil durdur · Yeni girişleri kapat'}).boundingBox()
  expect(button).not.toBeNull()
  expect(button!.x).toBeGreaterThanOrEqual(0)
  expect(button!.x + button!.width).toBeLessThanOrEqual(390)
  expect(await unobscured()).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('unverified account snapshots never show green protection or invented wallet values', async ({page}) => {
  await fixture(page, {enabled: true, withPosition: true, pendingAccountCheck: true})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card.getByText('Binance’te ✓', {exact: true})).toHaveCount(0)
  await expect(card.locator('.originalMetricGrid article').first()).toContainText('—')
  await card.locator('.originalSettings > summary').click()
  await expect(card).toContainText('Güncel Binance bakiyesi doğrulanamadı.')
})

test('small mobile stopped-state card wraps the actual long stop reason', async ({page}) => {
  await page.setViewportSize({width: 320, height: 844})
  await fixture(page, {pauseReason: 'INCOMPLETE_ORIGINAL_LIVE_HISTORY'})
  const hero = page.locator('.originalStatusHero')
  await expect(hero.getByRole('heading', {name: 'Durdu – yeni girişler kapalı'})).toBeVisible()
  await expect(hero).toContainText('INCOMPLETE_ORIGINAL_LIVE_HISTORY')
  expect(await hero.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
})

test('external positions cannot inherit protection checks and a tighter shared daily cap wins', async ({page}) => {
  await fixture(page, {enabled: true, withPosition: true, externalPosition: true, sharedDailyLimit: 2})
  const card = page.getByRole('region', {name: 'Kais Original V2 LIVE'})
  await expect(card.locator('.originalPosition')).toContainText('Dış pozisyon · bot tarafından yönetilmiyor.')
  await expect(card.getByText('Binance’te ✓', {exact: true})).toHaveCount(0)
  await expect(card.getByRole('progressbar', {name: 'Günlük kayıp sınırı kullanımı'})).toHaveAttribute('aria-valuemax', '2')
})

test('LIVE credential fingerprint mismatch stays fail closed while the vault card remains current', async ({page}) => {
  const writes = await fixture(page, {statusFingerprintMismatch: true})
  await page.getByRole('tab', {name: 'Bağlantı', exact: true}).click()
  await expect(page.getByRole('region', {name: 'Kayıtlı Binance bağlantısı'})).toContainText('saved-fixture-fingerprint')
  await expect(page.locator('body')).toContainText('LIVE durumu ile kayıtlı Binance anahtarı uyuşmuyor')
  expect(writes).toEqual([])
})

test('Vault connection card remains current when the separate LIVE status request fails', async ({page}) => {
  const writes = await fixture(page, {statusUnavailable: true})
  await page.getByRole('tab', {name: 'Bağlantı', exact: true}).click()
  await expect(page.getByRole('region', {name: 'Kayıtlı Binance bağlantısı'})).toContainText('saved-fixture-fingerprint')
  await expect(page.locator('body')).toContainText('Güncel LIVE durumu doğrulanamadı')
  expect(writes).toEqual([])
})

test('only Connections mounts key inputs; saving a changed key requires one current MFA code', async ({page}) => {
  const writes = await fixture(page)
  await expect(page.getByLabel('API KEY', {exact: true})).toHaveCount(0)
  await page.getByRole('tab', {name: 'Bağlantı', exact: true}).click()
  const connection = page.locator('.masterTradeLiveConnectionCard')
  await expect(connection.getByRole('heading', {name: 'Bağlantı', exact: true})).toBeVisible()
  const saved = connection.getByRole('region', {name: 'Kayıtlı Binance bağlantısı'})
  await expect(saved).toContainText('saved-fixture-fingerprint')
  await connection.getByLabel('API KEY', {exact: true}).fill('offline-key-for-ui-only')
  await connection.getByLabel('SECRET KEY', {exact: true}).fill('offline-secret-for-ui-only')
  const save = connection.getByRole('button', {name: 'Kaydet ve bağla'})
  await expect(save).toBeDisabled()
  await connection.getByRole('button', {name: 'Bağlantıyı test et'}).click()
  await expect(saved).toContainText('saved-fixture-fingerprint')
  await expect(saved).not.toContainText('tested-unsaved-fingerprint')
  await connection.getByLabel('2FA kodu', {exact: true}).fill('123456')
  await save.click()
  await expect(connection.getByLabel('API KEY', {exact: true})).toHaveValue('')
  await expect(connection.getByLabel('SECRET KEY', {exact: true})).toHaveValue('')
  await expect(connection.getByLabel('2FA kodu', {exact: true})).toHaveValue('')
  await expect(saved).toContainText('replacement-fixture-fingerprint')
  expect(writes.map(write => write.path)).toEqual([
    '/api/exchange-connections/test', '/api/exchange-connections/save', '/api/exchange-connections/activate',
  ])
  expect(writes[1].body).toMatchObject({mode: 'LIVE', totp_code: '123456'})
  expect(writes[2].body).not.toHaveProperty('totp_code')
})

for (const options of [{owner: false}, {available: false}]) {
  test(`Original V2 is hidden when ${JSON.stringify(options)}`, async ({page}) => {
    const writes = await fixture(page, options)
    await expect(page.getByRole('region', {name: 'Kais Original V2 LIVE'})).toHaveCount(0)
    expect(writes).toEqual([])
  })
}
