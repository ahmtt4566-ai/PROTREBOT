import {expect, test, type Page} from '@playwright/test'
import {join} from 'node:path'

test.use({baseURL: 'http://127.0.0.1:4174'})

const labels = {
  idle: 'Kais AI: hazır',
  thinking: 'Kais AI: düşünüyor',
  private: 'Kais AI: gizlilik modu',
  error: 'Kais AI: hata',
  off: 'Kais AI: kapalı',
} as const

test.beforeEach(async ({page}) => {
  await page.emulateMedia({reducedMotion: 'reduce'})
  await page.goto('/frontend/tests/fixtures/kais-eye.html')
})

for (const state of ['idle', 'thinking', 'private', 'error', 'off'] as const) {
  test(`${state} renders all SVG layers with the correct accessible label in three sizes and two themes`, async ({page}) => {
    for (const theme of ['dark', 'light']) {
      for (const size of [56, 36, 24]) {
        const eye = page.getByTestId(`${theme}-${size}-${state}`).getByRole('img', {name: labels[state], exact: true})
        await expect(eye).toBeVisible()
        await expect(eye).toHaveAttribute('data-state', state)
        await expect(eye).toHaveAttribute('width', String(size))
        await expect(eye).toHaveAttribute('height', String(size))
        await expect(eye).toHaveAttribute('data-detail', size === 24 ? 'compact' : size === 36 ? 'medium' : 'full')
        await expect(eye).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
        const box = await eye.boundingBox()
        expect(box!.width).toBe(size)
        expect(box!.height).toBe(size)
        for (const layer of ['ring', 'upper-lid', 'lower-lid', 'sclera', 'iris', 'pupil', 'pulse', 'reflection', 'privacy', 'unread']) {
          await expect(eye.locator(`g[data-layer="${layer}"]`)).toHaveCount(1)
        }
        await expect(eye.locator('[data-layer="privacy"]')).toHaveAttribute('visibility', state === 'private' ? 'visible' : 'hidden')
        await expect(eye.locator('[data-layer="thinking"]')).toHaveAttribute('visibility', state === 'thinking' ? 'visible' : 'hidden')
        await expect(eye.locator('[data-layer="error"]')).toHaveAttribute('visibility', state === 'error' ? 'visible' : 'hidden')
        if (state === 'private') {
          await expect(eye.locator('[data-layer="eye-interior"]')).toHaveCSS('opacity', '0')
          await expect(eye.locator('.kaisEyeOpening')).toHaveCSS('transform', 'matrix(1, 0, 0, 0.03, 0, 0)')
          await expect(eye.locator('.kaisEyeRing')).toHaveCSS('opacity', '0.4')
        } else if (state === 'error' || state === 'off') {
          await expect(eye.locator('[data-layer="eye-interior"]')).toHaveCSS('opacity', '0.45')
          await expect(eye.locator('[data-layer="upper-lid"] path')).toHaveAttribute('d', 'M12 30Q32 24 52 30')
        }
        if (size < 56) await expect(eye.locator('.kaisEyeDetail').first()).toHaveCSS('display', 'none')
        if (size === 24) await expect(eye.locator('.kaisEyeReflection')).toHaveCSS('display', 'none')
      }
    }
  })
}

test('Default size, clamped static gaze, unread badge, custom colors and English labels', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'no-preference'})
  await expect(page.getByTestId('default').locator('svg')).toHaveAttribute('width', '56')
  await expect(page.getByTestId('gaze').locator('[data-layer="gaze"]')).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 3, -2)')
  for (const name of ['private-gaze', 'off-gaze']) {
    await expect(page.getByTestId(name).locator('[data-layer="gaze"]')).toHaveCSS('transform', 'matrix(1, 0, 0, 1, 0, 0)')
  }
  await expect(page.getByTestId('unread').getByRole('img')).toHaveAttribute('aria-label', 'Kais AI: hazır · okunmamış bildirim')
  await expect(page.getByTestId('unread').locator('[data-layer="unread"]')).toHaveAttribute('visibility', 'visible')
  await expect(page.getByTestId('default').locator('[data-layer="unread"]')).toHaveAttribute('visibility', 'hidden')
  await expect(page.getByTestId('english').getByRole('img', {name: 'Kais AI: thinking', exact: true})).toBeVisible()
  await expect(page.getByTestId('custom').locator('.kaisEyeRingSegments')).toHaveCSS('stroke', 'rgb(0, 107, 96)')
  await expect(page.getByTestId('custom').locator('.kaisEyeRingGlow')).toHaveCSS('stroke', 'rgb(35, 203, 189)')
})

