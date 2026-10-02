import {expect, test, type Page} from '@playwright/test'
import {join} from 'node:path'

test.use({baseURL: 'http://127.0.0.1:4174'})

type ApiCall = {path: string; body: Record<string, unknown>}
type MockState = {
  userId: string; remaining: number; chatStatus: number; chatBody: Record<string, unknown>;
  confirmStatus: number; confirmations: ApiCall[]; chats: ApiCall[]; requests: string[];
  network: boolean; hold: Promise<void> | null; historyLimit: number; confirmNetwork: boolean;
  proactiveEnabled: boolean; proactiveMessage: Record<string, unknown> | null;
  preferenceStatus: number; checkInStatus: number; preferenceSaves: ApiCall[]; checkIns: ApiCall[];
}

const approval = {action: 'get_analysis', symbol: 'BTCUSDT', timeframe: '1h', cost: 7, analysis_cost: 7, confirmation_token: 'signed-approval-ui-test'}
const checkInMessage = {id: 'owned-status-check-in', reply: '1 açık pozisyon. PnL: 12,30.\nKoruma doğrulandı.\nVeri 4 saniye önce alındı.',
  language: 'tr', sources: ['get_my_positions', 'get_protection_status'], fetched_at: '2026-10-03T00:00:00Z', stale: false}

async function mockAssistant(page: Page): Promise<MockState> {
  const state: MockState = {
    userId: 'assistant-member', remaining: 19, chatStatus: 200,
    chatBody: {reply: '**Plan**\nYanıt düz metindir.', language: 'tr', sources: ['get_plans']},
    confirmStatus: 200, confirmations: [], chats: [], requests: [], network: false, hold: null, historyLimit: 2, confirmNetwork: false,
    proactiveEnabled: false, proactiveMessage: null, preferenceStatus: 200, checkInStatus: 200, preferenceSaves: [], checkIns: [],
  }
  await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'member-ui-test-session'))
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    state.requests.push(`${request.method()} ${path}`)
    const user = {id: state.userId, role: 'CUSTOMER', active: true, email_verified: true}
    if (path === '/api/assistant/proactive/preferences') {
      if (request.method() === 'POST') {
        const body = request.postDataJSON()
        state.preferenceSaves.push({path, body})
        if (state.preferenceStatus === 200) state.proactiveEnabled = body.enabled
      }
      await route.fulfill({status: request.method() === 'POST' ? state.preferenceStatus : 200,
        json: {enabled: state.proactiveEnabled, available: true, poll_interval_seconds: 60}})
      return
    }
    if (path === '/api/assistant/proactive/check-in') {
      state.checkIns.push({path, body: request.postDataJSON()})
      await route.fulfill({status: state.checkInStatus, json: {enabled: state.proactiveEnabled, available: true, poll_interval_seconds: 60,
        message: state.proactiveEnabled ? state.proactiveMessage : null}})
      return
    }
    if (path === '/api/assistant/usage') {
      await route.fulfill({json: {remaining: state.remaining, total: 20, resetsAt: '2026-10-04T00:00:00Z', limits: {
        max_input_chars: 80, history_messages: state.historyLimit, history_message_max_chars: 12, page_context_max_chars: 8, secret_min_alphanumeric_chars: 40,
      }}})
      return
    }
    if (path === '/api/assistant/chat') {
      state.chats.push({path, body: request.postDataJSON()})
      if (state.hold) await state.hold
      if (state.network) { await route.abort('failed'); return }
      await route.fulfill({status: state.chatStatus, json: state.chatBody, headers: state.chatStatus === 429 ? {'Retry-After': '45'} : {}})
      return
    }
    if (path === '/api/assistant/analysis/confirm') {
      state.confirmations.push({path, body: request.postDataJSON()})
      if (state.confirmNetwork) { await route.abort('failed'); return }
      await route.fulfill({status: state.confirmStatus, json: state.confirmStatus === 200 ? {
        data: {symbol: 'BTCUSDT', timeframe: '1h', direction: 'LONG', final_decision_score: 72, confidence: 81, opportunity_score: 78, mtf_alignment: 90, data_age_seconds: 4,
          entry: 987654.321, stop_loss: 123456.789},
        fetched_at: '2026-10-03T00:00:00Z', stale: false, sources: ['get_analysis'],
      } : {detail: 'Invalid confirmation'}})
      return
    }
    const fixtures: Record<string, unknown> = {
      '/api/v22/session': {user},
      '/api/v22/profile': {user, access: {canAccessMasterTrade: true, isPremium: false}},
      '/api/markets': [{symbol: 'BTCUSDT', display: 'BTC/USDT', price: 60000, change: 1, volume: 1000000}],
      '/api/health': {status: 'ok'},
      '/api/exchange-connections/status': {connections: {TESTNET: {configured: false, active: false}}, vault: {ready: true}},
      '/api/notifications': {items: [], unread: 0},
    }
    await route.fulfill({json: fixtures[path] ?? {}})
  })
  return state
}

