import {expect, test, type Page} from '@playwright/test'
import {type AccountOverview} from '../../account-settings-api'
import {mockAssistant} from './helpers/assistant-api'

const base = 'http://127.0.0.1:4176'
const account = (): AccountOverview => ({
  user: {id: 'account-test-user', display_name: 'Ada Yılmaz', email: 'ada@example.test', role: 'CUSTOMER', active: true, email_verified: true, created_at: '2026-10-01T12:00:00Z', last_login: '2026-10-05T12:00:00Z', password_changed_at: null, auth_methods: ['password']},
  subscription: {plan: 'FREE', status: 'NONE', expires_at: null, is_premium: false, features: [{key: 'scanner', label: 'Temel Scanner', included: true}, {key: 'auto', label: 'Auto Trade', included: false}]},
  preferences: {trading_mode: 'MANUAL', timeframe: '15m', exchange: 'BINANCE', risk_per_trade: 1, symbols: ['BTCUSDT', 'ETHUSDT']},
  security: {two_factor_enabled: false, active_sessions: 2, email_delivery_available: true, can_close_account: true, close_blocker: null},
  pending_email: null,
  sessions: [{id: 'current-session', current: true, device: 'Windows', browser: 'Chromium', created_at: '2026-10-05T12:00:00Z', last_seen_at: '2026-10-05T12:10:00Z', expires_at: '2026-10-06T12:00:00Z'}, {id: 'other-session', current: false, device: 'Android', browser: 'Chrome', created_at: '2026-10-04T12:00:00Z', last_seen_at: '2026-10-05T11:00:00Z', expires_at: '2026-10-06T12:00:00Z'}],
  activity: [{id: 'login-event', kind: 'LOGIN', message: 'Hesaba giriş yapıldı', created_at: '2026-10-05T12:00:00Z'}],
})

async function prepare(page: Page, options: {width?: number; owner?: boolean; google?: boolean; mail?: boolean; authenticated?: boolean} = {}) {
  await page.setViewportSize({width: options.width ?? 1440, height: 900})
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  const state = await mockAssistant(page)
  const data = account()
  if (options.owner) {data.user.id = 'account-owner'; data.user.role = 'OWNER'; data.security.can_close_account = false; data.security.close_blocker = 'Ana yönetici hesabı kapatılamaz.'}
  if (options.google) data.user.auth_methods = ['google']
  if (options.mail === false) data.security.email_delivery_available = false
  let authenticated = options.authenticated !== false
  await page.addInitScript(({id, authenticated}) => {
    sessionStorage.removeItem('protrebot-v25-session')
    localStorage.removeItem('protrebot-v25-session')
    if (authenticated) localStorage.setItem('protrebot-v25-session', `cookie-session:${id}`)
  }, {id: data.user.id, authenticated})
  await page.route('**/api/v22/{session,profile}', route => route.fulfill({status: authenticated ? 200 : 401, json: authenticated ? {user: data.user, profile: {preferences: data.preferences}, access: {isPremium: false, canAccessMasterTrade: options.owner === true}} : {detail: 'Oturum gerekli.'}}))
  await page.route('**/api/v22/account/overview', route => route.fulfill({json: data}))
  const mutations: Array<{path: string; method: string; body: Record<string, unknown>}> = []
  await page.route('**/api/v22/account/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    if (path.endsWith('/overview')) {await route.fulfill({json: data}); return}
    const body = request.postDataJSON() as Record<string, unknown>
    mutations.push({path, method: request.method(), body})
    if (path.endsWith('/profile')) data.user.display_name = String(body.display_name)
    if (path.endsWith('/preferences')) data.preferences = request.postDataJSON() as AccountOverview['preferences']
    if (path.endsWith('/email/request')) data.pending_email = {email: String(body.new_email), expires_at: '2026-10-05T13:00:00Z'}
    if (path.endsWith('/email/cancel')) data.pending_email = null
    if (path.endsWith('/sessions/revoke-others')) {data.sessions = data.sessions.filter(session => session.current); data.security.active_sessions = data.sessions.length}
    if (path.endsWith('/sessions/revoke')) {data.sessions = data.sessions.filter(session => session.id !== body.session_id); data.security.active_sessions = data.sessions.length}
    if (path.endsWith('/reauth/email')) {await route.fulfill({json: {challenge_id: 'email-challenge', expires_at: '2026-10-05T13:00:00Z'}}); return}
    if (path.endsWith('/2fa/setup')) {await route.fulfill({json: {secret: 'JBSWY3DPEHPK3PXP', otpauth_uri: 'otpauth://totp/KaisTrade:ada%40example.test?secret=JBSWY3DPEHPK3PXP&issuer=KaisTrade'}}); return}
    if (path.endsWith('/2fa/enable')) {data.security.two_factor_enabled = true; data.sessions = data.sessions.filter(session => session.current); data.security.active_sessions = 1; await route.fulfill({json: {recovery_codes: Array.from({length: 10}, (_, index) => `OFFLINE-CODE-${index + 1}`), reauthenticate: false, token: `cookie-session:${data.user.id}`}}); return}
    if (path.endsWith('/2fa/disable')) data.security.two_factor_enabled = false
    if (path.endsWith('/password') || path.endsWith('/2fa/disable')) {data.sessions = data.sessions.filter(session => session.current); data.security.active_sessions = 1; await route.fulfill({json: {ok: true, reauthenticate: false, token: `cookie-session:${data.user.id}`}}); return}
    if (path.endsWith('/close') || path.endsWith('/email/confirm')) {authenticated = false; await route.fulfill({json: {ok: true, reauthenticate: true}}); return}
    await route.fulfill({json: {ok: true}})
  })
  return {data, mutations, state, setAuthenticated: (value: boolean) => {authenticated = value}}
}

