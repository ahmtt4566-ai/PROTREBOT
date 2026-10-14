import {expect, test, type Page} from '@playwright/test'
import {execFileSync} from 'node:child_process'
import {mockAssistant} from './helpers/assistant-api'

const AUTH_URL = 'http://127.0.0.1:4175/'
const SESSION_KEY = 'protrebot-v25-session'

async function setup(page:Page) {
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  const state = await mockAssistant(page)
  state.sessionStatus = 401; state.profileStatus = 401
  return state
}

for (const viewport of [{width:390,height:844},{width:375,height:667},{width:320,height:568},{width:1440,height:900}]) test(`login preserves ticker geometry and controls at ${viewport.width}x${viewport.height}`, async ({page}) => {
  await setup(page)
  await page.setViewportSize(viewport)
  await page.goto(AUTH_URL)
  await expect(page.locator('.authCardLogin')).toBeVisible()
  await page.emulateMedia({reducedMotion:'reduce'})
  const measure = () => page.locator('.authMobileMarkets').evaluate(element => {
    const bounds = element.getBoundingClientRect()
    const ticker = element.querySelector('.authCoinTrack')!
    const style = getComputedStyle(ticker)
    return {top:bounds.top,height:bounds.height,width:bounds.width,animation:style.animationDuration,direction:style.animationDirection,content:ticker.textContent}
  })
  const current = await measure()
  const baselineCss = execFileSync('git',['show','HEAD:auth.css'],{encoding:'utf8',cwd:'..'})
  await page.evaluate(css => {
    const style = document.querySelector<HTMLStyleElement>('style[data-vite-dev-id$="/auth.css"]')!
    style.textContent = css
    const options = document.querySelector('.authLoginOptions')!
    const remember = options.querySelector('label')!
    remember.querySelector('span')!.textContent = 'Bu cihazda oturumu hatırla'
    options.replaceWith(remember)
    document.querySelector('.authCardLogin .authCardHead h2')!.textContent = 'Hesabınıza giriş yapın'
    document.querySelector('.authCardLogin .authCardLead')!.textContent = 'Çalışma alanınıza güvenli şekilde erişin.'
    document.querySelector('.authCardLogin .authSubmit')!.childNodes[0].textContent = 'GÜVENLİ GİRİŞ'
  },baselineCss)
  const baseline = await measure()
  expect(current.height).toBeCloseTo(baseline.height,1)
  expect(current.width).toBeCloseTo(baseline.width,1)
  expect(current.top).toBeCloseTo(baseline.top,0)
  expect(current.animation).toBe(baseline.animation)
  expect(current.direction).toBe(baseline.direction)
  await page.reload()
  await expect(page.getByRole('button',{name:'Giriş yap',exact:true})).toBeEnabled()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByRole('button',{name:'Parolayı göster',exact:true}).click()
  await expect(page.getByLabel('Parola',{exact:true})).toHaveAttribute('type','text')
  await page.getByRole('button',{name:'Parolayı gizle',exact:true}).click()
  await expect(page.getByLabel('Parola',{exact:true})).toHaveAttribute('type','password')
  await page.screenshot({path:test.info().outputPath('login-premium.png'),fullPage:true})
  await page.getByRole('button',{name:'Parolamı unuttum',exact:true}).filter({visible:true}).click()
  await expect(page.getByRole('button',{name:'YENİLEME BAĞLANTISI GÖNDER',exact:true})).toBeVisible()
  await page.getByRole('button',{name:'Giriş ekranına dön',exact:true}).click()
  await page.getByRole('button',{name:'Yeni hesap oluştur',exact:true}).click()
  await expect(page.getByRole('button',{name:'HESAP OLUŞTUR',exact:true})).toBeVisible()
})

test('Google button keeps its design and starts PKCE redirect with existing browser headers', async ({page}) => {
  await setup(page)
  let body:unknown
  await page.route('**/api/v22/auth/google/start', async route => {
    body = route.request().postDataJSON()
    expect(route.request().headers()['x-requested-with']).toBe('XMLHttpRequest')
    expect(route.request().headers()['authorization']).toBeUndefined()
    await route.fulfill({json: {authorization_url:'https://accounts.google.com/o/oauth2/v2/auth?client_id=offline-public-client&state=offline-state&code_challenge=offline-challenge&code_challenge_method=S256'}})
  })
  await page.route('https://accounts.google.com/o/oauth2/v2/auth**', route => route.fulfill({contentType:'text/html',body:'<h1>Offline Google authorization</h1>'}))
  await page.goto(AUTH_URL)
  const button = page.getByRole('button',{name:'Google ile devam et',exact:true})
  await expect(button).toBeEnabled()
  await expect(button).toHaveClass('authGoogle')
  await expect(button.locator('.authGoogleMark')).toHaveText('G')
  await page.getByRole('checkbox',{name:'Oturumu açık tut'}).uncheck()
  await button.click()
  await expect(page).toHaveURL(/https:\/\/accounts\.google\.com\/o\/oauth2\/v2\/auth\?/)
  expect(body).toEqual({remember:false})
})