async function openChat(page: Page) {
  await page.goto('/')
  await page.getByRole('button', {name: 'Müşteri Asistanını aç'}).click()
  const dialog = page.getByRole('dialog', {name: 'Müşteri Asistanı'})
  await expect(dialog.getByRole('textbox', {name: 'Mesajın'})).toBeEnabled()
  return dialog
}

for (const width of [1440, 390]) {
  test(`Native assistant dialog opens, closes, returns focus and fits ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 844})
    await mockAssistant(page)
    const dialog = await openChat(page)
    await expect(dialog).toBeVisible()
    expect(await dialog.evaluate(element => element.matches(':modal'))).toBe(true)
    await expect(dialog).toContainText('Günlük mesaj hakkı: 19/20')
    const box = await dialog.boundingBox()
    expect(box).not.toBeNull()
    if (width < 768) {
      expect(box!.width).toBeCloseTo(width, 0)
      expect(box!.y + box!.height).toBeCloseTo(844, 0)
    } else {
      expect(box!.x + box!.width).toBeCloseTo(width - 20, 0)
      expect(box!.height).toBeCloseTo(804, 0)
    }
    await page.screenshot({path: join(testInfo.outputDir, `assistant-${width}.png`)})
    await page.keyboard.press('Escape')
    await expect(dialog).not.toBeVisible()
    await expect(page.getByRole('button', {name: 'Müşteri Asistanını aç'})).toBeFocused()
    await page.getByRole('button', {name: 'Müşteri Asistanını aç'}).click()
    await dialog.getByRole('button', {name: 'Sohbeti kapat'}).click()
    await expect(page.getByRole('button', {name: 'Müşteri Asistanını aç'})).toBeFocused()
  })
}

test('Suggested question, typing state, plain text, copy exception and session history', async ({page}) => {
  const state = await mockAssistant(page)
  let release: () => void = () => {}
  state.hold = new Promise<void>(resolve => {release = resolve})
  state.chatBody = {reply: '**Plan**\n<img src=x onerror="window.assistantInjected=true">', language: 'tr', sources: ['get_plans']}
  const dialog = await openChat(page)
  await dialog.getByRole('button', {name: 'Premium ne kadar?', exact: true}).click()
  await expect(dialog).toContainText('Asistan yazıyor...')
  await expect(dialog.getByRole('button', {name: 'Kredim ne kadar?', exact: true})).toBeDisabled()
  release()
  await expect(dialog.locator('.assistantTyping')).toHaveCount(0)
  await expect(dialog.locator('.assistantMessage.assistant strong')).toHaveText('Plan')
  await expect(dialog.locator('.assistantMessage.assistant')).toContainText('<img')
  await expect(dialog.locator('.assistantMessage.assistant img')).toHaveCount(0)
  expect(await page.evaluate(() => Object.prototype.hasOwnProperty.call(window, 'assistantInjected'))).toBe(false)
  expect(state.chats).toHaveLength(1)
  expect(state.chats[0].body).toEqual({message: 'Premium ne kadar?', history: [], page_context: 'dashboar'})
  const copy = await dialog.locator('.assistantText').last().evaluate(element => {
    const range = document.createRange()
    range.selectNodeContents(element)
    const selection = window.getSelection()!
    selection.removeAllRanges(); selection.addRange(range)
    const event = new ClipboardEvent('copy', {bubbles: true, cancelable: true})
    document.dispatchEvent(event)
    return {prevented: event.defaultPrevented, selectable: getComputedStyle(element).userSelect}
  })
  expect(copy).toEqual({prevented: false, selectable: 'text'})
  const outsideProtected = await page.evaluate(() => {
    const range = document.createRange()
    range.selectNodeContents(document.querySelector('.v26Header')!)
    const selection = window.getSelection()!
    selection.removeAllRanges(); selection.addRange(range)
    const event = new ClipboardEvent('copy', {bubbles: true, cancelable: true})
    document.dispatchEvent(event)
    return event.defaultPrevented
  })
  expect(outsideProtected).toBe(true)
  await page.reload()
  await page.getByRole('button', {name: 'Müşteri Asistanını aç'}).click()
  await expect(page.locator('.assistantMessage')).toHaveCount(2)
  await expect(page.locator('.assistantMessage.assistant')).toContainText('<img')
})

test('Explicit approval posts only to confirmation endpoint, once, without persisting its proof', async ({page}) => {
  const state = await mockAssistant(page)
  state.chatBody = {reply: 'Analiz için onay gerekli.', language: 'tr', sources: ['get_analysis'], needs_confirmation: approval}
  const dialog = await openChat(page)
  await dialog.getByRole('button', {name: 'BTC için analiz durumu ne?', exact: true}).click()
  await expect(dialog).toContainText('7 kredi harcanacak')
  expect(state.confirmations).toHaveLength(0)
  const stored = await page.evaluate(() => sessionStorage.getItem('protrebot-assistant-chat:assistant-member'))
  expect(stored).not.toContain(approval.confirmation_token)
  await dialog.getByRole('button', {name: 'Onayla', exact: true}).evaluate(element => {
    const button = element as HTMLButtonElement
    button.click(); button.click()
  })
  await expect(dialog).toContainText('Onaylandı')
  await expect(dialog).toContainText('Final Decision: 72')
  await expect(dialog).toContainText('Veri 4 saniye önce alındı')
  await expect(dialog).toContainText('Entry/SL/TP seviyeleri için Master Trade')
  expect(state.confirmations).toHaveLength(1)
  expect(state.confirmations[0].body).toEqual({symbol: 'BTCUSDT', timeframe: '1h', confirmation_token: approval.confirmation_token, confirm: true})
  expect(state.chats).toHaveLength(1)
  await expect(dialog).not.toContainText('987654')
  expect(state.requests.filter(value => value.startsWith('POST '))).toEqual(['POST /api/assistant/chat', 'POST /api/assistant/analysis/confirm'])
})

test('Cancel and reload never approve; obsolete proof cannot execute', async ({page}) => {
  const state = await mockAssistant(page)
  state.chatBody = {reply: 'Analiz için onay gerekli.', language: 'tr', sources: ['get_analysis'], needs_confirmation: approval}
  const dialog = await openChat(page)
  await dialog.getByRole('button', {name: 'BTC için analiz durumu ne?', exact: true}).click()
  await dialog.getByRole('button', {name: 'Vazgeç', exact: true}).click()
  await expect(dialog).toContainText('Vazgeçildi')
  expect(state.confirmations).toHaveLength(0)
  await dialog.getByRole('button', {name: 'BTC için analiz durumu ne?', exact: true}).click()
  await expect(dialog.getByRole('button', {name: 'Onayla'})).toBeVisible()
  await page.reload()
  await page.getByRole('button', {name: 'Müşteri Asistanını aç'}).click()
  await expect(page.locator('.assistantMessage')).toContainText(['BTC', 'Vazgeçildi', 'BTC', 'Onay için analizi tekrar iste.'])
  await expect(page.getByRole('button', {name: 'Onayla'})).toHaveCount(0)
  expect(state.confirmations).toHaveLength(0)
})

test('Uncertain confirmation can be retried only by the user and reuses the same proof', async ({page}) => {
  const state = await mockAssistant(page)
  state.chatBody = {reply: 'Analiz için onay gerekli.', language: 'tr', sources: ['get_analysis'], needs_confirmation: approval}
  state.confirmNetwork = true
  const dialog = await openChat(page)
  await dialog.getByRole('button', {name: 'BTC için analiz durumu ne?', exact: true}).click()
  await dialog.getByRole('button', {name: 'Onayla', exact: true}).click()
  await expect(dialog.getByRole('alert')).toContainText('Onay sonucu alınamadı')
  expect(state.confirmations).toHaveLength(1)
  state.confirmNetwork = false
  await dialog.getByRole('button', {name: 'Onayla', exact: true}).click()
  await expect(dialog).toContainText('Onaylandı')
  expect(state.confirmations).toHaveLength(2)
  expect(state.confirmations[1].body).toEqual(state.confirmations[0].body)
  expect(state.chats).toHaveLength(1)
})

test('Unavailable session storage is reported while in-memory chat remains usable', async ({page}) => {
  await mockAssistant(page)
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem
    Storage.prototype.setItem = function(key, value) {
      if (key.startsWith('protrebot-assistant-chat:')) throw new DOMException('Storage unavailable', 'QuotaExceededError')
      return original.call(this, key, value)
    }
  })
  const dialog = await openChat(page)
  await expect(dialog).toContainText('Sohbet geçmişi okunurken veya kaydedilirken sorun oluştu')
  await dialog.getByRole('button', {name: 'Premium ne kadar?', exact: true}).click()
  await expect(dialog.locator('.assistantMessage.assistant')).toContainText('Yanıt düz metindir')
})

for (const failure of ['limit', 'budget', 'network', 'session'] as const) {
  test(`Assistant explains ${failure} without exposing server details`, async ({page}) => {
    const state = await mockAssistant(page)
    state.chatStatus = failure === 'limit' ? 429 : failure === 'budget' ? 503 : failure === 'session' ? 401 : 200
    state.chatBody = {reply: failure === 'limit' ? '45 saniye sonra tekrar dene.' : undefined, error_code: failure === 'budget' ? 'budget' : 'daily', detail: 'PRIVATE INTERNAL ERROR'}
    state.network = failure === 'network'
    const dialog = await openChat(page)
    await dialog.getByRole('button', {name: 'Kredim ne kadar?', exact: true}).click()
    const alert = dialog.getByRole('alert')
    await expect(alert).toContainText(failure === 'limit' ? 'Mesaj limitine' : failure === 'budget' ? 'bütçe nedeniyle' : failure === 'network' ? 'Ağ bağlantısı' : 'Oturumun sona ermiş')
    await expect(alert).not.toContainText('PRIVATE INTERNAL')
    if (failure === 'limit') await expect(alert).toContainText('45 saniye')
    if (failure === 'session') {
      await expect(alert.getByRole('link', {name: 'Giriş yap'})).toHaveAttribute('href', '/login')
      await expect(dialog.getByRole('textbox')).toBeDisabled()
      expect(await page.evaluate(() => sessionStorage.getItem('protrebot-assistant-chat:assistant-member'))).toBeNull()
    }
  })
}

test('Backend limits govern Unicode history truncation; secret input is neither sent nor saved', async ({page}) => {
  const state = await mockAssistant(page)
  state.chatBody = {reply: 'Long assistant response for history trimming', language: 'en', sources: []}
  const dialog = await openChat(page)
  const input = dialog.getByRole('textbox', {name: 'Mesajın'})
  for (const value of ['A sufficiently long first question', 'A second question', 'A third question']) {
    await input.fill(value); await dialog.getByRole('button', {name: 'Gönder', exact: true}).click()
    await expect(dialog.locator('.assistantMessage.assistant')).toHaveCount(state.chats.length)
    await expect(dialog.getByRole('button', {name: 'Premium ne kadar?', exact: true})).toBeEnabled()
  }
  expect(state.chats[2].body.history).toEqual([{role: 'user', content: 'A second que'}, {role: 'assistant', content: 'Long assista'}])
  const before = state.chats.length
  await input.fill('X'.repeat(40))
  await dialog.getByRole('button', {name: 'Gönder', exact: true}).click()
  await expect(dialog.getByRole('alert')).toContainText('Secret veya kimlik')
  expect(state.chats).toHaveLength(before)
  expect(await page.evaluate(() => sessionStorage.getItem('protrebot-assistant-chat:assistant-member'))).not.toContain('X'.repeat(40))
  await input.fill('ü'.repeat(81))
  await dialog.getByRole('button', {name: 'Gönder', exact: true}).click()
  await expect(dialog).toContainText('Mesaj en fazla 80 karakter')
  expect(state.chats).toHaveLength(before)
  state.historyLimit = 0
  await dialog.getByRole('button', {name: 'Sohbeti kapat'}).click()
  const refreshed = page.waitForResponse(response => new URL(response.url()).pathname === '/api/assistant/usage')
  await page.getByRole('button', {name: 'Müşteri Asistanını aç'}).click()
  await (await refreshed).finished()
  await input.fill('Allowed question')
  await dialog.getByRole('button', {name: 'Gönder', exact: true}).click()
  await expect.poll(() => state.chats.length).toBe(before + 1)
  expect(state.chats.at(-1)!.body.history).toEqual([])
})

test('EN labels, zero allowance fastpath and per-member history isolation', async ({page}) => {
  const state = await mockAssistant(page)
  state.remaining = 0
  state.chatBody = {reply: 'Your plan details.', language: 'en', sources: ['get_plans']}
  await page.goto('/')
  await page.evaluate(() => {document.documentElement.lang = 'en'})
  await page.getByRole('button', {name: 'Open Customer Assistant'}).click()
  const dialog = page.getByRole('dialog', {name: 'Customer Assistant'})
  await expect(dialog).toContainText('Daily messages remaining: 0/20')
  await dialog.getByRole('button', {name: 'How much is Premium?', exact: true}).click()
  await expect(dialog).toContainText('Your plan details.')
  expect(state.chats).toHaveLength(1)
  state.userId = 'different-member'
  await page.reload()
  await page.getByRole('button', {name: 'Müşteri Asistanını aç'}).click()
  await expect(page.locator('.assistantMessage')).toHaveCount(0)
})

test('Mobile composer follows the visual viewport when the keyboard reduces its height', async ({page}) => {
  await page.setViewportSize({width: 390, height: 844})
  await mockAssistant(page)
  const dialog = await openChat(page)
  await dialog.getByRole('textbox').focus()
  await page.evaluate(() => {
    Object.defineProperty(window.visualViewport, 'height', {configurable: true, get: () => 400})
    window.visualViewport!.dispatchEvent(new Event('resize'))
  })
  await expect(dialog).toHaveAttribute('data-compact-viewport', 'true')
  const box = await dialog.getByRole('textbox').boundingBox()
  expect(box!.y + box!.height).toBeLessThanOrEqual(400)
  const send = await dialog.getByRole('button', {name: 'Gönder', exact: true}).boundingBox()
  expect(send!.y + send!.height).toBeLessThanOrEqual(400)
})

for (const width of [1440, 390]) {
  test(`Proactive status is silent, unread, session-persistent and excluded from LLM history at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 844})
    const state = await mockAssistant(page)
    state.proactiveEnabled = true; state.proactiveMessage = checkInMessage
    await page.goto('/')
    await expect(page.getByRole('status', {name: 'Okunmamış durum özeti'})).toHaveText('1')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    expect(state.chats).toHaveLength(0)
    expect(state.confirmations).toHaveLength(0)
    expect(state.checkIns[0].body).toEqual({language: 'tr'})
    await page.reload()
    await expect(page.getByRole('status', {name: 'Okunmamış durum özeti'})).toHaveText('1')
    await page.getByRole('button', {name: 'Müşteri Asistanını aç'}).click()
    const dialog = page.getByRole('dialog', {name: 'Müşteri Asistanı'})
    await expect(dialog.locator('.assistantMessage')).toHaveCount(1)
    await expect(dialog).toContainText(checkInMessage.reply)
    await expect(page.locator('.assistantBadge')).toHaveCount(0)
    await page.reload()
    await page.getByRole('button', {name: 'Müşteri Asistanını aç'}).click()
    await expect(dialog.locator('.assistantMessage')).toHaveCount(1)
    await expect(page.locator('.assistantBadge')).toHaveCount(0)
    await dialog.getByRole('button', {name: 'Premium ne kadar?', exact: true}).click()
    await expect.poll(() => state.chats.length).toBe(1)
    expect(state.chats[0].body.history).toEqual([])
    expect(state.requests.filter(value => value.startsWith('POST '))).toEqual([
      'POST /api/assistant/proactive/check-in', 'POST /api/assistant/proactive/check-in',
      'POST /api/assistant/proactive/check-in', 'POST /api/assistant/chat',
    ])
  })
}