test('Reduced motion has unique gradients and clips, no animation classes, tracking or API requests', async ({page}, testInfo) => {
  const apiRequests: string[] = []
  page.on('request', request => {if (new URL(request.url()).pathname.startsWith('/api/')) apiRequests.push(request.url())})
  const ids = await page.locator('svg defs [id]').evaluateAll(elements => elements.map(element => element.id))
  expect(new Set(ids).size).toBe(ids.length)
  const eye = page.getByTestId('default').locator('svg')
  const initial = await eye.evaluate(element => element.outerHTML)
  await page.mouse.move(10, 400)
  await page.dispatchEvent('body', 'pointerdown', {pointerType: 'touch', clientX: 100, clientY: 300})
  await page.emulateMedia({reducedMotion: 'reduce'})
  expect(await eye.evaluate(element => element.outerHTML)).toBe(initial)
  expect(await page.locator('svg').evaluateAll(elements => elements.every(element =>
    !element.classList.contains('kaisEyeMotion') && !element.classList.contains('kaisEyeBlink') &&
    element.getAnimations({subtree: true}).length === 0 && Array.from(element.querySelectorAll('g')).every(group =>
      getComputedStyle(group).animationName === 'none' && getComputedStyle(group).transitionDuration === '0s')))).toBe(true)
  expect(apiRequests).toEqual([])
  await page.setViewportSize({width: 1000, height: 1200})
  await page.screenshot({path: join(testInfo.outputDir, 'kais-eye-states.png'), fullPage: true})
})

declare global {
  interface Window {
    kaisEyeProbe: {listeners: () => number; interactions: () => number; details: () => string[]; frameRequests: number; pendingFrames: () => number; canceledFrames: number; pendingTimers: () => number}
  }
}

async function freezeClock(page: Page) {
  await page.clock.install({time: new Date('2026-01-01T00:00:00Z')})
  await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'))
}

async function openBehavior(page: Page) {
  await page.addInitScript(() => {
    const monitored = new Set(['pointermove', 'pointerdown', 'pointerover', 'pointerout', 'pointercancel', 'pointerenter', 'pointerleave',
      'blur', 'focusin', 'focusout', 'visibilitychange', 'change', 'kais:react', 'keydown', 'scroll'])
    const rows: {target: EventTarget; type: string; listener: EventListenerOrEventListenerObject; capture: boolean}[] = []
    const add = EventTarget.prototype.addEventListener
    const remove = EventTarget.prototype.removeEventListener
    const captureOf = (options?: boolean | EventListenerOptions) => typeof options === 'boolean' ? options : options?.capture === true
    const observed = (target: EventTarget, type: string) => monitored.has(type) &&
      (target === window || target === document || target instanceof SVGSVGElement || target instanceof MediaQueryList)
    EventTarget.prototype.addEventListener = function(type, listener, options) {
      // Count production hook registrations, not unrelated browser test-harness listeners.
      const owned = /useKaisEye\.ts|useKaisPrivacy\.ts|useAssistantPresentation\.ts|useKaisPageReactions\.ts/.test(new Error().stack ?? '')
      if (owned && listener && observed(this, type) && !rows.some(row => row.target === this && row.type === type &&
          row.listener === listener && row.capture === captureOf(options))) {
        rows.push({target: this, type, listener, capture: captureOf(options)})
      }
      add.call(this, type, listener, options)
    }
    EventTarget.prototype.removeEventListener = function(type, listener, options) {
      const index = rows.findIndex(row => row.target === this && row.type === type && row.listener === listener && row.capture === captureOf(options))
      if (index >= 0) rows.splice(index, 1)
      remove.call(this, type, listener, options)
    }
    const pending = new Set<number>()
    const timers = new Set<number>()
    const browser: Window = window
    const setTimer = browser.setTimeout.bind(browser)
    const clearTimer = browser.clearTimeout.bind(browser)
    browser.setTimeout = (handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
      if (typeof handler !== 'function' || !/useKaisEye\.ts|useKaisPrivacy\.ts/.test(new Error().stack ?? '')) return setTimer(handler, timeout, ...args)
      const id = setTimer(() => {timers.delete(id); handler(...args)}, timeout)
      timers.add(id)
      return id
    }
    browser.clearTimeout = id => {if (id !== undefined) timers.delete(id); clearTimer(id)}
    const request = window.requestAnimationFrame.bind(window)
    const cancel = window.cancelAnimationFrame.bind(window)
    window.kaisEyeProbe = {listeners: () => rows.length,
      interactions: () => rows.filter(row => row.type !== 'visibilitychange' && row.type !== 'change').length,
      details: () => rows.map(row => `${row.type}:${typeof row.listener === 'function' ? row.listener.name : 'object'}`).sort(),
      frameRequests: 0, pendingFrames: () => pending.size, canceledFrames: 0, pendingTimers: () => timers.size}
    window.requestAnimationFrame = callback => {
      window.kaisEyeProbe.frameRequests++
      const id = request(time => {pending.delete(id); callback(time)})
      pending.add(id)
      return id
    }
    window.cancelAnimationFrame = id => {
      if (pending.delete(id)) window.kaisEyeProbe.canceledFrames++
      cancel(id)
    }
  })
  await page.emulateMedia({reducedMotion: 'no-preference'})
  await page.goto('/frontend/tests/fixtures/kais-eye.html?behavior')
  await expect(page.getByTestId('behavior-eye').locator('svg')).toHaveClass(/kaisEyeMotion/)
}

