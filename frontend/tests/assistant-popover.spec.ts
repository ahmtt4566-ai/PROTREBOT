import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

test.use({baseURL: 'http://127.0.0.1:4174'})

async function prepare(page: Page) {
  await page.addInitScript(() => {
    sessionStorage.setItem('protrebot-v25-session', 'assistant-popover-test-session')
    sessionStorage.setItem('protrebot-kais-launcher-seen', '1')
  })
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    const user = {id: 'assistant-popover-test-member', role: 'CUSTOMER', active: true, email_verified: true}
    const json = path === '/api/v22/session' || path === '/api/v22/profile'
      ? {user, access: {canAccessMasterTrade: true, isPremium: false}}
      : path === '/api/assistant/usage' ? {remaining: 19, total: 20, resetsAt: '2026-10-04T00:00:00Z',
        limits: {max_input_chars: 1000, history_messages: 10, history_message_max_chars: 1000, page_context_max_chars: 1000, secret_min_alphanumeric_chars: 40}}
      : path === '/api/assistant/proactive/preferences' ? {enabled: false, available: true, poll_interval_seconds: 60}
      : path === '/api/assistant/chat' ? {reply: 'UI fixture yanıtı.', language: 'tr', sources: ['get_plans']}
      : path === '/api/health' ? {status: 'ok'} : {}
    await route.fulfill({json})
  })
  await page.goto('/')
  await expect(page.getByRole('button', {name: 'Kais AI', exact: true})).toBeVisible()
}

