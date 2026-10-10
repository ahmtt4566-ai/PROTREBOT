import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

test.use({baseURL: 'http://127.0.0.1:4174'})

const TEXT = 'Merhaba, ben Kais AI. Bugün sana nasıl yardımcı olabilirim?'
const OPEN_LABEL = 'Karşılama mesajı, sohbeti aç'
const KEY = 'kaisAiHintShown'
const DISMISSED_KEY = 'kaisAiHintDismissedUntil'
const WEEK = 7 * 86400000
const bubble = (page: Page) => page.locator('[data-kais-greeting]')
const eye = (page: Page) => page.getByRole('button', {name: 'Kais AI', exact: true})

declare global {
  interface Window {
    greetingProbe: () => {timers: number; listeners: number; frames: number}
  }
}

async function installClock(page: Page) {
  await page.clock.install({time: new Date('2026-10-03T12:00:00Z')})
  await page.clock.pauseAt(new Date('2026-10-03T12:00:01Z'))
}

async function showAutomatically(page: Page, reducedMotion = true) {
  await page.clock.runFor(2500)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'entering')
  await page.clock.runFor(reducedMotion ? 150 : 200)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
}

async function prepare(page: Page, authenticated = true, reducedMotion = true) {
  await installClock(page)
  await page.emulateMedia({reducedMotion: reducedMotion ? 'reduce' : 'no-preference'})
  const state = await mockAssistant(page)
  if (!authenticated) { state.userId = ''; state.profileStatus = 401; state.sessionStatus = 401 }
  await page.addInitScript(authenticated => {
    if (!authenticated) sessionStorage.removeItem('protrebot-v25-session')
    sessionStorage.setItem('protrebot-kais-launcher-seen', '1')
  }, authenticated)
  await page.goto('/')
  if (authenticated) await expect(eye(page)).toBeVisible()
  else await expect(page.getByRole('heading', {name: 'İşlem Terminali'})).toBeVisible()
  return state
}

test('Opens at 2500ms, stays fully visible exactly 7000ms, then fades once for 300ms without LLM requests or focus changes', async ({page}) => {
  const state = await prepare(page, true, false)
  await eye(page).focus()
  await page.clock.runFor(2499)
  await expect(bubble(page)).toHaveCount(0)
  await page.clock.runFor(1)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'entering')
  await expect(bubble(page)).toHaveCSS('animation-duration', '0.2s')
  await expect(bubble(page)).toHaveAttribute('role', 'status')
  await expect(bubble(page)).toHaveAttribute('aria-live', 'polite')
  await expect(bubble(page).getByRole('button', {name: OPEN_LABEL})).toHaveText(TEXT)
  await page.clock.runFor(200)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
  await expect(eye(page)).toBeFocused()
  expect(await page.evaluate(key => sessionStorage.getItem(key), KEY)).toBe('1')
  await page.clock.runFor(6999)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
  await page.clock.runFor(1)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'closing')
  await expect(bubble(page)).toHaveCSS('animation-duration', '0.3s')
  await page.clock.runFor(299)
  await expect(bubble(page)).toHaveCount(1)
  await page.clock.runFor(1)
  await expect(bubble(page)).toHaveCount(0)
  expect(state.requests.filter(path => /\/assistant\/(?:chat|analysis\/confirm|proactive\/check-in)$/.test(path))).toEqual([])
  expect(state.requests.filter(path => path.startsWith('POST '))).toEqual([])
})

test('Keyboard X fades for 300ms, restores focus and records exactly seven days without opening chat', async ({page}) => {
  await prepare(page, true, false)
  await showAutomatically(page, false)
  const close = bubble(page).getByRole('button', {name: 'Bildirimi kapat'})
  await close.focus()
  await expect(close).toBeFocused()
  await expect(close).toHaveCSS('outline-style', 'solid')
  await page.keyboard.press('Enter')
  expect(await page.evaluate(key => Number(localStorage.getItem(key)) - Date.now(), DISMISSED_KEY)).toBe(WEEK)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'closing')
  await page.clock.runFor(299)
  await expect(bubble(page)).toHaveCount(1)
  await page.clock.runFor(1)
  await expect(bubble(page)).toHaveCount(0)
  await expect(eye(page)).toBeFocused()
  await expect(page.getByRole('dialog', {name: 'Kais AI', exact: true})).toHaveCount(0)
  await page.reload()
  await expect(eye(page)).toBeVisible()
  await page.clock.runFor(10000)
  await expect(bubble(page)).toHaveCount(0)
})

