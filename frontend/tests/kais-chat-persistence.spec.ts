import {expect, test, type Page} from '@playwright/test'
import {approval, mockAssistant, openChat} from './helpers/assistant-api'
import {openChatFromHint} from './helpers/assistant-ui'
import {CHAT_MAX_AGE_MS, chatStorageKey, type StoredChatMessage} from '../src/kais-chat-storage'
import {assistantCopy} from '../../ui-copy'

test.use({baseURL: 'http://127.0.0.1:4174'})
const NOW = Date.UTC(2026, 9, 3, 12)
const KEY = chatStorageKey('assistant-member')
const AUTH_URL = 'http://127.0.0.1:4175/'
const OWNER_URL = 'http://127.0.0.1:4176/'
const SESSION_KEY = 'protrebot-v25-session'
const USER_COOKIE = 'protrebot_session'
const COOKIE_HINT = 'cookie-session:assistant-member'
const dialog = (page: Page) => page.getByRole('dialog', {name: 'Kais AI', exact: true})
const seedRow = (id: number): StoredChatMessage => ({
  id: `seed-${id}`, role: id % 2 ? 'assistant' : 'user', language: 'tr', content: `Eski mesaj ${id}`,
})

async function setup(page: Page, remembered = false) {
  await page.clock.install({time: new Date(NOW)})
  await page.clock.pauseAt(new Date(NOW + 1000))
  await page.emulateMedia({reducedMotion: 'reduce'})
  if (remembered) {
    await page.addInitScript(({key, hint}) => {
      if (sessionStorage.getItem('assistant-remembered-test-seeded')) return
      sessionStorage.setItem('assistant-remembered-test-seeded', '1')
      if (!localStorage.getItem(key)) localStorage.setItem(key, hint)
    }, {key: SESSION_KEY, hint: COOKIE_HINT})
  }
  const state = await mockAssistant(page, remembered)
  await page.addInitScript(({remembered, key, hint}) => {
    const storage = remembered ? localStorage : sessionStorage
    if (storage.getItem(key) === 'member-ui-test-session') storage.setItem(key, hint)
  }, {remembered, key: SESSION_KEY, hint: COOKIE_HINT})
  await seedUserCookie(page)
  await page.route('**/api/v22/auth/login', async route => {
    const request = route.request()
    state.requests.push(`${request.method()} /api/v22/auth/login`)
    expect(request.headers()['x-requested-with']).toBe('XMLHttpRequest')
    expect(request.headers()['authorization']).toBeUndefined()
    state.profileStatus = 200
    state.sessionStatus = 200
    await route.fulfill({json: {token: `cookie-session:${state.userId}`, user: {
      id: state.userId, role: 'CUSTOMER', active: true, email_verified: true,
      email: 'member@example.test', display_name: 'UI Test Member',
    }}, headers: {'Set-Cookie': `${USER_COOKIE}=mock-signed-user-session; HttpOnly; SameSite=Lax; Path=/api`}})
  })
  await page.route('**/api/v22/auth/logout', async route => {
    state.requests.push('POST /api/v22/auth/logout')
    await route.fulfill({json: {ok: true}, headers: {
      'Set-Cookie': `${USER_COOKIE}=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/api`,
    }})
  })
  await page.route('**/api/web/access/logout', async route => {
    state.requests.push('POST /api/web/access/logout')
    await route.fulfill({json: {ok: true}, headers: {
      'Set-Cookie': 'protrebot_owner=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/api',
    }})
  })
  await page.route('**/api/web/access/check', async route => {
    if (!route.request().headers()['cookie']?.includes('protrebot_owner=mock-owner-access-cookie') || state.ownerStatus === 401) {
      state.requests.push('GET /api/web/access/check')
      await route.fulfill({status: 401, json: {authorized: false}})
      return
    }
    await route.fallback()
  })
  await page.route(/\/api\/v22\/(?:session|profile)(?:\?.*)?$/, async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const hasCookie = request.headers()['cookie']?.includes(`${USER_COOKIE}=mock-signed-user-session`) === true
    const status = path.endsWith('/profile') ? state.profileStatus : state.sessionStatus
    if (!hasCookie || status === 401) {
      state.requests.push(`${request.method()} ${path}`)
      await route.fulfill({status: 401, json: {detail: 'Not authenticated'}, headers: {
        'Set-Cookie': `${USER_COOKIE}=; Max-Age=0; HttpOnly; SameSite=Lax; Path=/api`,
      }})
      return
    }
    expect(request.headers()['authorization']).toBeUndefined()
    await route.fallback()
  })
  return state
}