for (const width of [1440, 1152, 390]) test(`Reference profile cards remain real and reachable at ${width}px`, async ({page}) => {
  await prepare(page, {width})
  await page.goto(`${base}/settings`)
  await expect(page.getByRole('heading', {name: 'Profil & Ayarlar'})).toBeVisible()
  for (const title of ['Hesap özeti','Mevcut paket','Güvenlik','İşlem tercihleri','Son aktiviteler','Tehlikeli işlemler']) await expect(page.getByRole('heading', {name: title})).toBeVisible()
  await expect(page.locator('.accountIdentity')).toContainText('Ada Yılmaz')
  await expect(page.locator('.accountPlan')).toContainText('FREE')
  await expect(page.getByText('Son değişiklik: —', {exact: true})).toBeVisible()
  await expect(page.locator('.accountActivities')).toContainText('Hesaba giriş yapıldı')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({path: test.info().outputPath(`profile-settings-${width}.png`)})
  await page.screenshot({path: test.info().outputPath(`profile-settings-${width}-full.png`), fullPage: true})
})

test('Profile and saved defaults persist after reload with no execution mutations', async ({page}) => {
  const {mutations} = await prepare(page)
  await page.goto(`${base}/settings`)
  await page.getByRole('button', {name: 'Profili düzenle', exact: true}).click()
  await page.getByLabel('Ad soyad').fill('Ada Yeni')
  await page.getByRole('dialog').getByRole('button', {name: 'Kaydet', exact: true}).click()
  await expect(page.locator('.accountIdentity')).toContainText('Ada Yeni')
  await page.getByLabel('Varsayılan zaman dilimi').selectOption('1h')
  await page.getByLabel('Varsayılan işlem bölümü').selectOption('AUTO')
  await page.getByLabel('Tercih edilen işlem riski (%)').fill('0.5')
  await page.getByLabel('Sembol ekle').fill('SUSDT')
  await page.getByRole('button', {name: 'Ekle', exact: true}).click()
  await page.getByRole('button', {name: 'Tercihleri kaydet'}).click()
  await expect(page.getByRole('status').filter({hasText: 'İşlem tercihleri kaydedildi'})).toBeVisible()
  await page.reload()
  await expect(page.locator('.accountIdentity')).toContainText('Ada Yeni')
  await expect(page.getByLabel('Varsayılan zaman dilimi')).toHaveValue('1h')
  await expect(page.getByLabel('Varsayılan işlem bölümü')).toHaveValue('AUTO')
  await expect(page.locator('.accountSymbols')).toContainText('SUSDT')
  expect(mutations.map(item => item.path)).toEqual(['/api/v22/account/profile', '/api/v22/account/preferences'])
})

test('Long account names fit mobile and header profile control opens a working dialog', async ({page}) => {
  const {data} = await prepare(page, {width: 390})
  data.user.display_name = 'A'.repeat(80)
  await page.goto(`${base}/settings`)
  await expect(page.locator('.accountIdentity h3')).toHaveText(data.user.display_name)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByRole('button', {name: 'Profil bilgilerini düzenle'}).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await expect(page.getByLabel('Ad soyad')).toHaveValue(data.user.display_name)
})

test('Preference risk remains inside existing one-percent ceiling and supports minimum value', async ({page}) => {
  const {mutations} = await prepare(page)
  await page.goto(`${base}/settings`)
  const risk = page.getByLabel('Tercih edilen işlem riski (%)')
  await risk.fill('1.1')
  await page.getByRole('button', {name: 'Tercihleri kaydet'}).click()
  expect(await risk.evaluate((element: HTMLInputElement) => element.validity.rangeOverflow)).toBe(true)
  expect(mutations).toEqual([])
  await risk.fill('0.1')
  await page.getByRole('button', {name: 'Tercihleri kaydet'}).click()
  await expect(page.getByRole('status').filter({hasText: 'İşlem tercihleri kaydedildi'})).toBeVisible()
  expect(mutations).toHaveLength(1)
  expect(mutations[0]).toMatchObject({path: '/api/v22/account/preferences', method: 'PATCH', body: {risk_per_trade: 0.1}})
})

