import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

test.use({baseURL:'http://127.0.0.1:4174'})
test.setTimeout(60000)

async function setup(page:Page) {
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  const mock = await mockAssistant(page)
  const fixture = {statusCode:200, statusDetail:'Status unavailable', mutations:[] as string[], requests:mock.requests, liveFields:{} as Record<string,unknown>}
  const user = {id:'master-owner', role:'OWNER', active:true, email_verified:true}
  await page.route('**/api/v22/{profile,session}', route => route.fulfill({json:{user, access:{isPremium:true, canAccessMasterTrade:true}}}))
  await page.route('**/api/v25/status', route => route.fulfill({status:fixture.statusCode, json:fixture.statusCode !== 200 ? {detail:fixture.statusDetail} : {
    connected:true, credentials:{configured:true}, real_trading_locked:false, armed:true, live_auto_trade:false,
    execution_state:'ARMED', recovery_ready:true, authorization:{valid:true}, policy_acknowledged:true,
    readiness:{ready:true,gates:[{key:'risk',passed:true},{key:'protection',passed:true}]},
    account:{positions:[],open_orders:[]}, events:[],
    ...fixture.liveFields,
  }}))
  await page.route('**/api/exchange-connections/status', route => route.fulfill({json:{connections:{LIVE:{configured:true,active:true}}}}))
  await page.route('**/api/v25/auto/**', async route => {
    fixture.mutations.push(new URL(route.request().url()).pathname)
    await route.fulfill({json:{ok:true}})
  })
  return fixture
}

test('LIVE history excludes DEMO data and close confirmation uses fresh per-plan PnL exactly once', async ({page}) => {
  const fixture = await setup(page)
  const plan = {id:'live-plan',symbol:'BTCUSDT',side:'BUY',status:'KORUMA AKTİF'}
  const performance = {
    total_trades:2,wins:2,losses:0,win_rate:100,total_profit:1000,total_loss:0,net_profit:1000,
    average_trade:500,best_trade:993,worst_trade:7,profit_factor:null,average_win:500,
    average_loss:null,losing_streak:0,max_drawdown:0,history_quality:'VERIFIED',
  }
  fixture.liveFields = {
    plans:[plan],
    account:{positions:[{symbol:'BTCUSDT',direction:'LONG',quantity:1,entry_price:100,mark_price:105}],open_orders:[]},
    journal:[],performance,daily_performance:performance,
  }
  await page.route('**/api/v25/position/close', async route => {
    fixture.mutations.push('/api/v25/position/close')
    expect(route.request().postDataJSON().plan_id).toBe('live-plan')
    fixture.liveFields = {
      ...fixture.liveFields,
      account:{positions:[],open_orders:[]},
      journal:[{id:'live-plan',symbol:'BTCUSDT',side:'BUY',created_at:new Date().toISOString(),verified_realized:true,realized_pnl:7}],
    }
    await route.fulfill({json:{ok:true}})
  })
  await page.goto('/master-trade?tab=pozisyonlar',{waitUntil:'domcontentloaded'})
  await page.getByRole('button',{name:'Close BTCUSDT position',exact:true}).click()
  await page.getByRole('button',{name:'CLOSE POSITION',exact:true}).evaluate(button => {
    button.dispatchEvent(new MouseEvent('click',{bubbles:true}))
    button.dispatchEvent(new MouseEvent('click',{bubbles:true}))
  })
  await expect(page.locator('.positionsPanel [role="status"]')).toContainText('POSITION CLOSED · PNL +$7.00')
  await expect(page.locator('.historyPanel')).toContainText('BTCUSDT')
  expect(fixture.mutations).toEqual(['/api/v25/position/close'])
  expect(fixture.requests.filter(path => /\/v21\/(journal|performance)/.test(path))).toEqual([])
})

for (const statusCode of [403,429,503]) test(`HTTP ${statusCode} account polling invalidates stale LIVE authority rather than leaving start enabled`, async ({page}) => {
  const fixture = await setup(page)
  await page.goto('/master-trade?tab=canli', {waitUntil:'domcontentloaded'})
  const start = page.locator('.masterTradeLiveAssistantActions').getByRole('button',{name:'START LIVE AUTO TRADE',exact:true})
  await expect(start).toBeEnabled()
  fixture.statusCode = statusCode
  await expect(start).toBeDisabled({timeout:12000})
  await expect(page.getByTestId('live-status-unverified')).toBeVisible()
  await expect(page.locator('.masterTradeFocusConnection')).not.toContainText('UNLOCKED')
  if (statusCode !== 403) {
    await expect(page.locator('.masterTradeLiveConfirmations input[type="checkbox"]')).toBeChecked()
    await expect(page.locator('.masterTradeStepper')).toContainText('ARMED')
    fixture.statusCode = 200
    await expect(start).toBeEnabled({timeout:12000})
  }
  expect(fixture.mutations).toEqual([])
})

