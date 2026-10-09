import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'
import {demoAccount,demoStatus,demoSummary} from './helpers/demo-api'

const strategyId = 'kais-original-v2-demo-v1'
const hash = 'a'.repeat(64)
const plan = {id:'dry-owned-plan',symbol:'BTCUSDT',decision:'BUY',status:'DRY_RUN',strategy_id:strategyId,order_authorized:false,execution_connected:false}
const dryPlans = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','AVAXUSDT']
  .map(symbol => ({...plan,symbol,id:`dry-owned-plan-${symbol}`}))
const summary = {...demoSummary,scanner:{...demoSummary.scanner,all_candidates:[],scan_duration_seconds:0},automation_trades:[]}

async function openDemo(page:Page,root:string,armed=true,flags={enabled:false,send_orders:false},malformed=false) {
  await page.route('**/*',route => {
    const url = new URL(route.request().url())
    return url.hostname === '127.0.0.1' ? route.fallback() : route.abort()
  })
  await page.routeWebSocket('**/*',socket => socket.close())
  const state = await mockAssistant(page)
  const mutations:Array<{path:string;body:unknown}> = []
  let failure = false
  let plans:typeof plan[] = []
  let automation = {...demoSummary.auto}
  await page.route('**/api/binance-demo/status',route => route.fulfill({json:{...demoStatus,configured:true,connected:true,armed}}))
  await page.route('**/api/binance-demo/account',route => route.fulfill({json:{...demoAccount,configured:true,connected:true,armed}}))
  await page.route('**/api/v21/summary',route => route.fulfill({json:{...summary,auto:automation}}))
  await page.route('**/api/v21/original-v2/status',route => route.fulfill({json:malformed ? {} : {
    strategy_id:strategyId,flags,selected_strategy_id:null,profile_hash:hash,policy_hash:hash,dry_run_plans:plans,
  }}))
  await page.route('**/api/v21/original-v2/dry-run',async route => {
    mutations.push({path:new URL(route.request().url()).pathname,body:route.request().postDataJSON()})
    plans = dryPlans
    await route.fulfill({json:{ok:true,dry_run:true,order:null,orders_sent:0,plans}})
  })
  await page.route('**/api/v21/auto/start',async route => {
    mutations.push({path:new URL(route.request().url()).pathname,body:route.request().postDataJSON()})
    automation = {...automation,enabled:!failure,last_error:failure ? 'Synthetic first cycle failed' : null}
    await route.fulfill({json:{...summary,auto:automation}})
  })
  await page.goto(root)
  if (root.endsWith(':4173')) await page.getByRole('button',{name:/TESTNET KOMUTA/}).click()
  else await page.locator('.v26DashboardChoices button').filter({has:page.locator('b',{hasText:/^İŞLEM$/})}).click()
  await page.getByRole('button',{name:/OTOMASYON/}).first().click()
  await expect(page.getByRole('region',{name:'Kais Original Demo profili'})).toBeVisible()
  return {state,mutations,setFailure:() => { failure = true }}
}