test('Google start failure and unsafe redirect use existing error UI without navigating', async ({page}) => {
  await setup(page)
  let unsafe = false
  await page.route('**/api/v22/auth/google/start', route => route.fulfill(unsafe
    ? {json:{authorization_url:'https://evil.example/'}}
    : {status:503,json:{detail:'Google service unavailable'}}))
  await page.goto(AUTH_URL)
  const button = page.getByRole('button',{name:'Google ile devam et',exact:true})
  await expect(button).toBeEnabled()
  await button.click()
  await expect(page.getByRole('alert')).toContainText('Sunucuda geçici bir sorun')
  await expect(button).toBeEnabled()
  unsafe = true
  await button.click()
  await expect(page.getByRole('alert')).toContainText('Google giriş yönlendirmesi doğrulanamadı')
  expect(page.url()).toBe(AUTH_URL)
})

test('Google callback loads existing session and stores only public remembered metadata', async ({page}) => {
  const state = await setup(page)
  state.sessionStatus = 200; state.profileStatus = 200
  await page.context().addCookies([{name:'protrebot_session',value:'offline-signed-session',url:AUTH_URL,httpOnly:true,sameSite:'Lax'}])
  await page.goto(AUTH_URL+'?google_login=success&google_remember=1')
  await expect.poll(() => page.evaluate(key => localStorage.getItem(key),SESSION_KEY)).toBe('cookie-session:assistant-member')
  expect(state.requests).toContain('GET /api/v22/session')
  await expect(page).toHaveURL(AUTH_URL)
  const storage = await page.evaluate(() => JSON.stringify({local:{...localStorage},session:{...sessionStorage}}))
  expect(storage).not.toContain('offline-signed-session')
  expect(storage).not.toContain('id_token')
  expect(storage).not.toContain('access_token')
})

test('Google registration requires explicit existing terms consent and no fabricated password', async ({page}) => {
  const state = await setup(page)
  let completeCalls = 0
  await page.route('**/api/v22/auth/google/pending', route => route.fulfill({json:{email:'new@example.test',display_name:'Google Fixture'}}))
  await page.route('**/api/v22/auth/google/complete', async route => {
    expect(route.request().postDataJSON()).toEqual({terms_accepted:true,marketing_accepted:false})
    expect(route.request().headers()['x-requested-with']).toBe('XMLHttpRequest')
    completeCalls++
    state.sessionStatus = 200; state.profileStatus = 200
    await route.fulfill({json:{token:'cookie-session:assistant-member',user:{id:'assistant-member',role:'CUSTOMER'},remember:false}})
  })
  await page.goto(AUTH_URL+'?google_login=consent')
  await expect(page.getByLabel('E-posta',{exact:true})).toHaveValue('new@example.test')
  await expect(page.locator('.authCard input[type=password]')).toHaveCount(0)
  await page.getByRole('button',{name:'HESAP OLUŞTUR',exact:true}).click()
  await expect(page.getByRole('alert')).toContainText("Kullanıcı Sözleşmesi'ni kabul etmelisiniz")
  expect(completeCalls).toBe(0)
  await page.locator('.authCard .authConsent:not(.authMarketingConsent) input').check()
  await page.getByRole('button',{name:'HESAP OLUŞTUR',exact:true}).click()
  await expect.poll(() => page.evaluate(key => sessionStorage.getItem(key),SESSION_KEY)).toBe('cookie-session:assistant-member')
  expect(completeCalls).toBe(1)
  expect(state.requests).toContain('GET /api/v22/session')
})

test('Unlinked existing email callback safely keeps ordinary password login available', async ({page}) => {
  await setup(page)
  await page.goto(AUTH_URL+'?google_login=account_link_required')
  await expect(page.getByRole('alert')).toContainText('Google hesabı otomatik bağlanmaz')
  await expect(page.getByRole('button',{name:'Giriş yap',exact:true})).toBeEnabled()
  await expect(page.locator('.authCard input[autocomplete="current-password"]')).toBeVisible()
  await expect(page).toHaveURL(AUTH_URL)
})
