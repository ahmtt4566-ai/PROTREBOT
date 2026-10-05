import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

async function prepare(page: Page, options: {registrationFailure?: boolean; resendStatus?: number; malformed?: boolean} = {}) {
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  await mockAssistant(page)
  await page.addInitScript(() => {localStorage.clear(); sessionStorage.clear()})
  await page.route('**/api/v22/{session,profile}', route => route.fulfill({status: 401, json: {detail: 'Oturum gerekli'}}))
  await page.route('**/api/v22/auth/verification-status?**', route => route.fulfill({json: {verified: false}}))
  const registrations: unknown[] = [], resends: unknown[] = []
  await page.route('**/api/v22/auth/register', route => {
    registrations.push(route.request().postDataJSON())
    return options.registrationFailure && registrations.length === 1
      ? route.fulfill({status: 503, headers: {'X-Email-Delivery-Error': '1', 'Retry-After': '60'}, json: {detail: 'Doğrulama maili gönderilemedi. Lütfen tekrar dene veya Google ile giriş yap.'}})
      : route.fulfill({json: {user: {id: 'offline-registration', email: 'ada@example.test', display_name: 'Ada', role: 'CUSTOMER', email_verified: false}, verification_status_token: 'offline-status-proof', message: 'E-posta kayıt için uygunsa doğrulama bağlantısı gönderildi.'}})
  })
  await page.route('**/api/v22/auth/resend-verification', route => {
    resends.push(route.request().postDataJSON())
    const status = options.resendStatus ?? 200
    return route.fulfill({
      status, headers: status === 429 ? {'Retry-After': '3600'} : status === 503 ? {'X-Email-Delivery-Error': '1', 'Retry-After': '60'} : {},
      json: options.malformed ? {} : status === 200
        ? {ok: true, message: 'E-posta kayıt için uygunsa doğrulama bağlantısı gönderildi.', retry_after: 60}
        : {detail: status === 429 ? 'Çok fazla deneme; daha sonra tekrar deneyin' : 'Doğrulama maili gönderilemedi. Lütfen tekrar dene.'},
    })
  })
  await page.goto('/register')
  return {registrations, resends}
}

async function register(page: Page) {
  await page.getByLabel('Ad soyad').fill('Ada')
  await page.getByLabel('E-posta', {exact: true}).fill('ada@example.test')
  await page.locator('input[autocomplete="new-password"]').nth(0).fill('Offline-Password1!')
  await page.locator('input[autocomplete="new-password"]').nth(1).fill('Offline-Password1!')
  await page.getByRole('checkbox').check()
  await page.getByRole('button', {name: 'HESAP OLUŞTUR', exact: true}).click()
}

test('Registration preserves consent and signed-status resend waits 60 seconds', async ({page}) => {
  const {registrations, resends} = await prepare(page)
  await page.clock.install()
  await register(page)
  await expect(page.getByRole('heading', {name: 'E-postanızı kontrol edin'})).toBeVisible()
  const resend = page.getByRole('button', {name: /Doğrulama mailini tekrar gönder/})
  await expect(resend).toBeDisabled()
  await expect(page.getByText('Spam klasörünü kontrol et.', {exact: false})).toBeVisible()
  await expect(page.getByRole('button', {name: 'Google ile devam et'})).toBeVisible()
  expect(registrations).toHaveLength(1)
  expect(registrations[0]).toMatchObject({email: 'ada@example.test', terms_accepted: true})
  expect(resends).toEqual([])
  await page.clock.runFor(59_000)
  await expect(resend).toBeDisabled()
  await page.clock.runFor(1000)
  await expect(resend).toBeEnabled()
  await resend.click()
  await expect.poll(() => resends.length).toBe(1)
  expect(resends[0]).toEqual({token: 'offline-status-proof'})
  await expect(resend).toBeDisabled()
})

test('Initial delivery failure retries the same registration without bypassing terms', async ({page}) => {
  const {registrations, resends} = await prepare(page, {registrationFailure: true})
  await page.clock.install()
  await register(page)
  await expect(page.getByRole('alert')).toContainText('Doğrulama maili gönderilemedi')
  const retry = page.getByRole('button', {name: /Doğrulama mailini tekrar gönder/})
  await expect(retry).toBeDisabled()
  await page.clock.runFor(60_000)
  await expect(retry).toBeEnabled()
  await retry.click()
  await expect(page.getByRole('heading', {name: 'E-postanızı kontrol edin'})).toBeVisible()
  expect(registrations).toHaveLength(2)
  expect(registrations[1]).toEqual(registrations[0])
  expect(resends).toEqual([])
})

for (const status of [429, 503]) test(`Resend ${status} shows an error and respects server cooldown`, async ({page}) => {
  const {resends} = await prepare(page, {resendStatus: status})
  await page.clock.install()
  await register(page)
  const resend = page.getByRole('button', {name: /Doğrulama mailini tekrar gönder/})
  await page.clock.runFor(60_000)
  await expect(resend).toBeEnabled()
  await resend.click()
  await expect(page.getByRole('alert')).toContainText(status === 429 ? 'Çok fazla deneme' : 'Doğrulama maili gönderilemedi')
  await expect(page.locator('.authVerificationPanel')).not.toContainText('bağlantısı gönderildi')
  await expect(resend).toContainText(status === 429 ? '3600 sn' : '60 sn')
  expect(resends).toHaveLength(1)
})

test('Malformed resend response is never displayed as successful delivery', async ({page}) => {
  await prepare(page, {malformed: true})
  await page.clock.install()
  await register(page)
  const resend = page.getByRole('button', {name: /Doğrulama mailini tekrar gönder/})
  await page.clock.runFor(60_000)
  await expect(resend).toBeEnabled()
  await resend.click()
  await expect(page.getByRole('alert')).toContainText('sonucu doğrulanamadı')
  await expect(page.locator('.authVerificationPanel')).not.toContainText('bağlantısı gönderildi')
})

test('Direct verification page does not invent a mail delivery failure or a resend proof', async ({page}) => {
  const {resends} = await prepare(page)
  await page.goto('/verify-email')
  await expect(page.locator('.authVerificationPanel')).toContainText('E-posta doğrulaması bekleniyor.')
  await expect(page.locator('.authVerificationPanel')).not.toContainText('maili gönderilemedi')
  await expect(page.getByRole('button', {name: /Doğrulama mailini tekrar gönder/})).toHaveCount(0)
  expect(resends).toEqual([])
})
