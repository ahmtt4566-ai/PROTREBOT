import {expect, test, type Page} from '@playwright/test'

test.use({baseURL: 'http://127.0.0.1:4174'})

declare global {
  interface Window {
    headerEyeFrames: number
    headerEyeListeners: () => {total: number; owned: {windowDocument: number; matchMedia: number; host: number}}
  }
}

async function clock(page: Page, random = 0) {
  await page.clock.install({time: new Date('2026-01-01T00:00:00Z')})
  await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'))
  await page.emulateMedia({reducedMotion: 'no-preference'})
  await page.addInitScript(random => { Math.random = () => random }, random)
}

async function openHeader(page: Page) {
  await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'header-eye-test-session'))
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    const user = {id: 'header-eye-test-member', role: 'CUSTOMER', active: true, email_verified: true}
    const json = path === '/api/v22/session' || path === '/api/v22/profile' ? {user, access: {canAccessMasterTrade: true, isPremium: false}}
      : path === '/api/assistant/usage' ? {remaining: 20, total: 20, resetsAt: '2026-10-04T00:00:00Z',
        limits: {max_input_chars: 1000, history_messages: 10, history_message_max_chars: 1000, page_context_max_chars: 1000, secret_min_alphanumeric_chars: 40}}
      : path === '/api/assistant/proactive/preferences' ? {enabled: false, available: true, poll_interval_seconds: 60}
      : path === '/api/health' ? {status: 'ok'} : {}
    await route.fulfill({json})
  })
  await page.goto('/')
  await expect(page.locator('.assistantLauncher .kaisHeaderEye')).toHaveAttribute('data-state', 'idle')
}

