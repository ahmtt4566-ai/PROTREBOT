import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'
import {demoAccount, demoStatus, demoSummary} from './helpers/demo-api'

const pageErrors = new WeakMap<Page,string[]>()
test.beforeEach(({page}) => {
  const errors:string[] = []
  pageErrors.set(page,errors)
  page.on('pageerror',error => errors.push(error.message))
})
test.afterEach(({page}) => expect(pageErrors.get(page)).toEqual([]))

async function prepare(page:Page) {
  await page.route('**/*',route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.fallback() : route.abort())
  await page.routeWebSocket('**/*',socket => socket.close())
  return mockAssistant(page)
}

async function enterTrading(page:Page,root:string) {
  await page.goto(root)
  if (root.endsWith(':4173')) await page.getByRole('button',{name:/TESTNET KOMUTA/}).click()
  else await page.locator('.v26DashboardChoices button').filter({has:page.locator('b',{hasText:/^İŞLEM$/})}).click()
  await expect(page.getByRole('region',{name:'Binance Futures Demo Köprüsü'})).toBeVisible()
}

for (const root of ['http://127.0.0.1:4173','http://127.0.0.1:4176']) {
  test(`${root} missing Demo limits never crashes trading and a valid retry recovers`,async ({page}) => {
    const state = await prepare(page)
    let repaired = false
    const {limits: _limits,...partialStatus} = demoStatus
    await page.route('**/api/binance-demo/status',route => route.fulfill({json:repaired ? demoStatus : partialStatus}))
    await enterTrading(page,root)
    await expect(page.locator('.demoMessage[role="alert"]')).toContainText('Demo durum yanıtı')
    await expect(page.locator('.appRecoveryShell')).toHaveCount(0)
    repaired = true
    await page.getByRole('button',{name:'Demo verilerini yeniden getir',exact:true}).click()
    await expect(page.locator('.demoMessage[role="alert"]')).toHaveCount(0)
    expect(state.requests.filter(request => /order|auto\/start|\/arm/.test(request))).toEqual([])
  })

  test(`${root} HTML success and incomplete summary cannot become trading state`,async ({page}) => {
    await prepare(page)
    await page.route('**/api/binance-demo/status',route => route.fulfill({contentType:'text/html',body:'<!doctype html><title>Proxy fallback</title>'}))
    await page.route('**/api/v21/summary',route => route.fulfill({json:{auto:demoSummary.auto}}))
    await enterTrading(page,root)
    await expect(page.locator('.demoMessage[role="alert"]')).toContainText('yanıt')
    await expect(page.locator('.appRecoveryShell')).toHaveCount(0)
  })

  test(`${root} account response cannot erase verified trading limits`,async ({page}) => {
    await prepare(page)
    await page.route('**/api/binance-demo/status',route => route.fulfill({json:{...demoStatus,configured:true}}))
    const {limits: _limits,...account} = demoAccount
    await page.route('**/api/binance-demo/account',route => route.fulfill({json:{...account,configured:true,connected:true}}))
    await enterTrading(page,root)
    await expect(page.locator('.appRecoveryShell')).toHaveCount(0)
    await expect(page.locator('.binanceDemoDeck')).toContainText(root.endsWith(':4173') ? '/ 5' : 'Max 5')
  })

  test(`${root} slow reads never overlap and a stalled response has a deadline`,async ({page}) => {
    await prepare(page)
    await page.clock.install()
    let requests = 0
    await page.route('**/api/binance-demo/status',() => { requests++ })
    await enterTrading(page,root)
    await expect.poll(() => requests).toBeGreaterThan(0)
    const initial = requests
    await page.clock.fastForward(25000)
    expect(requests).toBe(initial)
    await page.clock.fastForward(5001)
    await expect(page.locator('.demoMessage[role="alert"]')).toContainText('zamanında yanıt vermedi')
    await expect(page.locator('.appRecoveryShell')).toHaveCount(0)
  })
}

async function quickFixture(page:Page,root:string,flags={enabled:false,send_orders:false},failedPath='',firstCycleFailed=false) {
  await prepare(page)
  let status = {...demoStatus,configured:root.endsWith(':4173')}
  let summary = structuredClone(demoSummary)
  const mutations:Array<{path:string;body:unknown}> = []
  let selected:string|null = null
  await page.route('**/api/binance-demo/status',route => route.fulfill({json:status}))
  await page.route('**/api/binance-demo/account',route => route.fulfill({json:{...demoAccount,...status}}))
  await page.route('**/api/v21/summary',route => route.fulfill({json:summary}))
  await page.route('**/api/v21/original-v2/status',route => route.fulfill({json:{
    strategy_id:'kais-original-v2-demo-v1',flags,profile_hash:'a'.repeat(64),policy_hash:'b'.repeat(64),
    selected_strategy_id:selected,dry_run_plans:[],
  }}))
  for (const path of ['/exchange-connections/test','/exchange-connections/save','/exchange-connections/activate','/binance-demo/connect','/binance-demo/arm','/v21/auto/start']) {
    await page.route(`**/api${path}`,async route => {
      const body = route.request().postData() ? route.request().postDataJSON() : null
      mutations.push({path,body})
      if (path === failedPath) { await route.fulfill({status:503,json:{detail:'Sentetik Demo bağlantı hatası'}});return }
      if (path === '/binance-demo/arm') status = {...status,configured:true,connected:true,armed:true}
      if (path === '/v21/auto/start') {
        selected = body.strategy_id || (flags.enabled ? 'kais-original-v2-demo-v1' : null)
        summary = {...summary,auto:{...summary.auto,enabled:!firstCycleFailed,last_error:firstCycleFailed ? 'Sentetik ilk döngü hatası' : null}}
      }
      await route.fulfill({json:path === '/binance-demo/arm' ? status : path === '/v21/auto/start' ? summary : {ok:true}})
    })
  }
  await enterTrading(page,root)
  if (!root.endsWith(':4173')) {
    await page.getByLabel('Demo API Key',{exact:true}).fill('SyntheticDemoApiKey12345')
    await page.getByLabel('Demo Secret Key',{exact:true}).fill('SyntheticDemoSecret12345')
  }
  return mutations
}