test('server safety cancellation displays the Turkish reason and leaves start locked', async ({page}) => {
  const fixture = await setup(page)
  await page.goto('/master-trade?tab=canli', {waitUntil:'domcontentloaded'})
  const start = page.locator('.masterTradeLiveAssistantActions').getByRole('button',{name:'START LIVE AUTO TRADE',exact:true})
  await expect(start).toBeEnabled()
  fixture.liveFields = {armed:false,real_trading_locked:true,emergency:{active:true,reason:'TRANSIENT_MARKET_DATA_FAILURE'},live_lock_reason:{code:'TRANSIENT_MARKET_DATA_FAILURE',message:'Piyasa verisi alınamadığı için ARM iptal edildi.',at:new Date().toISOString()}}
  await expect(page.getByTestId('live-arm-cancellation')).toContainText('Piyasa verisi alınamadığı için ARM iptal edildi.',{timeout:12000})
  await expect(start).toBeDisabled()
  await page.setViewportSize({width:390,height:844})
  await expect(page.getByTestId('live-arm-cancellation')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect(fixture.mutations).toEqual([])
})

test('short account rate limit keeps ARM checked but blocks start until verified recovery', async ({page}) => {
  const fixture = await setup(page)
  await page.goto('/master-trade?tab=canli', {waitUntil:'domcontentloaded'})
  const start = page.locator('.masterTradeLiveAssistantActions').getByRole('button',{name:'START LIVE AUTO TRADE',exact:true})
  await expect(start).toBeEnabled()
  fixture.liveFields = {connected:false,readiness:{ready:false},account_rate_limit:{pending_verification:true,locked:false,message:'Binance hesap doğrulaması bekleniyor; ARM korunuyor, yeni girişler kapalı.'}}
  await expect(page.getByTestId('live-account-rate-limit-wait')).toContainText('ARM korunuyor',{timeout:12000})
  await expect(start).toBeDisabled()
  await expect(page.locator('.masterTradeLiveConfirmations input[type="checkbox"]')).toBeChecked()
  await page.setViewportSize({width:390,height:844})
  await expect(page.getByTestId('live-account-rate-limit-wait')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  fixture.liveFields = {account_rate_limit:null}
  await expect(start).toBeEnabled({timeout:12000})
  expect(fixture.mutations).toEqual([])
})

test('cancelled live confirmation sends no request and double confirmation sends exactly one', async ({page}) => {
  const fixture = await setup(page)
  await page.goto('/master-trade?tab=canli', {waitUntil:'domcontentloaded'})
  const start = page.locator('.masterTradeLiveAssistantActions').getByRole('button',{name:'START LIVE AUTO TRADE',exact:true})
  await expect(start).toBeEnabled()
  await start.click()
  await page.locator('.liveConfirm').getByRole('button',{name:'CANCEL',exact:true}).click()
  expect(fixture.mutations).toEqual([])
  await start.click()
  await page.locator('.liveConfirm input').fill('CANLI OTOMATİK')
  await page.locator('.liveConfirm').getByRole('button',{name:'CONFIRM',exact:true}).evaluate(button => {
    button.dispatchEvent(new MouseEvent('click',{bubbles:true}))
    button.dispatchEvent(new MouseEvent('click',{bubbles:true}))
  })
  await expect.poll(() => fixture.mutations.length).toBe(1)
  expect(fixture.mutations).toEqual(['/api/v25/auto/start'])
})

test('an accepted mutation with a failed status refresh is reported as unverified, never retried', async ({page}) => {
  const fixture = await setup(page)
  await page.setViewportSize({width:320,height:844})
  await page.route('**/api/v25/auto/start',async route => {
    fixture.mutations.push('/api/v25/auto/start')
    fixture.statusCode = 503
    await route.fulfill({json:{ok:true}})
  })
  await page.goto('/master-trade?tab=canli',{waitUntil:'domcontentloaded'})
  await page.locator('.masterTradeLiveAssistantActions').getByRole('button',{name:'START LIVE AUTO TRADE',exact:true}).click()
  await page.locator('.liveConfirm input').fill('CANLI OTOMATİK')
  await page.locator('.liveConfirm').getByRole('button',{name:'CONFIRM',exact:true}).click()
  await expect(page.locator('.liveNotice.error').first()).toContainText('LIVE durumu doğrulanamadı')
  await expect(page.locator('.liveNotice.error').first()).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect(fixture.mutations).toEqual(['/api/v25/auto/start'])
})

test('LIVE auto with enabled MFA challenges before activation and sends the fresh code only on confirmation', async ({page}) => {
  const fixture = await setup(page)
  const bodies:Record<string,unknown>[] = []
  let enabled = false
  await page.route('**/api/v25/auto/start', async route => {
    const body = route.request().postDataJSON()
    bodies.push(body)
    if (!body.totp_code) {
      await route.fulfill({status:401,json:{detail:{code:'LIVE_AUTO_MFA_REQUIRED',message:'Güncel 2FA kodunu girin.'}}})
    } else {
      expect(body).toEqual({confirmation:'CANLI OTOMATİK',totp_code:'123456'})
      enabled = true
      fixture.liveFields = {live_auto_trade:true,auto:{enabled:true}}
      await route.fulfill({json:{ok:true}})
    }
  })
  await page.goto('/master-trade?tab=canli',{waitUntil:'domcontentloaded'})
  await page.locator('.masterTradeLiveAssistantActions').getByRole('button',{name:'START LIVE AUTO TRADE',exact:true}).click()
  await page.locator('.liveConfirm input').fill('CANLI OTOMATİK')
  await page.locator('.liveConfirm').getByRole('button',{name:'CONFIRM',exact:true}).click()
  const code = page.locator('.liveConfirm input[type="password"]')
  await expect(code).toBeVisible()
  expect(enabled).toBe(false)
  expect(bodies).toEqual([{confirmation:'CANLI OTOMATİK'}])
  await expect(page.locator('.liveConfirm').getByRole('button',{name:'CONFIRM',exact:true})).toBeDisabled()
  await code.fill('123456')
  await page.locator('.liveConfirm input').first().fill('CANLI OTOMATİK')
  await page.locator('.liveConfirm').getByRole('button',{name:'CONFIRM',exact:true}).click()
  await expect.poll(() => enabled).toBe(true)
  expect(bodies.length).toBe(2)
  await expect(page.locator('.liveConfirm')).toHaveCount(0)
})

test('cancelling the MFA challenge leaves LIVE auto disabled and does not resend', async ({page}) => {
  await setup(page)
  let attempts = 0
  await page.route('**/api/v25/auto/start', async route => {
    attempts++
    await route.fulfill({status:401,json:{detail:{code:'LIVE_AUTO_MFA_REQUIRED'}}})
  })
  await page.goto('/master-trade?tab=canli',{waitUntil:'domcontentloaded'})
  await page.locator('.masterTradeLiveAssistantActions').getByRole('button',{name:'START LIVE AUTO TRADE',exact:true}).click()
  await page.locator('.liveConfirm input').fill('CANLI OTOMATİK')
  await page.locator('.liveConfirm').getByRole('button',{name:'CONFIRM',exact:true}).click()
  await expect(page.locator('.liveConfirm input[type="password"]')).toBeVisible()
  await page.locator('.liveConfirm').getByRole('button',{name:'CANCEL',exact:true}).click()
  await expect(page.locator('.liveConfirm')).toHaveCount(0)
  expect(attempts).toBe(1)
  await expect(page.locator('.masterTradeLiveAssistantActions').getByRole('button',{name:'START LIVE AUTO TRADE',exact:true})).toBeEnabled()
})

test('connection keys stay masked and are cleared once saved even if activation fails', async ({page}) => {
  await setup(page)
  const key = 'offline-private-api-key', secret = 'offline-private-secret'
  const calls:string[] = []
  await page.route('**/api/exchange-connections/{save,activate}', async route => {
    const path = new URL(route.request().url()).pathname
    calls.push(path)
    if (path.endsWith('/save')) {
      expect(route.request().postDataJSON()).toEqual({
        mode:'LIVE',api_key:key,secret_key:secret,confirmation:'CANLI KASAYA KAYDET',
      })
      await route.fulfill({json:{ok:true}})
    } else {
      await route.fulfill({status:502,json:{detail:'Binance Futures yetkisi doğrulanamadı.'}})
    }
  })
  await page.goto('/master-trade?tab=baglanti',{waitUntil:'domcontentloaded'})
  const card = page.locator('.masterTradeLiveConnectionCard')
  const apiInput = card.getByPlaceholder('Paste API key')
  const secretInput = card.getByPlaceholder('Paste secret key')
  await expect(apiInput).toHaveAttribute('type','password')
  await expect(secretInput).toHaveAttribute('type','password')
  await apiInput.fill(key)
  await secretInput.fill(secret)
  await card.getByRole('checkbox').check()
  await card.getByRole('button',{name:'CONNECT ACCOUNT',exact:true}).click()
  await expect(apiInput).toHaveValue('')
  await expect(secretInput).toHaveValue('')
  await expect(page.locator('.liveNotice.error').first()).toContainText('Futures yetkisi')
  const text = await page.locator('body').innerText()
  expect(text).not.toContain(key)
  expect(text).not.toContain(secret)
  expect(calls).toEqual(['/api/exchange-connections/save','/api/exchange-connections/activate'])
})

test('Master Trade connection deletion requires deactivation and explicit confirmation without any order request', async ({page}) => {
  const fixture = await setup(page)
  let active = true, configured = true
  const writes:string[] = []
  await page.route('**/api/exchange-connections/status', route => route.fulfill({
    json:{vault:{ready:true},connections:{LIVE:{active,configured}}},
  }))
  await page.route('**/api/exchange-connections/{deactivate,credentials}', async route => {
    const path = new URL(route.request().url()).pathname
    writes.push(path)
    if (path.endsWith('/deactivate')) {
      expect(route.request().postDataJSON()).toEqual({mode:'LIVE',confirmation:'BAĞLANTIYI KAPAT'})
      active = false
      fixture.liveFields = {connected:false,real_trading_locked:true,live_auto_trade:false}
    } else {
      expect(active).toBe(false)
      expect(route.request().method()).toBe('DELETE')
      expect(route.request().postDataJSON()).toEqual({mode:'LIVE',confirmation:'ANAHTARI KALICI SİL'})
      configured = false
      fixture.liveFields = {...fixture.liveFields,credentials:{configured:false}}
    }
    await route.fulfill({json:{ok:true}})
  })

  await page.goto('/master-trade?tab=baglanti',{waitUntil:'domcontentloaded'})
  const card = page.locator('.masterTradeLiveConnectionCard')
  await expect(card.getByRole('button',{name:'Anahtarı sil',exact:true})).toBeDisabled()
  await card.getByRole('button',{name:'Bağlantıyı kapat',exact:true}).click()
  await page.locator('.liveConfirm input').fill('BAĞLANTIYI KAPAT')
  await page.locator('.liveConfirm').getByRole('button',{name:'CONFIRM',exact:true}).click()
  await expect(card.getByRole('button',{name:'Anahtarı sil',exact:true})).toBeEnabled()
  await card.getByRole('button',{name:'Anahtarı sil',exact:true}).click()
  await page.locator('.liveConfirm input').fill('ANAHTARI KALICI SİL')
  await page.locator('.liveConfirm').getByRole('button',{name:'CONFIRM',exact:true}).click()
  await expect.poll(() => configured).toBe(false)
  expect(writes).toEqual(['/api/exchange-connections/deactivate','/api/exchange-connections/credentials'])
  expect(fixture.mutations).toEqual([])
})

for (const failure of ['futures', 'invalid-key', 'ip', 'server', 'network', 'reflected-credentials'] as const) {
    test(`connection ${failure} failure is shown next to credentials even when status refresh hangs`, async ({page}) => {
      const fixture = await setup(page)
      let failed = false
      let release: (() => void) | undefined
      const writes: string[] = []
      const reasons = {
        futures: 'Binance Futures yetkisi kapalı; API anahtarında Futures işlem iznini açın.',
        'invalid-key': 'Binance API anahtarı geçersiz; API Key ve Secret Key bilgilerini kontrol edin.',
        ip: 'Binance API anahtarı, IP izni veya Futures yetkisi doğrulanamadı. IP kısıtlaması varsa şu IP’yi ekleyin: 8.8.8.8.',
        server: 'Sunucu hatası (HTTP 500)',
        network: 'Bağlantı servisine ulaşılamadı',
        'reflected-credentials': 'Key veya secret hatalı. [GİZLİ] [GİZLİ]',
      }
      await page.route('**/api/v25/status', async route => {
        if (failed) await new Promise<void>(resolve => {release = resolve})
        await route.fallback()
      })
      await page.route('**/api/exchange-connections/save', async route => {
        writes.push(new URL(route.request().url()).pathname)
        failed = true
        if (failure === 'network') await route.abort('failed')
        else if (failure === 'server') await route.fulfill({status: 500, contentType: 'text/html', body: '<html>Internal Server Error</html>'})
        else await route.fulfill({status: 502, json: {detail: failure === 'reflected-credentials'
          ? 'Key veya secret hatalı. offline-private-api-key offline-private-secret' : reasons[failure]}})
      })
      await page.goto('/master-trade?tab=baglanti', {waitUntil: 'domcontentloaded'})
      const card = page.locator('.masterTradeLiveConnectionCard')
      await card.getByPlaceholder('Paste API key').fill('offline-private-api-key')
      await card.getByPlaceholder('Paste secret key').fill('offline-private-secret')
      await card.getByRole('checkbox').check()
      try {
        await card.getByRole('button', {name: 'CONNECT ACCOUNT', exact: true}).click()
        const alert = card.getByRole('alert')
        await expect(alert).toContainText(reasons[failure])
        await expect(alert).toBeInViewport()
        expect(writes).toEqual(['/api/exchange-connections/save'])
        expect(fixture.mutations).toEqual([])
        const body = await page.locator('body').innerText()
        expect(body).not.toContain('offline-private-api-key')
        expect(body).not.toContain('offline-private-secret')
        await expect(card.getByPlaceholder('Paste secret key')).toHaveAttribute('type', 'password')
      } finally {release?.()}
    })
  }

test('LIVE maximum loss is labelled in USDT and emergency close warning is explicit before any request', async ({page}) => {
  const fixture = await setup(page)
  const emergencies:unknown[] = []
  await page.route('**/api/v25/emergency', async route => {
    emergencies.push(route.request().postDataJSON())
    await route.fulfill({json:{ok:true}})
  })
  await page.goto('/master-trade?tab=canli',{waitUntil:'domcontentloaded'})
  const risk = page.locator('label').filter({hasText:'Risk / trade'}).first()
  await expect(risk).toContainText('USDT')
  await expect(risk).not.toContainText('%')
  await page.getByRole('button',{name:'EMERGENCY STOP',exact:true}).first().click()
  await expect(page.locator('.liveConfirm')).toContainText('açık bot pozisyonları reduce-only emirle kapatılır')
  await page.locator('.liveConfirm').getByRole('button',{name:'CANCEL',exact:true}).click()
  expect(fixture.mutations).toEqual([])
  expect(emergencies).toEqual([])
})

test('new scanner and market generations start immediately while old JSON remains pending', async ({page}) => {
  await setup(page)
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    let holdMarkets = true
    window.addEventListener('allow-new-master-market',() => {holdMarkets = false})
    window.fetch = async (input,init) => {
      const response = await original(input,init)
      const url = String(input)
      const holdMarket = holdMarkets && url.includes('/markets?limit=50')
      if (holdMarket || url.includes('/analysis-universe?interval=15m')) {
        const read = response.json.bind(response)
        response.json = async () => {
          const payload:unknown = await read()
          if (holdMarket) document.documentElement.dataset.masterMarketHeld = 'true'
          await new Promise<void>(resolve => window.addEventListener('release-old-master-json',() => resolve(),{once:true}))
          return payload
        }
      }
      return response
    }
  })
  let marketRequests = 0
  const scannerIntervals:string[] = []
  await page.route('**/api/markets**',route => {
    if (new URL(route.request().url()).searchParams.get('limit') === '50') marketRequests++
    return route.fulfill({json:['BTCUSDT','ETHUSDT'].map(symbol => ({symbol,display:symbol,price:symbol === 'BTCUSDT' ? 100 : 200,volume:1000000,change:1}))})
  })
  await page.route('**/api/analysis-universe**',route => {
    const interval = new URL(route.request().url()).searchParams.get('interval') || ''
    scannerIntervals.push(interval)
    const symbol = interval === '15m' ? 'BTCUSDT' : 'ETHUSDT'
    return route.fulfill({json:{results:[{symbol,display:symbol,price:200,direction:'LONG',confidence:88,final_decision_score:88,smart_score:88}]}})
  })
  await page.goto('/master-trade',{waitUntil:'domcontentloaded'})
  await expect(page.locator('html')).toHaveAttribute('data-master-market-held','true')
  await expect.poll(() => [...new Set(scannerIntervals)]).toEqual(['15m'])
  await page.locator('.masterReferenceAnalysis').getByRole('combobox',{name:'Zaman dilimi'}).selectOption('5m')
  await expect.poll(() => scannerIntervals.filter(interval => interval === '5m').length).toBe(1)
  const initialMarketRequests = marketRequests
  await page.evaluate(() => window.dispatchEvent(new Event('allow-new-master-market')))
  await page.locator('.refWatchlist [data-symbol="ETHUSDT"] .refMarketSelect').click()
  await expect.poll(() => marketRequests).toBe(initialMarketRequests + 1)
  await page.evaluate(async () => {
    window.dispatchEvent(new Event('release-old-master-json'))
    await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
  })
  await expect(page.locator('.refReportSymbol strong')).toHaveText('ETHUSDT')
  await expect(page.locator('.refWatchlist [data-symbol="ETHUSDT"]')).toHaveCount(1)
  await expect(page.locator('.refWatchlist [data-symbol="BTCUSDT"]')).toHaveCount(1)
  await expect(page.locator('.refWatchlist [data-symbol="BTCUSDT"] .refMarketScore')).toHaveText('—')
  await expect(page.locator('.refWatchlist [data-symbol="ETHUSDT"] .refMarketScore')).toContainText('88')
})