for (const type of ['premium-open', 'navigation'] as const) {
  test(`Page reaction ${type} looks briefly at bounded coordinates and returns to center`, async ({page}) => {
    await freezeClock(page)
    await openBehavior(page)
    const eye = page.getByTestId('behavior-eye').locator('svg')
    await page.evaluate(type => window.kaisReact({type, point: {x: 10000, y: -10000}}), type)
    await expect(eye).toHaveAttribute('data-reaction', type)
    await expect(eye).toHaveAttribute('data-look-x', '1')
    await expect(eye).toHaveAttribute('data-look-y', '-1')
    await page.clock.runFor(649)
    await expect(eye).toHaveAttribute('data-reaction', type)
    await page.clock.runFor(1)
    await expect(eye).toHaveAttribute('data-reaction', 'none')
    await expect(eye).toHaveAttribute('data-look-x', '0')
    await expect(eye).toHaveAttribute('data-look-y', '0')
  })
}

test('Explicit error notices surprise briefly; success notices do not react', async ({page}) => {
  await freezeClock(page)
  await openBehavior(page)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  await page.getByRole('button', {name: 'Success toast'}).click()
  await expect(eye).toHaveAttribute('data-reaction', 'none')
  await page.getByRole('button', {name: 'Error toast'}).click()
  await expect(eye).toHaveClass(/kaisEyeSurprised/)
  await expect(eye.locator('.kaisEyeOpening')).toHaveCSS('transform', 'matrix(1, 0, 0, 1.18, 0, 0)')
  await page.clock.runFor(599)
  await expect(eye).toHaveAttribute('data-reaction', 'error')
  await page.clock.runFor(1)
  await expect(eye).toHaveAttribute('data-reaction', 'none')
  await expect(eye).not.toHaveClass(/kaisEyeSurprised/)
})

test('Unread reaction pulses without changing the authoritative unread badge', async ({page}) => {
  await freezeClock(page)
  await openBehavior(page)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  await page.getByLabel('Unread', {exact: true}).check()
  await page.evaluate(() => window.kaisReact({type: 'unread'}))
  await expect(eye).toHaveClass(/kaisEyeReactUnread/)
  await expect(eye.locator('.kaisEyeRingGlow')).toHaveCSS('animation-duration', '1.2s')
  await page.clock.runFor(1200)
  await expect(eye).toHaveAttribute('data-reaction', 'none')
  await expect(eye).toHaveAttribute('data-unread', 'true')
  await expect(eye.locator('[data-layer="unread"]')).toHaveAttribute('visibility', 'visible')
  await page.getByLabel('Unread', {exact: true}).uncheck()
  await expect(eye.locator('.kaisEyeRingGlow')).toHaveCSS('animation-name', 'none')
})

test('Inactivity slow-blinks at exactly 60 seconds, half-closes at 3 minutes and wakes on motion', async ({page}) => {
  await freezeClock(page)
  await page.addInitScript(() => { Math.random = () => 1 })
  await openBehavior(page)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  await page.clock.runFor(59999)
  await expect(eye).toHaveAttribute('data-attention', 'awake')
  await page.clock.runFor(1)
  await expect(eye).toHaveAttribute('data-attention', 'sleepy')
  await expect(eye).toHaveClass(/kaisEyeSlowBlink/)
  await expect(eye.locator('.kaisEyeInterior')).toHaveCSS('animation-duration', '0.6s')
  await page.clock.runFor(600)
  await expect(eye).not.toHaveClass(/kaisEyeSlowBlink/)
  await page.clock.runFor(119399)
  await expect(eye).toHaveAttribute('data-attention', 'sleepy')
  await page.clock.runFor(1)
  await expect(eye).toHaveAttribute('data-attention', 'drowsy')
  await expect(eye.locator('.kaisEyeOpening')).toHaveCSS('transform', 'matrix(1, 0, 0, 0.55, 0, 0)')
  await page.evaluate(() => window.kaisReact({type: 'error'}))
  await expect(eye.locator('.kaisEyeOpening')).toHaveCSS('transition-duration', '0.15s')
  await expect(eye.locator('.kaisEyeOpening')).toHaveCSS('transform', 'matrix(1, 0, 0, 1.18, 0, 0)')
  await page.clock.runFor(600)
  await expect(eye).toHaveAttribute('data-attention', 'drowsy')
  await expect(eye.locator('.kaisEyeOpening')).toHaveCSS('transform', 'matrix(1, 0, 0, 0.55, 0, 0)')
  await page.evaluate(() => window.dispatchEvent(new PointerEvent('pointermove', {pointerType: 'mouse', clientX: 10000, clientY: 10000})))
  await expect(eye).toHaveAttribute('data-attention', 'awake')
  await expect(eye).not.toHaveClass(/kaisEyeDrowsy/)
})

