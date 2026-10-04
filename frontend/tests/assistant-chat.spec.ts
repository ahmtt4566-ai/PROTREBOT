import {expect, test, type Page} from '@playwright/test'
import {join} from 'node:path'
import type {KaisReaction} from '../../kais-reactions'
import {approval, checkInMessage, mockAssistant, openChat} from './helpers/assistant-api'

test.use({baseURL: 'http://127.0.0.1:4174'})

declare global {
  interface Window { kaisReactionLog: KaisReaction[] }
}

async function recordReactions(page: Page) {
  await page.addInitScript(() => {
    window.kaisReactionLog = []
    window.addEventListener('kais:react', event => window.kaisReactionLog.push(event.detail))
  })
  await page.clock.install({time: new Date('2026-01-01T00:00:00Z')})
  await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'))
  await page.emulateMedia({reducedMotion: 'no-preference'})
}

test('Committed Analyst/Scanner/Master Trade navigation reacts without assistant requests', async ({page}) => {
  await page.setViewportSize({width: 1440, height: 844})
  await recordReactions(page)
  const state = await mockAssistant(page)
  await page.goto('/')
  await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toBeVisible()
  for (const view of ['analyst', 'scanner', 'master-trade']) {
    const before = await page.evaluate(() => window.kaisReactionLog.length)
    await page.evaluate(view => window.dispatchEvent(new CustomEvent('protrebot-navigate', {detail: view})), view)
    await expect.poll(async () => {
      await page.clock.runFor(100)
      return page.evaluate(() => window.kaisReactionLog.length)
    }).toBeGreaterThan(before)
    await expect(page.locator('.assistantLauncher .kaisEye')).toHaveAttribute('data-reaction', 'navigation')
    const detail = await page.evaluate(() => window.kaisReactionLog.at(-1))
    expect(detail?.type).toBe('navigation')
    expect(Object.keys(detail!).sort()).toEqual(['point', 'type'])
    await page.clock.runFor(650)
    await expect(page.locator('.assistantLauncher .kaisEye')).toHaveAttribute('data-look-y', '0')
  }
  expect(state.chats).toEqual([])
  expect(state.requests.filter(path => path.startsWith('POST '))).toEqual([])
})

test('Opening the real premium card emits only its geometry, without premium/trade mutations', async ({page}) => {
  await page.setViewportSize({width: 1440, height: 844})
  await recordReactions(page)
  const state = await mockAssistant(page)
  await page.goto('/master-trade?tab=canli')
  const locked = page.locator('[data-premium-locked]').filter({hasText: 'START LIVE AUTO TRADE'}).getByRole('button').first()
  await expect.poll(async () => {
    await page.clock.runFor(100)
    return locked.isVisible()
  }).toBe(true)
  await expect(locked).toBeEnabled()
  await locked.click()
  await expect(page.getByRole('dialog')).toBeVisible()
  const eye = page.locator('.assistantLauncher .kaisEye')
  await expect(eye).toHaveAttribute('data-reaction', 'premium-open')
  const details = await page.evaluate(() => window.kaisReactionLog.filter(event => event.type === 'premium-open'))
  expect(details).toHaveLength(1)
  const detail = details[0]
  if (detail.type !== 'premium-open') throw new Error('Expected premium geometry')
  expect(detail.point.x).toBeGreaterThanOrEqual(0)
  expect(detail.point.x).toBeLessThanOrEqual(1440)
  expect(detail.point.y).toBeGreaterThanOrEqual(0)
  expect(detail.point.y).toBeLessThanOrEqual(844)
  expect(Object.keys(detail).sort()).toEqual(['point', 'type'])
  await page.clock.runFor(650)
  await expect(eye).toHaveAttribute('data-reaction', 'none')
  expect(state.requests.filter(path => path.startsWith('POST '))).toEqual([])
})

test('New owned proactive check-in emits unread without extra polling or LLM calls', async ({page}) => {
  await recordReactions(page)
  const state = await mockAssistant(page)
  state.proactiveEnabled = true
  state.proactiveMessage = checkInMessage
  await page.goto('/')
  await expect(page.getByRole('status', {name: 'Okunmamış durum özeti'})).toHaveText('1')
  await expect.poll(() => page.evaluate(() => window.kaisReactionLog)).toEqual([{type: 'unread'}])
  const eye = page.locator('.assistantLauncher .kaisEye')
  await expect(eye).toHaveClass(/kaisEyeReactUnread/)
  await page.clock.runFor(1200)
  await expect(eye).toHaveAttribute('data-reaction', 'none')
  await expect(eye).toHaveAttribute('data-unread', 'true')
  expect(state.checkIns).toHaveLength(1)
  expect(state.chats).toEqual([])
  expect(state.confirmations).toEqual([])
  expect(state.requests.filter(path => path.startsWith('POST '))).toEqual(['POST /api/assistant/proactive/check-in'])
})

