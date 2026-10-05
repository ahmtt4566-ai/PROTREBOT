import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

test.use({baseURL:'http://127.0.0.1:4174'})
test.setTimeout(60000)

async function setup(page:Page) {
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  const mock = await mockAssistant(page)
  const fixture = {statusCode:200, mutations:[] as string[], requests:mock.requests, liveFields:{} as Record<string,unknown>}
  const user = {id:'master-owner', role:'OWNER', active:true, email_verified:true}
  await page.route('**/api/v22/{profile,session}', route => route.fulfill({json:{user, access:{isPremium:true, canAccessMasterTrade:true}}}))
  await page.route('**/api/v25/status', route => route.fulfill({status:fixture.statusCode, json:fixture.statusCode !== 200 ? {detail:'Status unavailable'} : {
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