for (const type of ['keydown', 'scroll', 'pointerdown'] as const) {
  test(`Occurrence-only ${type} activity resets the inactivity deadline without reading keys or values`, async ({page}) => {
    await freezeClock(page)
    await openBehavior(page)
    const eye = page.getByTestId('behavior-eye').locator('svg')
    await page.clock.runFor(59000)
    await page.evaluate(type => {
      const event = type === 'keydown' ? new KeyboardEvent(type)
        : type === 'pointerdown' ? new PointerEvent(type, {pointerType: 'touch'}) : new Event(type)
      Object.defineProperty(event, 'key', {get() {throw new Error('Activity must not read keys')}})
      window.dispatchEvent(event)
    }, type)
    await page.clock.runFor(59999)
    await expect(eye).toHaveAttribute('data-attention', 'awake')
    await page.clock.runFor(1)
    await expect(eye).toHaveAttribute('data-attention', 'sleepy')
  })
}

test('Reduced motion ignores every emitted/raw reaction and inactivity, without replay on resume', async ({page}) => {
  await freezeClock(page)
  await openBehavior(page)
  await page.emulateMedia({reducedMotion: 'reduce'})
  const eye = page.getByTestId('behavior-eye').locator('svg')
  await expect(eye).not.toHaveClass(/kaisEyeMotion/)
  const initial = await eye.evaluate(element => element.outerHTML)
  const emitted = await page.evaluate(() => {
    let count = 0
    const listen = () => {count++}
    window.addEventListener('kais:react', listen)
    for (const type of ['premium-open', 'navigation'] as const) window.kaisReact({type, point: {x: 9999, y: 9999}})
    for (const type of ['error', 'unread'] as const) window.kaisReact({type})
    window.removeEventListener('kais:react', listen)
    for (const type of ['premium-open', 'navigation', 'error', 'unread']) {
      window.dispatchEvent(new CustomEvent('kais:react', {detail: {type, point: {x: 9999, y: 9999}}}))
    }
    return count
  })
  expect(emitted).toBe(0)
  await page.clock.runFor(180000)
  expect(await eye.evaluate(element => element.outerHTML)).toBe(initial)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingTimers())).toBe(0)
  await page.emulateMedia({reducedMotion: 'no-preference'})
  await expect(eye).toHaveClass(/kaisEyeMotion/)
  await expect(eye).toHaveAttribute('data-reaction', 'none')
  await expect(eye).toHaveAttribute('data-attention', 'awake')
})

for (const state of ['private', 'error', 'off'] as const) {
  test(`Page reactions cannot override ${state} state`, async ({page}) => {
    await openBehavior(page)
    await page.getByLabel('Eye state').selectOption(state)
    const eye = page.getByTestId('behavior-eye').locator('svg')
    await expect(eye).not.toHaveClass(/kaisEyeMotion/)
    await page.evaluate(() => {
      window.kaisReact({type: 'premium-open', point: {x: 9999, y: 9999}})
      window.kaisReact({type: 'navigation', point: {x: 9999, y: 9999}})
      window.kaisReact({type: 'error'})
      window.kaisReact({type: 'unread'})
    })
    await expect(eye).toHaveAttribute('data-state', state)
    await expect(eye).toHaveAttribute('data-reaction', 'none')
    await expect(eye).toHaveAttribute('data-look-x', '0')
  })
}

test('Replacing/interruption cancels old gaze resets and unmount cleans active reaction timers', async ({page}) => {
  await freezeClock(page)
  await openBehavior(page)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  await page.evaluate(() => window.kaisReact({type: 'navigation', point: {x: 9999, y: 9999}}))
  await page.clock.runFor(400)
  await page.evaluate(() => window.kaisReact({type: 'premium-open', point: {x: -9999, y: -9999}}))
  await page.clock.runFor(250)
  await expect(eye).toHaveAttribute('data-reaction', 'premium-open')
  await expect(eye).toHaveAttribute('data-look-x', '-1')
  await page.evaluate(() => window.dispatchEvent(new PointerEvent('pointermove', {pointerType: 'mouse', clientX: 9999, clientY: 9999})))
  await page.clock.runFor(16)
  await expect(eye).toHaveAttribute('data-reaction', 'none')
  await page.clock.runFor(400)
  await expect(eye).toHaveAttribute('data-look-x', '1')
  await page.evaluate(() => window.kaisReact({type: 'error'}))
  await page.getByRole('button', {name: 'Unmount eye', exact: true}).click()
  await expect.poll(() => page.evaluate(() => window.kaisEyeProbe.pendingTimers())).toBe(0)
  expect(await page.evaluate(() => window.kaisEyeProbe.listeners())).toBe(0)
})