for (const width of [1440, 390]) {
  test(`Native assistant dialog opens, closes, returns focus and fits ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 844})
    await mockAssistant(page)
    const dialog = await openChat(page)
    await expect(dialog).toBeVisible()
    await expect(dialog.locator('.assistantHeader > .kaisEye')).toHaveCSS('width', '36px')
    await expect(dialog.locator('.assistantHeader > .kaisEye')).toHaveCSS('height', '36px')
    expect(await dialog.evaluate(element => element.matches(':modal'))).toBe(true)
    await expect(dialog).toContainText('Günlük mesaj hakkı: 19/20')
    await expect.poll(() => dialog.evaluate(element => element.getAnimations().every(animation => animation.playState === 'finished'))).toBe(true)
    const box = await dialog.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.y).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(width)
    expect(box!.y + box!.height).toBeLessThanOrEqual(844)
    if (width < 768) {
      expect(box!.width).toBeCloseTo(width, 0)
      expect(box!.height).toBeCloseTo(844 * .75, 0)
      expect(box!.y).toBeCloseTo(844 * .25, 0)
      expect(box!.y + box!.height).toBeCloseTo(844, 0)
    } else {
      const anchor = await page.getByRole('button', {name: 'Kais AI', exact: true}).boundingBox()
      expect(anchor).not.toBeNull()
      expect(box!.width).toBeCloseTo(360, 0)
      expect(box!.height).toBeLessThanOrEqual(520)
      expect(box!.x + box!.width).toBeCloseTo(anchor!.x + anchor!.width, 0)
      expect(box!.y).toBeGreaterThanOrEqual(anchor!.y + anchor!.height)
      const arrow = await dialog.evaluate(element => Number.parseFloat(getComputedStyle(element).getPropertyValue('--assistant-arrow-left')))
      expect(box!.x + arrow).toBeCloseTo(anchor!.x + anchor!.width / 2, 0)
    }
    await page.screenshot({path: join(testInfo.outputDir, `assistant-${width}.png`)})
    await page.keyboard.press('Escape')
    await expect(dialog).not.toBeVisible()
    await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toBeFocused()
    await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
    await dialog.getByRole('button', {name: 'Kais AI sohbetini kapat'}).click()
    await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toBeFocused()
  })
}

test('Suggested question, typing state, plain text, copy exception and persistent history', async ({page}) => {
  const state = await mockAssistant(page)
  let release: () => void = () => {}
  state.hold = new Promise<void>(resolve => {release = resolve})
  state.chatBody = {reply: '**Plan**\n<img src=x onerror="window.assistantInjected=true">', language: 'tr', sources: ['get_plans']}
  const dialog = await openChat(page)
  await dialog.getByRole('button', {name: 'Premium ne kadar?', exact: true}).click()
  await expect(dialog).toContainText('Düşünüyor...')
  await expect(dialog.getByRole('button', {name: 'Kredim ne kadar?', exact: true})).toBeDisabled()
  release()
  await expect(dialog.locator('.assistantTyping')).toHaveCount(0)
  await expect(dialog.locator('.assistantMessage.assistant strong')).toHaveText('Plan')
  await expect(dialog.locator('.assistantMessage.assistant .kaisEye')).toHaveCSS('width', '24px')
  await expect(dialog.locator('.assistantMessage.assistant .kaisEye')).toHaveCSS('height', '24px')
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
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
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
  const stored = await page.evaluate(() => localStorage.getItem('kais-chat:v1:assistant-member'))
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
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
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

test('Unavailable chat persistence only warns while in-memory chat remains usable', async ({page}) => {
  const warnings: string[] = []
  page.on('console', message => {if (message.type() === 'warning') warnings.push(message.text())})
  await mockAssistant(page)
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem
    Storage.prototype.setItem = function(key, value) {
      if (key.startsWith('kais-chat:')) throw new DOMException('Storage unavailable', 'QuotaExceededError')
      return original.call(this, key, value)
    }
  })
  const dialog = await openChat(page)
  await dialog.getByRole('button', {name: 'Premium ne kadar?', exact: true}).click()
  await expect(dialog.locator('.assistantMessage.assistant')).toContainText('Yanıt düz metindir')
  await expect(dialog).not.toContainText('Sohbet geçmişi okunurken veya kaydedilirken sorun oluştu')
  expect(warnings.some(warning => warning.includes('Kais AI chat persistence disabled'))).toBe(true)
})

for (const failure of ['limit', 'budget', 'network', 'session', 'unavailable'] as const) {
  test(`Assistant explains ${failure} without exposing server details`, async ({page}) => {
    const state = await mockAssistant(page)
    state.chatStatus = failure === 'limit' ? 429 : failure === 'budget' || failure === 'unavailable' ? 503 : failure === 'session' ? 401 : 200
    state.chatBody = {reply: failure === 'limit' ? '45 saniye sonra tekrar dene.' : undefined, error_code: failure === 'budget' ? 'budget' : 'daily', detail: 'PRIVATE INTERNAL ERROR'}
    state.network = failure === 'network'
    const dialog = await openChat(page)
    await dialog.getByRole('button', {name: 'Kredim ne kadar?', exact: true}).click()
    const alert = dialog.getByRole('alert')
    await expect(alert).toContainText(failure === 'limit' ? 'Mesaj limitine' : failure === 'budget' ? 'bütçe nedeniyle' : failure === 'network' ? 'Ağ bağlantısı'
      : failure === 'session' ? 'Oturumun sona ermiş' : 'Asistan şu an kullanılamıyor')
    await expect(dialog.locator('.assistantHeader > .kaisEye')).toHaveAttribute('data-state', failure === 'session' || failure === 'unavailable' ? 'off' : 'error')
    await expect(alert).not.toContainText('PRIVATE INTERNAL')
    if (failure === 'limit') await expect(alert).toContainText('45 saniye')
    if (failure === 'session') {
      await expect(alert.getByRole('link', {name: 'Giriş yap'})).toHaveAttribute('href', '/login')
      await expect(dialog.getByRole('textbox')).toBeDisabled()
      expect(await page.evaluate(() => localStorage.getItem('kais-chat:v1:assistant-member'))).toBeNull()
    }
  })
}

test('Backend limits govern Unicode history truncation; secret input is neither sent nor saved', async ({page}) => {
  const state = await mockAssistant(page)
  state.chatBody = {reply: 'Long assistant response for history trimming', language: 'en', sources: []}
  const dialog = await openChat(page)
  const input = dialog.getByRole('textbox', {name: 'Kais AI mesajın'})
  for (const value of ['A sufficiently long first question', 'A second question', 'A third question']) {
    await input.fill(value); await dialog.getByRole('button', {name: 'Kais AI mesajını gönder', exact: true}).click()
    await expect(dialog.locator('.assistantMessage.assistant')).toHaveCount(state.chats.length)
    await expect(dialog.getByRole('button', {name: 'Premium ne kadar?', exact: true})).toBeEnabled()
  }
  expect(state.chats[2].body.history).toEqual([{role: 'user', content: 'A second que'}, {role: 'assistant', content: 'Long assista'}])
  const before = state.chats.length
  await input.fill('X'.repeat(40))
  await dialog.getByRole('button', {name: 'Kais AI mesajını gönder', exact: true}).click()
  await expect(dialog.getByRole('alert')).toContainText('Secret veya kimlik')
  expect(state.chats).toHaveLength(before)
  expect(await page.evaluate(() => localStorage.getItem('kais-chat:v1:assistant-member'))).not.toContain('X'.repeat(40))
  await input.fill('ü'.repeat(81))
  await dialog.getByRole('button', {name: 'Kais AI mesajını gönder', exact: true}).click()
  await expect(dialog).toContainText('Mesaj en fazla 80 karakter')
  expect(state.chats).toHaveLength(before)
  state.historyLimit = 0
  await dialog.getByRole('button', {name: 'Kais AI sohbetini kapat'}).click()
  const refreshed = page.waitForResponse(response => new URL(response.url()).pathname === '/api/assistant/usage')
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  await (await refreshed).finished()
  await input.fill('Allowed question')
  await dialog.getByRole('button', {name: 'Kais AI mesajını gönder', exact: true}).click()
  await expect.poll(() => state.chats.length).toBe(before + 1)
  expect(state.chats.at(-1)!.body.history).toEqual([])
})

test('EN labels, zero allowance fastpath and per-member history isolation', async ({page}) => {
  const state = await mockAssistant(page)
  state.remaining = 0
  state.chatBody = {reply: 'Your plan details.', language: 'en', sources: ['get_plans']}
  await page.goto('/')
  await page.evaluate(() => {document.documentElement.lang = 'en'})
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: 'Kais AI'})
  await expect(dialog).toContainText('Daily messages remaining: 0/20')
  await expect(dialog.locator('.assistantHeader > .kaisEye')).toHaveAttribute('data-state', 'error')
  await dialog.getByRole('button', {name: 'How much is Premium?', exact: true}).click()
  await expect(dialog).toContainText('Your plan details.')
  expect(state.chats).toHaveLength(1)
  state.userId = 'different-member'
  await page.reload()
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
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
  const send = await dialog.getByRole('button', {name: 'Kais AI mesajını gönder', exact: true}).boundingBox()
  expect(send!.y + send!.height).toBeLessThanOrEqual(400)
})

for (const width of [1440, 390]) {
  test(`Proactive status is silent, unread, persistent and excluded from LLM history at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 844})
    const state = await mockAssistant(page)
    state.proactiveEnabled = true; state.proactiveMessage = checkInMessage
    await page.goto('/')
    await expect(page.getByRole('status', {name: 'Okunmamış durum özeti'})).toHaveText('1')
    await expect(page.locator('.assistantLauncher .kaisEye')).toHaveAttribute('data-unread', 'true')
    await expect(page.locator('.assistantLauncher .kaisEyeRingGlow')).toHaveCSS('animation-name', 'kais-eye-unread')
    await expect(page.getByRole('dialog')).toHaveCount(0)
    expect(state.chats).toHaveLength(0)
    expect(state.confirmations).toHaveLength(0)
    expect(state.checkIns[0].body).toEqual({language: 'tr'})
    await page.reload()
    await expect(page.getByRole('status', {name: 'Okunmamış durum özeti'})).toHaveText('1')
    await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
    const dialog = page.getByRole('dialog', {name: 'Kais AI'})
    await expect(dialog.locator('.assistantMessage')).toHaveCount(1)
    await expect(dialog).toContainText(checkInMessage.reply)
    await expect(page.locator('.assistantBadge')).toHaveCount(0)
    await expect(page.locator('.assistantLauncher .kaisEye')).toHaveAttribute('data-unread', 'false')
    await expect(page.locator('.assistantLauncher .kaisEyeRingGlow')).toHaveCSS('animation-name', 'none')
    await page.reload()
    await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
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
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  await expect(setting).toBeEnabled()
  await expect(setting).not.toBeChecked()
  expect(state.checkIns).toHaveLength(calls)
  state.userId = 'other-proactive-member'; state.proactiveEnabled = true; state.proactiveMessage = null
  await page.reload()
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
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
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: 'Kais AI'})
  const setting = dialog.getByRole('checkbox', {name: /Status check-ins/})
  await expect(setting).toBeEnabled()
  await setting.click()
  await expect(dialog).toContainText('Data is stale')
  await expect(dialog.locator('.assistantMessage')).toHaveAttribute('lang', 'en')
  expect(state.checkIns.map(call => call.body)).toEqual([{language: 'en'}])
  expect(state.chats).toHaveLength(0)
})