for (const root of ['http://127.0.0.1:4173','http://127.0.0.1:4174']) {
  test(`${root} Original selection and read-only flags never mutate settings`,async ({page}) => {
    const fixture = await openDemo(page,root)
    const panel = page.getByRole('region',{name:'Kais Original Demo profili'})
    await expect(panel.getByText('Kapalı',{exact:true})).toHaveCount(2)
    await expect(page.locator('.v21AutoRules')).toBeVisible()
    await page.getByLabel('Demo stratejisi').selectOption(strategyId)
    await expect(panel).toContainText('3x · risk 3 USDT')
    await expect(panel).toContainText('TP2 kapanışı yok')
    await expect(page.locator('.v21AutoRules')).toHaveCount(0)
    await expect(page.locator('.v21GateStrip')).toHaveCount(0)
    await page.screenshot({path:`C:\\Users\\ahmtt\\.copilot\\session-state\\301a903d-273a-4d29-87b4-b58003522a5e\\files\\demo-original-mocked-${root.endsWith(':4173') ? 'compat' : 'root'}.png`,fullPage:true})
    expect(fixture.mutations).toEqual([])
    expect(fixture.state.requests.filter(request => !request.startsWith('GET '))).toEqual([])
  })

  test(`${root} both flags on explicit dry-run records no orders and requires second confirmation`,async ({page}) => {
    const fixture = await openDemo(page,root,true,{enabled:true,send_orders:true})
    await page.getByLabel('Demo stratejisi').selectOption(strategyId)
    const dryRun = page.getByRole('button',{name:'Emirsiz tek karar döngüsü',exact:true})
    await expect(dryRun).toBeDisabled()
    await page.locator('.v21AutoControl').filter({has:page.getByRole('heading',{name:'Demo Otomasyon Motoru',exact:true})}).getByPlaceholder('DEMO OTOMATİK',{exact:true}).fill('DEMO OTOMATİK')
    await dryRun.click()
    await expect(page.getByRole('status').filter({hasText:'gönderilen emir: 0'})).toBeVisible()
    await page.getByText('Plan kayıtları ve sabit politika',{exact:true}).click()
    await expect(page.getByRole('region',{name:'Kais Original Demo profili'})).toContainText('dry-owned-plan')
    expect(fixture.mutations).toEqual([{path:'/api/v21/original-v2/dry-run',body:{strategy_id:strategyId,confirmation:'DEMO OTOMATİK'}}])
    expect(fixture.state.requests.filter(request => !request.startsWith('GET '))).toEqual([])
  })

  test(`${root} disarmed Original cannot run and blank approval does not start automation`,async ({page}) => {
    const fixture = await openDemo(page,root,false)
    await page.getByLabel('Demo stratejisi').selectOption(strategyId)
    await page.locator('.v21AutoControl').filter({has:page.getByRole('heading',{name:'Demo Otomasyon Motoru',exact:true})}).getByPlaceholder('DEMO OTOMATİK',{exact:true}).fill('DEMO OTOMATİK')
    await expect(page.getByRole('button',{name:'Emirsiz tek karar döngüsü',exact:true})).toBeDisabled()
    expect(fixture.mutations).toEqual([])
  })

  test(`${root} legacy flag-off start keeps its payload and blank approval is blocked`,async ({page}) => {
    const fixture = await openDemo(page,root)
    await page.getByRole('button',{name:'KONTROLLÜ DEMO OTOMASYONU BAŞLAT',exact:true}).click()
    expect(fixture.mutations).toEqual([])
    await page.locator('.v21AutoControl').filter({has:page.getByRole('heading',{name:'Demo Otomasyon Motoru',exact:true})}).getByPlaceholder('DEMO OTOMATİK',{exact:true}).fill('DEMO OTOMATİK')
    await page.getByRole('button',{name:'KONTROLLÜ DEMO OTOMASYONU BAŞLAT',exact:true}).click()
    expect(fixture.mutations).toEqual([{path:'/api/v21/auto/start',body:{confirmation:'DEMO OTOMATİK'}}])
  })

  test(`${root} Original start sends identity and failed first cycle is not shown as success`,async ({page}) => {
    const fixture = await openDemo(page,root)
    fixture.setFailure()
    await page.getByLabel('Demo stratejisi').selectOption(strategyId)
    await page.locator('.v21AutoControl').filter({has:page.getByRole('heading',{name:'Demo Otomasyon Motoru',exact:true})}).getByPlaceholder('DEMO OTOMATİK',{exact:true}).fill('DEMO OTOMATİK')
    await page.getByRole('button',{name:'KONTROLLÜ DEMO OTOMASYONU BAŞLAT',exact:true}).click()
    await expect(page.locator('.demoMessage')).toContainText('Synthetic first cycle failed')
    expect(fixture.mutations).toEqual([{path:'/api/v21/auto/start',body:{strategy_id:strategyId,confirmation:'DEMO OTOMATİK'}}])
  })

  test(`${root} malformed flag status is explicit and cannot authorize dry-run`,async ({page}) => {
    const fixture = await openDemo(page,root,true,{enabled:false,send_orders:false},true)
    await page.getByLabel('Demo stratejisi').selectOption(strategyId)
    const panel = page.getByRole('region',{name:'Kais Original Demo profili'})
    await expect(panel.getByRole('alert')).toContainText('durum yanıtı geçersiz')
    await expect(panel.getByRole('button',{name:'Emirsiz tek karar döngüsü',exact:true})).toBeDisabled()
    await expect(panel.getByText('Kapalı',{exact:true})).toHaveCount(0)
    expect(fixture.mutations).toEqual([])
  })
}