test('Reaction emitter sends only frozen metadata, reads no extra content and makes no network calls', async ({page}) => {
  await openBehavior(page)
  const requests: string[] = []
  page.on('request', request => requests.push(request.url()))
  const result = await page.evaluate(() => {
    const network: string[] = []
    const details: unknown[] = []
    window.fetch = () => {network.push('fetch'); throw new Error('No reaction networking')}
    XMLHttpRequest.prototype.open = () => {network.push('xhr'); throw new Error('No reaction networking')}
    navigator.sendBeacon = () => {network.push('beacon'); return false}
    window.WebSocket = class extends WebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        network.push('socket')
        super(url, protocols)
      }
    }
    const receive = (event: CustomEvent) => details.push({detail: event.detail, frozen: Object.isFrozen(event.detail)})
    window.addEventListener('kais:react', receive)
    const metadata = {type: 'navigation' as const, point: {x: 100, y: 200},
      get content() {throw new Error('Emitter must not read content')}}
    window.kaisReact(metadata)
    window.kaisReact({type: 'premium-open', point: {x: 200, y: 100}})
    window.kaisReact({type: 'error'})
    window.kaisReact({type: 'unread'})
    window.removeEventListener('kais:react', receive)
    return {network, details}
  })
  expect(result.network).toEqual([])
  expect(requests).toEqual([])
  expect(result.details).toEqual([
    {detail: {type: 'navigation', point: {x: 100, y: 200}}, frozen: true},
    {detail: {type: 'premium-open', point: {x: 200, y: 100}}, frozen: true},
    {detail: {type: 'error'}, frozen: true},
    {detail: {type: 'unread'}, frozen: true},
  ])
})

test('Workspace publisher discards hidden/reduced navigation instead of replaying it', async ({page}) => {
  await freezeClock(page)
  await openBehavior(page)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  await page.getByRole('button', {name: 'Change workspace'}).click()
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => true})
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(eye).not.toHaveClass(/kaisEyeMotion/)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingFrames())).toBe(0)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingTimers())).toBe(0)
  await page.evaluate(() => {
    Reflect.deleteProperty(document, 'hidden')
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.clock.runFor(16)
  await expect(eye).toHaveAttribute('data-reaction', 'none')
  await page.emulateMedia({reducedMotion: 'reduce'})
  await page.getByRole('button', {name: 'Change workspace'}).click()
  await page.clock.runFor(16)
  await page.emulateMedia({reducedMotion: 'no-preference'})
  await expect(eye).toHaveAttribute('data-reaction', 'none')
  await page.getByRole('button', {name: 'Change workspace'}).click()
  await page.clock.runFor(16)
  await expect(eye).toHaveAttribute('data-reaction', 'navigation')
})

test('Mouse samples are coalesced into one frame and normalized gaze stays inside -1..1', async ({page}) => {
  await openBehavior(page)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  const before = await page.evaluate(() => window.kaisEyeProbe.frameRequests)
  await page.evaluate(() => {
    for (let index = 0; index < 100; index++) {
      window.dispatchEvent(new PointerEvent('pointermove', {pointerType: 'mouse', clientX: 10000, clientY: -10000}))
    }
  })

  await expect(eye).toHaveAttribute('data-look-x', '1')
  await expect(eye).toHaveAttribute('data-look-y', '-1')
  expect(await page.evaluate(() => window.kaisEyeProbe.frameRequests)).toBe(before + 1)
  await expect(eye.locator('.kaisEyeGaze')).toHaveCSS('transition-duration', '0.12s')
  await page.evaluate(() => window.dispatchEvent(new PointerEvent('pointermove', {pointerType: 'mouse', clientX: -10000, clientY: 10000})))
  await expect(eye).toHaveAttribute('data-look-x', '-1')
  await expect(eye).toHaveAttribute('data-look-y', '1')
  await expect(eye.locator('.kaisEyeGaze')).toHaveCSS('transform', 'matrix(1, 0, 0, 1, -3, 2)')
})

test('Ten simulated idle minutes keep heap, listeners and scheduled work bounded; hidden/unmount stop everything', async ({page}, testInfo) => {
  await freezeClock(page)
  await openBehavior(page)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  const listeners = await page.evaluate(() => window.kaisEyeProbe.details())
  const timers = await page.evaluate(() => window.kaisEyeProbe.pendingTimers())
  const session = await page.context().newCDPSession(page)
  await session.send('HeapProfiler.collectGarbage')
  const before = await session.send('Runtime.getHeapUsage')
  expect(before.usedSize).toBeGreaterThan(0)
  await page.clock.runFor(600000)
  await expect(eye).toHaveAttribute('data-attention', 'drowsy')
  expect(await page.evaluate(() => window.kaisEyeProbe.details())).toEqual(listeners)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingTimers())).toBeLessThanOrEqual(timers)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingFrames())).toBe(0)
  await session.send('HeapProfiler.collectGarbage')
  const after = await session.send('Runtime.getHeapUsage')
  const maximumGrowth = 2 * 1024 * 1024
  await testInfo.attach('kais-10-minute-heap', {body: JSON.stringify({
    simulatedIdleMilliseconds: 600000, beforeBytes: before.usedSize, afterBytes: after.usedSize,
    growthBytes: after.usedSize - before.usedSize, maximumGrowthBytes: maximumGrowth,
    listenersBefore: listeners.length, listenersAfter: await page.evaluate(() => window.kaisEyeProbe.listeners()),
    timersBefore: timers, timersAfter: await page.evaluate(() => window.kaisEyeProbe.pendingTimers()),
  }, null, 2), contentType: 'application/json'})
  expect(after.usedSize - before.usedSize).toBeLessThanOrEqual(maximumGrowth)
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => true})
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(eye).not.toHaveClass(/kaisEyeMotion/)
  expect(await eye.evaluate(element => element.getAnimations({subtree: true}).length)).toBe(0)
  expect(await page.evaluate(() => window.kaisEyeProbe.interactions())).toBe(0)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingTimers())).toBe(0)
  await page.getByRole('button', {name: 'Unmount eye', exact: true}).evaluate(button => {
    if (!(button instanceof HTMLButtonElement)) throw new Error('Expected unmount button')
    button.click()
  })
  await expect.poll(() => page.evaluate(() => window.kaisEyeProbe.listeners())).toBe(0)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingFrames())).toBe(0)
  await session.detach()
})

