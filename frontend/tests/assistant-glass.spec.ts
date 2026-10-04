import {expect, test} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'
import {assistantCopy} from '../../ui-copy'

test.use({baseURL: 'http://127.0.0.1:4174'})

for (const width of [1440, 768, 390, 320]) {
  test(`Glass panel structure, settings, chips, copy and composer at ${width}px`, async ({page, context}) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
    await page.setViewportSize({width, height: 844})
    await page.emulateMedia({reducedMotion: 'reduce'})
    const state = await mockAssistant(page)
    state.chatBody = {reply: 'Salt okunur platform yanıtı.', language: 'tr', sources: []}
    await page.goto('/')
    const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
    await launcher.click()
    const panel = page.getByRole('dialog', {name: 'Kais AI', exact: true})
    const input = panel.getByRole('textbox', {name: assistantCopy.tr.input, exact: true})
    await expect(input).toBeFocused()
    await expect(panel.locator('.assistantState')).toHaveText('Çevrimiçi')
    await expect(panel.getByRole('button', {name: assistantCopy.tr.clear, exact: true})).toBeVisible()
    await expect(panel.locator('.assistantSubtitle')).toHaveText(assistantCopy.tr.subtitle)
    const progress = panel.getByRole('progressbar', {name: assistantCopy.tr.usageProgress})
    await expect(progress).toHaveAttribute('max', '20')
    await expect(progress).toHaveAttribute('value', '19')
    const box = await panel.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(width)
    expect(box!.y + box!.height).toBeLessThanOrEqual(844)
    expect(box!.width).toBe(width >= 768 ? 400 : width)
    expect(box!.height).toBe(width >= 768 ? 600 : 633)
    await expect(panel).toHaveCSS('backdrop-filter', 'blur(14px)')
    await expect(panel).toHaveCSS('animation-name', 'none')
    const welcome = panel.locator('.assistantEmpty')
    expect(await welcome.evaluate(element => element.scrollHeight <= element.clientHeight)).toBe(true)
    const welcomeBox = await welcome.boundingBox()
    const messagesBox = await panel.locator('.assistantMessages').boundingBox()
    expect(welcomeBox!.y + welcomeBox!.height).toBeLessThanOrEqual(messagesBox!.y + messagesBox!.height)
    const suggestions = panel.locator('.assistantSuggestions')
    await expect(suggestions.getByRole('button')).toHaveText(assistantCopy.tr.questions)
    await expect(suggestions).toHaveCSS('flex-wrap', 'nowrap')
    const rows = await suggestions.getByRole('button').evaluateAll(elements => elements.map(element => element.getBoundingClientRect().y))
    expect(new Set(rows).size).toBe(1)
    expect(await suggestions.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true)

    const settings = panel.getByRole('button', {name: assistantCopy.tr.settings, exact: true})
    await expect(panel.getByRole('checkbox', {name: assistantCopy.tr.proactiveSetting})).toHaveCount(0)
    await settings.click()
    const menu = panel.locator('.assistantSettingsMenu')
    await expect(menu).toBeVisible()
    const menuBox = await menu.boundingBox()
    expect(menuBox!.x).toBeGreaterThanOrEqual(0)
    expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(width)
    const checkbox = menu.getByRole('checkbox', {name: assistantCopy.tr.proactiveSetting, exact: true})
    const enabledResponse = page.waitForResponse(response => response.url().endsWith('/api/assistant/proactive/preferences') &&
      response.request().method() === 'POST' && response.request().postDataJSON().enabled === true)
    await checkbox.click()
    await (await enabledResponse).finished()
    await expect(checkbox).toBeChecked()
    await expect(checkbox).toBeEnabled()
    expect(state.preferenceSaves.map(call => call.body)).toEqual([{enabled: true}])
    const disabledResponse = page.waitForResponse(response => response.url().endsWith('/api/assistant/proactive/preferences') &&
      response.request().method() === 'POST' && response.request().postDataJSON().enabled === false)
    await checkbox.click()
    await (await disabledResponse).finished()
    await expect(checkbox).not.toBeChecked()
    await expect(checkbox).toBeEnabled()
    expect(state.preferenceSaves.map(call => call.body)).toEqual([{enabled: true}, {enabled: false}])
    await page.keyboard.press('Escape')
    await expect(menu).toHaveCount(0)
    await expect(settings).toBeFocused()
    await expect(panel).toBeVisible()

    await expect(panel.locator('#assistant-length')).toBeHidden()
    const height = (await input.boundingBox())!.height
    await input.fill('First\nSecond\nThird\nFourth\nFifth')
    await expect.poll(async () => (await input.boundingBox())!.height).toBeGreaterThan(height)
    expect((await input.boundingBox())!.height).toBeLessThanOrEqual(120)
    await input.fill('?'.repeat(64))
    await expect(panel.locator('#assistant-length')).toBeVisible()
    await expect(panel.locator('#assistant-length')).toHaveText('64/80')
    await input.fill('Platform kullanımı')
    await expect(panel.locator('#assistant-length')).toBeHidden()
    await input.focus()
    await expect(input).toHaveCSS('outline-color', 'rgb(66, 229, 166)')
    await expect(panel.locator('#assistant-secret svg')).toBeVisible()
    let release: () => void = () => {}
    state.hold = new Promise<void>(resolve => {release = resolve})
    await panel.getByRole('button', {name: assistantCopy.tr.send, exact: true}).click()
    await expect(panel.locator('.assistantTyping')).toContainText('Yazıyor...')
    await expect(panel.locator('.assistantTypingDots i')).toHaveCount(3)
    await expect(panel.locator('.assistantTypingDots i').first()).toHaveCSS('animation-name', 'none')
    await expect(suggestions).toHaveCount(0)
    release()
    await expect(panel.locator('.assistantMessage.assistant')).toHaveCount(1)
    await expect(panel.locator('.assistantTyping')).toHaveCount(0)
    await expect(input).toHaveValue('')
    await expect.poll(async () => (await input.boundingBox())!.height).toBe(height)
    await panel.getByRole('button', {name: assistantCopy.tr.copyReply, exact: true}).click()
    await expect(panel.locator('.assistantCopyRow')).toContainText(assistantCopy.tr.copied)
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('Salt okunur platform yanıtı.')
    expect(state.confirmations).toHaveLength(0)
    await page.mouse.click(2, 2)
    await expect(panel).not.toBeVisible()
    await expect(launcher).toBeFocused()
  })
}

