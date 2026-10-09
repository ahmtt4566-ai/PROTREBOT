import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

const email = 'ada@example.test'
async function prepare(page: Page, baseURL: string) {
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  const assistant = await mockAssistant(page)
  assistant.sessionStatus = 401
  await page.addInitScript(() => {
    if (!sessionStorage.getItem('kaistrade-email-v2-test-initialized')) {
      localStorage.clear(); sessionStorage.clear()
      sessionStorage.setItem('kaistrade-email-v2-test-initialized', '1')
    }
  })
  const calls: {path: string; method: string}[] = []
  const state = {verified: false, canExchange: true, authenticated: false, pending: true, email,
    retryAfter: 60, confirmationError: '', exchangeError: 0, hold: null as Promise<void> | null,
    statusHold: null as Promise<void> | null}
  await page.route('**/api/v22/public', route => route.fulfill({json: {auth_available: true, setup_required: false, email_verification_v2_enabled: true}}))
  await page.route('**/api/v22/auth/register', route => route.fulfill({json: {email_verification_v2_enabled: true, message: 'E-posta kayıt için uygunsa doğrulama bağlantısı gönderildi.'}}))
  await page.route('**/api/v22/auth/{registration/**,verify-email}', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    calls.push({path, method: request.method()})
    if (path.endsWith('/verify-email')) {
      expect(request.method()).toBe('POST')
      if (state.hold) await state.hold
      if (state.confirmationError) return route.fulfill({status: 400, json: {detail: {code: state.confirmationError, message: 'Bağlantı kullanılamıyor'}}})
      state.verified = true
      return route.fulfill({json: {ok: true, verified: true, email, already_verified: false}})
    }
    if (path.endsWith('/status')) {
      if (state.statusHold) await state.statusHold
      return state.pending || state.authenticated
        ? route.fulfill({json: {verified: state.verified, email: state.email, can_exchange: state.canExchange, already_authenticated: state.authenticated, retry_after: state.retryAfter, can_change_email: !state.verified && state.pending}})
        : route.fulfill({status: 401, json: {detail: {code: 'pending', message: 'Bekleyen kayıt oturumu gerekli'}}})
    }
    if (path.endsWith('/exchange')) {
      expect(state.verified).toBe(true)
      expect(state.pending).toBe(true)
      if (state.exchangeError) return route.fulfill({status: state.exchangeError, json: {detail: 'Oturum işlemi şu anda tamamlanamadı.'}})
      state.pending = false
      if (state.canExchange) {state.authenticated = true; assistant.sessionStatus = 200}
      return route.fulfill({json: state.canExchange ? {requires_login: false, token: 'cookie-session:offline-user'} : {requires_login: true, email}})
    }
    if (path.endsWith('/resend')) return route.fulfill({json: {ok: true, message: 'Yeni bağlantı gönderildi — gelen kutunu kontrol et', retry_after: 60}})
    if (path.endsWith('/email')) {
      const body = request.postDataJSON()
      expect(body.current_password).toBe('Offline-Password1!')
      state.email = body.new_email
      return route.fulfill({json: {ok: true, email: state.email, retry_after: 60}})
    }
    return route.fulfill({status: 404, json: {detail: 'Bilinmeyen doğrulama isteği'}})
  })

  return {state, calls, goto: (path: string) => page.goto(baseURL + path)}
}

test('Waiting screen corrects the same pending account email with password and a fresh cooldown', async ({page, baseURL}) => {
  const mock = await prepare(page, baseURL!)
  mock.state.retryAfter = 0
  await mock.goto('/verify-email')
  await page.getByRole('button', {name: 'E-postanı mı yanlış yazdın? Değiştir'}).click()
  await page.getByLabel('Yeni e-posta').fill('corrected@example.test')
  await page.getByLabel('Parolan', {exact: true}).fill('Offline-Password1!')
  await page.getByRole('button', {name: 'Adresi değiştir ve gönder'}).click()
  await expect(page.getByText('corrected@example.test', {exact: true})).toBeVisible()
  await expect(page.getByRole('button', {name: 'Tekrar gönder'})).toBeDisabled()
  expect(mock.calls.filter(call => call.path.endsWith('/email'))).toHaveLength(1)
  expect(mock.calls.filter(call => call.path.endsWith('/exchange'))).toHaveLength(0)
  expect(await page.evaluate(() => JSON.stringify({...localStorage, ...sessionStorage}))).not.toContain('Offline-Password1!')
})