test('Private focus, reduced motion and hidden tabs suspend interaction listeners and every animation', async ({page}) => {
  await openBehavior(page)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  await page.getByLabel('Eye state').selectOption('thinking')
  await page.getByLabel('Unread', {exact: true}).check()
  await expect(eye.locator('.kaisEyeRotor')).toHaveCSS('animation-name', 'kais-eye-spin')
  await expect(eye.locator('.kaisEyePulse')).toHaveCSS('animation-name', 'kais-eye-heartbeat')
  await expect(eye.locator('.kaisEyeRingGlow')).toHaveCSS('animation-name', 'kais-eye-unread')
  await page.getByLabel('Private field', {exact: true}).focus()
  await expect(eye).not.toHaveClass(/kaisEyeMotion/)
  await page.getByLabel('Eye state').focus()
  await expect(eye).toHaveClass(/kaisEyeMotion/)
  await page.emulateMedia({reducedMotion: 'reduce'})
  await expect(eye).not.toHaveClass(/kaisEyeMotion|kaisEyeBlink|kaisEyeEngaged/)
  await expect(eye.locator('.kaisEyeRotor')).toHaveCSS('animation-name', 'none')
  await expect(eye.locator('.kaisEyePulse')).toHaveCSS('animation-name', 'none')
  await expect(eye.locator('.kaisEyeRingGlow')).toHaveCSS('animation-name', 'none')
  await expect(eye.locator('.kaisEyeGaze')).toHaveCSS('transition-duration', '0s')
  await page.evaluate(() => window.dispatchEvent(new PointerEvent('pointermove', {pointerType: 'mouse', clientX: 10000, clientY: 10000})))
  await expect(eye).toHaveAttribute('data-look-x', '0')
  await expect(eye).toHaveAttribute('data-look-y', '0')
  await page.emulateMedia({reducedMotion: 'no-preference'})
  await expect(eye).toHaveClass(/kaisEyeMotion/)
  const listeners = await page.evaluate(() => window.kaisEyeProbe.details())
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => true})
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(eye).not.toHaveClass(/kaisEyeMotion|kaisEyeBlink|kaisEyeEngaged/)
  await expect.poll(() => page.evaluate(() => window.kaisEyeProbe.interactions())).toBe(0)
  expect(await eye.evaluate(element => element.getAnimations({subtree: true}).length)).toBe(0)
  await page.evaluate(() => {
    Reflect.deleteProperty(document, 'hidden')
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(eye).toHaveClass(/kaisEyeMotion/)
  await expect.poll(() => page.evaluate(() => window.kaisEyeProbe.details())).toEqual(listeners)
})

test('Unmount cleans listeners and queued frames, including React StrictMode remounts', async ({page}) => {
  await openBehavior(page)
  const mountedCount = await page.evaluate(() => window.kaisEyeProbe.listeners())
  expect(mountedCount).toBeGreaterThan(0)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingTimers())).toBe(3)
  await page.getByRole('button', {name: 'Unmount eye', exact: true}).evaluate(button => {
    if (!(button instanceof HTMLButtonElement)) throw new Error('Expected unmount button')
    window.dispatchEvent(new PointerEvent('pointermove', {pointerType: 'mouse', clientX: 10000, clientY: 10000}))
    button.click()
  })
  await expect.poll(() => page.evaluate(() => window.kaisEyeProbe.listeners())).toBe(0)
  await expect.poll(() => page.evaluate(() => window.kaisEyeProbe.pendingFrames())).toBe(0)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingTimers())).toBe(0)
  expect(await page.evaluate(() => window.kaisEyeProbe.canceledFrames)).toBeGreaterThan(0)
  await page.getByRole('button', {name: 'Mount eye', exact: true}).click()
  await expect.poll(() => page.evaluate(() => window.kaisEyeProbe.listeners())).toBe(mountedCount)
  await page.getByRole('button', {name: 'Unmount eye', exact: true}).click()
  await expect.poll(() => page.evaluate(() => window.kaisEyeProbe.listeners())).toBe(0)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingTimers())).toBe(0)
})