for (const width of [1440, 390]) {
  test(`Header appearance, accessible button and opening behavior at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 844})
    await page.emulateMedia({reducedMotion: 'reduce'})
    await openHeader(page)
    const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
    const eye = launcher.locator('.kaisHeaderEye')
    await expect(eye).toHaveAttribute('aria-hidden', 'true')
    await expect(eye).toHaveCSS('width', '56px')
    await expect(eye).toHaveCSS('height', '56px')
    expect(await launcher.evaluate(element => element.tagName)).toBe('BUTTON')
    const rect = await launcher.boundingBox()
    expect(rect?.width).toBeGreaterThanOrEqual(44)
    expect(rect?.height).toBeGreaterThanOrEqual(44)
    await page.screenshot({path: testInfo.outputPath(`kais-header-${width}.png`)})
    await expect(launcher).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
    expect(await launcher.evaluate(element => getComputedStyle(element, '::before').display)).toBe('none')
    await launcher.click()
    await expect(page.getByRole('dialog', {name: 'Kais AI'})).toBeVisible()
    const panelEye = page.locator('.assistantHeader .kaisEye')
    await expect(panelEye).toHaveClass(/kaisHeaderEye/)
  })
}

test('Mouse and touch pointermove are RAF-throttled, clipped and bounded to 7 units with 60% vertical movement', async ({page}) => {
  await clock(page)
  await openHeader(page)
  const eye = page.locator('.assistantLauncher .kaisHeaderEye')
  await page.evaluate(() => {
    const original = window.requestAnimationFrame.bind(window)
    window.headerEyeFrames = 0
    window.requestAnimationFrame = callback => { window.headerEyeFrames++; return original(callback) }
    for (let index = 0; index < 3; index++) {
      window.dispatchEvent(new PointerEvent('pointermove', {pointerType: 'mouse', clientX: -100000, clientY: -100000}))
    }
  })
  expect(await page.evaluate(() => window.headerEyeFrames)).toBe(1)
  await page.clock.runFor(32)
  const transform = await eye.locator('.kaisEyeGaze').evaluate(element => element.style.transform)
  const [x, y] = transform.match(/-?[\d.]+/g)!.map(Number)
  expect(x).toBeLessThan(0)
  expect(y).toBeLessThan(0)
  expect(Math.hypot(x, y)).toBeLessThanOrEqual(7)
  expect(y / x).toBeCloseTo(.6)
  const clip = await eye.locator('clipPath').getAttribute('id')
  await expect(eye.locator('.kaisEyeInterior')).toHaveAttribute('clip-path', `url(#${clip})`)
  await page.evaluate(() => window.dispatchEvent(new PointerEvent('pointermove',
    {pointerType: 'touch', clientX: 100000, clientY: 100000})))
  await page.clock.runFor(32)
  expect(Number(await eye.getAttribute('data-look-x'))).toBeGreaterThan(0)
  expect(Number(await eye.getAttribute('data-look-y'))).toBeGreaterThan(0)
  await page.evaluate(() => window.dispatchEvent(new PointerEvent('pointerout', {relatedTarget: null})))
  await expect(eye).toHaveAttribute('data-look-x', '0')
  await expect(eye).toHaveAttribute('data-look-y', '0')
})

for (const [random, interval] of [[0, 9000], [1, 11000]]) {
  test(`First blink at 3 seconds, then ${interval}ms boundary with 200ms scaleY and glow dimming`, async ({page}) => {
    await clock(page, random)
    await openHeader(page)
    const eye = page.locator('.assistantLauncher .kaisHeaderEye')
    await page.clock.runFor(2999)
    await expect(eye).not.toHaveClass(/kaisEyeBlink/)
    await page.clock.runFor(2)
    await expect(eye).toHaveClass(/kaisEyeBlink/)
    await expect(eye.locator('.kaisEyeInterior')).toHaveCSS('animation-name', 'kais-eye-blink')
    await expect(eye.locator('.kaisEyeInterior')).toHaveCSS('animation-duration', '0.2s')
    await expect(eye.locator('.cyberEyeGlow')).toHaveCSS('animation-name', 'kais-header-glow-blink')
    await page.clock.runFor(200)
    await expect(eye).not.toHaveClass(/kaisEyeBlink/)
    await page.clock.runFor(interval - 202)
    await expect(eye).not.toHaveClass(/kaisEyeBlink/)
    await page.clock.runFor(2)
    await expect(eye).toHaveClass(/kaisEyeBlink/)
  })
}

test('Reduced motion and hidden tab stop tracking and blinking', async ({page}) => {
  await clock(page)
  await openHeader(page)
  const eye = page.locator('.assistantLauncher .kaisHeaderEye')
  await page.emulateMedia({reducedMotion: 'reduce'})
  await expect(eye).not.toHaveClass(/kaisEyeMotion/)
  await page.evaluate(() => window.dispatchEvent(new PointerEvent('pointermove', {pointerType: 'touch', clientX: 0, clientY: 0})))
  await page.clock.runFor(7000)
  await expect(eye).toHaveAttribute('data-look-x', '0')
  await expect(eye).not.toHaveClass(/kaisEyeBlink/)
  await page.emulateMedia({reducedMotion: 'no-preference'})
  await expect(eye).toHaveClass(/kaisEyeMotion/)
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => true})
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(eye).not.toHaveClass(/kaisEyeMotion/)
  await page.clock.runFor(7000)
  await expect(eye).not.toHaveClass(/kaisEyeBlink/)
  await expect(eye).toHaveAttribute('data-look-x', '0')
})