test('Session marker survives reload, calendar day and route changes; clearing the tab session allows automatic showing again', async ({page}) => {
  await prepare(page)
  await showAutomatically(page)
  await expect(bubble(page)).toBeVisible()
  await page.clock.setSystemTime(new Date('2026-10-04T12:00:00Z'))
  await page.reload()
  await expect(eye(page)).toBeVisible()
  await page.clock.runFor(10000)
  await expect(bubble(page)).toHaveCount(0)
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('protrebot-navigate', {detail: 'analyst'})))
  await page.clock.runFor(10000)
  await expect(bubble(page)).toHaveCount(0)
  await page.evaluate(key => sessionStorage.removeItem(key), KEY)
  await page.reload()
  await expect(eye(page)).toBeVisible()
  await showAutomatically(page)
  await expect(bubble(page)).toBeVisible()
})

test('Dismissal suppresses a fresh tab until the exact seven-day boundary', async ({page}) => {
  const until = new Date('2026-10-10T12:00:01Z').getTime()
  await page.addInitScript(until => localStorage.setItem('kaisAiHintDismissedUntil', String(until)), until)
  await prepare(page)
  await page.clock.runFor(10000)
  await expect(bubble(page)).toHaveCount(0)
  await page.clock.setSystemTime(new Date(until - 1))
  await page.reload()
  await expect(eye(page)).toBeVisible()
  await expect(bubble(page)).toHaveCount(0)
  await page.clock.setSystemTime(new Date(until))
  await page.reload()
  await expect(eye(page)).toBeVisible()
  await showAutomatically(page)
  await expect(bubble(page)).toBeVisible()
})

test('Eye overrides session and seven-day dismissal, opens only the hint and causes zero API requests', async ({page}) => {
  await page.addInitScript(() => {
    sessionStorage.setItem('kaisAiHintShown', '1')
    localStorage.setItem('kaisAiHintDismissedUntil', String(Date.now() + 7 * 86400000))
  })
  const state = await prepare(page)
  await page.clock.runFor(3000)
  await expect(bubble(page)).toHaveCount(0)
  const before = [...state.requests]
  await eye(page).click()
  await page.clock.runFor(150)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
  await expect(eye(page)).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByRole('dialog', {name: 'Kais AI', exact: true})).toHaveCount(0)
  expect(state.requests).toEqual(before)
})

test('Eye opens immediately before the automatic delay and resets an open timer without replaying entry', async ({page}) => {
  await prepare(page, true, false)
  await eye(page).click()
  await page.clock.runFor(200)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
  await page.clock.runFor(6000)
  await eye(page).click()
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
  await expect(bubble(page)).toHaveCSS('animation-name', 'none')
  await page.clock.runFor(6999)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
  await page.clock.runFor(1)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'closing')
})

test('Eye cancels an in-progress fade and restores full opacity for a fresh lifetime', async ({page}) => {
  await prepare(page, true, false)
  await showAutomatically(page, false)
  await page.clock.runFor(7000)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'closing')
  await page.clock.runFor(100)
  await eye(page).click()
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
  await expect(bubble(page)).toHaveCSS('opacity', '1')
  await page.clock.runFor(6999)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
  await page.clock.runFor(1)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'closing')
})

for (const target of ['message', 'link'] as const) {
  test(`${target} opens the existing chat, focuses composer and never sends an LLM request`, async ({page}) => {
    const state = await prepare(page)
    await showAutomatically(page)
    await bubble(page).getByRole('button', {name: target === 'message' ? OPEN_LABEL : 'Sohbeti aç →', exact: true}).click()
    const dialog = page.getByRole('dialog', {name: 'Kais AI', exact: true})
    await expect(dialog).toBeVisible()
    await expect(dialog.getByRole('textbox', {name: 'Kais AI mesajın'})).toBeFocused()
    await expect(bubble(page)).toHaveCount(0)
    expect(state.requests.filter(path => path.startsWith('POST '))).toEqual([])
    await page.keyboard.press('Escape')
    await page.clock.runFor(10000)
    await expect(bubble(page)).toHaveCount(0)
  })
}