test('mobile production Demo key setup remains accessible and prepares without orders',async ({page}) => {
  await page.setViewportSize({width:390,height:844})
  const mutations = await quickFixture(page,'http://127.0.0.1:4176')
  for (const name of ['Demo API Key','Demo Secret Key']) {
    const field = page.getByLabel(name,{exact:true})
    await expect(field).toBeVisible()
    const bounds = await field.boundingBox()
    expect(bounds).not.toBeNull()
    expect(bounds!.x).toBeGreaterThanOrEqual(0)
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390)
    expect(bounds!.height).toBeGreaterThanOrEqual(44)
  }
  expect(await page.locator('.demoRecoveryArea').evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true)
  await page.getByRole('button',{name:'Demo bağlantısını hazırla',exact:true}).click()
  await expect(page.locator('.demoMessage-ok')).toContainText('Emir gönderilmedi')
  expect(mutations.map(request => request.path)).toEqual([
    '/exchange-connections/test','/exchange-connections/save','/exchange-connections/activate',
    '/binance-demo/connect','/binance-demo/arm',
  ])
})

for (const root of ['http://127.0.0.1:4173','http://127.0.0.1:4176']) {
  test(`${root} explicit prepare obtains only Demo readiness and sends zero orders`,async ({page}) => {
    const mutations = await quickFixture(page,root)
    await page.getByRole('button',{name:'Demo bağlantısını hazırla',exact:true}).click()
    await expect(page.locator('.demoMessage-ok')).toContainText('Emir gönderilmedi')
    expect(mutations.map(request => request.path)).toEqual([
      '/exchange-connections/test',...(root.endsWith(':4173') ? [] : ['/exchange-connections/save']),
      '/exchange-connections/activate','/binance-demo/connect','/binance-demo/arm',
    ])
    expect(mutations.at(-1)?.body).toEqual({confirmation:'DEMO'})
    expect(mutations.filter(request => /v25|order|auto\/start/.test(request.path))).toEqual([])
  })

  for (const flags of [{enabled:false,send_orders:false},{enabled:true,send_orders:true}]) {
    test(`${root} one explicit Demo start works without typed approvals with flags ${JSON.stringify(flags)}`,async ({page}) => {
      const mutations = await quickFixture(page,root,flags)
      await page.getByRole('button',{name:/OTOMASYON/}).first().click()
      if (flags.enabled) await page.getByLabel('Demo stratejisi').selectOption('kais-original-v2-demo-v1')
      await page.getByRole('button',{name:"Demo'yu hazırla ve otomasyonu başlat",exact:true}).click()
      await expect(page.locator('.demoMessage-ok')).toContainText('Demo otomasyonu başlatıldı')
      expect(mutations.at(-1)).toEqual({path:'/v21/auto/start',body:{
        confirmation:'DEMO OTOMATİK',...(flags.enabled ? {strategy_id:'kais-original-v2-demo-v1'} : {}),
      }})
      expect(mutations.filter(request => /v25|\/order/.test(request.path))).toEqual([])
    })
  }

  test(`${root} Original cannot silently start legacy or send when its server flags are off`,async ({page}) => {
    const mutations = await quickFixture(page,root)
    await page.getByRole('button',{name:/OTOMASYON/}).first().click()
    await page.getByLabel('Demo stratejisi').selectOption('kais-original-v2-demo-v1')
    await page.getByRole('button',{name:"Demo'yu hazırla ve otomasyonu başlat",exact:true}).click()
    await expect(page.locator('.demoMessage-error')).toContainText('Kais Original Demo emirleri sunucu ayarında kapalı')
    expect(mutations).toEqual([])
  })

  test(`${root} a failed Demo preparation step never arms or starts automation`,async ({page}) => {
    const mutations = await quickFixture(page,root,{enabled:false,send_orders:false},'/exchange-connections/activate')
    await page.getByRole('button',{name:/OTOMASYON/}).first().click()
    await page.getByRole('button',{name:"Demo'yu hazırla ve otomasyonu başlat",exact:true}).click()
    await expect(page.locator('.demoMessage-error')).toContainText('Sentetik Demo bağlantı hatası')
    expect(mutations.at(-1)?.path).toBe('/exchange-connections/activate')
    expect(mutations.filter(request => /v25|order|auto\/start|\/arm/.test(request.path))).toEqual([])
  })

  test(`${root} a failed first cycle is an error, never successful Demo activation`,async ({page}) => {
    await quickFixture(page,root,{enabled:true,send_orders:true},'',true)
    await page.getByRole('button',{name:/OTOMASYON/}).first().click()
    await page.getByLabel('Demo stratejisi').selectOption('kais-original-v2-demo-v1')
    await page.getByRole('button',{name:"Demo'yu hazırla ve otomasyonu başlat",exact:true}).click()
    await expect(page.locator('.demoMessage-error')).toContainText('Sentetik ilk döngü hatası')
    await expect(page.locator('.demoMessage-ok')).toHaveCount(0)
  })
}