for (const width of [320, 390, 768, 1024, 1440]) {
  test(`Kais AI stays in the header without covering controls at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 844})
    const state = await mockAssistant(page)
    state.proactiveEnabled = true; state.proactiveMessage = checkInMessage
    await page.goto('/')
    const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
    await expect(launcher).toBeVisible()
    await expect(launcher.locator('.kaisEye')).toHaveCSS('width', '67.1875px')
    await expect(launcher.locator('.kaisEye')).toHaveCSS('height', '67.1875px')
    await expect(launcher.locator('.assistantBadge')).toHaveText('1')
    for (const view of ['dashboard', 'master-trade', 'live', 'trading', 'scanner']) {
      await page.evaluate(target => window.dispatchEvent(new CustomEvent('protrebot-navigate', {detail: target})), view)
      await expect(launcher).toBeVisible()
      await expect(page.locator('.assistantDialog')).toHaveCount(0)
      const geometry = await launcher.evaluate(element => {
        const box = element.getBoundingClientRect()
        const header = element.closest('header')!.getBoundingClientRect()
        const controls = Array.from(document.querySelectorAll<HTMLElement>('button, input[type="checkbox"], [role="button"], a'))
          .filter(button => button !== element && button.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) && !button.closest('.v26NotificationPanel'))
          .map(button => {
            const rect = button.getBoundingClientRect()
            return {left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
              name: button.getAttribute('aria-label') ?? button.textContent?.trim().slice(0, 80) ?? button.className}
          })
        return {left: box.left, right: box.right, top: box.top, bottom: box.bottom, width: box.width, height: box.height,
          headerTop: header.top, headerBottom: header.bottom, position: getComputedStyle(element).position,
          inHeader: Boolean(element.closest('.v26Header, .masterTradeTerminalHeader')), controls}
      })
      expect(geometry.width).toBeGreaterThanOrEqual(44)
      expect(geometry.height).toBeGreaterThanOrEqual(44)
      expect(geometry.left).toBeGreaterThanOrEqual(0)
      expect(geometry.right).toBeLessThanOrEqual(width)
      expect(geometry.top).toBeGreaterThanOrEqual(geometry.headerTop)
      expect(geometry.bottom).toBeLessThanOrEqual(geometry.headerBottom)
      expect(geometry.position).toBe('relative')
      expect(geometry.inHeader).toBe(true)
      await expect(launcher).toHaveCSS('border-radius', '50%')
      await expect(launcher).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
      await expect(launcher).toHaveCSS('background-image', 'none')
      for (const control of geometry.controls) {
        expect(geometry.right <= control.left || geometry.left >= control.right ||
          geometry.bottom <= control.top || geometry.top >= control.bottom, `${view}: ${control.name}`).toBe(true)
      }
    }
    expect(state.chats).toHaveLength(0)
    expect(state.confirmations).toHaveLength(0)
  })
}

for (const width of [320, 390]) {
  test(`Open mobile menu controls are not covered by Kais AI at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 844})
    await mockAssistant(page)
    await page.goto('/')
    await page.getByRole('button', {name: 'Menü', exact: true}).click()
    const menu = page.getByRole('dialog', {name: 'Çalışma alanları', exact: true})
    await expect(menu).toBeVisible()
    const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
    const box = await launcher.boundingBox()
    expect(box).not.toBeNull()
    const controls = await menu.locator('button, a').evaluateAll(elements => elements
      .filter(element => element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}))
      .map(element => {
        const rect = element.getBoundingClientRect()
        return {left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom}
      }))
    expect(controls.length).toBeGreaterThan(0)
    for (const control of controls) {
      expect(box!.x + box!.width <= control.left || box!.x >= control.right ||
        box!.y + box!.height <= control.top || box!.y >= control.bottom).toBe(true)
    }
    await menu.getByRole('button', {name: 'Menüyü kapat', exact: true}).click()
    await expect(menu).not.toBeVisible()
  })
}