test('Opt-out is server-backed, survives reload, and does not share another member history or preference', async ({page}) => {
  const state = await mockAssistant(page)
  state.proactiveEnabled = true; state.proactiveMessage = checkInMessage
  const dialog = await openChat(page)
  await expect(dialog).toContainText(checkInMessage.reply)
  const setting = dialog.getByRole('checkbox', {name: /Durum yoklamaları/})
  await setting.click()
  await expect(setting).not.toBeChecked()
  await expect(setting).toBeEnabled()
  expect(state.preferenceSaves.map(call => call.body)).toEqual([{enabled: false}])
  const calls = state.checkIns.length
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await page.reload()
  await page.getByRole('button', {name: 'Müşteri Asistanını aç'}).click()
  await expect(setting).toBeEnabled()
  await expect(setting).not.toBeChecked()
  expect(state.checkIns).toHaveLength(calls)
  state.userId = 'other-proactive-member'; state.proactiveEnabled = true; state.proactiveMessage = null
  await page.reload()
  await page.getByRole('button', {name: 'Müşteri Asistanını aç'}).click()
  await expect(setting).toBeChecked()
  await expect(dialog.locator('.assistantMessage')).toHaveCount(0)
  await expect(page.locator('.assistantBadge')).toHaveCount(0)
  expect(state.chats).toHaveLength(0)
  expect(state.confirmations).toHaveLength(0)
})