async function seedUserCookie(page: Page) {
  await page.context().addCookies([{
    name: USER_COOKIE, value: 'mock-signed-user-session', domain: '127.0.0.1',
    path: '/api', httpOnly: true, sameSite: 'Lax', secure: false,
  }])
}

async function expectNoUserCookie(page: Page) {
  expect((await page.context().cookies()).filter(cookie => cookie.name === USER_COOKIE)).toEqual([])
}

async function seed(page: Page, raw: string, extra?: string) {
  await page.addInitScript(({key, raw, extra}) => {
    if (sessionStorage.getItem('kais-chat-test-seeded')) return
    sessionStorage.setItem('kais-chat-test-seeded', '1')
    localStorage.setItem(key, raw)
    if (extra) localStorage.setItem(extra, raw)
  }, {key: KEY, raw, extra})
}

async function stored(page: Page) {
  return page.evaluate(key => localStorage.getItem(key), KEY)
}

async function send(page: Page, content: string) {
  const panel = dialog(page)
  const before = await panel.locator('.assistantMessage.assistant').count()
  await panel.getByRole('textbox', {name: 'Kais AI mesajın', exact: true}).fill(content)
  await panel.getByRole('button', {name: 'Kais AI mesajını gönder', exact: true}).click()
  await expect(panel.locator('.assistantMessage.assistant')).toHaveCount(before + 1)
  await expect(panel.locator('.assistantTyping')).toHaveCount(0)
  await expect(panel.getByRole('textbox', {name: 'Kais AI mesajın', exact: true})).toBeEnabled()
  await expect(panel.locator('.assistantSuggestions')).toHaveCount(0)
}

async function openAt(page: Page, url: string) {
  await page.goto(url)
  await openChatFromHint(page)
  await expect(dialog(page).getByRole('textbox')).toBeEnabled()
}

async function logoutUi(page: Page) {
  await page.getByRole('button', {name: 'Profil menüsünü aç', exact: true}).click()
  await page.getByRole('menu').getByRole('menuitem', {name: /^Çıkış/}).click()
}

async function chatKeys(page: Page) {
  return page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('kais-chat:')))
}

async function seedOwner(page: Page) {
  await page.context().addCookies([{
    name: 'protrebot_owner', value: 'mock-owner-access-cookie', domain: '127.0.0.1',
    path: '/api', httpOnly: true, sameSite: 'Lax', secure: false,
  }])
  await page.addInitScript(() => {
    if (sessionStorage.getItem('assistant-owner-test-seeded')) return
    sessionStorage.setItem('assistant-owner-test-seeded', '1')
    sessionStorage.setItem('protrebot.web.owner-access', 'cookie-owner')
  })
}