for (const width of [1440, 390]) {
  test(`Final Kais AI closed, open and private screenshots at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 844})
    await mockAssistant(page)
    await page.emulateMedia({reducedMotion: 'reduce'})
    await page.addInitScript(() => sessionStorage.setItem('protrebot-kais-launcher-seen', '1'))
    await page.goto('/')
    const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
    await expect(launcher).toBeVisible()
    await page.screenshot({path: testInfo.outputPath(`kais-final-closed-${width}.png`)})
    await launcher.click()
    const dialog = page.getByRole('dialog', {name: 'Kais AI', exact: true})
    const composer = dialog.getByRole('textbox', {name: 'Kais AI mesajın', exact: true})
    await expect(composer).toBeEnabled()
    await page.screenshot({path: testInfo.outputPath(`kais-final-open-${width}.png`)})
    await composer.fill('secret=' + 'A1'.repeat(20))
    await expect(dialog.locator('.assistantHeader > .kaisEye')).toHaveAttribute('data-state', 'private')
    await expect(dialog.locator('#assistant-secret-warning')).toBeVisible()
    await page.screenshot({path: testInfo.outputPath(`kais-final-private-${width}.png`)})
  })
}

test('Eye tracks coordinates, respects private focus and never reads or sends content', async ({page}) => {
  const state = await mockAssistant(page)
  await page.goto('/')
  const eye = page.locator('.assistantLauncher .kaisEye')
  const gaze = eye.locator('[data-layer="gaze"]')
  await expect(eye).toBeVisible()
  await expect(eye).toHaveAttribute('aria-label', 'Kais AI: hazır')
  await page.mouse.move(10, 600)
  await expect.poll(() => eye.getAttribute('data-look-x')).not.toBe('0')
  await page.evaluate(() => {
    const wrapper = document.createElement('div')
    wrapper.dataset.private = 'true'
    const input = document.createElement('input')
    input.setAttribute('aria-label', 'Private test field')
    input.value = 'PRIVATE CONTENT MUST NOT BE READ'
    Object.defineProperty(input, 'value', {get() {throw new Error('Eye read a private value')}})
    wrapper.append(input)
    document.body.append(wrapper)
    input.focus()
  })
  await expect(eye).toHaveAttribute('data-state', 'private')
  await page.mouse.move(800, 400)
  await expect(eye.locator('[data-layer="eye-interior"]')).toHaveCSS('opacity', '0')
  await expect(gaze).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 0, 0)')
  await page.getByRole('button', {name: 'Kais AI', exact: true}).focus()
  await expect(eye).toHaveAttribute('data-state', 'idle')
  await page.dispatchEvent('body', 'pointerdown', {clientX: 10, clientY: 500, pointerType: 'touch'})
  await expect.poll(() => eye.getAttribute('data-look-x')).not.toBe('0')
  await page.dispatchEvent('body', 'pointerup', {pointerType: 'touch'})
  await expect(eye).toHaveAttribute('data-look-x', '0')
  expect(state.chats).toHaveLength(0)
  expect(state.confirmations).toHaveLength(0)
  const requests = state.requests.filter(path => path.includes('/assistant/'))
  expect(requests.length).toBeGreaterThan(0)
  expect(requests.every(path => path === 'GET /api/assistant/proactive/preferences')).toBe(true)
  expect(await page.evaluate(() => localStorage.getItem('kais-chat:v1:assistant-member') ?? '')).not.toContain('PRIVATE CONTENT')
})

for (const width of [1440, 390]) {
  test(`Active Connection Center credentials close the eye; market search remains open at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 844})
    const state = await mockAssistant(page)
    await page.goto('/')
    await page.getByRole('button', {name: /AYARLAR/}).click()
    const eye = page.locator('.assistantLauncher .kaisEye')
    const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
    for (const name of ['Demo API Key', 'Demo Secret Key', 'Live API Key', 'Live Secret Key']) {
      const field = page.getByLabel(name, {exact: true})
      await expect(field).toHaveAttribute('data-private', 'true')
      await field.focus()
      await expect(eye).toHaveAttribute('data-state', 'private')
      await expect(eye).toHaveAttribute('aria-label', 'Kais AI: gizlilik modu')
      await page.mouse.move(0, 0)
      await launcher.focus()
      await expect(eye).toHaveAttribute('data-state', 'idle')
    }
    await page.goto('/master-trade?tab=analiz')
    const search = page.getByRole('textbox', {name: 'Search markets', exact: true})
    await search.fill('BTC')
    await expect(search).not.toHaveAttribute('data-private', 'true')
    await expect(eye).toHaveAttribute('data-state', 'idle')
    expect(state.requests.filter(path => path.startsWith('POST '))).toEqual([])
  })
}