test('LIVE session recovery requires explicit confirmation and MFA without starting entries', async ({page}) => {
  const fixture = await setup(page)
  fixture.statusCode = 403
  fixture.statusDetail = 'LIVE kontrolü başka bir kullanıcı veya oturuma ait.'
  fixture.liveFields = {armed:false,real_trading_locked:true,live_auto_trade:false}
  await page.route('**/api/v25/session/recover', async route => {
    expect(route.request().method()).toBe('POST')
    expect(route.request().postDataJSON()).toEqual({confirmation:'LIVE OTURUMUNU GERİ BAĞLA',totp_code:'123456'})
    fixture.mutations.push('/api/v25/session/recover')
    fixture.statusCode = 200
    await route.fulfill({json:{ok:true,entries_enabled:false,orders_created:false}})
  })
  await page.goto('/master-trade?tab=canli',{waitUntil:'domcontentloaded'})
  await page.getByTestId('live-status-unverified').getByRole('button',{name:'LIVE oturumunu geri bağla',exact:true}).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText('otomasyon başlamaz')
  await expect(dialog.getByRole('button',{name:'CONFIRM',exact:true})).toBeDisabled()
  await dialog.locator('input').first().fill('LIVE OTURUMUNU GERİ BAĞLA')
  await expect(dialog.getByRole('button',{name:'CONFIRM',exact:true})).toBeDisabled()
  await dialog.locator('input[type="password"]').fill('123456')
  await dialog.getByRole('button',{name:'CONFIRM',exact:true}).click()
  await expect(page.getByTestId('live-status-unverified')).toHaveCount(0)
  await expect(page.locator('.masterTradeLiveAssistantActions').getByRole('button',{name:'START LIVE AUTO TRADE',exact:true})).toBeDisabled()
  expect(fixture.mutations).toEqual(['/api/v25/session/recover'])
})