for (const random of [0, 1]) {
  test(`Blink and glance respect the ${random === 0 ? 'minimum' : 'maximum'} random intervals`, async ({page}) => {
    await page.clock.install({time: new Date('2026-01-01T00:00:00Z')})
    await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'))
    await page.addInitScript(value => {Math.random = () => value}, random)
    await openBehavior(page)
    const eye = page.getByTestId('behavior-eye').locator('svg')
    const blinkAt = random === 0 ? 3000 : 6000
    const glanceAt = random === 0 ? 30000 : 60000
    await page.clock.runFor(blinkAt - 1)
    await expect(eye).not.toHaveClass(/kaisEyeBlink/)
    await page.clock.runFor(1)
    await expect(eye).toHaveClass(/kaisEyeBlink/)
    await expect(eye.locator('.kaisEyeUpperLid')).toHaveCSS('animation-duration', '0.15s')
    await expect(eye.locator('.kaisEyeLowerLid')).toHaveCSS('animation-duration', '0.15s')
    await page.clock.runFor(150)
    await expect(eye).not.toHaveClass(/kaisEyeBlink/)
    await page.clock.runFor(glanceAt - blinkAt - 151)
    await expect(eye).toHaveAttribute('data-look-x', '0')
    await page.clock.runFor(1)
    await expect(eye).toHaveAttribute('data-look-x', random === 0 ? '-0.6' : '0.6')
    await page.clock.runFor(500)
    await expect(eye).toHaveAttribute('data-look-x', '0')
  })
}

test('Hover and press open the eye and brighten the ring; off/error stay motionless', async ({page}) => {
  await openBehavior(page)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  await eye.hover()
  await expect(eye).toHaveClass(/kaisEyeEngaged/)
  await expect(eye.locator('.kaisEyeOpening')).toHaveCSS('transform', 'matrix(1, 0, 0, 1.12, 0, 0)')
  await expect(eye.locator('.kaisEyeRingGlow')).toHaveCSS('opacity', '0.65')
  await page.mouse.move(500, 500)
  await expect(eye).not.toHaveClass(/kaisEyeEngaged/)
  await eye.dispatchEvent('pointerdown', {pointerType: 'touch', clientX: 50, clientY: 50})
  await expect(eye).toHaveClass(/kaisEyeEngaged/)
  for (const state of ['error', 'off']) {
    await page.getByLabel('Eye state').selectOption(state)
    await expect(eye).not.toHaveClass(/kaisEyeMotion|kaisEyeBlink|kaisEyeEngaged/)
    await expect(eye).toHaveAttribute('data-look-x', '0')
    expect(await eye.evaluate(element => element.getAnimations({subtree: true}).length)).toBe(0)
  }
})

test('Privacy combines focus and inherited desktop hover, delays reopening 400 ms and cancels on reentry', async ({page}) => {
  await page.clock.install({time: new Date('2026-01-01T00:00:00Z')})
  await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'))
  await openBehavior(page)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  const privateField = page.getByLabel('Private field', {exact: true})
  const normal = page.getByLabel('Normal field', {exact: true})
  await privateField.hover()
  await expect(eye).toHaveAttribute('data-state', 'private')
  await expect(eye).toHaveAttribute('aria-label', labels.private)
  await expect(eye).not.toHaveClass(/kaisEyeMotion/)
  await expect(eye.locator('.kaisEyeOpening')).toHaveCSS('transition-duration', '0.15s')
  await privateField.focus()
  await normal.hover()
  await page.clock.runFor(500)
  await expect(eye).toHaveAttribute('data-state', 'private')
  await normal.focus()
  await page.clock.runFor(399)
  await expect(eye).toHaveAttribute('data-state', 'private')
  await page.clock.runFor(1)
  await expect(eye).toHaveAttribute('data-state', 'idle')
  await privateField.hover()
  await normal.focus()
  await page.clock.runFor(500)
  await expect(eye).toHaveAttribute('data-state', 'private')
  await normal.hover()
  await page.clock.runFor(200)
  await page.getByLabel('Private password', {exact: true}).focus()
  await page.clock.runFor(500)
  await expect(eye).toHaveAttribute('data-state', 'private')
  await normal.focus()
  await page.clock.runFor(400)
  await expect(eye).toHaveAttribute('data-state', 'idle')
})

test('Privacy reads only exact markers, not values; normal and false-marked fields remain open under reduced motion', async ({page}) => {
  const errors: string[] = []
  const requests: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {if (new URL(request.url()).pathname.startsWith('/api/')) requests.push(request.url())})
  await openBehavior(page)
  await page.emulateMedia({reducedMotion: 'reduce'})
  const eye = page.getByTestId('behavior-eye').locator('svg')
  for (const label of ['Normal field', 'False marker']) {
    await page.getByLabel(label, {exact: true}).hover()
    await page.getByLabel(label, {exact: true}).focus()
    await expect(eye).toHaveAttribute('data-state', 'idle')
  }
  const privateField = page.getByLabel('Private field', {exact: true})
  await privateField.evaluate(element => {
    Object.defineProperty(element, 'value', {get() {throw new Error('Privacy hook read a field value')}})
  })
  await privateField.focus()
  await expect(eye).toHaveAttribute('data-state', 'private')
  await expect(eye).not.toHaveClass(/kaisEyeMotion|kaisEyeTransitions|kaisEyeBlink|kaisEyeEngaged/)
  await expect(eye.locator('.kaisEyeOpening')).toHaveCSS('transition-duration', '0s')
  await expect(eye.locator('.kaisEyeOpening')).toHaveCSS('transform', 'matrix(1, 0, 0, 0.03, 0, 0)')
  expect(await eye.evaluate(element => element.getAnimations({subtree: true}).length)).toBe(0)
  expect(errors).toEqual([])
  expect(requests).toEqual([])
})

