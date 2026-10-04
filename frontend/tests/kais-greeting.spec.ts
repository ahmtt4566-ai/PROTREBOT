import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

test.use({baseURL: 'http://127.0.0.1:4174'})

const TEXT = 'Merhaba, ben Kais AI. Bugün sana nasıl yardımcı olabilirim?'
const OPEN_LABEL = 'Karşılama mesajı, sohbeti aç'
const MEMBER = 'greeting-test-member'
const KEY = `protrebot-kais-greeting:${MEMBER}`
const bubble = (page: Page) => page.locator('[data-kais-greeting]')

declare global {
  interface Window {
    greetingProbe: () => {timers: number; listeners: number}
  }
}

async function installClock(page: Page) {
  await page.clock.install({time: new Date('2026-10-03T12:00:00Z')})
  await page.clock.pauseAt(new Date('2026-10-03T12:00:01Z'))
}

async function prepare(page: Page, authenticated = true, reducedMotion = true) {
  await installClock(page)
  await page.emulateMedia({reducedMotion: reducedMotion ? 'reduce' : 'no-preference'})
  await page.addInitScript(authenticated => {
    if (authenticated) sessionStorage.setItem('protrebot-v25-session', 'greeting-ui-test-session')
    sessionStorage.setItem('protrebot-kais-launcher-seen', '1')
  }, authenticated)
  const state = {userId: MEMBER, llmRequests: [] as string[]}
  page.on('request', request => {
    const path = new URL(request.url()).pathname
    if (['/api/assistant/chat', '/api/assistant/confirm', '/api/assistant/proactive/check-in'].includes(path)) state.llmRequests.push(path)
  })
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (!authenticated && ['/api/v22/session', '/api/v22/profile'].includes(path)) {
      await route.fulfill({status: 401, json: {detail: 'Not authenticated'}})
      return
    }
    const user = authenticated ? {id: state.userId, role: 'CUSTOMER', active: true, email_verified: true} : null
    const json = ['/api/v22/session', '/api/v22/profile'].includes(path)
      ? {user, access: {canAccessMasterTrade: authenticated, isPremium: false}}
      : path === '/api/assistant/usage' ? {remaining: 19, total: 20, resetsAt: '2026-10-06T00:00:00Z',
        limits: {max_input_chars: 1000, history_messages: 10, history_message_max_chars: 1000, page_context_max_chars: 1000, secret_min_alphanumeric_chars: 40}}
      : path === '/api/assistant/proactive/preferences' ? {enabled: false, available: true, poll_interval_seconds: 60}
      : path === '/api/health' ? {status: 'ok'} : {}
    await route.fulfill({json})
  })
  await page.goto('/')
  if (authenticated) await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toBeVisible()
  else await expect(page.getByRole('heading', {name: 'İşlem Terminali'})).toBeVisible()
  return state
}

test('First visit shows fixed greeting at 2 seconds, preserves focus and hides exactly 8 seconds later without LLM requests', async ({page}) => {
  const state = await prepare(page)
  const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
  await launcher.focus()
  await page.clock.runFor(1999)
  await expect(bubble(page)).toHaveCount(0)
  await page.clock.runFor(1)
  await expect(bubble(page)).toBeVisible()
  await expect(bubble(page)).toHaveAttribute('role', 'status')
  await expect(bubble(page)).toHaveAttribute('aria-live', 'polite')
  const message = bubble(page).getByRole('button', {name: OPEN_LABEL, exact: true})
  await expect(message).toBeVisible()
  await expect(message).toHaveText(TEXT)
  await expect(launcher).toHaveCount(1)
  await expect(launcher).toBeFocused()
  expect(await page.evaluate(key => localStorage.getItem(key), KEY)).toBe('2026-10-03')
  await page.clock.runFor(7999)
  await expect(bubble(page)).toBeVisible()
  await page.clock.runFor(1)
  await expect(bubble(page)).toHaveCount(0)
  expect(state.llmRequests).toEqual([])
})