test('Original runtime shows its scan and approved percentage instead of stale legacy scanner', async ({page}) => {
  const fixture = await setup(page)
  fixture.liveFields = {
    auto:{last_skip_reason:'automation_inactive'}, scanner:{last_cycle_stage:'skipped',last_skip_reason:'automation_inactive'},
    original_v2_live:{available:true,enabled:true,paused:false,risk_percent:1,daily_loss_percent:5,
      symbols:['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT'],
      scanner:{stage:'WAITING_NEXT_CANDLE',last_cycle_at:'2026-10-11T21:01:00Z',last_scan_at:'2026-10-11T21:00:00Z',scanned_symbol_count:8,candidate_count:0}},
  }
  await page.goto('/master-trade?tab=canli',{waitUntil:'domcontentloaded'})
  const runtime = page.getByTestId('original-runtime-status')
  await expect(runtime).toContainText('8 sembol')
  await expect(runtime).toContainText('%1')
  await expect(runtime).toContainText('Sonraki 15m mum bekleniyor')
  await expect(runtime).not.toContainText('Henüz tamamlanmadı')
  await expect(page.locator('.masterTradeLiveOperations')).toContainText('Original V2')
  await expect(page.locator('.masterTradeLiveOperations')).not.toContainText('automation_inactive')
  await expect(page.locator('.masterTradeLiveSetup')).toContainText('V25 SUPERVISED · DEVRE DIŞI')
  await runtime.scrollIntoViewIfNeeded()
  await page.screenshot({path:test.info().outputPath('original-runtime-desktop.png')})
  await page.setViewportSize({width:390,height:844})
  await runtime.scrollIntoViewIfNeeded()
  await expect(runtime).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({path:test.info().outputPath('original-runtime-mobile.png')})
  expect(fixture.mutations).toEqual([])
})