test('Email stays unchanged until verified, supports cancel and explicitly reports send failure', async ({page}) => {
  const {data, mutations} = await prepare(page)
  await page.goto(`${base}/profile`)
  await page.getByRole('button', {name: 'E-postayı değiştir'}).click()
  await page.getByLabel('Yeni e-posta').fill('new@example.test')
  await page.getByLabel('Mevcut parola').fill('Offline-Password1!')
  await page.getByRole('button', {name: 'Doğrulama gönder'}).click()
  await expect(page.locator('.accountPending')).toContainText('new@example.test')
  await expect(page.locator('.accountIdentity')).toContainText('ada@example.test')
  expect(data.user.email).toBe('ada@example.test')
  await page.locator('.accountPending').getByRole('button', {name: 'İptal et'}).click()
  await expect(page.locator('.accountPending')).toHaveCount(0)
  await page.route('**/api/v22/account/email/request', route => route.fulfill({status: 503, json: {detail: 'E-posta gönderilemedi.'}}))
  await page.getByRole('button', {name: 'E-postayı değiştir'}).click()
  await page.getByLabel('Yeni e-posta').fill('another@example.test')
  await page.getByLabel('Mevcut parola').fill('Offline-Password1!')
  await page.getByRole('button', {name: 'Doğrulama gönder'}).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toHaveText('E-posta gönderilemedi.')
  await expect(page.locator('.accountPending')).toHaveCount(0)
  expect(mutations.some(item => item.path.endsWith('/email/confirm'))).toBe(false)
})

test('Email token stays out of URL and requires explicit confirmation before session rotation', async ({page}) => {
  const {mutations} = await prepare(page)
  await page.goto(`${base}/profile?email_token=offline-email-token`)
  await expect(page).toHaveURL(`${base}/profile`)
  await expect(page.getByRole('button', {name: 'E-posta değişikliğini onayla'})).toBeVisible()
  expect(mutations).toEqual([])
  await page.getByRole('button', {name: 'E-posta değişikliğini onayla'}).click()
  await expect(page.getByRole('button', {name: 'GÜVENLİ GİRİŞ'})).toBeVisible()
  expect(mutations[0].body).toEqual({token: 'offline-email-token'})
  expect(await page.evaluate(() => localStorage.getItem('protrebot-v25-session'))).toBeNull()
})

test('Password mismatch sends nothing; successful change preserves current device and clears passwords', async ({page}) => {
  const {mutations, data} = await prepare(page)
  await page.goto(`${base}/settings`)
  await page.getByRole('button', {name: 'Parolayı değiştir'}).click()
  await page.getByLabel('Yeni parola', {exact: true}).fill('Offline-Password1!')
  await page.getByLabel('Yeni parola tekrar').fill('Offline-Password2!')
  await page.getByLabel('Mevcut parola').fill('Offline-OldPassword1!')
  await page.getByRole('dialog').getByRole('button', {name: 'Kaydet', exact: true}).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toHaveText('Yeni parolalar eşleşmiyor.')
  expect(mutations).toEqual([])
  await page.getByLabel('Yeni parola tekrar').fill('Offline-Password1!')
  await page.getByRole('dialog').getByRole('button', {name: 'Kaydet', exact: true}).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('heading', {name: 'Profil & Ayarlar'})).toBeVisible()
  expect(data.sessions.map(session => session.id)).toEqual(['current-session'])
  expect(await page.evaluate(() => localStorage.getItem('protrebot-v25-session'))).toBe('cookie-session:account-test-user')
  expect(await page.evaluate(() => JSON.stringify({...localStorage, ...sessionStorage}))).not.toContain('Offline-Password')
})

test('Password change rejects missing common rules without sending a request', async ({page}) => {
  const {mutations} = await prepare(page)
  await page.goto(`${base}/settings`)
  await page.getByRole('button', {name: 'Parolayı değiştir'}).click()
  await page.locator('input[autocomplete="new-password"]').nth(0).fill('abcdefghijk')
  await page.locator('input[autocomplete="new-password"]').nth(1).fill('abcdefghijk')
  await page.getByLabel('Mevcut parola').fill('Offline-OldPassword1!')
  await page.getByRole('dialog').getByRole('button', {name: 'Kaydet', exact: true}).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('Büyük harf, Rakam, Sembol')
  expect(mutations).toEqual([])
  await expect(page.getByRole('heading', {name: 'Profil & Ayarlar'})).toBeVisible()
})

test('Disabling two-factor authentication keeps the current device only', async ({page}) => {
  const {data, mutations} = await prepare(page)
  data.security.two_factor_enabled = true
  await page.goto(`${base}/settings`)
  await page.getByRole('button', {name: '2FA yönet'}).click()
  await page.getByLabel('Mevcut parola').fill('Offline-Password1!')
  await page.getByLabel('2FA veya kurtarma kodu').fill('OFFLINE-RECOVERY-CODE')
  await page.getByRole('dialog').getByRole('button', {name: '2FA devre dışı bırak'}).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(data.sessions.map(session => session.id)).toEqual(['current-session'])
  expect(data.security.two_factor_enabled).toBe(false)
  expect(mutations.map(item => item.path)).toEqual(['/api/v22/account/2fa/disable'])
  expect(await page.evaluate(() => localStorage.getItem('protrebot-v25-session'))).toBe('cookie-session:account-test-user')
})

