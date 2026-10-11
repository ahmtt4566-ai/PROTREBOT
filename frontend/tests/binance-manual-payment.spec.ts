import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

test.use({baseURL:'http://127.0.0.1:4174'})
const requestRow = {
  id:'a'.repeat(32),reference_code:'KT-ABC234',amount:'119.90',currency:'USDT',method:'binance_manual',
  status:'created',transaction_number:null as string|null,expires_at:'2026-10-13T00:00:00Z',
}
async function setup(page:Page,{enabled=true,stripeEnabled=true,submitFails=false,paid=false,approvedPeriod=false} = {}) {
  const assistant = await mockAssistant(page)
  const calls:{path:string;body:unknown}[] = []
  let row:typeof requestRow|null = approvedPeriod ? {...requestRow,status:'approved',transaction_number:'BINANCE-123'} : null
  await page.route('**/api/v22/subscription/config',route => route.fulfill({
    json:{stripe_enabled:stripeEnabled,binance_enabled:enabled},
    headers:{'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*'},
  }))
  await page.route('**/api/v22/subscription',route => route.fulfill({
    json:{status:'EXPIRED',plan:null,master_trade_access:false},
    headers:{'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*'},
  }))
  await page.addInitScript(() => {
    Object.defineProperty(navigator,'clipboard',{value:{writeText:async (text:string) => {
      sessionStorage.setItem('copied-binance-reference',text)
    }}})
  })
  await page.route('**/api/v22/payments/binance-manual/**',async route => {
    const path = new URL(route.request().url()).pathname
    const headers = {'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'GET, POST, OPTIONS'}
    if (route.request().method() === 'OPTIONS') {await route.fulfill({status:204,headers});return}
    const respond = (json:unknown,status=200) => route.fulfill({json,status,headers})
    if (path.endsWith('/config')) {await respond(enabled ? {enabled:true,amount:'119.90',currency:'USDT',pay_id:'987654321'} : {enabled:false});return}
    if (path.endsWith('/current')) {await respond({request:row,period:approvedPeriod ? {period_start:'2026-10-18T10:00:00Z',period_end:'2026-11-17T10:00:00Z'} : null});return}
    calls.push({path,body:route.request().postData() ? route.request().postDataJSON() : null})
    if (path.endsWith('/submit')) {
      if (submitFails) {await respond({detail:'Bu Binance işlem numarası başka bir talepte kullanılmış.'},409);return}
      row = {...requestRow,status:'pending',transaction_number:'BINANCE-123'}
    } else {
      if (paid) {await respond({detail:'Ücretli Stripe aboneliğiniz aktif.'},409);return}
      row = {...requestRow}
    }
    await respond(row)
  })
  await page.goto('/pricing',{waitUntil:'domcontentloaded'})
  if (stripeEnabled) await expect(page.getByRole('button',{name:'START MASTER MODE'})).toBeVisible()
  else if (enabled) await expect(page.getByRole('region',{name:'Binance ile ödeme'})).toBeVisible()
  else await expect(page.getByText('Ödeme yakında açılacak.',{exact:true})).toBeVisible()
  return {calls,assistant}
}

test('Missing Pay ID hides the method and preserves Stripe buttons',async ({page}) => {
  const {calls} = await setup(page,{enabled:false})
  await expect(page.getByRole('button',{name:'Binance ile öde',exact:true})).toHaveCount(0)
  await expect(page.getByRole('button',{name:'START FREE TRIAL'})).toBeVisible()
  await expect(page.getByRole('button',{name:'START MASTER MODE'})).toBeVisible()
  expect(calls).toEqual([])
})

test('Unconfigured Stripe hides both plans and security note; Binance offers 30 days without trial',async ({page}) => {
  const {calls,assistant} = await setup(page,{stripeEnabled:false})
  await expect(page.locator('.subscriptionPlan')).toHaveCount(0)
  await expect(page.getByRole('button',{name:'START FREE TRIAL'})).toHaveCount(0)
  await expect(page.getByRole('button',{name:'START MASTER MODE'})).toHaveCount(0)
  await expect(page.locator('.subscriptionSecurityNote')).toHaveCount(0)
  await expect(page.getByText('30 günlük MASTER MODE · Ücretsiz deneme yok. Otomatik yenileme yok.')).toBeVisible()
  await page.getByRole('button',{name:'Binance ile öde',exact:true}).click()
  expect(calls.map(call => call.body)).toEqual([null])
  expect(assistant.requests.filter(path => /^POST .*subscription/.test(path))).toEqual([])
  await page.getByRole('button',{name:'BILLING',exact:true}).click()
  await expect(page.getByRole('region',{name:'Binance ile ödeme'})).toBeVisible()
  await expect(page.getByRole('heading',{name:'Manage billing'})).toHaveCount(0)
  await expect(page.getByText('Billing history is managed in Stripe',{exact:true})).toHaveCount(0)
  await expect(page.getByText('Secure Stripe billing',{exact:true})).toHaveCount(0)
})

test('No configured payment method shows a quiet unavailable message in pricing and billing',async ({page}) => {
  const {calls} = await setup(page,{stripeEnabled:false,enabled:false})
  await expect(page.locator('.subscriptionPlan')).toHaveCount(0)
  await expect(page.getByRole('region',{name:'Binance ile ödeme'})).toHaveCount(0)
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.getByRole('button',{name:'BILLING',exact:true}).click()
  await expect(page.getByText('Ödeme yakında açılacak.',{exact:true})).toBeVisible()
  await expect(page.getByRole('heading',{name:'Manage billing'})).toHaveCount(0)
  expect(calls).toEqual([])
})

test('Stripe configuration becoming available restores the unchanged plans and security note',async ({page}) => {
  await setup(page,{stripeEnabled:false})
  await page.route('**/api/v22/subscription/config',route => route.fulfill({
    json:{stripe_enabled:true,binance_enabled:true},
    headers:{'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*'},
  }))
  await page.reload({waitUntil:'domcontentloaded'})
  await expect(page.getByRole('button',{name:'START FREE TRIAL'})).toBeVisible()
  await expect(page.getByRole('button',{name:'START MASTER MODE'})).toBeVisible()
  await expect(page.getByText('SECURE STRIPE BILLING',{exact:true})).toBeVisible()
  await expect(page.getByText('7 days free',{exact:true})).toBeVisible()
})

test('Configuration API failure never invents available methods or an empty configuration',async ({page}) => {
  await setup(page)
  await page.route('**/api/v22/subscription/config',route => route.fulfill({
    status:503,json:{detail:'Ödeme yöntemleri şu anda yüklenemiyor.'},
    headers:{'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*'},
  }))
  await page.reload({waitUntil:'domcontentloaded'})
  await expect(page.getByRole('alert')).toContainText('Ödeme yöntemleri şu anda yüklenemiyor.')
  await expect(page.getByRole('button',{name:'START FREE TRIAL'})).toHaveCount(0)
  await expect(page.getByRole('region',{name:'Binance ile ödeme'})).toHaveCount(0)
  await expect(page.getByText('Ödeme yakında açılacak.',{exact:true})).toHaveCount(0)
})

test('Server payment data and copyable note lead to pending without entitlement or Stripe writes',async ({page}) => {
  const {calls,assistant} = await setup(page)
  await page.getByRole('button',{name:'Binance ile öde',exact:true}).click()
  const card = page.getByRole('region',{name:'Binance ile ödeme'})
  await expect(card.getByText('119.90 USDT',{exact:true})).toBeVisible()
  await expect(card.getByText('987654321',{exact:true})).toBeVisible()
  await expect(card.getByText('Ödeme notuna bu kodu yaz:',{exact:true})).toBeVisible()
  await card.getByRole('button',{name:'Referans kodunu kopyala'}).click()
  expect(await page.evaluate(() => sessionStorage.getItem('copied-binance-reference'))).toBe('KT-ABC234')
  await card.getByLabel('Binance işlem / sipariş numarası').fill('BINANCE-123')
  await card.getByRole('button',{name:'Ödemeyi bildirdim'}).click()
  await expect(card.getByText('Bekliyor',{exact:true})).toBeVisible()
  await expect(card.getByText(/Bu işlem erişim açmaz/)).toBeVisible()
  expect(calls.map(call => call.body)).toEqual([null,{transaction_number:'BINANCE-123'}])
  expect(assistant.requests.filter(path => /^POST .*subscription|^POST .*activate/.test(path))).toEqual([])
  await page.getByRole('button',{name:'BILLING',exact:true}).click()
  await expect(page.getByText('Bekliyor',{exact:true})).toBeVisible()
})

test('Duplicate transaction error retains form and never shows pending',async ({page}) => {
  await setup(page,{submitFails:true})
  await page.getByRole('button',{name:'Binance ile öde',exact:true}).click()
  await page.getByLabel('Binance işlem / sipariş numarası').fill('BINANCE-123')
  await page.getByRole('button',{name:'Ödemeyi bildirdim'}).click()
  await expect(page.getByRole('region',{name:'Binance ile ödeme'}).getByRole('alert')).toContainText('başka bir talepte')
  await expect(page.getByText('Bekliyor',{exact:true})).toHaveCount(0)
  await expect(page.getByLabel('Binance işlem / sipariş numarası')).toHaveValue('BINANCE-123')
})

test('Paid Stripe denial remains an explicit error without opening payment details',async ({page}) => {
  await setup(page,{paid:true})
  await page.getByRole('button',{name:'Binance ile öde',exact:true}).click()
  await expect(page.getByRole('region',{name:'Binance ile ödeme'}).getByRole('alert')).toContainText('Ücretli Stripe')
  await expect(page.getByLabel('Binance işlem / sipariş numarası')).toHaveCount(0)
  await expect(page.getByRole('button',{name:'START MASTER MODE'})).toBeVisible()
})

test('Stripe button still submits the original checkout product without Binance mutation',async ({page}) => {
  const {calls} = await setup(page)
  let checkout:unknown = null
  await page.route('**/api/v22/subscription/checkout',async route => {
    const headers = {'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'POST, OPTIONS'}
    if (route.request().method() === 'OPTIONS') {await route.fulfill({status:204,headers});return}
    checkout = route.request().postDataJSON()
    await route.fulfill({json:{checkout_url:'http://127.0.0.1:4174/billing?checkout=success'},headers})
  })
  await page.getByRole('button',{name:'START MASTER MODE'}).click()
  await expect(page).toHaveURL(/billing\?checkout=success/)
  expect(checkout).toEqual({plan:'MASTER_MODE',billing_interval:'monthly'})
  expect(calls).toEqual([])
})

test('Approved Binance subscription displays its real expiry without Stripe management actions',async ({page},testInfo) => {
    await setup(page)
    await page.route('**/api/v22/subscription',route => route.fulfill({json:{
      status:'ACTIVE',plan:'MASTER_MODE',master_trade_access:true,mode:'BINANCE_MANUAL',
      current_period_end:'2026-11-10T10:00:00Z',cancel_at_period_end:true,next_payment_amount:null,
    },headers:{'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*'}}))
    await page.getByRole('button',{name:'BILLING',exact:true}).click()
    await expect(page.getByText('Binance ile ödendi',{exact:true}).first()).toBeVisible()
    await expect(page.getByText('10 Kas 2026',{exact:true}).first()).toBeVisible()
    await expect(page.getByText('Otomatik yenileme veya otomatik ücretlendirme yoktur.')).toBeVisible()
    await expect(page.getByRole('button',{name:'MANAGE BILLING PORTAL'})).toHaveCount(0)
    await expect(page.getByText('Cancellation scheduled',{exact:true})).toHaveCount(0)
    await page.screenshot({path:testInfo.outputPath('binance-member-billing.png'),fullPage:true})
})

test('Configured Stripe trial still sends the original TRIAL checkout and never creates a Binance request',async ({page}) => {
  const {calls} = await setup(page)
  let checkout:unknown = null
  await page.route('**/api/v22/subscription/checkout',async route => {
    const headers = {'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'POST, OPTIONS'}
    if (route.request().method() === 'OPTIONS') {await route.fulfill({status:204,headers});return}
    checkout = route.request().postDataJSON()
    await route.fulfill({json:{checkout_url:'http://127.0.0.1:4174/billing?checkout=trial'},headers})
  })
  await page.getByRole('button',{name:'START FREE TRIAL'}).click()
  await expect(page).toHaveURL(/billing\?checkout=trial/)
  expect(checkout).toEqual({plan:'TRIAL',billing_interval:'monthly'})
  expect(calls).toEqual([])
})

test('Paid Stripe billing still offers the original portal and cancellation actions',async ({page}) => {
    await setup(page)
    await page.route('**/api/v22/subscription',route => route.fulfill({json:{
      status:'ACTIVE',plan:'MASTER_MODE',master_trade_access:true,mode:'STRIPE',
      current_period_end:'2026-11-10T10:00:00Z',cancel_at_period_end:false,next_payment_amount:119.90,
    },headers:{'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*'}}))
    await page.getByRole('button',{name:'BILLING',exact:true}).click()
    await expect(page.getByRole('button',{name:'MANAGE BILLING PORTAL'})).toBeVisible()
    await expect(page.getByRole('button',{name:'CANCEL AT PERIOD END'})).toBeVisible()
    await expect(page.getByText('Binance ile ödendi',{exact:true})).toHaveCount(0)
})

test('Approved future Binance period is visible during trial without pretending it is already active',async ({page}) => {
  await setup(page,{approvedPeriod:true})
  await page.route('**/api/v22/subscription',route => route.fulfill({json:{
    status:'TRIALING',plan:'TRIAL',master_trade_access:true,mode:'STRIPE',
    trial_end:'2026-10-18T10:00:00Z',current_period_end:'2026-10-18T10:00:00Z',next_payment_amount:119.90,
  },headers:{'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*'}}))
  await page.getByRole('button',{name:'BILLING',exact:true}).click()
  const card = page.getByRole('region',{name:'Binance ile ödeme'})
  await expect(card.getByText('Binance ile ödendi',{exact:true})).toBeVisible()
  await expect(card.getByText('17.11.2026',{exact:true})).toBeVisible()
  await expect(page.getByText('Free trial',{exact:true}).first()).toBeVisible()
  await expect(page.getByRole('button',{name:'MANAGE BILLING PORTAL'})).toBeVisible()
})

test('User payment bell opens billing and marks only the notification, without payment or order writes',async ({page}) => {
  const {calls,assistant} = await setup(page)
  const notification = {id:'binance:'+'f'.repeat(32),type:'BINANCE_APPROVED',severity:'success',
    title:'Binance ödemen onaylandı',message:'Ödemen onaylandı, MASTER MODE 10.11.2026 tarihine kadar aktif.',
    timestamp:'2026-10-11T00:00:00Z',read:false,target:'/billing'}
  const reads:string[] = []
  await page.route('**/api/v21/notifications**',async route => {
    const headers = {'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'GET, POST, OPTIONS'}
    if (route.request().method() === 'OPTIONS') {await route.fulfill({status:204,headers});return}
    if (route.request().method() === 'POST') {reads.push(new URL(route.request().url()).pathname);notification.read=true}
    await route.fulfill({headers,json:{items:[notification],unread:notification.read ? 0 : 1}})
  })
  await page.goto('/dashboard')
  await page.getByRole('button',{name:/^Bildirimler(?:,|$)/}).click()
  await page.getByRole('dialog',{name:'Bildirimler',exact:true}).getByText('Binance ödemen onaylandı',{exact:true}).click()
  await expect(page).toHaveURL(/\/billing$/)
  await expect(page.getByRole('heading',{name:'Subscription control center',exact:true})).toBeVisible()
  expect(reads).toEqual([`/api/v21/notifications/binance%3A${'f'.repeat(32)}/read`])
  expect(calls).toEqual([])
  expect(assistant.requests.filter(path => /^POST .*subscription|^POST .*order/.test(path))).toEqual([])
})