for (const width of [1440, 390]) {
  test(`Opening and typing animate only without reduced motion at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 844})
    await page.emulateMedia({reducedMotion: 'no-preference'})
    const state = await mockAssistant(page)
    let release: () => void = () => {}
    state.hold = new Promise<void>(resolve => {release = resolve})
    await page.goto('/')
    await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
    const panel = page.getByRole('dialog', {name: 'Kais AI', exact: true})
    await expect(panel).toHaveCSS('animation-name', width >= 768 ? 'assistant-popover-in' : 'assistant-sheet-in')
    await panel.getByRole('textbox').fill('Platform kullanımı')
    await panel.getByRole('button', {name: assistantCopy.tr.send, exact: true}).click()
    const dots = panel.locator('.assistantTypingDots i').first()
    await expect(dots).toHaveCSS('animation-name', 'assistant-pulse')
    await page.emulateMedia({reducedMotion: 'reduce'})
    await expect(panel).toHaveCSS('animation-name', 'none')
    await expect(dots).toHaveCSS('animation-name', 'none')
    release()
  })
}

test('Opaque fallback remains readable when backdrop support rule is unavailable', async ({page}) => {
  await mockAssistant(page)
  await page.goto('/')
  await page.evaluate(() => {
    for (const sheet of Array.from(document.styleSheets)) {
      for (let index = sheet.cssRules.length - 1; index >= 0; index--) {
        const rule = sheet.cssRules[index]
        if (rule instanceof CSSSupportsRule && rule.conditionText.includes('backdrop-filter') &&
            rule.cssText.includes('.assistantDialog')) sheet.deleteRule(index)
      }
    }
  })
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  const panel = page.getByRole('dialog', {name: 'Kais AI', exact: true})
  await expect(panel).toHaveCSS('background-color', 'rgb(12, 24, 32)')
  await expect(panel).toHaveCSS('backdrop-filter', 'none')
  await expect(panel).toHaveCSS('color', 'rgb(238, 249, 250)')
})

test('Clipboard denial is explicitly surfaced without affecting chat or saving feedback', async ({page}) => {
  const state = await mockAssistant(page)
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {value: {writeText: async () => {
      throw new DOMException('Permission denied', 'NotAllowedError')
    }}, configurable: true})
  })
  await page.goto('/')
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  const panel = page.getByRole('dialog', {name: 'Kais AI', exact: true})
  await panel.getByRole('textbox').fill('Platform kullanımı')
  await panel.getByRole('button', {name: assistantCopy.tr.send, exact: true}).click()
  await panel.getByRole('button', {name: assistantCopy.tr.copyReply, exact: true}).click()
  await expect(panel.locator('.assistantCopyRow')).toContainText(assistantCopy.tr.copyFailed)
  expect(state.chats).toHaveLength(1)
  expect(await page.evaluate(() => localStorage.getItem('kais-chat:v1:assistant-member'))).not.toContain(assistantCopy.tr.copyFailed)
})