for (const hours of [1,4,12,24]) for (const renewal of [false,true]) test(`Original session ${hours} hours ${renewal ? 'renewal' : 'opening'} requires MFA and displays effective expiry`, async ({page}) => {
  const fixture = await setup(page)
  const original = {available:true,enabled:false,paused:false,risk_percent:0.5,daily_loss_percent:5,symbols:['BTCUSDT'],
    session:renewal ? {hours:4,expires_at:new Date(Date.now() - 60000).toISOString(),remaining_seconds:0,authority_limited:false,warning_sent:true} : null}
  fixture.liveFields = {original_v2_live:original}
  const expires = new Date(Date.now() + 1800000).toISOString()
  await page.route('**/api/v25/original-v2/start', async route => {
    fixture.mutations.push('/api/v25/original-v2/start')
    expect(route.request().postDataJSON()).toEqual({confirmation:'ORIGINAL V2 CANLI',totp_code:'123456',risk_percent:0.5,daily_loss_percent:5,session_hours:hours})
    fixture.liveFields = {live_auto_trade:true,auto:{enabled:true},original_v2_live:{...original,enabled:true,
      session:{hours,expires_at:expires,remaining_seconds:1800,authority_limited:true,warning_sent:false}}}
    await route.fulfill({json:{enabled:true}})
  })
  await page.goto('/master-trade?tab=canli',{waitUntil:'domcontentloaded'})
  const card = page.getByRole('region',{name:'Kais Original V2 LIVE'})
  const duration = card.getByLabel('Oturum süresi',{exact:true})
  await expect(duration).toBeVisible()
  await expect(duration).toHaveValue('1')
  await expect(duration.locator('option')).toHaveText(['1 saat','4 saat','12 saat','24 saat'])
  await duration.selectOption(String(hours))
  await card.getByLabel('ORIGINAL V2 CANLI yazın').fill('ORIGINAL V2 CANLI')
  const start = card.getByRole('button',{name:renewal ? 'Original V2 oturumunu yenile' : 'Original V2 LIVE aç',exact:true})
  await start.click()
  expect(fixture.mutations).toEqual([])
  await card.getByLabel('2FA kodu',{exact:true}).fill('123456')
  await start.click()
  await expect(duration).toBeDisabled()
  await expect(duration).toHaveValue(String(hours))
  await expect(card.getByTestId('original-runtime-status')).toContainText('Yetki süresiyle sınırlı')
  await expect(card.getByTestId('original-runtime-status')).not.toContainText('Oturum yok')
  await expect(card.getByTestId('original-session-remaining')).toContainText('0 sa')
  if (hours === 24 && !renewal) {
    await card.getByTestId('original-runtime-status').scrollIntoViewIfNeeded()
    await page.screenshot({path:test.info().outputPath('original-session-desktop.png')})
    for (const width of [390,320]) {
      await page.setViewportSize({width,height:844})
      await card.getByTestId('original-runtime-status').scrollIntoViewIfNeeded()
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
      await page.screenshot({path:test.info().outputPath(`original-session-${width}.png`)})
    }
  }
  expect(fixture.mutations).toEqual(['/api/v25/original-v2/start'])
})