test('Failed preference save leaves the verified setting unchanged and surfaces the failure', async ({page}) => {
  const state = await mockAssistant(page)
  state.proactiveEnabled = true; state.preferenceStatus = 503
  const dialog = await openChat(page)
  const setting = dialog.getByRole('checkbox', {name: /Durum yoklamaları/})
  await expect(setting).toBeEnabled()
  await setting.click()
  await expect(dialog.getByRole('alert')).toContainText('değişiklik doğrulanmadı')
  await expect(setting).toBeChecked()
  await expect(setting).toBeEnabled()
  state.preferenceStatus = 200
  await setting.click()
  await expect(setting).not.toBeChecked()
  await expect(dialog.getByRole('alert')).toHaveCount(0)
})

test('Check-in retry retries the failed check and does not erase its error after a successful preference read', async ({page}) => {
  const state = await mockAssistant(page)
  state.proactiveEnabled = true; state.checkInStatus = 503
  const dialog = await openChat(page)
  await expect(dialog.getByRole('alert')).toContainText('Yoklama bilgisi')
  let calls = state.checkIns.length
  await dialog.getByRole('button', {name: 'Tekrar dene', exact: true}).click()
  await expect.poll(() => state.checkIns.length).toBeGreaterThan(calls)
  await expect(dialog.getByRole('alert')).toContainText('Yoklama bilgisi')
  calls = state.checkIns.length
  state.checkInStatus = 200
  await dialog.getByRole('button', {name: 'Tekrar dene', exact: true}).click()
  await expect.poll(() => state.checkIns.length).toBeGreaterThan(calls)
  await expect(dialog.getByRole('alert')).toHaveCount(0)
})

test('EN proactive templates retain stale disclosure and use the page language without chat calls', async ({page}) => {
  const state = await mockAssistant(page)
  state.proactiveMessage = {...checkInMessage, language: 'en', stale: true,
    reply: '1 open positions. PnL: could not be verified.\nProtection could not be verified.\nData is stale or its freshness could not be verified.'}
  await page.goto('/')
  await page.evaluate(() => {document.documentElement.lang = 'en'})
  await page.getByRole('button', {name: 'Open Customer Assistant'}).click()
  const dialog = page.getByRole('dialog', {name: 'Customer Assistant'})
  const setting = dialog.getByRole('checkbox', {name: /Status check-ins/})
  await expect(setting).toBeEnabled()
  await setting.click()
  await expect(dialog).toContainText('Data is stale')
  await expect(dialog.locator('.assistantMessage')).toHaveAttribute('lang', 'en')
  expect(state.checkIns.map(call => call.body)).toEqual([{language: 'en'}])
  expect(state.chats).toHaveLength(0)
})