test('Two copies have unique clip IDs and unmount removes interaction listeners without network calls', async ({page}, testInfo) => {
  await clock(page)
  const requests: string[] = []
  page.on('request', request => { if (new URL(request.url()).pathname.startsWith('/api/')) requests.push(request.url()) })
  await page.addInitScript(() => {
    const records: Array<{target: EventTarget; type: string; listener: EventListenerOrEventListenerObject;
      capture: boolean; owned: boolean}> = []
    const globals: EventTarget[] = [window, document]
    const nativeGlobals = globals.map(target => ({target, add: target.addEventListener.bind(target),
      remove: target.removeEventListener.bind(target)}))
    const add = EventTarget.prototype.addEventListener
    const remove = EventTarget.prototype.removeEventListener
    const recordAdd = (target: EventTarget, type: string, listener: EventListenerOrEventListenerObject | null,
      options?: boolean | AddEventListenerOptions) => {
      if (!listener || !(globals.includes(target) || target instanceof MediaQueryList || target instanceof HTMLButtonElement)) return
      const capture = typeof options === 'boolean' ? options : options?.capture ?? false
      if (!records.some(record => record.target === target && record.type === type &&
        record.listener === listener && record.capture === capture)) {
        records.push({target, type, listener, capture,
          owned: /useHeaderKaisEye|useAssistantPresentation|useKaisPrivacy/.test(new Error().stack ?? '')})
      }
    }
    const recordRemove = (target: EventTarget, type: string, listener: EventListenerOrEventListenerObject | null,
      options?: boolean | EventListenerOptions) => {
      const capture = typeof options === 'boolean' ? options : options?.capture ?? false
      const index = records.findIndex(record => record.target === target && record.type === type &&
        record.listener === listener && record.capture === capture)
      if (index >= 0) records.splice(index, 1)
    }
    EventTarget.prototype.addEventListener = function(type, listener, options) {
      recordAdd(this, type, listener, options)
      add.call(this, type, listener, options)
    }
    EventTarget.prototype.removeEventListener = function(type, listener, options) {
      recordRemove(this, type, listener, options)
      remove.call(this, type, listener, options)
    }
    for (const {target, add, remove} of nativeGlobals) {
      target.addEventListener = (type, listener, options) => { recordAdd(target, type, listener, options); add(type, listener, options) }
      target.removeEventListener = (type, listener, options) => { recordRemove(target, type, listener, options); remove(type, listener, options) }
    }
    window.headerEyeListeners = () => ({
      total: records.length,
      owned: {
        windowDocument: records.filter(record => record.owned && globals.includes(record.target)).length,
        matchMedia: records.filter(record => record.owned && record.target instanceof MediaQueryList).length,
        host: records.filter(record => record.owned && record.target instanceof HTMLButtonElement).length,
      },
    })
  })
  await page.goto('/frontend/tests/fixtures/kais-header-eye.html')
  const eyes = page.locator('.kaisHeaderEye')
  await expect(eyes).toHaveCount(0)
  // Bootstrap Playwright's main-world pointer interceptor before taking the baseline.
  await page.locator('button').evaluateAll(elements => elements.length)
  const baseline = await page.evaluate(() => window.headerEyeListeners())
  expect(baseline.owned).toEqual({windowDocument: 0, matchMedia: 0, host: 0})
  await page.getByRole('button', {name: 'Toggle eyes'}).click()
  await expect(eyes).toHaveCount(2)
  const ids = await eyes.locator('clipPath').evaluateAll(elements => elements.map(element => element.id))
  expect(new Set(ids).size).toBe(2)
  const active = await page.evaluate(() => window.headerEyeListeners())
  expect(active.owned.windowDocument).toBeGreaterThan(0)
  expect(active.owned.matchMedia).toBeGreaterThan(0)
  expect(active.owned.host).toBeGreaterThan(0)
  expect(active.total).toBeGreaterThan(baseline.total)
  await page.getByRole('button', {name: 'Toggle eyes'}).click()
  await expect(eyes).toHaveCount(0)
  const unmounted = await page.evaluate(() => window.headerEyeListeners())
  expect(unmounted).toEqual(baseline)
  await page.clock.runFor(60000)
  await page.getByRole('button', {name: 'Toggle eyes'}).click()
  await expect(eyes).toHaveCount(2)
  expect(await page.evaluate(() => window.headerEyeListeners())).toEqual(active)
  await page.getByRole('button', {name: 'Toggle eyes'}).click()
  await expect(eyes).toHaveCount(0)
  const remountedCleanup = await page.evaluate(() => window.headerEyeListeners())
  expect(remountedCleanup).toEqual(baseline)
  await testInfo.attach('listener-baselines', {body: JSON.stringify({baseline, active, unmounted, remountedCleanup}, null, 2),
    contentType: 'application/json'})
  expect(requests).toEqual([])
})