test('Google-only changes require email reauthentication and mail-unavailable UI is honest', async ({page}) => {
  const {mutations} = await prepare(page, {google: true})
  await page.goto(`${base}/settings`)
  await page.getByRole('button', {name: 'Parola oluştur'}).click()
  await expect(page.getByLabel('Mevcut parola')).toHaveCount(0)
  await page.getByRole('button', {name: 'Doğrulama kodu gönder'}).click()
  await expect(page.getByRole('dialog').getByRole('status')).toContainText('Mevcut doğrulanmış adresinize doğrulama kodu gönderildi.')
  await page.getByLabel('E-posta kodu').fill('123456')
  await page.getByLabel('Yeni parola', {exact: true}).fill('Offline-NewPassword1!')
  await page.getByLabel('Yeni parola tekrar').fill('Offline-NewPassword1!')
  await page.getByRole('dialog').getByRole('button', {name: 'Kaydet', exact: true}).click()
  await expect.poll(() => mutations.find(item => item.path.endsWith('/password'))?.body).toMatchObject({challenge_id: 'email-challenge', email_code: '123456'})
})

test('Unavailable mail and protected OWNER closure never pretend to work', async ({page}) => {
  await prepare(page, {owner: true, mail: false})
  await page.goto(`${base}/settings`)
  await expect(page.getByRole('button', {name: 'E-postayı değiştir'})).toBeDisabled()
  await expect(page.getByRole('button', {name: 'Hesabı kapat', exact: true})).toBeDisabled()
  await expect(page.locator('.accountDanger')).toContainText('Ana yönetici hesabı kapatılamaz.')
  await expect(page.getByText('E-posta hizmeti kullanılamıyor.', {exact: false})).toBeVisible()
})

test('TOTP enrollment displays real QR, validates code and shows recovery codes only once', async ({page}) => {
  const {mutations} = await prepare(page)
  await page.clock.install()
  await page.goto(`${base}/settings`)
  await page.getByRole('button', {name: '2FA etkinleştir'}).click()
  await page.getByLabel('Mevcut parola').fill('Offline-Password1!')
  await page.getByRole('button', {name: 'Kurulumu başlat'}).click()
  await expect(page.locator('.verificationQR svg')).toHaveCount(1)
  await expect(page.locator('.verificationKey code')).not.toHaveText('JBSWY3DPEHPK3PXP')
  await page.getByRole('button', {name: 'Göster', exact: true}).click()
  await expect(page.locator('.verificationKey code')).toHaveText('JBSWY3DPEHPK3PXP')
  await page.getByRole('button', {name: 'Devam et', exact: true}).click()
  await page.getByLabel('Doğrulama kodu 1. rakam').fill('123456')
  await expect(page.locator('.verificationRecoveryCodes')).toContainText('OFFLINE-CODE-1')
  await expect(page.locator('.verificationRecoveryCodes li')).toHaveCount(10)
  expect(await page.evaluate(() => localStorage.getItem('protrebot-v25-session'))).toBe('cookie-session:account-test-user')
  await page.clock.runFor(46000)
  await expect(page.locator('.verificationRecoveryCodes')).toContainText('OFFLINE-CODE-1')
  await expect(page.getByRole('heading', {name: 'Profil & Ayarlar'})).toBeVisible()
  await expect(page.getByRole('button', {name: 'Bitir', exact: true})).toBeDisabled()
  await page.getByRole('checkbox', {name: 'Kodları güvenli bir yere kaydettim'}).check()
  await page.getByRole('button', {name: 'Bitir', exact: true}).click()
  await expect(page.getByText('OFFLINE-CODE-1')).toHaveCount(0)
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('heading', {name: 'Profil & Ayarlar'})).toBeVisible()
  expect(mutations.filter(item => item.path.includes('/2fa/')).map(item => item.path)).toEqual(['/api/v22/account/2fa/setup', '/api/v22/account/2fa/enable'])
})

test('Session management revokes other sessions without losing current session', async ({page}) => {
  const {data, mutations} = await prepare(page)
  await page.goto(`${base}/settings`)
  await page.getByRole('button', {name: 'Oturumları yönet'}).click()
  await expect(page.locator('.accountSessions')).toContainText('Bu cihaz')
  await page.getByRole('button', {name: 'Diğer oturumları kapat'}).click()
  await expect(page.locator('.accountSessions > li')).toHaveCount(1)
  expect(data.sessions[0].id).toBe('current-session')
  expect(mutations[0].path).toBe('/api/v22/account/sessions/revoke-others')
  expect(await page.evaluate(() => localStorage.getItem('protrebot-v25-session'))).toBe('cookie-session:account-test-user')
})

async function openVerificationCode(page: Page, width = 1440) {
  const mock = await prepare(page, {width})
  await page.goto(`${base}/settings`)
  await page.getByRole('button', {name: '2FA etkinleştir'}).click()
  await page.getByLabel('Mevcut parola').fill('Offline-Password1!')
  await page.getByRole('button', {name: 'Kurulumu başlat'}).click()
  await expect(page.locator('.verificationQR svg')).toBeVisible()
  return mock
}