for (const language of ['tr', 'en'] as const) {
  test(`Draft secret warning follows the backend pattern and configured threshold without sending data (${language})`, async ({page}) => {
    const state = await mockAssistant(page)
    state.secretMinimum = 12
    await page.goto('/')
    await page.evaluate(value => {document.documentElement.lang = value}, language)
    await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
    const dialog = page.getByRole('dialog', {name: 'Kais AI'})
    const input = dialog.locator('#assistant-input')
    const eye = dialog.locator('.assistantHeader > .kaisEye')
    const warning = dialog.locator('#assistant-secret-warning')
    await expect(input).toBeEnabled()
    const cases: [string, boolean][] = [
      ['Sen kimsin?', false], ['Who are you?', false], ['secretary', false], ['mysecret', false],
      ['password: ', false], ['api_key', false], ['A'.repeat(11), false], ['A'.repeat(12), true],
      ['secret', true], ['SECRET_key', true], ['secret key', true], ['secret-key', true],
      ['sk-test-key', true], ['sk-ant-test', true], ['Bearer abc', true],
      ['password=abc', true], ['parola:abc', true], ['api_key=abc', true], ['token:abc', true],
    ]
    for (const [value, privateDraft] of cases) {
      await input.fill(value)
      if (privateDraft) {
        await expect(input).toHaveAttribute('data-private', 'true')
        await expect(warning).toHaveText(language === 'tr' ? "API Secret'ı buraya yazma." : 'Do not enter an API Secret here.')
        await expect(warning).toHaveAttribute('role', 'status')
        await expect(input).toHaveAttribute('aria-describedby', 'assistant-secret-warning assistant-length')
        await expect(eye).toHaveAttribute('data-state', 'private')
        await expect(eye).toHaveAttribute('aria-label', language === 'tr' ? 'Kais AI: gizlilik modu' : 'Kais AI: private mode')
        await expect(dialog.getByRole('button', {name: language === 'tr' ? 'Kais AI mesajını gönder' : 'Send Kais AI message', exact: true})).toBeEnabled()
      } else {
        await expect(input).not.toHaveAttribute('data-private', 'true')
        await expect(warning).toHaveCount(0)
        await expect(eye).toHaveAttribute('data-state', 'idle')
      }
    }
    await input.fill('Platform yardımı')
    await expect(warning).toHaveCount(0)
    await expect(eye).toHaveAttribute('data-state', 'idle')
    expect(state.chats).toEqual([])
    expect(state.confirmations).toEqual([])
    expect(state.requests.filter(path => path.startsWith('POST '))).toEqual([])
    expect(await page.evaluate(() => localStorage.getItem('kais-chat:v1:assistant-member') ?? '')).not.toContain('abc')
  })
}