for (const interaction of ['hover', 'touch', 'focus'] as const) {
  test(`${interaction} pauses and resumes only the remaining lifetime, with a matching progress line`, async ({page}) => {
    await prepare(page, true, false)
    await showAutomatically(page, false)
    await page.clock.runFor(2000)
    const fill = bubble(page).locator('.kaisGreetingProgress > span')
    if (interaction === 'hover') await bubble(page).hover()
    else if (interaction === 'touch') await bubble(page).dispatchEvent('touchstart')
    else await bubble(page).getByRole('button', {name: 'Bildirimi kapat'}).focus()
    await expect(bubble(page)).toHaveAttribute('data-paused', 'true')
    const pausedTransform = await fill.evaluate(element => getComputedStyle(element).transform)
    const scale = Number(pausedTransform.match(/matrix\(([^,]+)/)?.[1])
    expect(scale).toBeCloseTo(5 / 7, 2)
    const trackBox = (await bubble(page).locator('.kaisGreetingProgress').boundingBox())!
    const fillBox = (await fill.boundingBox())!
    expect(fillBox.x + fillBox.width).toBeCloseTo(trackBox.x + trackBox.width, 1)
    expect(fillBox.x).toBeCloseTo(trackBox.x + trackBox.width * 2 / 7, 1)
    await page.clock.fastForward(60000)
    await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
    expect(await fill.evaluate(element => getComputedStyle(element).transform)).toBe(pausedTransform)
    if (interaction === 'hover') await page.mouse.move(0, 0)
    else if (interaction === 'touch') await bubble(page).dispatchEvent('touchend')
    else await eye(page).focus()
    await expect(bubble(page)).toHaveAttribute('data-paused', 'false')
    await page.clock.runFor(4999)
    await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
    await page.clock.runFor(1)
    await expect(bubble(page)).toHaveAttribute('data-phase', 'closing')
  })
}

test('Moving keyboard focus between notification buttons keeps the same pause', async ({page}) => {
  await prepare(page)
  await showAutomatically(page)
  await bubble(page).getByRole('button', {name: OPEN_LABEL}).focus()
  await page.clock.runFor(10000)
  await bubble(page).getByRole('button', {name: 'Sohbeti aç →'}).focus()
  await page.clock.runFor(10000)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
  await expect(bubble(page)).toHaveAttribute('data-paused', 'true')
})

test('Escape fades out without persisting a seven-day dismissal or opening chat', async ({page}) => {
  await prepare(page, true, false)
  await showAutomatically(page, false)
  await page.keyboard.press('Escape')
  await expect(bubble(page)).toHaveAttribute('data-phase', 'closing')
  await page.clock.runFor(299)
  await expect(bubble(page)).toHaveCount(1)
  await page.clock.runFor(1)
  await expect(bubble(page)).toHaveCount(0)
  expect(await page.evaluate(key => localStorage.getItem(key), DISMISSED_KEY)).toBeNull()
})

test('Hidden tab pauses both the waiting delay and visible lifetime', async ({page}) => {
  await prepare(page)
  const visibility = async (hidden: boolean) => page.evaluate(hidden => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden})
    document.dispatchEvent(new Event('visibilitychange'))
  }, hidden)
  await page.clock.runFor(500)
  await visibility(true)
  await page.clock.fastForward(60000)
  await expect(bubble(page)).toHaveCount(0)
  await visibility(false)
  await page.clock.runFor(1999)
  await expect(bubble(page)).toHaveCount(0)
  await page.clock.runFor(1)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'entering')
  await page.clock.runFor(150)
  await page.clock.runFor(2000)
  await visibility(true)
  await page.clock.fastForward(60000)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
  await visibility(false)
  await page.clock.runFor(4999)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
  await page.clock.runFor(1)
  await expect(bubble(page)).toHaveAttribute('data-phase', 'closing')
  await page.clock.runFor(150)
  await expect(bubble(page)).toHaveCount(0)
})