test('Desktop enrollment hides its key, copies explicitly and remains inactive when abandoned', async ({page, context}, testInfo) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  const {data, mutations} = await openVerificationCode(page)
  await expect(page.locator('.verificationKey code')).toHaveAttribute('aria-label', 'Kurulum anahtarı gizli')
  expect(await page.locator('.verificationQR svg').evaluate(element => Math.round(element.getBoundingClientRect().width))).toBe(180)
  await page.screenshot({path: testInfo.outputPath('two-factor-desktop-connect.png'), animations: 'disabled'})
  const primary = page.getByRole('button', {name: 'Devam et', exact: true})
  await primary.hover()
  await expect(primary).toHaveCSS('background-color', 'rgb(55, 201, 138)')
  const copy = page.getByRole('button', {name: 'Kopyala', exact: true})
  await copy.hover()
  await expect(copy).toHaveCSS('border-color', 'rgb(55, 201, 138)')
  await copy.focus()
  await copy.press('Tab')
  await expect(primary).toBeFocused()
  await expect(primary).toHaveCSS('outline-color', 'rgb(55, 201, 138)')
  await page.getByRole('button', {name: 'Kopyala', exact: true}).click()
  await expect(page.getByRole('status')).toContainText('Kopyalandı')
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('JBSWY3DPEHPK3PXP')
  await page.getByRole('button', {name: 'Pencereyi kapat'}).click()
  expect(data.security.two_factor_enabled).toBe(false)
  expect(mutations.map(item => item.path)).toEqual(['/api/v22/account/2fa/setup'])
  expect(await page.evaluate(() => JSON.stringify({...localStorage, ...sessionStorage}))).not.toContain('JBSWY3DPEHPK3PXP')
})

test('Mobile digit entry supports focus, backspace, six-digit paste and invalid-code recovery', async ({page}, testInfo) => {
  const {data} = await openVerificationCode(page, 390)
  await expect(page.locator('.verificationKey code')).toHaveText('JBSWY3DPEHPK3PXP')
  const dialog = page.getByRole('dialog')
  expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
  expect(await dialog.evaluate(element => Math.round(element.getBoundingClientRect().height))).toBe(900)
  await page.screenshot({path: testInfo.outputPath('two-factor-mobile-connect.png'), animations: 'disabled'})
  await page.route('**/api/v22/account/2fa/enable', route => route.fulfill({status: 401, json: {detail: 'Geçersiz kod'}}))
  await page.getByRole('button', {name: 'Devam et', exact: true}).click()
  const inputs = page.locator('.verificationDigits input')
  await expect(inputs.nth(0)).toBeFocused()
  await page.screenshot({path: testInfo.outputPath('two-factor-mobile-code.png'), animations: 'disabled'})
  await inputs.nth(0).press('1')
  await expect(inputs.nth(1)).toBeFocused()
  await inputs.nth(1).press('Backspace')
  await expect(inputs.nth(0)).toBeFocused()
  await expect(inputs.nth(0)).toHaveValue('')
  await inputs.nth(0).evaluate(element => {
    const clipboard = new DataTransfer(); clipboard.setData('text', '123456')
    element.dispatchEvent(new ClipboardEvent('paste', {clipboardData: clipboard, bubbles: true}))
  })
  await expect(dialog.getByRole('alert')).toHaveText('Kod hatalı. Telefonunun saatinin otomatik ayarda olduğundan emin ol.')
  for (let index = 0; index < 6; index++) await expect(inputs.nth(index)).toHaveValue('')
  await expect(inputs.nth(0)).toBeFocused()
  expect(data.security.two_factor_enabled).toBe(false)
})

test('Complete code submits once, waits for server success and gates saving all ten backup codes', async ({page}, testInfo) => {
  const {mutations} = await openVerificationCode(page)
  let release: (() => void) | undefined
  const hold = new Promise<void>(resolve => {release = resolve})
  let requests = 0
  await page.route('**/api/v22/account/2fa/enable', async route => {
    requests++
    await hold
    await route.fallback()
  })
  await page.getByRole('button', {name: 'Devam et', exact: true}).click()
  const first = page.getByLabel('Doğrulama kodu 1. rakam')
  await first.fill('12345')
  expect(requests).toBe(0)
  await page.getByLabel('Doğrulama kodu 6. rakam').fill('6')
  await expect(page.getByRole('status')).toContainText('Kod doğrulanıyor')
  await expect(page.getByRole('button', {name: 'Pencereyi kapat'})).toBeDisabled()
  expect(requests).toBe(1)
  await expect(page.locator('.verificationRecoveryCodes')).toHaveCount(0)
  release?.()
  await expect(page.locator('.verificationRecoveryCodes li')).toHaveCount(10)
  await expect(page.locator('.verificationQR')).toHaveCount(0)
  await expect(page.locator('.verificationDigits')).toHaveCount(0)
  await expect(page.getByRole('button', {name: 'Bitir', exact: true})).toBeDisabled()
  await page.screenshot({path: testInfo.outputPath('two-factor-desktop-backup.png'), animations: 'disabled'})
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', {name: 'İndir', exact: true}).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('kaistrade-yedek-kodlar.txt')
  const stream = await download.createReadStream()
  const chunks: Buffer[] = []
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk))
  const text = Buffer.concat(chunks).toString('utf8')
  for (let index = 1; index <= 10; index++) expect(text).toContain(`OFFLINE-CODE-${index}`)
  await expect(page.getByRole('button', {name: 'Bitir', exact: true})).toBeDisabled()
  await page.getByRole('checkbox', {name: 'Kodları güvenli bir yere kaydettim'}).check()
  await page.getByRole('button', {name: 'Bitir', exact: true}).click()
  await expect(page.getByRole('status')).toHaveText('İki aşamalı doğrulama başarıyla etkinleştirildi.')
  expect(mutations.filter(item => item.path.endsWith('/enable'))).toHaveLength(1)
  expect(await page.evaluate(() => JSON.stringify({...localStorage, ...sessionStorage}))).not.toContain('OFFLINE-CODE-')
})

