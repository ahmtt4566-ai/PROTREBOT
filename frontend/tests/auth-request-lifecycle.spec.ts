import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

const root = 'http://127.0.0.1:4176'
const sessionKey = 'protrebot-v25-session'

async function setup(page:Page) {
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  const state = await mockAssistant(page)
  await page.addInitScript(key => sessionStorage.setItem(key,'cookie-session:assistant-member'),sessionKey)
  return state
}

test('Owner denial unlocks the form even when cookie logout never responds', async ({page}) => {
  const state = await setup(page)
  state.ownerStatus = 401
  await page.route('**/api/web/access/logout', () => {})
  await page.goto(root)
  await expect(page.getByRole('button',{name:'GÜVENLİ PANELE GİR',exact:true})).toBeEnabled()
  await expect(page.locator('.v26App')).toHaveCount(0)
})

test('Owner check has a bounded deadline and never opens protected content on timeout', async ({page}) => {
  await setup(page)
  await page.clock.install()
  await page.route('**/api/web/access/check', () => {})
  await page.goto(root)
  await expect(page.getByRole('button',{name:'DOĞRULANIYOR…',exact:true})).toBeDisabled()
  await page.clock.fastForward(15001)
  await expect(page.getByRole('button',{name:'GÜVENLİ PANELE GİR',exact:true})).toBeEnabled()
  await expect(page.locator('.webAccessCard')).toContainText('Sunucu yanıt vermedi')
  await expect(page.locator('.v26App')).toHaveCount(0)
})

test('Session timeout preserves only public metadata and supports a verified retry', async ({page}) => {
  const state = await setup(page)
  await page.clock.install()
  let stalled = true
  await page.route('**/api/v22/session', async route => {
    if (stalled) return
    await route.fulfill({json:{user:{id:state.userId,role:'CUSTOMER',active:true,email_verified:true}}})
  })
  await page.goto(root)
  await expect(page.locator('.authLoading')).toBeVisible()
  await page.clock.fastForward(15001)
  await expect(page.getByRole('alert')).toContainText('Sunucu yanıt vermedi')
  await expect(page.locator('.v26App')).toHaveCount(0)
  expect(await page.evaluate(key => sessionStorage.getItem(key),sessionKey)).toBe('cookie-session:assistant-member')
  stalled = false
  await page.getByRole('button',{name:'Oturumu yeniden kontrol et',exact:true}).click()
  await expect(page.locator('.v26DashboardChoices')).toBeVisible()
  expect(state.requests.filter(request => /order|auto\/start|arm/.test(request))).toEqual([])
})

test('A hanging login releases the submit button without fabricating a session or retrying the POST', async ({page}) => {
  const state = await setup(page)
  state.sessionStatus = 401
  await page.clock.install()
  let attempts = 0
  await page.route('**/api/v22/auth/login', () => { attempts++ })
  await page.goto(root)
  await page.getByLabel('E-posta',{exact:true}).fill('member@example.test')
  await page.getByLabel('Parola',{exact:true}).fill('OfflineOnly!123')
  await page.getByRole('button',{name:'GÜVENLİ GİRİŞ',exact:true}).click()
  await expect(page.locator('.authSubmit')).toBeDisabled()
  await page.clock.fastForward(15001)
  await expect(page.getByRole('alert')).toContainText('Sunucu yanıt vermedi')
  await expect(page.getByRole('button',{name:'GÜVENLİ GİRİŞ',exact:true})).toBeEnabled()
  expect(attempts).toBe(1)
  await expect(page.locator('.v26App')).toHaveCount(0)
})