test('Registration navigates via History API; reload resumes server-side pending state', async ({page, baseURL}) => {
  const mock = await prepare(page, baseURL!)
  await mock.goto('/register')
  await page.getByLabel('Ad soyad').fill('Ada')
  await page.getByLabel('E-posta', {exact: true}).fill(email)
  await page.locator('input[autocomplete="new-password"]').nth(0).fill('Offline-Password1!')
  await page.locator('input[autocomplete="new-password"]').nth(1).fill('Offline-Password1!')
  await page.getByRole('checkbox').check()
  await page.getByRole('button', {name: 'HESAP OLUŞTUR', exact: true}).click()
  await expect(page).toHaveURL(baseURL + '/verify-email')
  await expect(page.getByRole('heading', {name: 'E-postanı doğrula'})).toBeVisible()
  await expect(page.getByText('E-postana bir doğrulama bağlantısı gönderdik.')).toBeVisible()
  await expect(page.getByText(email, {exact: true})).toBeVisible()
  await page.reload()
  await expect(page.getByText('E-postana bir doğrulama bağlantısı gönderdik.')).toBeVisible()
  expect(mock.calls.filter(call => call.path.endsWith('/exchange'))).toHaveLength(0)
})

test('Waiting polls every four seconds, refreshes on focus and stops after verified conversion', async ({page, baseURL}) => {
  const mock = await prepare(page, baseURL!)
  let release: (() => void) | undefined
  mock.state.statusHold = new Promise<void>(resolve => {release = resolve})
  await page.clock.install()
  await mock.goto('/verify-email')
  await expect(page.getByRole('heading', {name: 'E-postanı doğrula'})).toBeVisible()
  await page.clock.pauseAt(new Date(await page.evaluate(() => Date.now()) + 1000))
  release?.()
  mock.state.statusHold = null
  await expect(page.getByText('E-postana bir doğrulama bağlantısı gönderdik.')).toBeVisible()
  const count = () => mock.calls.filter(call => call.path.endsWith('/status')).length
  const initial = count()
  await page.clock.runFor(3999)
  expect(count()).toBe(initial)
  await page.clock.runFor(1)
  await expect.poll(count).toBe(initial + 1)
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect.poll(count).toBe(initial + 2)
  mock.state.verified = true
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect(page.getByRole('heading', {name: 'E-posta Doğrulandı'})).toBeVisible()
  await expect(page.getByText('Hesabın güvende. Panele yönlendiriliyorsun…')).toBeVisible()
  expect(mock.calls.filter(call => call.path.endsWith('/exchange'))).toHaveLength(1)
  const stopped = count()
  await page.clock.runFor(2999)
  expect(count()).toBe(stopped)
  await expect(page).toHaveURL(baseURL + '/verify-email')
  await page.clock.runFor(1)
  await expect(page).toHaveURL(baseURL + '/dashboard')
})

test('Link POST waits for real server approval and exchanges only once under StrictMode', async ({page, baseURL}) => {
  const mock = await prepare(page, baseURL!)
  let release: (() => void) | undefined
  mock.state.hold = new Promise<void>(resolve => {release = resolve})
  await mock.goto('/verify-email?token=ev2_offline-link')
  await expect(page.getByRole('heading', {name: 'Doğrulanıyor…'})).toBeVisible()
  await expect(page.getByRole('heading', {name: 'E-posta Doğrulandı'})).toHaveCount(0)
  expect(mock.calls.filter(call => call.path.endsWith('/exchange'))).toHaveLength(0)
  release?.()
  await expect(page.getByRole('heading', {name: 'E-posta Doğrulandı'})).toBeVisible()
  expect(mock.calls.filter(call => call.path.endsWith('/verify-email'))).toHaveLength(1)
  expect(mock.calls.filter(call => call.path.endsWith('/exchange'))).toHaveLength(1)
})

test('Different browser never logs in automatically and pre-fills login email', async ({page, baseURL}) => {
  const mock = await prepare(page, baseURL!)
  mock.state.pending = false
  await page.clock.install()
  await mock.goto('/verify-email?token=ev2_other-device')
  await expect(page.getByText('Hesabın doğrulandı. Devam etmek için giriş yap.')).toBeVisible()
  expect(mock.calls.filter(call => call.path.endsWith('/exchange'))).toHaveLength(0)
  await page.clock.runFor(8000)
  await expect(page).toHaveURL(baseURL + '/verify-email')
  await page.getByRole('button', {name: 'Giriş yap', exact: true}).click()
  await expect(page).toHaveURL(baseURL + '/login')
  await expect(page.getByLabel('E-posta', {exact: true})).toHaveValue(email)
})