test('Reduced motion and hidden tabs pause eye and typing animations', async ({page}) => {
  const state = await mockAssistant(page)
  let release: () => void = () => {}
  state.hold = new Promise<void>(resolve => {release = resolve})
  const dialog = await openChat(page)
  await dialog.getByRole('button', {name: 'Premium ne kadar?', exact: true}).click()
  await expect(dialog.locator('.assistantTyping')).toBeVisible()
  const eye = dialog.locator('.kaisEye')
  const lid = eye.locator('.kaisEyeLids')
  const gaze = eye.locator('[data-layer="gaze"]')
  await expect(eye.locator('.kaisEyeRotor')).toHaveCSS('animation-name', 'kais-eye-spin')
  await page.emulateMedia({reducedMotion: 'reduce'})
  await expect(dialog).toHaveAttribute('data-motion-paused', 'true')
  await expect(lid).toHaveCSS('animation-name', 'none')
  await expect(dialog.locator('.assistantTypingDots i').first()).toHaveCSS('animation-name', 'none')
  await page.mouse.move(10, 700)
  await expect(gaze).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 0, 0)')
  await page.emulateMedia({reducedMotion: 'no-preference'})
  await expect(dialog).toHaveAttribute('data-motion-paused', 'false')
  await expect(lid).toHaveCSS('animation-name', 'none')
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => true})
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(dialog).toHaveAttribute('data-motion-paused', 'true')
  await expect(lid).toHaveCSS('animation-name', 'none')
  await expect(dialog.locator('.assistantTypingDots i').first()).toHaveCSS('animation-name', 'none')
  await page.mouse.move(20, 600)
  await expect(gaze).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 0, 0)')
  await page.evaluate(() => {
    Reflect.deleteProperty(document, 'hidden')
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(dialog).toHaveAttribute('data-motion-paused', 'false')
  release()
  await expect(dialog.locator('.assistantTyping')).toHaveCount(0)
})

test('Keyboard opens Kais AI and navigation closes the panel without trade mutations', async ({page}) => {
  const state = await mockAssistant(page)
  await page.goto('/')
  const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
  await launcher.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('dialog', {name: 'Kais AI'})).toBeVisible()
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('protrebot-navigate', {detail: 'scanner'})))
  await expect(page.locator('.assistantDialog')).toHaveCount(0)
  await expect(launcher).toBeFocused()
  expect(state.confirmations).toHaveLength(0)
  expect(state.requests.filter(path => path.startsWith('POST '))).toEqual([])
})