for (const width of [1440, 480, 390, 320]) {
  test(`Specified layout, typography, arrow and 44px close target at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 844})
    await prepare(page)
    await showAutomatically(page)
    const box = (await bubble(page).boundingBox())!
    const anchor = (await page.locator('.assistantLauncher .kaisEye').boundingBox())!
    expect(box.width).toBe(width <= 480 ? width - 32 : 300)
    expect(box.x).toBeGreaterThanOrEqual(16)
    expect(box.x + box.width).toBeLessThanOrEqual(width - 16)
    expect(box.y).toBeCloseTo(anchor.y + anchor.height + 10, 1)
    expect(box.y + box.height).toBeLessThanOrEqual(828)
    const arrow = await bubble(page).evaluate(element => Number.parseFloat(getComputedStyle(element).getPropertyValue('--kais-greeting-arrow')))
    expect(box.x + arrow).toBeCloseTo(anchor.x + anchor.width / 2, 0)
    await expect(bubble(page)).toHaveCSS('background-color', 'rgb(17, 23, 20)')
    await expect(bubble(page)).toHaveCSS('border-radius', '12px')
    await expect(bubble(page).getByRole('button', {name: OPEN_LABEL})).toHaveCSS('font-size', '14px')
    await expect(bubble(page).getByRole('button', {name: OPEN_LABEL})).toHaveCSS('font-weight', '400')
    const close = (await bubble(page).getByRole('button', {name: 'Bildirimi kapat'}).boundingBox())!
    expect(close.width).toBeGreaterThanOrEqual(44)
    expect(close.height).toBeGreaterThanOrEqual(44)
    const message = (await bubble(page).getByRole('button', {name: OPEN_LABEL}).boundingBox())!
    expect(close.y + close.height).toBeLessThanOrEqual(message.y)
    await expect(bubble(page).locator('.kaisGreetingArrow')).toHaveAttribute('aria-hidden', 'true')
    await expect(bubble(page).locator('.kaisGreetingProgress')).toHaveAttribute('aria-hidden', 'true')
    expect(await bubble(page).evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true)
    await page.screenshot({path: testInfo.outputPath(`kais-greeting-open-${width}.png`)})
  })
}

test('Reduced motion uses 150ms opacity only and a static progress line', async ({page}) => {
  await prepare(page)
  await page.clock.runFor(2500)
  await expect(bubble(page)).toHaveCSS('animation-name', 'kais-greeting-in-reduced')
  await expect(bubble(page)).toHaveCSS('animation-duration', '0.15s')
  await expect(bubble(page)).toHaveCSS('transform', 'none')
  await page.clock.runFor(2150)
  await expect(bubble(page).locator('.kaisGreetingProgress > span')).toHaveCSS('transform', 'none')
  await page.keyboard.press('Escape')
  await expect(bubble(page)).toHaveCSS('animation-duration', '0.15s')
  await page.clock.runFor(150)
  await expect(bubble(page)).toHaveCount(0)
})

const greetingViews = {
  dashboard: '', trading: 'İşlem Masası', analyst: 'Analyst', scanner: 'Scanner',
  'master-trade': 'Master Trade', live: 'Gerçek Futures Hazırlık Merkezi',
  risk: 'Risk Kasası', performance: 'Performance', ops: 'Bulut Operasyon ve Kanıt Merkezi',
  setup: 'Sunucu ve Anahtar Kapıları', pricing: 'Plans & Pricing', billing: 'Billing & Subscription',
}

for (const width of [320, 360, 390, 768, 1440]) {
  for (const [view, title] of Object.entries(greetingViews)) {
    test(`Greeting follows the actual eye by 10px in ${view} at ${width}px`, async ({page}) => {
      await page.setViewportSize({width, height: 844})
      await installClock(page)
      await page.emulateMedia({reducedMotion: 'reduce'})
      await mockAssistant(page)
      await page.goto('/')
      await expect(eye(page)).toBeVisible()
      await page.evaluate(target => window.dispatchEvent(new CustomEvent('protrebot-navigate', {detail: target})), view)
      if (view === 'dashboard') await expect(page.locator('#dashboard-title')).toBeVisible()
      else await expect(page.locator('.v26ModeBar h1')).toHaveText(title)
      await expect.poll(async () => { await page.clock.runFor(100); return eye(page).isVisible() }).toBe(true)
      await page.evaluate(() => document.fonts.ready)
      await showAutomatically(page)
      await page.evaluate(() => window.scrollTo({top: 0, left: 0, behavior: 'instant'}))
      await expect.poll(() => eye(page).evaluate(element =>
        element.closest('header')!.getAnimations({subtree: true}).every(animation =>
          !(animation instanceof CSSTransition) || animation.playState !== 'running'))).toBe(true)
      await expect.poll(async () => {
        await page.clock.runFor(32)
        return bubble(page).evaluate(element => {
          const anchor = document.querySelector('.assistantLauncher .kaisEye')!.getBoundingClientRect()
          const box = element.getBoundingClientRect()
          return {visible: getComputedStyle(element).visibility === 'visible',
            gap: Math.round((box.top - anchor.bottom) * 100) / 100, width: box.width,
            leftFits: box.left >= 16, rightFits: box.right <= window.innerWidth - 16}
        })
      }).toEqual({visible: true, gap: 10, width: width <= 480 ? width - 32 : 300, leftFits: true, rightFits: true})
    })
  }
}

for (const storage of ['localStorage', 'sessionStorage'] as const) {
  for (const operation of ['getItem', 'setItem'] as const) {
    test(`Blocked ${storage} ${operation} logs a warning but hint remains usable`, async ({page}) => {
      const errors: string[] = []
      const warnings: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      page.on('console', message => { if (message.type() === 'warning' && message.text().includes('Kais AI hint storage unavailable')) warnings.push(message.text()) })
      await page.addInitScript(({storage, operation}) => {
        if (operation === 'getItem') {
          const get = Storage.prototype.getItem
          Storage.prototype.getItem = function(key) {
            if (this === window[storage] && key.startsWith('kaisAiHint')) throw new DOMException('Storage blocked', 'SecurityError')
            return get.call(this, key)
          }
        } else {
          const set = Storage.prototype.setItem
          Storage.prototype.setItem = function(key, value) {
            if (this === window[storage] && key.startsWith('kaisAiHint')) throw new DOMException('Storage full', 'QuotaExceededError')
            set.call(this, key, value)
          }
        }
      }, {storage, operation})
      await prepare(page)
      await showAutomatically(page)
      await expect(bubble(page)).toBeVisible()
      await bubble(page).getByRole('button', {name: 'Bildirimi kapat'}).press('Enter')
      await page.clock.runFor(150)
      await expect(bubble(page)).toHaveCount(0)
      expect(errors).toEqual([])
      expect(warnings.length).toBeGreaterThan(0)
    })
  }
}

for (const path of ['/login', '/register', '/forgot-password', '/reset-password', '/verify-email']) {
  test(`${path} blocks automatic showing but manual eye click still works`, async ({page}) => {
    await installClock(page)
    await page.emulateMedia({reducedMotion: 'reduce'})
    await page.goto('/frontend/tests/fixtures/kais-greeting.html')
    await page.getByRole('combobox', {name: 'Page route'}).selectOption(path)
    await page.clock.runFor(10000)
    await expect(bubble(page)).toHaveCount(0)
    expect(await page.evaluate(key => sessionStorage.getItem(key), KEY)).toBeNull()
    await eye(page).click()
    await page.clock.runFor(150)
    await expect(bubble(page)).toBeVisible()
  })
}

test('No assistant or hint appears for an unauthenticated session', async ({page}) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await prepare(page, false)
  await page.clock.runFor(10000)
  await expect(page.locator('.assistantLauncher')).toHaveCount(0)
  await expect(bubble(page)).toHaveCount(0)
  expect(await page.evaluate(key => sessionStorage.getItem(key), KEY)).toBeNull()
  expect(errors).toEqual([])
})

for (const elapsed of [0, 2500, 2650, 9650]) {
  test(`Unmount cleans all timers, listeners and animation frames at ${elapsed}ms`, async ({page}) => {
    await installClock(page)
    await page.emulateMedia({reducedMotion: 'reduce'})
    await page.addInitScript(() => {
      const timers = new Set<number>()
      const frames = new Set<number>()
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
      const request = browser.requestAnimationFrame.bind(browser)
      const cancel = browser.cancelAnimationFrame.bind(browser)
      browser.requestAnimationFrame = callback => {
        if (!new Error().stack?.includes('KaisGreeting')) return request(callback)
        const frame = request(time => { frames.delete(frame); callback(time) })
        frames.add(frame)
        return frame
      }
      browser.cancelAnimationFrame = frame => { frames.delete(frame); cancel(frame) }
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
      window.greetingProbe = () => ({timers: timers.size, listeners: listeners.length, frames: frames.size})
    })
    await page.goto('/frontend/tests/fixtures/kais-greeting.html')
    await expect(page.getByRole('button', {name: 'Toggle greeting'})).toBeVisible()
    if (elapsed) {
      await page.clock.runFor(2500)
      await expect(bubble(page)).toHaveAttribute('data-phase', 'entering')
      if (elapsed >= 2650) {
        await page.clock.runFor(150)
        await expect(bubble(page)).toHaveAttribute('data-phase', 'visible')
      }
      if (elapsed === 9650) await page.clock.runFor(7000)
      await expect(bubble(page)).toBeVisible()
    }
    const active = await page.evaluate(() => window.greetingProbe())
    expect(active.timers).toBeGreaterThan(0)
    expect(active.listeners).toBeGreaterThan(0)
    await page.getByRole('button', {name: 'Toggle greeting'}).click()
    expect(await page.evaluate(() => window.greetingProbe())).toEqual({timers: 0, listeners: 0, frames: 0})
    await page.clock.runFor(60000)
    await expect(bubble(page)).toHaveCount(0)
    expect(await page.evaluate(() => window.greetingProbe())).toEqual({timers: 0, listeners: 0, frames: 0})
  })
}