test('Original persistence diagnostics use one GET and preserve locked trading inputs', async ({page}) => {
  const fixture = await setup(page)
  fixture.liveFields = {original_v2_live:{available:true,enabled:false,paused:true,pause_reason:'Kalıcı kayıt doğrulanamadı.',risk_percent:0.5,daily_loss_percent:5,symbols:['BTCUSDT']}}
  const methods: string[] = []
  await page.route('**/api/v25/original-v2/persistence-diagnostics', route => {
    methods.push(route.request().method())
    return route.fulfill({json:{read_only:true,build_commit:'offline123456',database_status:'CONNECTED',record_exists:true,database_revision:120,local_revision:100,original_record_exists:true,diagnosis:'DATABASE_AHEAD',last_write:{result:'SKIPPED',submitted_revision:99,checked_at:'2026-10-11T20:00:00Z'}}})
  })
  await page.goto('/master-trade?tab=canli',{waitUntil:'domcontentloaded'})
  const card = page.getByRole('region',{name:'Kais Original V2 LIVE'})
  await card.getByLabel('İşlem riski (%)',{exact:true}).fill('0.65')
  await card.getByLabel('2FA kodu',{exact:true}).fill('123456')
  await card.getByRole('button',{name:'Kalıcı kayıt teşhisi',exact:true}).click()
  const result = page.getByTestId('original-persistence-diagnostics')
  await expect(result).toContainText('Veritabanı yerel durumdan ileride')
  await expect(result).toContainText('Veritabanı revision: 120')
  await expect(result).toContainText('Yerel revision: 100')
  await expect(card.getByLabel('İşlem riski (%)',{exact:true})).toHaveValue('0.65')
  await expect(card.getByLabel('2FA kodu',{exact:true})).toHaveValue('123456')
  await expect(card.getByRole('button',{name:'Original V2 LIVE aç',exact:true})).toBeDisabled()
  await page.setViewportSize({width:390,height:844})
  await result.scrollIntoViewIfNeeded()
  await expect(result).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect(methods).toEqual(['GET'])
  expect(fixture.mutations).toEqual([])
})