test('Keyboard-accessible X closes greeting and it does not return on the same day after reload', async ({page}) => {
  await prepare(page)
  await page.clock.runFor(2000)
  const close = bubble(page).getByRole('button', {name: 'Kais AI karşılama balonunu kapat'})
  await close.focus()
  await expect(close).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(bubble(page)).toHaveCount(0)
  await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toBeFocused()
  await page.reload()
  await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toBeVisible()
  await page.clock.runFor(10000)
  await expect(bubble(page)).toHaveCount(0)
})

test('Greeting returns on the next local calendar day', async ({page}) => {
  await prepare(page)
  await page.clock.runFor(2000)
  await expect(bubble(page)).toBeVisible()
  await page.clock.setSystemTime(new Date('2026-10-04T12:00:00Z'))
  await page.reload()
  await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toBeVisible()
  await page.clock.runFor(2000)
  await expect(bubble(page)).toBeVisible()
  expect(await page.evaluate(key => localStorage.getItem(key), KEY)).toBe('2026-10-04')
})

test('Daily record is isolated per authenticated user', async ({page}) => {
  await page.addInitScript(() => localStorage.setItem('protrebot-kais-greeting:greeting-second-member', '2026-10-03'))
  await prepare(page)
  await page.clock.runFor(2000)
  await expect(bubble(page)).toBeVisible()
  const dates = await page.evaluate(() => [localStorage.getItem('protrebot-kais-greeting:greeting-test-member'),
    localStorage.getItem('protrebot-kais-greeting:greeting-second-member')])
  expect(dates).toEqual(['2026-10-03', '2026-10-03'])
})

for (const target of ['bubble', 'eye'] as const) {
  test(`Clicking ${target} opens the existing chat, dismisses greeting and does not send an LLM request`, async ({page}) => {
    const state = await prepare(page)
    await page.clock.runFor(2000)
    await expect(bubble(page)).toBeVisible()
    if (target === 'bubble') await bubble(page).getByRole('button', {name: OPEN_LABEL, exact: true}).click()
    else await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
    const dialog = page.getByRole('dialog', {name: 'Kais AI', exact: true})
    await expect(dialog).toBeVisible()
    await expect(dialog.getByRole('textbox', {name: 'Kais AI mesajın'})).toBeFocused()
    await expect(bubble(page)).toHaveCount(0)
    expect(state.llmRequests).toEqual([])
  })
}

test('Opening chat before the delay suppresses greeting even after closing chat', async ({page}) => {
  await prepare(page)
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  await expect(page.getByRole('dialog', {name: 'Kais AI', exact: true})).toBeVisible()
  await page.clock.runFor(3000)
  await expect(bubble(page)).toHaveCount(0)
  await page.keyboard.press('Escape')
  await page.clock.runFor(10000)
  await expect(bubble(page)).toHaveCount(0)
  expect(await page.evaluate(key => localStorage.getItem(key), KEY)).toBeNull()
})

for (const width of [1440, 390]) {
  test(`Greeting aligns below eye, avoids other header icons and stays inside ${width}px viewport`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 844})
    await prepare(page)
    await page.clock.runFor(2000)
    await expect(bubble(page)).toBeVisible()
    const box = await bubble(page).boundingBox()
    const anchor = await page.getByRole('button', {name: 'Kais AI', exact: true}).boundingBox()
    expect(box!.x).toBeGreaterThanOrEqual(12)
    expect(box!.x + box!.width).toBeLessThanOrEqual(width - 12)
    expect(box!.y).toBeGreaterThanOrEqual(anchor!.y + anchor!.height)
    expect(box!.y + box!.height).toBeLessThanOrEqual(832)
    const arrow = await bubble(page).evaluate(element => Number.parseFloat(getComputedStyle(element).getPropertyValue('--kais-greeting-arrow')))
    expect(box!.x + arrow).toBeCloseTo(anchor!.x + anchor!.width / 2, 0)
    const others = await page.locator('.v26HeaderActions button:not(.assistantLauncher)').evaluateAll(elements =>
      elements.map(element => element.getBoundingClientRect().toJSON()))
    expect(others.length).toBeGreaterThan(0)
    for (const other of others) {
      expect(box!.x >= other.right || box!.x + box!.width <= other.left ||
        box!.y >= other.bottom || box!.y + box!.height <= other.top).toBe(true)
    }
    await page.screenshot({path: testInfo.outputPath(`kais-greeting-open-${width}.png`)})
  })
}