test('Focused marker changes and private target removal are observed without a new focus event', async ({page}) => {
  await page.clock.install({time: new Date('2026-01-01T00:00:00Z')})
  await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'))
  await openBehavior(page)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  const normal = page.getByLabel('Normal field', {exact: true})
  await normal.focus()
  await normal.evaluate(element => element.setAttribute('data-private', 'true'))
  await expect(eye).toHaveAttribute('data-state', 'private')
  await normal.evaluate(element => element.setAttribute('data-private', 'false'))
  await page.clock.runFor(399)
  await expect(eye).toHaveAttribute('data-state', 'private')
  await page.clock.runFor(1)
  await expect(eye).toHaveAttribute('data-state', 'idle')
  const privateField = page.getByLabel('Private field', {exact: true})
  await privateField.focus()
  await expect(eye).toHaveAttribute('data-state', 'private')
  await privateField.evaluate(element => element.remove())
  await page.clock.runFor(400)
  await expect(eye).toHaveAttribute('data-state', 'idle')
})

test('Privacy owns one document listener set and cleans pending reopening timers on hidden tabs and unmount', async ({page}) => {
  await openBehavior(page)
  const details = await page.evaluate(() => window.kaisEyeProbe.details())
  expect(details.filter(value => value === 'focusin:focusIn')).toHaveLength(1)
  expect(details.filter(value => value === 'pointerover:pointerOver')).toHaveLength(1)
  const eye = page.getByTestId('behavior-eye').locator('svg')
  await page.getByLabel('Private field', {exact: true}).focus()
  await expect(eye).toHaveAttribute('data-state', 'private')
  await page.getByLabel('Normal field', {exact: true}).focus()
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingTimers())).toBe(1)
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', {configurable: true, get: () => true})
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect.poll(() => page.evaluate(() => window.kaisEyeProbe.interactions())).toBe(0)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingTimers())).toBe(0)
  await page.getByLabel('Private password', {exact: true}).focus()
  await page.evaluate(() => {
    Reflect.deleteProperty(document, 'hidden')
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(eye).toHaveAttribute('data-state', 'private')
  await page.getByRole('button', {name: 'Unmount eye', exact: true}).click()
  await expect.poll(() => page.evaluate(() => window.kaisEyeProbe.listeners())).toBe(0)
  expect(await page.evaluate(() => window.kaisEyeProbe.pendingTimers())).toBe(0)
})

test.describe('Touch', () => {
  test.use({isMobile: true, hasTouch: true, viewport: {width: 390, height: 844}})
  test('A touch looks briefly at the tapped point, returns to center and ignores mouse moves', async ({page}) => {
    await page.clock.install({time: new Date('2026-01-01T00:00:00Z')})
    await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'))
    await openBehavior(page)
    const eye = page.getByTestId('behavior-eye').locator('svg')
    await page.evaluate(() => window.dispatchEvent(new PointerEvent('pointermove', {pointerType: 'mouse', clientX: 10000, clientY: 10000})))
    await expect(eye).toHaveAttribute('data-look-x', '0')
    await page.evaluate(() => window.dispatchEvent(new PointerEvent('pointerdown', {pointerType: 'touch', clientX: 10000, clientY: -10000})))
    await page.clock.runFor(20)
    await expect(eye).toHaveAttribute('data-look-x', '1')
    await expect(eye).toHaveAttribute('data-look-y', '-1')
    await page.clock.runFor(650)
    await expect(eye).toHaveAttribute('data-look-x', '0')
    await expect(eye).toHaveAttribute('data-look-y', '0')
  })
  test('Mobile ignores hover privacy but closes on password focus and reopens after leaving', async ({page}) => {
    await openBehavior(page)
    const eye = page.getByTestId('behavior-eye').locator('svg')
    const password = page.getByLabel('Private password', {exact: true})
    await password.dispatchEvent('pointerover', {pointerType: 'mouse'})
    await expect(eye).toHaveAttribute('data-state', 'idle')
    await password.tap()
    await expect(eye).toHaveAttribute('data-state', 'private')
    await expect(eye.locator('[data-layer="privacy"]')).toHaveAttribute('visibility', 'visible')
    await page.getByLabel('Normal field', {exact: true}).tap()
    await expect(eye).toHaveAttribute('data-state', 'idle')
  })
})