test('Server waiting period is respected and reduced motion disables verification animations', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'reduce'})
  await openVerificationCode(page)
  await page.clock.install()
  let requests = 0
  await page.route('**/api/v22/account/2fa/enable', route => {
    requests++
    return requests === 1
      ? route.fulfill({status: 429, headers: {'Retry-After': '5'}, json: {detail: 'Çok fazla deneme. Lütfen bekle.'}})
      : route.fulfill({status: 401, json: {detail: 'Oturum gerekli.'}})
  })
  await page.getByRole('button', {name: 'Devam et', exact: true}).click()
  const inputs = page.locator('.verificationDigits input')
  await inputs.nth(0).fill('123456')
  await expect(page.getByRole('status')).toContainText('Yeni deneme için 5 saniye bekle.')
  await expect(inputs.nth(0)).toHaveAttribute('readonly', '')
  expect(await page.locator('.verificationSymbol').evaluate(element => getComputedStyle(element).animationName)).toBe('none')
  expect(await page.locator('.verificationSteps li').first().evaluate(element => getComputedStyle(element, '::before').transitionDuration)).toBe('0s')
  await page.clock.runFor(5000)
  await expect(inputs.nth(0)).not.toHaveAttribute('readonly', '')
  expect(requests).toBe(1)
  await inputs.nth(0).fill('123456')
  await expect(page.getByRole('dialog').getByRole('alert')).toHaveText('Oturum gerekli.')
  expect(requests).toBe(2)
})

test('Enrollment requiring a new session still gates backup saving and announces completion before login', async ({page}) => {
  const {setAuthenticated} = await openVerificationCode(page)
  await page.route('**/api/v22/account/2fa/enable', route => {
    setAuthenticated(false)
    return route.fulfill({json: {recovery_codes: Array.from({length: 10}, (_, index) => `REAUTH-CODE-${index + 1}`), reauthenticate: true}})
  })
  await page.getByRole('button', {name: 'Devam et', exact: true}).click()
  await page.getByLabel('Doğrulama kodu 1. rakam').fill('123456')
  await expect(page.locator('.verificationRecoveryCodes li')).toHaveCount(10)
  await expect(page.getByRole('button', {name: 'Bitir', exact: true})).toBeDisabled()
  await page.getByRole('checkbox', {name: 'Kodları güvenli bir yere kaydettim'}).check()
  await page.getByRole('button', {name: 'Bitir', exact: true}).click()
  await expect(page.getByRole('status')).toHaveText('İki aşamalı doğrulama başarıyla etkinleştirildi.')
  await expect(page.locator('.verificationRecoveryCodes')).toHaveCount(0)
  await page.getByRole('button', {name: 'Giriş yap', exact: true}).click()
  await expect(page.getByRole('button', {name: 'GÜVENLİ GİRİŞ', exact: true})).toBeVisible()
})

test('Refreshing security data does not erase unsaved trading preferences', async ({page}) => {
  await prepare(page)
  await page.goto(`${base}/settings`)
  await page.getByLabel('Tercih edilen işlem riski (%)').fill('0.5')
  await page.getByRole('button', {name: 'Oturumları yönet'}).click()
  await page.getByRole('button', {name: 'Diğer oturumları kapat'}).click()
  await expect(page.locator('.accountSessions > li')).toHaveCount(1)
  await page.getByRole('button', {name: 'Pencereyi kapat'}).click()
  await expect(page.getByLabel('Tercih edilen işlem riski (%)')).toHaveValue('0.5')
})

