import {expect,test,type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

test.use({baseURL:'http://127.0.0.1:4174'})
async function setup(page:Page,{binance=true,verified=true,expired=false}={}) {
  const assistant = await mockAssistant(page)
  const calls:string[] = []
  const now = Date.now()
  let active = false
  const state = () => ({enabled:true,used:active || expired,active:active && !expired,
    started_at:active || expired ? new Date(now-(expired ? 8 : 0)*86400000).toISOString() : null,
    ends_at:active || expired ? new Date(now+(expired ? -1 : 7)*86400000).toISOString() : null})
  const headers = {'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'GET, POST, OPTIONS'}
  await page.route('**/api/v22/subscription/config',route => route.fulfill({
    json:{stripe_enabled:true,binance_enabled:binance,free_trial_enabled:true},headers,
  }))
  await page.route('**/api/v22/free-trial**',route => {
    if (route.request().method() === 'OPTIONS') return route.fulfill({status:204,headers})
    if (route.request().method() === 'POST') {
      calls.push(route.request().url())
      if (!verified) return route.fulfill({status:403,json:{detail:'Deneme için e-posta adresinizi doğrulayın.'},headers})
      active = true
    }
    return route.fulfill({json:state(),headers})
  })
  await page.route('**/api/v22/payments/binance-manual/**',route => route.fulfill({
    json:route.request().url().endsWith('/config') ? {enabled:true,amount:'119.90',currency:'USDT',pay_id:'LOCAL-TEST'} : {request:null,period:null},
    headers,
  }))
  await page.route('**/api/v22/profile',route => route.fulfill({json:{
    user:{id:'assistant-member',role:'CUSTOMER',active:true,email_verified:verified},
    access:{isPremium:active,assistantAllowed:!active,freeTrial:active,canExecuteLive:false},
  },headers}))
  await page.goto('/pricing',{waitUntil:'domcontentloaded'})
  await expect(page.getByRole('heading',{name:'1 Hafta Ücretsiz Deneme'})).toBeVisible()
  return {calls,assistant}
}

test('Two centered cards hide Stripe even when configured; trial starts once without checkout and disables assistant',async ({page}) => {
  const {calls,assistant} = await setup(page)
  await expect(page.locator('.freeTrialPlans .subscriptionPlan')).toHaveCount(2)
  for (const width of [1440,1280]) {
    await page.setViewportSize({width,height:900})
    const geometry = await page.locator('.freeTrialPlans').evaluate(element => {
      const bounds = element.getBoundingClientRect()
      const cards = [...element.querySelectorAll('.subscriptionPlan')].map(card => {
        const rect = card.getBoundingClientRect()
        return {x:rect.x,y:rect.y,width:rect.width,height:rect.height}
      })
      return {x:bounds.x,width:bounds.width,cards,overflow:document.documentElement.scrollWidth > window.innerWidth}
    })
    expect(geometry.overflow).toBe(false)
    expect(Math.abs(geometry.x-(width-geometry.width)/2)).toBeLessThanOrEqual(2)
    expect(Math.abs(geometry.cards[0].width-geometry.cards[1].width)).toBeLessThanOrEqual(1)
    expect(Math.abs(geometry.cards[0].height-geometry.cards[1].height)).toBeLessThanOrEqual(1)
    expect(geometry.cards[0].y).toBe(geometry.cards[1].y)
    expect(geometry.cards[1].x).toBeGreaterThan(geometry.cards[0].x+geometry.cards[0].width)
  }
  await expect(page.getByText('119.90 USD',{exact:true})).toBeVisible()
  await expect(page.getByRole('button',{name:'START FREE TRIAL'})).toHaveCount(0)
  await expect(page.getByText('SECURE STRIPE BILLING',{exact:true})).toHaveCount(0)
  await page.getByRole('button',{name:'Ücretsiz denemeyi başlat',exact:true}).click()
  await expect(page.getByText('Kalan süre:')).toContainText('7 gün')
  await expect(page.getByRole('button',{name:'Deneme aktif',exact:true})).toBeDisabled()
  await expect(page.locator('.assistantLauncher')).toHaveCount(0)
  expect(calls).toHaveLength(1)
  expect(assistant.requests.filter(path => /^POST .*subscription/.test(path))).toEqual([])
  await expect(page.getByText('LIVE işlem MASTER MODE ile açılır',{exact:true})).toBeVisible()
})

test('Unverified account gets explicit refusal without opening trial',async ({page}) => {
  await setup(page,{verified:false})
  await page.getByRole('button',{name:'Ücretsiz denemeyi başlat',exact:true}).click()
  await expect(page.getByRole('alert')).toContainText('e-posta adresinizi doğrulayın')
  await expect(page.getByRole('button',{name:'Deneme aktif',exact:true})).toHaveCount(0)
})

test('Expired trial keeps single-use lock and upgrade message; unavailable payment is Yakında',async ({page}) => {
  const {calls} = await setup(page,{binance:false,expired:true})
  await expect(page.getByRole('button',{name:'Deneme hakkı kullanıldı',exact:true})).toBeDisabled()
  await expect(page.getByRole('button',{name:'Yakında',exact:true})).toBeDisabled()
  await expect(page.getByText('Deneme sona erdi. Erişimini sürdürmek için MASTER MODE’a geç.')).toBeVisible()
  expect(calls).toEqual([])
})

test('MASTER MODE button opens existing Binance payment without starting a trial',async ({page}) => {
  const {calls} = await setup(page)
  await expect(page.getByRole('region',{name:'Binance ile ödeme'})).toHaveCount(0)
  await page.getByRole('button',{name:'Binance ile öde',exact:true}).click()
  await expect(page.getByRole('region',{name:'Binance ile ödeme'})).toBeVisible()
  expect(calls).toEqual([])
})

test('Payment-free trial billing never presents automatic Stripe charging or management',async ({page}) => {
  await setup(page)
  const now = Date.now()
  await page.route('**/api/v22/subscription',route => route.fulfill({
    json:{mode:'FREE_TRIAL',plan:'TRIAL',status:'TRIALING',master_trade_access:true,
      trial_end:new Date(now+7*86400000).toISOString(),current_period_end:new Date(now+7*86400000).toISOString()},
    headers:{'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*'},
  }))
  await page.getByRole('button',{name:'ABONELİK',exact:true}).click()
  await expect(page.getByRole('heading',{name:'Abonelik yönetimi',exact:true})).toBeVisible()
  await expect(page.getByRole('heading',{name:'1 Hafta Ücretsiz Deneme',exact:true})).toBeVisible()
  await expect(page.getByText('Kalan deneme süresi: 7 gün',{exact:true})).toBeVisible()
  await expect(page.getByText(/Stripe will automatically charge/)).toHaveCount(0)
  await expect(page.getByRole('button',{name:/MANAGE BILLING|CANCEL SUBSCRIPTION/})).toHaveCount(0)
})
