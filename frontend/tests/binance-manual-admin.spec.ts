import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'
test.use({baseURL:'http://127.0.0.1:4177'})

const pending = {
  id:'b'.repeat(32),user_id:'customer-1',email:'customer@example.test',display_name:'Ödeme Kullanıcısı',
  reference_code:'KT-BC2345',amount:'119.90',currency:'USDT',method:'binance_manual',
  status:'pending',transaction_number:'BINANCE-000123',created_at:'2026-10-10T10:00:00Z',
  expires_at:'2026-10-12T10:00:00Z',decision_note:null as string|null,
}
const cors = {'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'GET, POST, OPTIONS'}
async function setup(page:Page,{fail=false,role='OWNER',readFail=false,disabled=false} = {}) {
  await mockAssistant(page)
  await page.addInitScript(() => {
    Object.defineProperty(navigator,'clipboard',{value:{writeText:async (text:string) => sessionStorage.setItem('copied-binance',text)}})
  })
  await page.route('**/api/v22/session',route => route.fulfill({headers:cors,json:{
    user:{id:'owner',role,active:true,email_verified:true,display_name:'Owner',email:'owner@example.test'},
    access:{isAdmin:role === 'OWNER',isPremium:true,canAccessMasterTrade:true},
  }}))
  await page.route('**/api/v22/admin/overview',route => route.fulfill({headers:cors,json:{
    customers:[],audit:[],total_users:0,active_users:0,verified_users:0,admins:1,new_users:0,
    pro_users:0,free_users:0,active_subscriptions:0,expired_subscriptions:0,billing_live:false,
  }}))
  const state = {fail,readFail,disabled,empty:false,hold:null as Promise<void>|null,rows:[{...pending}],writes:[] as {path:string;body:unknown}[]}
  await page.route('**/api/v22/admin/binance-payments**',async route => {
    const req = route.request()
    if (req.method() === 'OPTIONS') {await route.fulfill({status:204,headers:cors});return}
    const path = new URL(req.url()).pathname
    if (req.method() === 'POST') {
      state.writes.push({path,body:req.postDataJSON()})
      if (state.disabled) {await route.fulfill({headers:cors,json:{enabled:false,message:'Binance ödemeleri şu an kapalı'}});return}
      if (state.hold) await state.hold
      if (state.fail) {await route.fulfill({status:503,headers:cors,json:{detail:'Karar kaydedilemedi.'}});return}
      const row = state.rows[0]
      row.status = path.endsWith('/approve') ? 'approved' : 'rejected'
      row.decision_note = row.status === 'rejected' ? req.postDataJSON().reason : null
      await route.fulfill({headers:cors,json:row});return
    }
    if (state.disabled) {await route.fulfill({headers:cors,json:{enabled:false,message:'Binance ödemeleri şu an kapalı'}});return}
    if (state.readFail) {await route.fulfill({status:503,headers:cors,json:{detail:'Liste okunamadı.'}});return}
    const status = new URL(req.url()).searchParams.get('status')
    const requested = new URL(req.url()).searchParams.get('request_id')
    const items = state.empty ? [] : state.rows.filter(row => requested ? row.id === requested : row.status === status)
    await route.fulfill({headers:cors,json:{items,total:items.length,limit:25,offset:0,
      summary:{pending:state.rows.filter(row => row.status === 'pending').length,
        approved_today:state.rows.filter(row => row.status === 'approved').length,rejected:state.rows.filter(row => row.status === 'rejected').length}}})
  })
  await page.goto('/admin?section=binance-payments',{waitUntil:'domcontentloaded'})
  return state
}

test('Disabled Binance administration shows a neutral card without payment actions',async ({page}) => {
  const state = await setup(page,{disabled:true})
  const panel = page.getByRole('region',{name:'Binance ödemeleri'})
  await expect(panel.getByRole('heading',{name:'Kapalı',exact:true})).toBeVisible()
  await expect(panel.getByRole('status')).toContainText('Binance ödemeleri şu an kapalı')
  await expect(panel.getByRole('tab')).toHaveCount(0)
  await expect(panel.getByRole('article')).toHaveCount(0)
  await expect(panel.getByRole('alert')).toHaveCount(0)
  expect(state.writes).toEqual([])
  state.disabled = false
  await panel.getByRole('button',{name:'Yenile',exact:true}).click()
  await expect(panel.getByRole('article')).toHaveCount(1)
})

for (const action of ['approve','reject'] as const) {
  test(`Disabling payments before ${action} hides stale cards without reporting success`,async ({page}) => {
    const state = await setup(page)
    const panel = page.getByRole('region',{name:'Binance ödemeleri'})
    const card = panel.getByRole('article')
    if (action === 'approve') await card.getByLabel('Binance hesabımda kontrol ettim').check()
    else await card.getByLabel('Red gerekçesi',{exact:true}).fill('Ödeme bulunamadı.')
    state.disabled = true
    await card.getByRole('button',{name:action === 'approve' ? 'Onayla' : 'Reddet',exact:true}).click()
    await expect(panel.getByRole('heading',{name:'Kapalı',exact:true})).toBeVisible()
    await expect(panel.getByRole('article')).toHaveCount(0)
    await expect(panel.getByRole('alert')).toHaveCount(0)
    expect(state.rows[0].status).toBe('pending')
    expect(state.writes).toHaveLength(1)
  })
}

test('OWNER card requires confirmation; copy and approval use the correct request',async ({page}) => {
  const state = await setup(page)
  const card = page.getByRole('article',{name:'Ödeme KT-BC2345'})
  await expect(card).toBeVisible()
  await expect(card.getByText('119.90 USDT',{exact:true})).toBeVisible()
  await expect(card.getByText('İnceleme bekliyor · süre sınırı yok')).toBeVisible()
  await expect(card.getByRole('button',{name:'Onayla',exact:true})).toBeDisabled()
  await card.getByRole('button',{name:'İşlem numarasını kopyala'}).click()
  expect(await page.evaluate(() => sessionStorage.getItem('copied-binance'))).toBe('BINANCE-000123')
  await card.getByLabel('Binance hesabımda kontrol ettim').check()
  await card.getByRole('button',{name:'Onayla',exact:true}).click()
  await expect(page.getByText('Ödeme onaylandı.',{exact:false})).toBeVisible()
  expect(state.writes).toEqual([{path:`/api/v22/admin/binance-payments/${pending.id}/approve`,body:{checked_in_binance:true}}])
  await page.getByRole('tab',{name:'Onaylanan',exact:true}).click()
  await expect(card).toBeVisible()
  await expect(card.getByRole('button',{name:'Onayla',exact:true})).toHaveCount(0)
})

test('Rejection requires a nonblank reason and failures retain the card and input',async ({page}) => {
  const state = await setup(page,{fail:true})
  const card = page.getByRole('article',{name:'Ödeme KT-BC2345'})
  await expect(card.getByRole('button',{name:'Reddet',exact:true})).toBeDisabled()
  await card.getByLabel('Red gerekçesi',{exact:true}).fill('   ')
  await expect(card.getByRole('button',{name:'Reddet',exact:true})).toBeDisabled()
  await card.getByLabel('Red gerekçesi',{exact:true}).fill('Ödeme bulunamadı.')
  await card.getByRole('button',{name:'Reddet',exact:true}).click()
  await expect(page.getByRole('region',{name:'Binance ödemeleri'}).getByRole('alert')).toContainText('Karar kaydedilemedi.')
  await expect(card.getByLabel('Red gerekçesi',{exact:true})).toHaveValue('Ödeme bulunamadı.')
  state.fail = false
  await card.getByRole('button',{name:'Reddet',exact:true}).click()
  await page.getByRole('tab',{name:'Reddedilen',exact:true}).click()
  await expect(card.getByText('Ödeme bulunamadı.',{exact:false})).toBeVisible()
  expect(state.writes.every(write => JSON.stringify(write.body) === JSON.stringify({reason:'Ödeme bulunamadı.'}))).toBe(true)
})

test('List errors are not empty states; failed refresh keeps existing cards',async ({page}) => {
  const state = await setup(page)
  const panel = page.getByRole('region',{name:'Binance ödemeleri'})
  await expect(panel.getByRole('article')).toHaveCount(1)
  state.readFail = true
  await panel.getByRole('button',{name:'Yenile',exact:true}).click()
  await expect(panel.getByRole('alert')).toContainText('Mevcut liste güncellenemedi.')
  await expect(panel.getByRole('article')).toHaveCount(1)
  await expect(panel.getByText('Bu durumda ödeme talebi yok.')).toHaveCount(0)
  state.readFail = false
  state.empty = true
  await panel.getByRole('button',{name:'Yenile',exact:true}).click()
  await expect(panel.getByText('Bu durumda ödeme talebi yok.')).toBeVisible()
})

test('Initial API failure displays an error and unknown summary, not an empty list',async ({page}) => {
  await setup(page,{readFail:true})
  const panel = page.getByRole('region',{name:'Binance ödemeleri'})
  await expect(panel.getByRole('alert')).toContainText('Liste okunamadı.')
  await expect(panel.getByText('Bu durumda ödeme talebi yok.')).toHaveCount(0)
  await expect(panel.locator('.adminBinanceSummary strong')).toHaveText(['—','—','—'])
})

test('Repeated approval clicks while saving create only one request',async ({page}) => {
  const state = await setup(page)
  const card = page.getByRole('article',{name:'Ödeme KT-BC2345'})
  await card.getByLabel('Binance hesabımda kontrol ettim').check()
  let release:() => void = () => {}
  state.hold = new Promise<void>(resolve => {release=resolve})
  await card.getByRole('button',{name:'Onayla',exact:true}).evaluate(button => {
    button.dispatchEvent(new MouseEvent('click',{bubbles:true}))
    button.dispatchEvent(new MouseEvent('click',{bubbles:true}))
  })
  await expect(card.getByRole('button',{name:'Onayla',exact:true})).toBeDisabled()
  await expect.poll(() => state.writes.length).toBe(1)
  release()
  await expect(page.getByText('Ödeme onaylandı.',{exact:false})).toBeVisible()
  expect(state.writes).toHaveLength(1)
})

test('Moderators cannot open OWNER Binance payment administration',async ({page}) => {
  const state = await setup(page,{role:'MODERATOR'})
  await expect(page).toHaveURL(/\/moderator/)
  await expect(page.getByRole('heading',{name:'Genel Bakış',exact:true})).toBeVisible()
  await expect(page.getByRole('region',{name:'Binance ödemeleri'})).toHaveCount(0)
  expect(state.writes).toEqual([])
})

test('OWNER bell opens the exact payment card and focuses it without making a decision',async ({page}) => {
  const state = await setup(page)
  const notification = {id:'binance:'+'e'.repeat(32),type:'BINANCE_PENDING',severity:'info',title:'Binance ödeme talebi',
    message:'Ödeme Kullanıcısı · 119.90 USDT · KT-BC2345',timestamp:'2026-10-11T00:00:00Z',read:false,
    target:`/admin?section=binance-payments&payment=${pending.id}`}
  const reads:string[] = []
  await page.route('**/api/v21/notifications**',async route => {
    if (route.request().method() === 'OPTIONS') {await route.fulfill({status:204,headers:cors});return}
    if (route.request().method() === 'POST') {reads.push(new URL(route.request().url()).pathname);notification.read=true}
    await route.fulfill({headers:cors,json:{items:[notification],unread:notification.read ? 0 : 1}})
  })
  await page.goto('/dashboard')
  await page.getByRole('button',{name:/^Bildirimler(?:,|$)/}).click()
  await page.getByRole('dialog',{name:'Bildirimler',exact:true}).getByText('Binance ödeme talebi',{exact:true}).click()
  await expect(page).toHaveURL(new RegExp(`/admin\\?section=binance-payments&payment=${pending.id}$`))
  const card = page.getByRole('article',{name:'Ödeme KT-BC2345'})
  await expect(card).toBeFocused()
  await expect(card.getByLabel('Binance hesabımda kontrol ettim')).not.toBeChecked()
  expect(state.writes).toEqual([])
  expect(reads).toEqual([`/api/v21/notifications/binance%3A${'e'.repeat(32)}/read`])
})

for (const width of [1440,390]) {
  test(`Binance admin layout fits ${width}px and preserves tabs`,async ({page},testInfo) => {
    await page.setViewportSize({width,height:950})
    await setup(page)
    const panel = page.getByRole('region',{name:'Binance ödemeleri'})
    await expect(panel.getByRole('article')).toHaveCount(1)
    const overflow = await panel.evaluate(element => element.scrollWidth > element.clientWidth)
    expect(overflow).toBe(false)
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
    await page.screenshot({path:testInfo.outputPath(`binance-admin-${width}.png`),fullPage:true})
    await page.getByRole('tab',{name:'Süresi dolan',exact:true}).click()
    await expect(panel.getByText('Bu durumda ödeme talebi yok.')).toBeVisible()
  })
}