for (const failure of ['timeout','503','401'] as const) {
  test(`Session refresh ${failure} distinguishes temporary failure from expired authentication`, async ({page}) => {
    const state = await setup(page)
    await page.clock.install()
    let attempts = 0
    await page.route('**/api/v22/session', async route => {
      attempts++
      if (attempts === 1) {
        await route.fulfill({json:{user:{id:state.userId,role:'CUSTOMER',active:true,email_verified:true}}})
      } else if (failure !== 'timeout') {
        await route.fulfill({status:Number(failure),json:{detail:'Offline session refresh failure'}})
      }
    })
    await page.goto(root)
    await expect(page.locator('.v26DashboardChoices')).toBeVisible()
    await page.clock.fastForward(45001)
    await expect.poll(() => attempts).toBe(2)
    if (failure === 'timeout') await page.clock.fastForward(15001)
    if (failure === '401') {
      await expect(page.getByRole('button',{name:'GÜVENLİ GİRİŞ',exact:true})).toBeVisible()
      await expect(page.locator('.v26App')).toHaveCount(0)
      expect(await page.evaluate(key => sessionStorage.getItem(key),sessionKey)).toBeNull()
    } else {
      await expect(page.locator('.v26DashboardChoices')).toBeVisible()
      expect(await page.evaluate(key => sessionStorage.getItem(key),sessionKey)).toBe('cookie-session:assistant-member')
    }
    expect(state.requests.filter(request => /order|auto\/start|arm/.test(request))).toEqual([])
  })
}

for (const role of ['CUSTOMER','OWNER'] as const) {
  test(`Maintenance refresh expiry preserves ${role} access boundaries`, async ({page}) => {
    const state = await setup(page)
    await page.clock.install()
    let attempts = 0
    await page.route('**/api/v22/session', async route => {
      attempts++
      await route.fulfill(attempts === 1
        ? {json:{user:{id:state.userId,role,active:true,email_verified:true},maintenance:{mode:'MAINTENANCE'}}}
        : {status:401,json:{detail:'Offline expired session'}})
    })
    await page.goto(root)
    if (role === 'OWNER') await expect(page.locator('.v26DashboardChoices')).toBeVisible()
    else {
      await expect(page.locator('.authMaintenance')).toBeVisible()
      await expect(page.locator('.v26App')).toHaveCount(0)
    }
    await page.clock.fastForward(45001)
    await expect.poll(() => attempts).toBe(2)
    await expect.poll(() => page.evaluate(key => sessionStorage.getItem(key),sessionKey)).toBeNull()
    await expect(page.locator('.v26App')).toHaveCount(0)
    if (role === 'CUSTOMER') await expect(page.locator('.authMaintenance')).toBeVisible()
    else await expect(page.getByRole('button',{name:'GÜVENLİ GİRİŞ',exact:true})).toBeVisible()
    expect(state.requests.filter(request => /order|auto\/start|arm/.test(request))).toEqual([])
  })
}

test('Master Trade access timeout is unavailable, not premium denial, and retries verification', async ({page}) => {
  await setup(page)
  await page.clock.install()
  let stalled = true
  await page.route('**/api/v22/profile', async route => {
    if (stalled) return
    await route.fulfill({json:{access:{canAccessMasterTrade:true,isPremium:false}}})
  })

  await page.goto(root+'/master-trade')
  await expect(page.getByText('Master Trade erişim kontrol ediliyor…',{exact:true})).toBeVisible()
  await page.clock.fastForward(15001)
  await expect(page.getByRole('alert')).toContainText('Erişim doğrulanamadı')
  await expect(page.getByText('Premium members only.',{exact:true})).toHaveCount(0)
  await expect(page.locator('.masterTradeWorkspace')).toHaveCount(0)
  stalled = false
  await page.getByRole('button',{name:'Erişimi yeniden kontrol et',exact:true}).click()
  await expect(page.getByRole('button',{name:'Erişimi yeniden kontrol et',exact:true})).toHaveCount(0)
  await expect(page.getByRole('alert').filter({hasText:'Erişim doğrulanamadı'})).toHaveCount(0)
})

test('A stalled Live screen chunk reaches safe recovery without account or order calls', async ({page}) => {
  const state = await setup(page)
  await page.clock.install()
  await page.route(/\/assets\/LiveTradingPanel-[^/]+\.js$/, () => {})
  await page.goto(root)
  await expect(page.locator('.v26DashboardChoices')).toBeVisible()
  await page.locator('.v26DashboardChoices button').filter({has:page.locator('b',{hasText:/^CANLI$/})}).click()
  await expect(page.locator('.v26Loading')).toBeVisible()
  await page.clock.fastForward(15001)
  await expect(page.locator('.appRecoveryShell')).toBeVisible()
  await expect(page.getByRole('button',{name:'Paneli yeniden yükle',exact:true})).toBeEnabled()
  expect(state.requests.filter(request => /\/api\/v25\//.test(request))).toEqual([])
  expect(state.requests.filter(request => /order|auto\/start|arm/.test(request))).toEqual([])
})