for (const width of [1440, 390]) {
  test(`Popover opens, focuses composer, closes outside and returns focus at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 844})
    await page.emulateMedia({reducedMotion: 'reduce'})
    await prepare(page)
    const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
    const anchor = await launcher.boundingBox()
    await page.screenshot({path: testInfo.outputPath(`kais-popover-closed-${width}.png`)})
    await launcher.click()
    const dialog = page.getByRole('dialog', {name: 'Kais AI', exact: true})
    await expect(dialog.getByRole('textbox', {name: 'Kais AI mesajın'})).toBeFocused()
    await expect(dialog.locator('.assistantHeader > .kaisHeaderEye')).toHaveCSS('width', '36px')
    await expect(dialog.getByRole('button', {name: 'Kais AI sohbetini temizle'})).toBeVisible()
    const box = await dialog.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.y).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(width)
    expect(box!.y + box!.height).toBeLessThanOrEqual(844)
    if (width >= 768) {
      expect(box!.width).toBeCloseTo(400, 0)
      expect(box!.height).toBeLessThanOrEqual(600)
      expect(box!.y).toBeGreaterThanOrEqual(anchor!.y + anchor!.height)
      const arrow = await dialog.evaluate(element => Number.parseFloat(getComputedStyle(element).getPropertyValue('--assistant-arrow-left')))
      expect(box!.x + arrow).toBeCloseTo(anchor!.x + anchor!.width / 2, 0)
      expect(await dialog.evaluate(element => getComputedStyle(element, '::before').display)).not.toBe('none')
    } else {
      expect(box!.width).toBeCloseTo(390, 0)
      expect(box!.height).toBeCloseTo(844 * .75, 0)
      expect(box!.y).toBeCloseTo(844 * .25, 0)
      expect(await dialog.evaluate(element => getComputedStyle(element, '::before').display)).toBe('none')
    }
    const ids = await page.locator('.assistantLauncher clipPath, .assistantHeader clipPath').evaluateAll(elements => elements.map(element => element.id))
    expect(new Set(ids).size).toBe(2)
    await page.screenshot({path: testInfo.outputPath(`kais-popover-open-${width}.png`)})
    await page.keyboard.press('Escape')
    await expect(dialog).not.toBeVisible()
    await expect(launcher).toBeFocused()
    await launcher.click()
    await expect(dialog).toBeVisible()
    await page.mouse.click(2, 2)
    await expect(dialog).not.toBeVisible()
    await expect(launcher).toBeFocused()
  })
}

test('Small desktop viewport contains the popover and keeps the composer visible', async ({page}) => {
  await page.setViewportSize({width: 800, height: 420})
  await page.emulateMedia({reducedMotion: 'reduce'})
  await prepare(page)
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: 'Kais AI', exact: true})
  await expect(dialog.getByRole('textbox', {name: 'Kais AI mesajın'})).toBeFocused()
  const box = await dialog.boundingBox()
  const composer = await dialog.locator('.assistantComposer').boundingBox()
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.y).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(800)
  expect(box!.y + box!.height).toBeLessThanOrEqual(420)
  expect(composer!.y + composer!.height).toBeLessThanOrEqual(box!.y + box!.height)
})

test('New title eye tracks and blinks, while reduced motion disables popover animation', async ({page}) => {
  await page.clock.install({time: new Date('2026-01-01T00:00:00Z')})
  await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'))
  await page.emulateMedia({reducedMotion: 'no-preference'})
  await prepare(page)
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: 'Kais AI', exact: true})
  await expect(dialog.getByRole('textbox', {name: 'Kais AI mesajın'})).toBeFocused()
  const eye = dialog.locator('.assistantHeader > .kaisHeaderEye')
  await page.evaluate(() => window.dispatchEvent(new PointerEvent('pointermove', {pointerType: 'touch', clientX: 0, clientY: 0})))
  await page.clock.runFor(32)
  expect(Number(await eye.getAttribute('data-look-x'))).toBeLessThan(0)
  await page.clock.runFor(2969)
  await expect(eye).toHaveClass(/kaisEyeBlink/)
  await expect(eye.locator('.kaisEyeInterior')).toHaveCSS('animation-duration', '0.2s')
  await page.emulateMedia({reducedMotion: 'reduce'})
  await expect(dialog).toHaveCSS('animation-name', 'none')
  await expect(eye).not.toHaveClass(/kaisEyeMotion/)
  await expect(eye).toHaveAttribute('data-look-x', '0')
})

for (const [width, height] of [[800, 420], [1280, 600], [1440, 900], [768, 844], [390, 844], [320, 844]]) {
  test(`Fixed header and composer stay inside ${width}x${height}, including keyboard viewport`, async ({page}) => {
    await page.setViewportSize({width, height})
    await page.emulateMedia({reducedMotion: 'reduce'})
    const state = await mockAssistant(page)
    state.chatBody = {reply: 'Uzun platform yanıtı.\n'.repeat(80), language: 'tr', sources: []}
    await page.goto('/')
    await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
    const dialog = page.getByRole('dialog', {name: 'Kais AI', exact: true})
    const input = dialog.getByRole('textbox', {name: 'Kais AI mesajın', exact: true})
    const send = dialog.getByRole('button', {name: 'Kais AI mesajını gönder', exact: true})
    const assertContained = async (top: number, bottom: number) => {
      const panel = (await dialog.boundingBox())!
      expect(panel.y).toBeGreaterThanOrEqual(top)
      expect(panel.y + panel.height).toBeLessThanOrEqual(bottom)
      for (const locator of [dialog.locator('.assistantHeader'), dialog.locator('.assistantComposer'), input, send]) {
        const box = (await locator.boundingBox())!
        expect(box.x).toBeGreaterThanOrEqual(panel.x)
        expect(box.x + box.width).toBeLessThanOrEqual(panel.x + panel.width)
        expect(box.y).toBeGreaterThanOrEqual(panel.y)
        expect(box.y + box.height).toBeLessThanOrEqual(panel.y + panel.height)
      }
    }
    await expect(input).toBeFocused()
    await assertContained(0, height)
    await input.fill('Platform kullanımı')
    await send.click()
    await expect(dialog.locator('.assistantMessage.assistant')).toHaveCount(1)
    await expect(dialog.locator('.assistantTyping')).toHaveCount(0)
    expect(await dialog.getByRole('log').evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true)
    await input.fill('Birinci\nİkinci\nÜçüncü\nDördüncü\nBeşinci')
    await assertContained(0, height)
    await page.evaluate(() => {
      const viewport = window.visualViewport!
      Object.defineProperties(viewport, {
        height: {configurable: true, value: 260},
        offsetTop: {configurable: true, value: 40},
      })
      viewport.dispatchEvent(new Event('resize'))
    })
    await expect(dialog).toHaveAttribute('data-compact-viewport', 'true')
    await expect.poll(async () => (await dialog.boundingBox())!.y + (await dialog.boundingBox())!.height).toBeLessThanOrEqual(300)
    await assertContained(40, 300)
    expect((await input.boundingBox())!.height).toBeLessThanOrEqual(80)
    const headerBefore = await dialog.locator('.assistantHeader').boundingBox()
    const composerBefore = await dialog.locator('.assistantComposer').boundingBox()
    await dialog.getByRole('log').evaluate(element => {element.scrollTop = 0})
    expect(await dialog.locator('.assistantHeader').boundingBox()).toEqual(headerBefore)
    expect(await dialog.locator('.assistantComposer').boundingBox()).toEqual(composerBefore)
  })
}

test('Content, secret warning and assistant response remain in the popover with one eye design', async ({page}) => {
  await page.emulateMedia({reducedMotion: 'reduce'})
  await prepare(page)
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: 'Kais AI', exact: true})
  const composer = dialog.getByRole('textbox', {name: 'Kais AI mesajın'})
  await expect(composer).toBeFocused()
  await expect(dialog).toContainText('Günlük mesaj hakkı: 19/20')
  await expect(dialog.getByRole('button', {name: 'Premium üyelik ne kadar?', exact: true})).toBeVisible()
  await composer.fill(`API Secret: ${'A'.repeat(40)}`)
  await expect(dialog.locator('#assistant-secret-warning')).toBeVisible()
  await composer.fill('Planım hakkında yardım')
  await dialog.getByRole('button', {name: 'Kais AI mesajını gönder'}).click()
  await expect(dialog.locator('.assistantMessage.assistant .assistantText')).toHaveText('UI fixture yanıtı.')
  await expect(dialog.locator('.assistantMessage.assistant > .kaisHeaderEye')).toHaveCSS('width', '24px')
})

test('Greeting opens at the top; only appended messages scroll down, not reopening or finishing a failed request', async ({page}, testInfo) => {
  await page.setViewportSize({width: 1440, height: 844})
  await page.emulateMedia({reducedMotion: 'reduce'})
  await prepare(page)
  const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
  await launcher.click()
  const dialog = page.getByRole('dialog', {name: 'Kais AI', exact: true})
  const composer = dialog.getByRole('textbox', {name: 'Kais AI mesajın'})
  const log = dialog.getByRole('log')
  await expect(composer).toBeFocused()
  await expect.poll(() => log.evaluate(element => element.scrollTop)).toBe(0)
  const greeting = await dialog.locator('.assistantEmpty h3').boundingBox()
  const viewport = await log.boundingBox()
  expect(greeting!.y).toBeGreaterThanOrEqual(viewport!.y)
  expect(greeting!.y + greeting!.height).toBeLessThanOrEqual(viewport!.y + viewport!.height)
  await page.screenshot({path: testInfo.outputPath('kais-greeting-top-1440.png')})
  let calls = 0
  let release = () => {}
  const held = new Promise<void>(resolve => { release = resolve })
  const reply = 'UI fixture uzun yanıtı.\n'.repeat(60)
  await page.route('**/api/assistant/chat', async route => {
    if (++calls === 1) {
      await route.fulfill({json: {reply, language: 'tr', sources: ['get_plans']}})
    } else {
      await held
      await route.fulfill({status: 503, json: {detail: 'UI fixture provider unavailable'}})
    }
  })
  await composer.fill('Planımı anlat')
  await dialog.getByRole('button', {name: 'Kais AI mesajını gönder'}).click()
  await expect(dialog.locator('.assistantMessage.assistant .assistantText')).toHaveText(reply.trim())
  const atBottom = () => log.evaluate(element => Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop) <= 1)
  await expect.poll(atBottom).toBe(true)
  await log.evaluate(element => { element.scrollTop = 0 })
  await page.keyboard.press('Escape')
  await launcher.click()
  await expect(composer).toBeFocused()
  await expect.poll(() => log.evaluate(element => element.scrollTop)).toBe(0)
  await composer.fill('Bir soru daha')
  await dialog.getByRole('button', {name: 'Kais AI mesajını gönder'}).click()
  await expect(dialog.locator('.assistantTyping')).toBeVisible()
  await expect.poll(atBottom).toBe(true)
  await log.evaluate(element => { element.scrollTop = 0 })
  release()
  await expect(dialog.locator('.assistantTyping')).toHaveCount(0)
  await expect.poll(() => log.evaluate(element => element.scrollTop)).toBe(0)
  await dialog.getByRole('button', {name: 'Kais AI sohbetini temizle'}).click()
  await expect(dialog.locator('.assistantEmpty')).toBeVisible()
  await expect.poll(() => log.evaluate(element => element.scrollTop)).toBe(0)
})