test('Master Trade header transfer preserves the session, pending approval and keyboard focus', async ({page}) => {
  await page.setViewportSize({width: 1440, height: 844})
  const state = await mockAssistant(page)
  state.chatBody = {reply: 'Analiz için onay gerekiyor.', language: 'tr', needs_confirmation: approval}
  await page.goto('/master-trade')
  const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
  await expect(page.locator('.assistantMasterSlot').getByRole('button', {name: 'Kais AI', exact: true})).toBeVisible()
  await launcher.click()
  const dialog = page.getByRole('dialog', {name: 'Kais AI'})
  await dialog.getByRole('button', {name: 'BTC için analiz durumu ne?', exact: true}).click()
  await expect(dialog.getByRole('button', {name: 'Onayla', exact: true})).toBeEnabled()
  const preferenceReads = state.requests.filter(path => path === 'GET /api/assistant/proactive/preferences').length
  await page.setViewportSize({width: 390, height: 844})
  await expect(page.locator('.v26HeaderActions .assistantLauncher')).toHaveCount(1)
  await expect(dialog.getByRole('button', {name: 'Onayla', exact: true})).toBeEnabled()
  await page.keyboard.press('Escape')
  await expect(launcher).toBeFocused()
  await launcher.click()
  await dialog.getByRole('button', {name: 'Onayla', exact: true}).click()
  await expect(dialog).toContainText('Onaylandı')
  expect(state.confirmations).toHaveLength(1)
  expect(state.confirmations[0].body.confirmation_token).toBe(approval.confirmation_token)
  expect(state.chats).toHaveLength(1)
  expect(state.requests.filter(path => path === 'GET /api/assistant/proactive/preferences')).toHaveLength(preferenceReads)
})

for (const language of ['tr', 'en'] as const) {
  test(`Kais AI branding, welcome and identity reply in ${language}`, async ({page}) => {
    const state = await mockAssistant(page)
    const reply = language === 'tr'
      ? 'Ben Kais AI, bu platformun yapay zeka asistanıyım. Plan, kredi, API bağlantısı ve platform kullanımı hakkında yardımcı olurum; işlem yapmam.'
      : "I am Kais AI, this platform's AI assistant. I help with plans, credits, API connections and platform usage; I do not trade."
    state.chatBody = {reply, language, sources: []}
    await page.goto('/')
    await page.evaluate(value => {document.documentElement.lang = value}, language)
    const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
    await expect(launcher).toHaveAttribute('aria-label', 'Kais AI')
    await launcher.click()
    const dialog = page.getByRole('dialog', {name: 'Kais AI'})
    await expect(dialog.getByRole('heading', {name: 'Kais AI', exact: true})).toBeVisible()
    await expect(dialog.locator('.assistantEmpty')).toContainText(language === 'tr' ? 'Merhaba, ben Kais AI.' : 'Hi, I am Kais AI.')
    await expect(dialog.locator('.assistantEmpty')).toContainText(language === 'tr' ? 'API bağlantısı' : 'API connections')
    await expect(dialog.locator('.assistantEmpty')).toContainText(language === 'tr' ? 'İşlem yapmam.' : 'I do not trade.')
    const input = dialog.getByRole('textbox', {name: language === 'tr' ? 'Kais AI mesajın' : 'Your Kais AI message'})
    await expect(input).toHaveAttribute('placeholder', 'Kais AI')
    await expect(page.locator('body')).not.toContainText(/Müşteri Asistanı|Customer Assistant/i)
    const question = language === 'tr' ? 'Sen kimsin?' : 'Who are you?'
    await input.fill(question)
    await dialog.getByRole('button', {name: language === 'tr' ? 'Kais AI mesajını gönder' : 'Send Kais AI message', exact: true}).click()
    await expect(dialog.locator('.assistantMessage.assistant .assistantText')).toHaveText(reply)
    expect(state.chats).toHaveLength(1)
    expect(state.chats[0].body.message).toBe(question)
    expect(state.confirmations).toHaveLength(0)
  })
}

test('Header launcher shows its intro for exactly five seconds once per tab session', async ({page}) => {
  await page.clock.install({time: new Date('2026-01-01T00:00:00Z')})
  await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'))
  await mockAssistant(page)
  await page.goto('/')
  const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
  const intro = launcher.locator('.assistantIntro')
  await expect(intro).toBeVisible()
  await expect(intro).toHaveAttribute('aria-hidden', 'true')
  expect(await page.evaluate(() => sessionStorage.getItem('protrebot-kais-launcher-seen'))).toBe('1')
  await page.clock.runFor(4999)
  await expect(intro).toBeVisible()
  await page.clock.runFor(1)
  await expect(intro).toHaveCount(0)
  await expect(launcher.locator(':scope > span:not(.assistantBadge)')).toHaveCount(0)
  await page.reload()
  await expect(launcher).toBeVisible()
  await expect(intro).toHaveCount(0)
})

test('Composer grows and shrinks without manual resizing, counts characters, sends and clears plain messages', async ({page}) => {
  const state = await mockAssistant(page)
  const dialog = await openChat(page)
  const input = dialog.getByRole('textbox', {name: 'Kais AI mesajın'})
  await expect.poll(() => dialog.evaluate(element => element.getAnimations().every(animation => animation.playState === 'finished'))).toBe(true)
  const initial = (await input.boundingBox())!.height
  const multiline = 'First\nSecond\nThird\nFourth\nFifth'
  await input.fill(multiline)
  await expect.poll(async () => (await input.boundingBox())!.height).toBeGreaterThan(initial)
  expect((await input.boundingBox())!.height).toBeLessThanOrEqual(120)
  await expect(input).toHaveCSS('resize', 'none')
  await expect(dialog.locator('#assistant-length')).toHaveText(`${Array.from(multiline).length}/80`)
  expect(state.chats).toHaveLength(0)
  await dialog.getByRole('button', {name: 'Kais AI mesajını gönder', exact: true}).click()
  await expect(dialog.locator('.assistantMessage')).toHaveCount(2)
  await expect(input).toHaveValue('')
  await expect.poll(async () => (await input.boundingBox())!.height).toBe(initial)
  expect(state.chats).toHaveLength(1)
  expect(state.chats[0].body.message).toBe(multiline)
  await dialog.getByRole('button', {name: 'Kais AI sohbetini temizle', exact: true}).click()
  await expect(dialog.locator('.assistantMessage')).toHaveCount(0)
  expect(state.confirmations).toHaveLength(0)
})