test('Account closes only after typed confirmation and never calls permanent erasure', async ({page}) => {
  const {mutations} = await prepare(page)
  await page.goto(`${base}/settings`)
  await page.getByRole('button', {name: 'Hesabı kapat', exact: true}).click()
  await page.getByLabel('Mevcut parola').fill('Offline-Password1!')
  await page.getByRole('dialog').getByRole('button', {name: 'Hesabı kapat', exact: true}).click()
  expect(mutations).toEqual([])
  await page.getByLabel('Onay için HESABI KAPAT yazın').fill('HESABI KAPAT')
  await page.getByRole('dialog').getByRole('button', {name: 'Hesabı kapat', exact: true}).click()
  await expect(page.getByRole('button', {name: 'GÜVENLİ GİRİŞ'})).toBeVisible()
  expect(mutations[0]).toMatchObject({path: '/api/v22/account/close', method: 'POST', body: {confirmation: 'HESABI KAPAT'}})
  expect(mutations.some(item => item.method === 'DELETE')).toBe(false)
})

test('MFA password login stores no session until challenge verification succeeds', async ({page}) => {
  const {data, setAuthenticated} = await prepare(page, {authenticated: false})
  await page.route('**/api/v22/auth/login', route => route.fulfill({json: {mfa_required: true, challenge_id: 'offline-login-challenge'}}))
  await page.route('**/api/v22/auth/2fa/login', async route => {
    expect(route.request().postDataJSON()).toEqual({challenge_id: 'offline-login-challenge', code: '123456'})
    setAuthenticated(true)
    await route.fulfill({json: {token: `cookie-session:${data.user.id}`, user: data.user, remember: true}})
  })
  await page.goto(`${base}/settings`)
  await page.getByLabel('E-posta', {exact: true}).fill('ada@example.test')
  await page.getByLabel('Parola', {exact: true}).fill('Offline-Password1!')
  await page.getByRole('button', {name: 'GÜVENLİ GİRİŞ'}).click()
  await expect(page.getByRole('heading', {name: 'İki aşamalı doğrulama'})).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('protrebot-v25-session'))).toBeNull()
  await page.getByLabel('2FA veya kurtarma kodu').fill('123456')
  await page.getByRole('button', {name: 'DOĞRULA VE GİRİŞ YAP'}).click()
  await expect(page.getByRole('heading', {name: 'Profil & Ayarlar'})).toBeVisible()
})

test('Google MFA handoff strips challenge from URL and verifies before restoring session', async ({page}) => {
  const {data, setAuthenticated} = await prepare(page, {authenticated: false})
  await page.route('**/api/v22/auth/2fa/login', async route => {
    expect(route.request().postDataJSON()).toEqual({challenge_id: 'offline-google-challenge', code: 'RECOVERY-CODE'})
    setAuthenticated(true)
    await route.fulfill({json: {token: `cookie-session:${data.user.id}`, user: data.user, remember: true}})
  })
  await page.goto(`${base}/settings?mfa_challenge=offline-google-challenge&google_remember=1`)
  await expect(page).toHaveURL(`${base}/settings`)
  await expect(page.getByRole('heading', {name: 'İki aşamalı doğrulama'})).toBeVisible()
  await page.getByLabel('2FA veya kurtarma kodu').fill('RECOVERY-CODE')
  await page.getByRole('button', {name: 'DOĞRULA VE GİRİŞ YAP'}).click()
  await expect(page.getByRole('heading', {name: 'Profil & Ayarlar'})).toBeVisible()
})

test('Expired MFA does not create a session or claim success', async ({page}) => {
  await prepare(page, {authenticated: false})
  await page.route('**/api/v22/auth/2fa/login', route => route.fulfill({status: 401, json: {detail: 'Doğrulama süresi doldu. Yeniden giriş yapın.'}}))
  await page.goto(`${base}/settings?mfa_challenge=expired-challenge`)
  await page.getByLabel('2FA veya kurtarma kodu').fill('123456')
  await page.getByRole('button', {name: 'DOĞRULA VE GİRİŞ YAP'}).click()
  await expect(page.getByRole('alert').filter({hasText: 'Doğrulama süresi doldu'})).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('protrebot-v25-session') || sessionStorage.getItem('protrebot-v25-session'))).toBeNull()
  await expect(page.getByRole('heading', {name: 'Profil & Ayarlar'})).toHaveCount(0)
})

test('Password recovery stays accessible to authenticated users', async ({page}) => {
  await prepare(page)
  await page.goto(`${base}/settings`)
  await page.getByRole('button', {name: 'Parolayı değiştir'}).click()
  await page.getByRole('link', {name: 'Parolamı unuttum'}).click()
  await expect(page.getByRole('button', {name: 'YENİLEME BAĞLANTISI GÖNDER'})).toBeVisible()
})

test('Saved symbol/timeframe apply on initial load without enabling Auto Trade', async ({page}) => {
  const {data} = await prepare(page, {owner: true})
  data.preferences = {...data.preferences, timeframe: '3m', symbols: ['ETHUSDT']}
  await page.goto(`${base}/master-trade?tab=analiz`)
  await expect(page.locator('.refReportSymbol')).toContainText('ETH')
  await expect(page.getByLabel('Zaman dilimi', {exact: true})).toHaveValue('3m')
  await expect(page.getByRole('switch', {name: 'Auto Trade', exact: true})).toHaveAttribute('aria-checked', 'false')
})