test('market failures and status polls preserve Original inputs and scroll without scanning hidden analysis', async ({page}) => {
  const fixture = await setup(page)
  fixture.liveFields = {original_v2_live:{
    available:true,enabled:false,paused:false,risk_percent:0.5,daily_loss_percent:5,
    symbols:['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT'],
  }}
  let scannerCalls = 0
  let navigations = 0
  page.on('framenavigated',frame => {if (frame === page.mainFrame()) navigations++})
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    window.fetch = async (input, init) => {
      const response = await original(input, init)
      if (String(input).includes('/exchange-connections/status')) {
        const read = response.json.bind(response)
        response.json = async () => {
          const payload:unknown = await read()
          await new Promise(resolve => setTimeout(resolve,200))
          return payload
        }
      }
      return response
    }
  })
  await page.route('**/api/analysis-universe**',route => {
    scannerCalls++
    return route.fulfill({status:503,json:{detail:'Upstream cooldown active'}})
  })
  await page.route('**/api/{markets*,analysis/**,klines/**}',route =>
    route.fulfill({status:503,headers:{'Retry-After':'180'},json:{detail:'Piyasa verisi geçici olarak alınamıyor.'}}))
  await page.goto('/master-trade?tab=canli',{waitUntil:'domcontentloaded'})
  const card = page.getByRole('region',{name:'Kais Original V2 LIVE'})
  const risk = card.getByLabel('İşlem riski (%)',{exact:true})
  await expect(risk).toBeVisible()
  await expect(page.locator('.v26OfflineNotice')).toHaveCount(0)
  await risk.fill('0.65')
  await card.getByLabel('Oturum süresi',{exact:true}).selectOption('24')
  await card.getByLabel('ORIGINAL V2 CANLI yazın').fill('Henüz onaylamadım')
  await risk.scrollIntoViewIfNeeded()
  const scroll = await page.evaluate(() => window.scrollY)
  expect(scroll).toBeGreaterThan(200)
  const navigationCount = navigations
  await page.waitForTimeout(11500)
  await expect(risk).toHaveValue('0.65')
  await expect(card.getByLabel('Oturum süresi',{exact:true})).toHaveValue('24')
  await expect(card.getByLabel('ORIGINAL V2 CANLI yazın')).toHaveValue('Henüz onaylamadım')
  expect(await page.evaluate(() => window.scrollY)).toBeCloseTo(scroll,0)
  expect(navigations).toBe(navigationCount)
  expect(scannerCalls).toBe(0)
  expect(fixture.mutations).toEqual([])
})