for (const width of [1440, 390]) {
  for (const theme of ['dark', 'light'] as const) {
    test(`Modern header launcher and glass panel screenshots, chip wrapping and contrast at ${width}px (${theme})`, async ({page}, testInfo) => {
      await page.setViewportSize({width, height: 844})
      await page.emulateMedia({reducedMotion: 'reduce'})
      const state = await mockAssistant(page)
      await page.addInitScript(() => sessionStorage.setItem('protrebot-kais-launcher-seen', '1'))
      state.chatBody = {reply: 'Plan, kredi ve platform kullanımı hakkında yardımcı olurum.', language: 'tr', needs_confirmation: approval}
      await page.goto('/master-trade?tab=canli')
      await page.evaluate(value => {document.documentElement.dataset.theme = value}, theme)
      const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
      await expect(launcher).toBeVisible()
      await expect(launcher.locator('.kaisEye')).toHaveCSS('width', '56px')
      await page.screenshot({path: join(testInfo.outputDir, `kais-header-closed-${theme}-${width}.png`)})
      await launcher.click()
      const dialog = page.getByRole('dialog', {name: 'Kais AI'})
      await expect(dialog.locator('.assistantState')).toHaveText('Hazır')
      await dialog.getByRole('textbox').fill('Platform rehberi')
      await dialog.getByRole('button', {name: 'Kais AI mesajını gönder', exact: true}).click()
      await expect(dialog.locator('.assistantMessage.assistant')).toBeVisible()
      await expect(dialog.getByRole('button', {name: 'Onayla', exact: true})).toBeEnabled()
      const layout = await dialog.evaluate(element => {
        const messages = element.querySelector('.assistantMessages')!
        const user = element.querySelector('.assistantMessage.user')!.getBoundingClientRect()
        const avatar = element.querySelector('.assistantMessage.assistant > .kaisEye')!.getBoundingClientRect()
        const bubble = element.querySelector('.assistantMessage.assistant .assistantBubble')!.getBoundingClientRect()
        const chips = element.querySelector('.assistantSuggestions')!
        const colors = getComputedStyle(element)
        const values = (color: string) => {
          const parts = color.match(/[\d.]+/g)?.map(Number)
          if (!parts || parts.length < 3) throw new Error(`Unsupported computed color: ${color}`)
          return parts
        }
        const background = values(colors.backgroundColor)
        const behind = document.documentElement.dataset.theme === 'light' ? 0 : 255
        const base = background.slice(0, 3).map(value => value * (background[3] ?? 1) + behind * (1 - (background[3] ?? 1)))
        const luminance = (rgb: number[]) => rgb.map(value => {
          const channel = value / 255
          return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4
        }).reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0)
        const ratio = (color: string) => {
          const foreground = luminance(values(color).slice(0, 3))
          const surface = luminance(base)
          return (Math.max(foreground, surface) + .05) / (Math.min(foreground, surface) + .05)
        }
        return {
          avatarWidth: avatar.width, avatarRight: avatar.right, assistantLeft: bubble.left, userRight: user.right,
          messageRight: messages.getBoundingClientRect().right, chipWrap: getComputedStyle(chips).flexWrap,
          chipOverflow: chips.scrollWidth > chips.clientWidth, horizontalOverflow: element.scrollWidth > element.clientWidth,
          textContrast: ratio(colors.color), mutedContrast: ratio(getComputedStyle(element.querySelector('.assistantState')!).color),
          backgroundImage: colors.backgroundImage, blur: colors.backdropFilter,
        }
      })
      expect(layout.avatarWidth).toBe(24)
      expect(layout.avatarRight).toBeLessThanOrEqual(layout.assistantLeft)
      expect(layout.userRight).toBeLessThanOrEqual(layout.messageRight)
      expect(layout.chipWrap).toBe('wrap')
      expect(layout.chipOverflow).toBe(false)
      expect(layout.horizontalOverflow).toBe(false)
      expect(layout.textContrast).toBeGreaterThanOrEqual(4.5)
      expect(layout.mutedContrast).toBeGreaterThanOrEqual(4.5)
      expect(layout.backgroundImage).toBe('none')
      expect(layout.blur).toBe('blur(20px)')
      await page.screenshot({path: join(testInfo.outputDir, `kais-panel-open-${theme}-${width}.png`)})
      expect(state.chats).toHaveLength(1)
      expect(state.confirmations).toHaveLength(0)
      await page.keyboard.press('Escape')
      await expect(launcher).toBeFocused()
    })
  }
}