test('Reduced motion removes animation but still shows greeting', async ({page}) => {
  await prepare(page, true, false)
  await page.clock.runFor(2000)
  await expect(bubble(page)).toBeVisible()
  await expect(bubble(page)).toHaveCSS('animation-name', 'kais-greeting-in')
  await page.emulateMedia({reducedMotion: 'reduce'})
  await expect(bubble(page)).toHaveCSS('animation-name', 'none')
})

const greetingViews = {
  dashboard: '', trading: 'İşlem Masası', analyst: 'Analyst', scanner: 'Scanner',
  'master-trade': 'Master Trade', live: 'Gerçek Futures Hazırlık Merkezi',
  risk: 'Risk Kasası', performance: 'Performance', ops: 'Bulut Operasyon ve Kanıt Merkezi',
  setup: 'Sunucu ve Anahtar Kapıları', pricing: 'Plans & Pricing', billing: 'Billing & Subscription',
}

for (const width of [320, 360, 390, 768, 1440]) {
  for (const [view, title] of Object.entries(greetingViews)) {
  test(`Greeting clears the entire header by 8px in ${view} at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 844})
    await installClock(page)
    await page.emulateMedia({reducedMotion: 'reduce'})
    await mockAssistant(page)
    await page.goto('/')
    const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
    await expect(launcher).toBeVisible()
      await page.evaluate(target => window.dispatchEvent(new CustomEvent('protrebot-navigate', {detail: target})), view)
      if (view === 'dashboard') await expect(page.locator('#dashboard-title')).toBeVisible()
      else await expect(page.locator('.v26ModeBar h1')).toHaveText(title)
      await expect.poll(async () => {
        await page.clock.runFor(100)
        return launcher.isVisible()
      }).toBe(true)
      await page.evaluate(() => document.fonts.ready)
      await page.evaluate(() => window.scrollTo({top: 0, left: 0, behavior: 'instant'}))
      await page.clock.runFor(2000)
      await expect.poll(() => launcher.evaluate(element =>
        element.closest('header')!.getAnimations({subtree: true}).every(animation =>
          !(animation instanceof CSSTransition) || animation.playState !== 'running'))).toBe(true)
      await expect(bubble(page)).toBeVisible()
      const geometry = await launcher.evaluate(element => {
        const header = element.closest('header')!
        return {
          bottom: header.getBoundingClientRect().bottom,
          controls: Array.from(header.querySelectorAll<HTMLElement>('button, a, input, [role="button"]'))
            .filter(control => control.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}))
            .map(control => control.getBoundingClientRect().bottom),
        }
      })
      const box = (await bubble(page).boundingBox())!
      expect(box.y, view).toBeGreaterThanOrEqual(geometry.bottom + 8)
      for (const bottom of geometry.controls) expect(box.y, view).toBeGreaterThanOrEqual(bottom + 8)
      expect(box.x, view).toBeGreaterThanOrEqual(12)
      expect(box.x + box.width, view).toBeLessThanOrEqual(width - 12)
  })
  }
}

test('Hidden tab pauses both waiting delay and visible lifetime', async ({page}) => {
  await prepare(page)
  await page.clock.runFor(500)
  const visibility = async (hidden: boolean) => page.evaluate(hidden => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden})
    document.dispatchEvent(new Event('visibilitychange'))
  }, hidden)
  await visibility(true)
  await page.clock.runFor(60000)
  await expect(bubble(page)).toHaveCount(0)
  await visibility(false)
  await page.clock.runFor(1499)
  await expect(bubble(page)).toHaveCount(0)
  await page.clock.runFor(1)
  await expect(bubble(page)).toBeVisible()
  await page.clock.runFor(2000)
  await visibility(true)
  await page.clock.runFor(60000)
  await visibility(false)
  await page.clock.runFor(5999)
  await expect(bubble(page)).toBeVisible()
  await page.clock.runFor(1)
  await expect(bubble(page)).toHaveCount(0)
})

for (const operation of ['getItem', 'setItem'] as const) {
  test(`Blocked localStorage ${operation} logs warning without a page error or greeting`, async ({page}) => {
    const errors: string[] = []
    const warnings: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (message.type() === 'warning' && message.text().includes('Kais AI greeting disabled')) warnings.push(message.text()) })
    await page.addInitScript(operation => {
      if (operation === 'getItem') {
        const get = Storage.prototype.getItem
        Storage.prototype.getItem = function(key) {
          if (this === localStorage && key.startsWith('protrebot-kais-greeting:')) throw new DOMException('Storage blocked', 'SecurityError')
          return get.call(this, key)
        }
      } else {
        const set = Storage.prototype.setItem
        Storage.prototype.setItem = function(key, value) {
          if (this === localStorage && key.startsWith('protrebot-kais-greeting:')) throw new DOMException('Storage full', 'QuotaExceededError')
          set.call(this, key, value)
        }
      }
    }, operation)
    await prepare(page)
    await page.clock.runFor(10000)
    await expect(bubble(page)).toHaveCount(0)
    expect(errors).toEqual([])
    expect(warnings.length).toBeGreaterThan(0)
  })
}

test('No assistant or greeting is shown for an unauthenticated session', async ({page}) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (message.type() === 'error' && /TypeError|güvenli ekran koruması/.test(message.text())) errors.push(message.text())
  })
  await prepare(page, false)
  await page.clock.runFor(10000)
  await expect(page.locator('.assistantLauncher')).toHaveCount(0)
  await expect(bubble(page)).toHaveCount(0)
  expect(await page.evaluate(key => localStorage.getItem(key), KEY)).toBeNull()
  expect(errors).toEqual([])
})

for (const elapsed of [0, 2000]) {
  test(`Unmount cleans greeting timers and listeners at ${elapsed}ms`, async ({page}) => {
    await installClock(page)
    await page.emulateMedia({reducedMotion: 'reduce'})
    await page.addInitScript(() => {
      const timers = new Set<number>()
      const listeners: Array<{target: EventTarget; type: string; listener: EventListenerOrEventListenerObject | null; capture: boolean}> = []
      const browser: Window = window
      const set = browser.setTimeout.bind(browser)
      const clear = browser.clearTimeout.bind(browser)
      browser.setTimeout = (handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
        if (typeof handler !== 'function' || !new Error().stack?.includes('KaisGreeting')) return set(handler, timeout, ...args)
        const timer = set(() => { timers.delete(timer); handler(...args) }, timeout)
        timers.add(timer)
        return timer
      }
      browser.clearTimeout = timer => { if (timer !== undefined) timers.delete(timer); clear(timer) }
      const add = EventTarget.prototype.addEventListener
      const remove = EventTarget.prototype.removeEventListener
      EventTarget.prototype.addEventListener = function(type, listener, options) {
        const capture = typeof options === 'boolean' ? options : options?.capture ?? false
        if (new Error().stack?.includes('KaisGreeting') && !listeners.some(record => record.target === this &&
          record.type === type && record.listener === listener && record.capture === capture)) listeners.push({target: this, type, listener, capture})
        add.call(this, type, listener, options)
      }
      EventTarget.prototype.removeEventListener = function(type, listener, options) {
        const capture = typeof options === 'boolean' ? options : options?.capture ?? false
        const index = listeners.findIndex(record => record.target === this && record.type === type &&
          record.listener === listener && record.capture === capture)
        if (index >= 0) listeners.splice(index, 1)
        remove.call(this, type, listener, options)
      }
      window.greetingProbe = () => ({timers: timers.size, listeners: listeners.length})
    })
    await page.goto('/frontend/tests/fixtures/kais-greeting.html')
    await expect(page.getByRole('button', {name: 'Toggle greeting'})).toBeVisible()
    if (elapsed) {
      await page.clock.runFor(elapsed)
      await expect(bubble(page)).toBeVisible()
    }
    const active = await page.evaluate(() => window.greetingProbe())
    expect(active.timers).toBeGreaterThan(0)
    expect(active.listeners).toBeGreaterThan(0)
    await page.getByRole('button', {name: 'Toggle greeting'}).click()
    expect(await page.evaluate(() => window.greetingProbe())).toEqual({timers: 0, listeners: 0})
    await page.clock.runFor(60000)
    await expect(bubble(page)).toHaveCount(0)
    expect(await page.evaluate(() => window.greetingProbe())).toEqual({timers: 0, listeners: 0})
  })
}