async function prepareAdmin(page: Page) {
  const {data} = await prepare(page, {owner: true})
  const closed = account(); closed.user.active = false
  closed.security.active_sessions = 0; closed.sessions = []
  await page.route('**/api/v22/admin/overview', route => route.fulfill({json: {total_users: 1, active_users: 0, verified_users: 1, admins: 0, new_users: 0, pro_users: 0, free_users: 1, active_subscriptions: 0, expired_subscriptions: 0, customers: [], audit: [], billing_live: false}}))
  await page.route('**/api/v22/admin/system-health', route => route.fulfill({json: {overall_status: 'ok', checks: [], counts: {}, checked_at: '2026-10-05T12:00:00Z'}}))
  await page.route('**/api/v22/admin/errors?**', route => route.fulfill({json: {items: [], total: 0, limit: 100, offset: 0}}))
  await page.route('**/api/v22/admin/maintenance', route => route.fulfill({json: {mode: 'NORMAL', reason: '', updated_at: null, updated_by: null}}))
  const queries: URL[] = []
  await page.route('**/api/v22/admin/accounts?**', route => {
    queries.push(new URL(route.request().url()))
    return route.fulfill({json: {users: [{...closed.user, subscription: closed.subscription, two_factor_enabled: false, active_sessions: 0, closed_at: '2026-10-05T12:30:00Z'}], total: 1, page: 1, page_size: 8}})
  })
  await page.route('**/api/v22/admin/accounts/account-test-user', route => route.fulfill({json: {...closed, pending_email: {email: 'pending@example.test', expires_at: '2026-10-05T13:00:00Z'}}}))
  return {data, closed, queries}
}

test('Admin users filters and details use same records; closed users remain visible and no secrets appear', async ({page}) => {
  const {data, queries} = await prepareAdmin(page)
  await page.goto(`${base}/admin`)
  await page.getByRole('button', {name: 'Users', exact: true}).click()
  const users = page.getByRole('region', {name: 'Kullanıcı hesapları'})
  await expect(users).toContainText('ada@example.test')
  await expect(users).toContainText('Premium erişimi yok')
  await expect(users).toContainText('Kapalı / devre dışı')
  await page.getByLabel('Premium filtresi').selectOption('FREE')
  await expect.poll(() => queries.at(-1)?.searchParams.get('premium')).toBe('free')
  await page.getByLabel('Hesap durumu filtresi').selectOption('inactive')
  await expect.poll(() => queries.at(-1)?.searchParams.get('status')).toBe('inactive')
  await page.getByRole('button', {name: 'Ada Yılmaz detayları'}).click()
  await expect(page.getByRole('dialog')).toContainText('pending@example.test')
  await expect(page.getByRole('dialog')).toContainText('15m')
  await expect(page.getByRole('button', {name: 'Hesabı yeniden aç'})).toBeVisible()
  const body = await page.locator('body').innerText()
  expect(body).not.toContain('JBSWY3DPEHPK3PXP')
  expect(body).not.toContain('OFFLINE-CODE')
  expect(data.user.role).toBe('OWNER')
})

test('Admin mutations require confirmation, retain user records and explicitly report reset mail failure', async ({page}) => {
  const {closed} = await prepareAdmin(page)
  const calls: Array<{method: string; path: string; body: Record<string, unknown>}> = []
  await page.route('**/api/v22/customers/account-test-user/status', async route => {
    const body = route.request().postDataJSON() as {active: boolean; reason: string}
    calls.push({method: route.request().method(), path: new URL(route.request().url()).pathname, body})
    closed.user.active = body.active
    await route.fulfill({json: {ok: true}})
  })
  await page.route('**/api/v22/admin/accounts/account-test-user/password-reset', route => route.fulfill({status: 503, json: {detail: 'E-posta sağlayıcısı kullanılamıyor.'}}))
  await page.goto(`${base}/admin`)
  await page.getByRole('button', {name: 'Users', exact: true}).click()
  await page.getByRole('button', {name: 'Ada Yılmaz detayları'}).click()
  await page.getByRole('button', {name: 'Hesabı yeniden aç'}).click()
  expect(calls).toEqual([])
  await page.getByRole('button', {name: 'İşlemi onayla'}).click()
  await expect(page.getByRole('button', {name: 'Hesabı devre dışı bırak'})).toBeVisible()
  expect(calls).toEqual([{method: 'POST', path: '/api/v22/customers/account-test-user/status', body: {active: true, reason: 'Admin kullanıcı yönetimi'}}])
  await expect(page.getByRole('dialog')).toContainText('ada@example.test')
  await page.getByRole('button', {name: 'Parola yenileme e-postası gönder', exact: true}).click()
  await page.getByRole('button', {name: 'İşlemi onayla'}).click()
  await expect(page.getByRole('dialog').getByRole('alert')).toHaveText('E-posta sağlayıcısı kullanılamıyor.')
  await expect(page.getByText('Parola yenileme e-postası gönderildi.', {exact: true})).toHaveCount(0)
})