for (const width of [1440, 390]) {
  test(`Completed chat survives reload without an empty welcome flash at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 844})
    const state = await setup(page)
    await openChat(page)
    await send(page, 'Plan ve kredi hakkında bilgi istiyorum.')
    expect(JSON.parse((await stored(page))!).messages).toHaveLength(2)
    await page.reload()
    await openChatFromHint(page)
    await expect(dialog(page).locator('.assistantMessage')).toHaveCount(2)
    await expect(dialog(page).locator('.assistantEmpty')).toHaveCount(0)
    await expect(dialog(page)).toContainText('Plan ve kredi hakkında bilgi istiyorum.')
    await expect(dialog(page)).toContainText('Yanıt düz metindir.')
    expect(state.chats).toHaveLength(1)
    await page.screenshot({path: testInfo.outputPath(`kais-chat-restored-${width}.png`)})
    await send(page, 'API bağlantısı hakkında bilgi istiyorum.')
    expect(state.chats.at(-1)!.body.history).toHaveLength(2)
  })
}

test('Reset removes local history and the empty welcome survives reload', async ({page}) => {
  await setup(page)
  await openChat(page)
  await send(page, 'Plan bilgisi')
  await dialog(page).getByRole('button', {name: assistantCopy.tr.clear, exact: true}).click()
  await expect(dialog(page).locator('.assistantMessage')).toHaveCount(0)
  await expect(dialog(page).locator('.assistantEmpty')).toBeVisible()
  expect(await stored(page)).toBeNull()
  await page.reload()
  await openChatFromHint(page)
  await expect(dialog(page).locator('.assistantMessage')).toHaveCount(0)
  await expect(dialog(page).locator('.assistantEmpty')).toBeVisible()
  expect(await stored(page)).toBeNull()
})

test('The real shared logout entry clears chat storage, session token and the open in-memory chat', async ({page}) => {
  await setup(page)
  await openChat(page)
  await send(page, 'Kredi bilgisi')
  await page.evaluate(async () => {
    const path = '/api.ts'
    const api: {clearUserSessionToken: () => void} = await import(path)
    api.clearUserSessionToken()
  })
  await expect(dialog(page).locator('.assistantMessage')).toHaveCount(0)
  await expect(dialog(page).getByRole('textbox')).toBeDisabled()
  expect(await stored(page)).toBeNull()
  expect(await page.evaluate(() => sessionStorage.getItem('protrebot-v25-session'))).toBeNull()
  await page.clock.runFor(60000)
  expect(await stored(page)).toBeNull()
})

test('Changing the verified user deletes every foreign chat version and never shows its messages', async ({page}) => {
  const state = await setup(page)
  await seed(page, JSON.stringify({version: 1, savedAt: NOW, messages: [seedRow(0)]}), 'kais-chat:v0:another-member')
  await openChat(page)
  await expect(dialog(page)).toContainText('Eski mesaj 0')
  state.userId = 'new-assistant-member'
  await page.reload()
  await openChatFromHint(page)
  await expect(dialog(page).locator('.assistantMessage')).toHaveCount(0)
  expect(await page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('kais-chat:')))).toEqual([])
  await send(page, 'Yeni kullanıcı plan bilgisi')
  expect(await stored(page)).toBeNull()
  expect(await page.evaluate(() => localStorage.getItem('kais-chat:v1:new-assistant-member'))).not.toBeNull()
})

test('Only the latest 50 messages persist, Unicode content is capped at 4000 and model history never exceeds 12', async ({page}) => {
  const state = await setup(page)
  state.historyLimit = 40
  state.chatBody = {reply: '🙂'.repeat(4001), language: 'tr', sources: []}
  await seed(page, JSON.stringify({version: 1, savedAt: NOW, messages: Array.from({length: 50}, (_, i) => seedRow(i))}))
  await openChat(page)
  await send(page, 'Yeni soru')
  const saved = JSON.parse((await stored(page))!) as {messages: StoredChatMessage[]}
  expect(saved.messages).toHaveLength(50)
  expect(saved.messages[0].id).toBe('seed-2')
  expect(Array.from(saved.messages.at(-1)!.content)).toHaveLength(4000)
  expect(state.chats[0].body.history).toHaveLength(12)
  expect(state.chats[0].body.history).toEqual(Array.from({length: 12}, (_, i) => {
    const message = seedRow(i + 38)
    return {role: message.role, content: Array.from(message.content).slice(0, 12).join('')}
  }))
  await page.reload()
  await openChatFromHint(page)
  await expect(dialog(page).locator('.assistantMessage')).toHaveCount(50)
})

test('A record older than 30 days is deleted with the browser clock advanced', async ({page}) => {
  await setup(page)
  await seed(page, JSON.stringify({version: 1, savedAt: NOW, messages: [seedRow(0)]}))
  await page.clock.setSystemTime(new Date(NOW + CHAT_MAX_AGE_MS + 1001))
  await openChat(page)
  await expect(dialog(page).locator('.assistantEmpty')).toBeVisible()
  expect(await stored(page)).toBeNull()
})

for (const raw of ['{broken', JSON.stringify({version: 0, savedAt: NOW, messages: [seedRow(0)]}),
  JSON.stringify({version: 1, savedAt: NOW, messages: [{...seedRow(0), confirmation_token: 'should-not-load'}]})]) {
  test(`Invalid or obsolete history starts clean without throwing (${raw.slice(0, 20)})`, async ({page}) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await setup(page)
    await seed(page, raw)
    await openChat(page)
    await expect(dialog(page).locator('.assistantEmpty')).toBeVisible()
    expect(await stored(page)).toBeNull()
    await send(page, 'Temiz sohbet')
    expect(errors).toEqual([])
  })
}

for (const failure of ['blocked', 'full'] as const) {
  test(`LocalStorage ${failure} only disables persistence, not in-memory chat`, async ({page}) => {
    const errors: string[] = []
    const warnings: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (message.type() === 'warning') warnings.push(message.text()) })
    await setup(page)
    await page.addInitScript(failure => {
      if (failure === 'blocked') {
        Object.defineProperty(window, 'localStorage', {get: () => { throw new DOMException('Denied', 'SecurityError') }})
      } else {
        const original = Storage.prototype.setItem
        Storage.prototype.setItem = function(key, value) {
          if (key.startsWith('kais-chat:')) throw new DOMException('Full', 'QuotaExceededError')
          return original.call(this, key, value)
        }
      }
    }, failure)
    await openChat(page)
    await send(page, 'Plan bilgisi')
    await send(page, 'Kredi bilgisi')
    await expect(dialog(page).locator('.assistantMessage')).toHaveCount(4)
    await expect(dialog(page).locator('.assistantNotice')).toHaveCount(0)
    expect(errors).toEqual([])
    expect(warnings.some(value => value.includes('Kais AI chat persistence disabled'))).toBe(true)
  })
}

test('Secret-like user and assistant text remains exact on screen and in the API but is masked at rest', async ({page}) => {
  const state = await setup(page)
  const userText = 'api-privateValue'
  const reply = 'Bearer response-credential; secret=short-private-value; ' + 'aB3d'.repeat(12)
  state.chatBody = {reply, language: 'tr', sources: []}
  await openChat(page)
  await send(page, userText)
  await expect(dialog(page).locator('.assistantMessage.user .assistantText')).toHaveText(userText)
  await expect(dialog(page).locator('.assistantMessage.assistant .assistantText')).toHaveText(reply)
  expect(state.chats[0].body.message).toBe(userText)
  const raw = (await stored(page))!
  for (const value of [userText, 'response-credential', 'short-private-value', 'aB3d'.repeat(12)]) expect(raw).not.toContain(value)
  expect(raw).toContain('[MASKED]')
})

test('Confirmation proof is never serialized, even when echoed in the completed reply; reload cannot approve it', async ({page}) => {
  const state = await setup(page)
  state.chatBody = {reply: `Analiz onayı: ${approval.confirmation_token}`, language: 'tr', sources: [], needs_confirmation: approval}
  await openChat(page)
  await send(page, 'Analiz durumu')
  await expect(dialog(page).getByRole('button', {name: 'Onayla', exact: true})).toBeVisible()
  const raw = (await stored(page))!
  expect(raw).not.toContain(approval.confirmation_token)
  expect(raw).not.toContain('confirmation_token')
  expect(raw).not.toContain('"decision"')
  await page.reload()
  await openChatFromHint(page)
  await expect(dialog(page).getByRole('button', {name: 'Onayla', exact: true})).toHaveCount(0)
  expect(state.confirmations).toEqual([])
})

test('Pending and failed messages never persist or enter the next model context', async ({page}) => {
  const state = await setup(page)
  await openChat(page)
  await send(page, 'Tamamlanmış soru')
  let release: () => void = () => {}
  state.hold = new Promise<void>(resolve => {release = resolve})
  await dialog(page).getByRole('textbox').fill('Bekleyen ve başarısız soru')
  await dialog(page).getByRole('button', {name: 'Kais AI mesajını gönder', exact: true}).click()
  await expect(dialog(page).locator('.assistantTyping')).toBeVisible()
  expect(await stored(page)).not.toContain('Bekleyen ve başarısız soru')
  state.network = true
  release()
  await expect(dialog(page).getByRole('alert')).toContainText('Ağ bağlantısı')
  expect(await stored(page)).not.toContain('Bekleyen ve başarısız soru')
  state.network = false; state.hold = null
  await send(page, 'Sonraki soru')
  expect(JSON.stringify(state.chats.at(-1)!.body.history)).not.toContain('Bekleyen')
})

test('Assistant API 401 deletes only its current user history, and a different reauthenticated user starts clean', async ({page}) => {
  const state = await setup(page)
  await openChat(page)
  await send(page, 'Önceki oturum')
  await page.evaluate(({now, message}) => localStorage.setItem('kais-chat:v1:unrelated-user',
    JSON.stringify({version: 1, savedAt: now, messages: [message]})), {now: NOW, message: seedRow(0)})
  state.chatStatus = 401; state.chatBody = {error_code: 'session'}
  await dialog(page).getByRole('textbox').fill('Süresi dolan oturum')
  await dialog(page).getByRole('button', {name: 'Kais AI mesajını gönder', exact: true}).click()
  await expect(dialog(page).getByRole('alert')).toContainText('Oturumun sona ermiş')
  expect(await stored(page)).toBeNull()
  expect(await page.evaluate(() => localStorage.getItem('kais-chat:v1:unrelated-user'))).not.toBeNull()
  state.userId = 'reauthenticated-member'; state.chatStatus = 200
  await page.reload()
  await openChatFromHint(page)
  await expect(dialog(page).locator('.assistantEmpty')).toBeVisible()
  expect(await stored(page)).toBeNull()
})

test('Native cross-tab storage events apply last-writer history and reset without echoing writes', async ({page, context}) => {
  await setup(page)
  await openChat(page)
  await send(page, 'Birinci sekme')
  const peer = await context.newPage()
  try {
    await setup(peer)
    await openChat(peer)
    await expect(dialog(peer).locator('.assistantMessage')).toHaveCount(2)
    await send(peer, 'İkinci sekme')
    await expect(dialog(page).locator('.assistantMessage')).toHaveCount(4)
    await expect(dialog(page)).toContainText('İkinci sekme')
    await dialog(page).getByRole('button', {name: assistantCopy.tr.clear, exact: true}).click()
    await expect(dialog(page).locator('.assistantMessage')).toHaveCount(0)
    await expect(dialog(page).locator('.assistantEmpty')).toBeVisible()
    await expect(dialog(peer).locator('.assistantMessage')).toHaveCount(0)
    await expect(dialog(peer).locator('.assistantEmpty')).toBeVisible()
    expect(await stored(page)).toBeNull()
    expect(await stored(peer)).toBeNull()
    for (const tab of [page, peer]) {
      await tab.reload()
      await openChatFromHint(tab)
      await expect(dialog(tab).locator('.assistantMessage')).toHaveCount(0)
      await expect(dialog(tab).locator('.assistantEmpty')).toBeVisible()
      expect(await stored(tab)).toBeNull()
    }
  } finally { await peer.close() }
})

test('No authenticated user means no launcher, no history display and no anonymous record', async ({page}) => {
  const state = await setup(page)
  state.userId = ''
  await page.goto('/')
  await expect(page.getByRole('heading', {name: 'İşlem Terminali'})).toBeVisible()
  await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toHaveCount(0)
  expect(await page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('kais-chat:')))).toEqual([])
  expect(state.chats).toEqual([])
})

test('Real AuthGate logout button clears history and real login as another user starts and persists a clean chat', async ({page}) => {
  const state = await setup(page, true)
  await openAt(page, AUTH_URL)
  await send(page, 'Gerçek çıkış öncesi sohbet')
  await page.keyboard.press('Escape')
  await logoutUi(page)
  await expect(page.getByRole('heading', {name: 'Hesabınıza giriş yapın', exact: true})).toBeVisible()
  await expect(page.locator('.assistantMessage')).toHaveCount(0)
  expect(await chatKeys(page)).toEqual([])
  expect(state.requests).toContain('POST /api/v22/auth/logout')
  await expectNoUserCookie(page)
  state.userId = 'signed-in-second-member'
  await page.getByLabel('E-posta', {exact: true}).fill('member@example.test')
  await page.getByLabel('Parola', {exact: true}).fill('UiLoginOnly123!')
  await page.getByRole('button', {name: 'GÜVENLİ GİRİŞ', exact: true}).click()
  await expect.poll(() => page.evaluate(key => localStorage.getItem(key) || sessionStorage.getItem(key), SESSION_KEY)).toBe('cookie-session:signed-in-second-member')
  expect(await page.evaluate(() => document.cookie)).not.toContain('mock-signed-user-session')
  await openChatFromHint(page)
  await expect(dialog(page).locator('.assistantEmpty')).toBeVisible()
  await expect(dialog(page).locator('.assistantMessage')).toHaveCount(0)
  expect(await chatKeys(page)).toEqual([])
  await send(page, 'Yeni oturumda yeni sohbet')
  expect(await page.evaluate(() => localStorage.getItem('kais-chat:v1:signed-in-second-member'))).not.toBeNull()
  expect(await stored(page)).toBeNull()
})

test('Profile refresh 401 wipes chat and signed cookie, retaining only a harmless hint until verified access resumes', async ({page}) => {
  const state = await setup(page)
  await openChat(page)
  await send(page, 'Profil kontrolü öncesi sohbet')
  await page.evaluate(() => localStorage.setItem('kais-chat:v0:foreign-user', 'old-record'))
  state.profileStatus = 401
  await page.evaluate(() => window.dispatchEvent(new Event('protrebot-access-refresh')))
  await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toHaveCount(0)
  await expect(page.locator('.assistantMessage')).toHaveCount(0)
  expect(await chatKeys(page)).toEqual([])
  expect(await page.evaluate(key => sessionStorage.getItem(key), SESSION_KEY)).toBe(COOKIE_HINT)
  await expectNoUserCookie(page)
  state.profileStatus = 200
  expect(await page.evaluate(async () => (await fetch('/api/v22/profile')).status)).toBe(401)
  state.userId = 'profile-verified-member'
  await seedUserCookie(page)
  await page.evaluate(() => window.dispatchEvent(new Event('protrebot-access-refresh')))
  await openChatFromHint(page)
  await expect(dialog(page).locator('.assistantEmpty')).toBeVisible()
  await send(page, 'Doğrulanmış yeni oturum')
  expect(await page.evaluate(() => localStorage.getItem('kais-chat:v1:profile-verified-member'))).not.toBeNull()
  expect(await stored(page)).toBeNull()
})

test('Missing token on access refresh wipes all chat history and removes in-memory messages', async ({page}) => {
  await setup(page)
  await openChat(page)
  await send(page, 'Token kaybolmadan önce')
  await page.evaluate(key => {
    sessionStorage.removeItem(key)
    localStorage.removeItem(key)
    window.dispatchEvent(new Event('protrebot-access-refresh'))
  }, SESSION_KEY)
  await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toHaveCount(0)
  await expect(page.locator('.assistantMessage')).toHaveCount(0)
  expect(await chatKeys(page)).toEqual([])
})

test('Real logout in one tab stops the receiving tab, and later submit or reload cannot resurrect history', async ({page, context}) => {
  const state = await setup(page, true)
  await openAt(page, AUTH_URL)
  const peer = await context.newPage()
  try {
    const peerState = await setup(peer, true)
    await openAt(peer, AUTH_URL)
    await page.evaluate(() => window.dispatchEvent(new Event('protrebot-access-refresh')))
    await openChatFromHint(page)
    await expect(dialog(page).getByRole('textbox')).toBeEnabled()
    await send(page, 'İki sekmeli çıkış öncesi')
    await expect(dialog(peer).locator('.assistantMessage')).toHaveCount(2)
    await dialog(peer).getByRole('textbox').fill('Çıkıştan sonra kaydedilmemeli')
    await page.keyboard.press('Escape')
    await logoutUi(page)
    await expect(page.getByRole('heading', {name: 'Hesabınıza giriş yapın', exact: true})).toBeVisible()
    await expect(page.locator('.assistantMessage')).toHaveCount(0)
    expect(await chatKeys(page)).toEqual([])
    await expect(dialog(peer).getByRole('textbox')).toBeDisabled()
    await expect(dialog(peer).locator('.assistantMessage')).toHaveCount(0)
    expect(await chatKeys(peer)).toEqual([])
    await dialog(peer).locator('form').evaluate(form => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})))
    expect(peerState.chats).toEqual([])
    await peer.clock.runFor(60000)
    expect(await chatKeys(peer)).toEqual([])
    await peer.reload()
    await expect(peer.getByRole('heading', {name: 'Hesabınıza giriş yapın', exact: true})).toBeVisible()
    await expect(peer.locator('.assistantMessage')).toHaveCount(0)
    expect(await chatKeys(peer)).toEqual([])
    expect(state.chats).toHaveLength(1)
  } finally { await peer.close() }
})

test('Changing the shared token stops the receiving tab until the new user is verified', async ({page, context}) => {
  await setup(page, true)
  await openChat(page)
  await send(page, 'Eski token ile sohbet')
  const peer = await context.newPage()
  try {
    const peerState = await setup(peer, true)
    await openChat(peer)
    peerState.userId = 'token-replacement-member'
    await page.evaluate(key => localStorage.setItem(key, 'cookie-session:token-replacement-member'), SESSION_KEY)
    await expect(dialog(peer).getByRole('textbox')).toBeDisabled()
    await expect(dialog(peer).locator('.assistantMessage')).toHaveCount(0)
    expect(await chatKeys(peer)).toEqual([])
    await peer.evaluate(() => window.dispatchEvent(new Event('protrebot-access-refresh')))
    await expect(peer.locator('.assistantLauncher .kaisEye')).toHaveAttribute('data-state', 'idle')
    await openChatFromHint(peer)
    await expect(dialog(peer).locator('.assistantEmpty')).toBeVisible()
    await send(peer, 'Yeni token ile temiz sohbet')
    expect(await peer.evaluate(() => localStorage.getItem('kais-chat:v1:token-replacement-member'))).not.toBeNull()
    expect(await stored(peer)).toBeNull()
  } finally { await peer.close() }
})

test('Production WebAccessGate logout hides its badge for members and clears chat and owner cookie when explicitly available', async ({page}) => {
  await setup(page)
  await seedOwner(page)
  await openAt(page, OWNER_URL)
  await send(page, 'Owner çıkışı öncesi sohbet')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', {name: 'Güvenli oturum · Çıkış', exact: true})).toHaveCount(0)
  await page.evaluate(key => {
    sessionStorage.removeItem(key)
    localStorage.removeItem(key)
    window.dispatchEvent(new Event('protrebot-session-changed'))
  }, SESSION_KEY)
  await page.getByRole('button', {name: 'Güvenli oturum · Çıkış', exact: true}).click()
  await expect(page.getByRole('heading', {name: 'Yönetici erişimi', exact: true})).toBeVisible()
  expect(await chatKeys(page)).toEqual([])
  expect(await page.evaluate(() => sessionStorage.getItem('protrebot.web.owner-access'))).toBeNull()
  expect((await page.context().cookies()).filter(cookie => cookie.name === 'protrebot_owner')).toEqual([])
  expect((await page.context().cookies()).find(cookie => cookie.name === USER_COOKIE)?.httpOnly).toBe(true)
})

test('Production owner-access verification failure clears owner token and all chat history', async ({page}) => {
  const state = await setup(page)
  await seedOwner(page)
  await openAt(page, OWNER_URL)
  await send(page, 'Owner doğrulama öncesi')
  state.ownerStatus = 401
  await page.reload()
  await expect(page.getByRole('heading', {name: 'Yönetici erişimi', exact: true})).toBeVisible()
  expect(await chatKeys(page)).toEqual([])
  expect(await page.evaluate(() => sessionStorage.getItem('protrebot.web.owner-access'))).toBeNull()
})

test('Member maintenance polling 401 clears private chat and session without redundant logout', async ({page}) => {
  const state = await setup(page, true)
  await openAt(page, AUTH_URL)
  await send(page, 'Bakım yoklaması öncesi')
  state.sessionStatus = 401
  await page.clock.runFor(45000)
  await expect.poll(() => state.requests.filter(path => path === 'GET /api/v22/session').length).toBeGreaterThan(1)
  await expect(page.getByRole('heading', {name: 'Hesabınıza giriş yapın', exact: true})).toBeVisible()
  await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toHaveCount(0)
  await expect(page.locator('.assistantMessage')).toHaveCount(0)
  expect(await chatKeys(page)).toEqual([])
  expect(await page.evaluate(key => [localStorage.getItem(key), sessionStorage.getItem(key)], SESSION_KEY)).toEqual([null, null])
  await expectNoUserCookie(page)
  expect(state.requests).not.toContain('POST /api/v22/auth/logout')
})