test('a superseded analysis JSON response cannot overwrite the selected symbol snapshot', async ({page}) => {
  await setup(page)
  await page.addInitScript(() => {
    const original = window.fetch.bind(window)
    window.fetch = async (input, init) => {
      const response = await original(input, init)
      const url = String(input)
      if (url.includes('/analysis/BTCUSDT')) {
        const read = response.json.bind(response)
        response.json = async () => {
          const payload:unknown = await read()
          await new Promise<void>(resolve => window.addEventListener('master-release-old-analysis', () => resolve(), {once:true}))
          return payload
        }
      }
      return response
    }
  })
  await page.route('**/api/markets**', route => route.fulfill({json:['BTCUSDT','ETHUSDT'].map(symbol => ({symbol,display:symbol,price:symbol === 'BTCUSDT' ? 100 : 200,volume:1000000,change:1}))}))
  await page.route('**/api/analysis/**', route => {
    const price = route.request().url().includes('ETHUSDT') ? 200 : 100
    return route.fulfill({json:{direction:'LONG',confidence:80,entry:price,stop_loss:price-5,tp1:price+5,tp2:price+10,tp3:price+15}})
  })
  await page.route('**/api/klines/**', route => {
    const price = route.request().url().includes('ETHUSDT') ? 200 : 100
    return route.fulfill({json:Array.from({length:160},(_,index)=>({time:1790185500+index*900,open:price,high:price+1,low:price-1,close:price,volume:1000}))})
  })
  await page.goto('/master-trade', {waitUntil:'domcontentloaded'})
  await page.locator('.refWatchlist [data-symbol="ETHUSDT"] .refMarketSelect').click()
  await expect(page.locator('.refMetrics article:first-child strong')).toContainText('$200.00')
  await page.evaluate(async () => {
    window.dispatchEvent(new Event('master-release-old-analysis'))
    await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
  })
  await expect(page.locator('.refMetrics article:first-child strong')).toContainText('$200.00')
})