test('Retry after exchange failure never consumes the confirmed link again', async ({page, baseURL}) => {
  const mock = await prepare(page, baseURL!)
  mock.state.exchangeError = 503
  await mock.goto('/verify-email?token=ev2_retry-exchange')
  await expect(page.getByText('Oturum işlemi şu anda tamamlanamadı.')).toBeVisible()
  await expect(page).toHaveURL(baseURL + '/verify-email')
  expect(mock.state.authenticated).toBe(false)
  mock.state.exchangeError = 0
  await page.getByRole('button', {name: 'Tekrar dene', exact: true}).click()
  await expect(page.getByRole('heading', {name: 'E-posta Doğrulandı'})).toBeVisible()
  expect(mock.calls.filter(call => call.path.endsWith('/verify-email'))).toHaveLength(1)
  expect(mock.calls.filter(call => call.path.endsWith('/exchange'))).toHaveLength(2)
})

test('2FA account uses normal login rather than automatic session conversion', async ({page, baseURL}) => {
  const mock = await prepare(page, baseURL!)
  mock.state.canExchange = false
  await mock.goto('/verify-email?token=ev2_mfa')
  await expect(page.getByText('Hesabın doğrulandı. Devam etmek için giriş yap.')).toBeVisible()
  expect(mock.state.authenticated).toBe(false)
  expect(mock.calls.filter(call => call.path.endsWith('/exchange'))).toHaveLength(1)
  await expect(page.getByRole('button', {name: 'Panele geç'})).toHaveCount(0)
})

for (const [code, heading] of [['expired', 'Bağlantının süresi doldu'], ['used', 'Bu bağlantı zaten kullanılmış'], ['invalid', 'Doğrulama tamamlanamadı']]) {
  test(`Server ${code} error preserves calm error state and resend action`, async ({page, baseURL}) => {
    const mock = await prepare(page, baseURL!)
    mock.state.confirmationError = code
    await mock.goto('/verify-email?token=ev2_error')
    await expect(page.getByRole('heading', {name: heading})).toBeVisible()
    await expect(page.getByRole('heading', {name: 'E-posta Doğrulandı'})).toHaveCount(0)
    await page.getByRole('button', {name: 'Yeni bağlantı gönder', exact: true}).click()
    await expect(page.getByText('Yeni bağlantı gönderildi — gelen kutunu kontrol et')).toBeVisible()
    expect(mock.calls.filter(call => call.path.endsWith('/exchange'))).toHaveLength(0)
  })
}

test('Countdown is server-backed, resend stays disabled until sixty seconds', async ({page, baseURL}) => {
  const mock = await prepare(page, baseURL!)
  await page.clock.install()
  await mock.goto('/verify-email')
  const button = page.getByRole('button', {name: /Tekrar gönder/})
  await expect(button).toContainText('1:00')
  await expect(button).toBeDisabled()
  await expect(page.getByText(email, {exact: true})).toBeVisible()
  await page.evaluate(() => {Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'})})
  mock.state.retryAfter = 0
  await page.clock.runFor(60000)
  await expect(button).toBeEnabled()
  await button.click()
  await expect(page.getByRole('status')).toHaveText('Yeni bağlantı gönderildi — gelen kutunu kontrol et')
  await expect(button).toBeDisabled()
  expect(mock.calls.filter(call => call.path.endsWith('/resend'))).toHaveLength(1)
})

for (const width of [1440, 390]) {
  test(`Verification layout fits ${width}px with accessible controls and reduced motion`, async ({page, baseURL}, testInfo) => {
    await page.setViewportSize({width, height: 900})
    await page.emulateMedia({reducedMotion: 'reduce'})
    const mock = await prepare(page, baseURL!)
    await mock.goto('/verify-email')
    await expect(page.getByRole('heading', {name: 'E-postanı doğrula'})).toBeVisible()
    const dimensions = await page.evaluate(() => ({
      width: document.documentElement.scrollWidth, viewport: innerWidth,
      button: document.querySelector('.ev-primary')!.getBoundingClientRect().height,
      motion: getComputedStyle(document.querySelector('.verificationSymbol')!).animationName,
      font: getComputedStyle(document.querySelector('#ev-title')!).fontFamily,
    }))
    expect(dimensions.width).toBeLessThanOrEqual(dimensions.viewport)
    expect(dimensions.button).toBeGreaterThanOrEqual(44)
    expect(dimensions.motion).toBe('none')
    expect(dimensions.font).toContain('Plus Jakarta Sans')
    await page.screenshot({path: testInfo.outputPath(`verification-${width}.png`), fullPage: true})
  })
}
