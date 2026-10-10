import {expect, test} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'
import {openChatFromHint} from './helpers/assistant-ui'

test.use({baseURL: 'http://127.0.0.1:4175'})

for (const width of [1440, 768, 390, 320]) {
  test(`Home header uses enlarged real dimensions without overlap at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 844})
    await page.emulateMedia({reducedMotion: 'reduce'})
    await mockAssistant(page)
    await page.goto('/')
    const header = page.locator('.v26HomeHeader')
    const profile = header.getByRole('button', {name: 'Profil menüsünü aç', exact: true})
    await expect(profile).toBeVisible()
    await expect(header.getByRole('button', {name: 'Piyasa verisini yenile'})).toBeEnabled()
    await expect(page.locator('.v26OfflineNotice')).toHaveCount(0)
    await page.evaluate(() => document.fonts.ready)
    await header.locator('.v26BrandLogo img').evaluate((image: HTMLImageElement) => image.decode())

    const geometry = await header.evaluate(element => {
      const rect = (selector: string) => {
        const node = element.querySelector(selector)
        if (!node) throw new Error(`Missing header element: ${selector}`)
        const box = node.getBoundingClientRect()
        return {left: box.left, right: box.right, top: box.top, bottom: box.bottom,
          width: box.width, height: box.height, transform: getComputedStyle(node).transform}
      }
      return {
        header: element.getBoundingClientRect().toJSON(),
        logo: rect('.v26BrandLogo img'),
        controls: ['.assistantLauncher', '.v26Refresh', '.v26NotificationButton', '.authMemberTrigger'].map(rect),
        icons: ['.assistantLauncher > svg', '.v26Refresh > svg', '.v26NotificationButton > svg', '.authProfileGlyph'].map(rect),
        gap: Number.parseFloat(getComputedStyle(element.querySelector('.v26HeaderActions')!).gap),
      }
    })
    expect(geometry.header.left).toBeGreaterThanOrEqual(0)
    expect(geometry.header.right).toBeLessThanOrEqual(width)
    expect(geometry.header.height).toBeCloseTo(width === 1440 ? 89.8 : width === 768 ? 85.8 : 81.8, 1)
    const content = await page.locator('.v26Dashboard').boundingBox()
    expect(content).not.toBeNull()
    expect(content!.y).toBeCloseTo(width === 1440 ? 127.8 : width === 768 ? 99.8 : 89.8, 1)
    expect(geometry.logo.width).toBeCloseTo(width > 480 ? 302.4 : width === 390 ? 216 : 132, 1)
    expect(geometry.logo.transform).toBe('none')
    expect(geometry.gap).toBe(width <= 700 ? 9.6 : 7.2)
    for (const [index, control] of geometry.controls.entries()) {
      expect(control.width).toBeGreaterThanOrEqual(44)
      expect(control.height).toBeGreaterThanOrEqual(44)
      expect(control.left).toBeGreaterThanOrEqual(0)
      expect(control.right).toBeLessThanOrEqual(width)
      expect(control.top).toBeGreaterThanOrEqual(geometry.header.top)
      expect(control.bottom).toBeLessThanOrEqual(geometry.header.bottom)
      expect(control.transform).toBe('none')
      if (index > 0) expect(control.left).toBeGreaterThanOrEqual(geometry.controls[index - 1].right)
      const icon = geometry.icons[index]
      const expectedSize = index === 0 ? 67.2 : index === 3 ? width === 768 ? 30 : 21.6 : 20.4
      expect(icon.width).toBeCloseTo(expectedSize, 1)
      expect(icon.height).toBeCloseTo(expectedSize, 1)
      expect((icon.left + icon.right) / 2).toBeCloseTo((control.left + control.right) / 2, 1)
      expect((icon.top + icon.bottom) / 2).toBeCloseTo((control.top + control.bottom) / 2, 1)
    }
    await expect(header.locator('.assistantLauncher > svg')).toHaveCSS('overflow', 'hidden')
    await expect(header.locator('.v26HomeHeaderStatus')).toHaveText('ÇEVRİMİÇİ')
    await expect(header.locator('.v26HomeHeaderStatus')).toHaveCSS('display', width === 1440 ? 'flex' : 'none')
    await expect(header.locator('.v26HomeHeaderStatus')).toHaveCSS('font-size', '9px')
    await expect(header.locator('.v26HomeHeaderStatus > i')).toHaveCSS('width', '8px')
    await expect(header.locator('.v26HomeHeaderStatus > i')).toHaveCSS('height', '8px')

    const inkFits = await header.locator('.v26BrandLogo img').evaluate((image: HTMLImageElement) => {
      const canvas = document.createElement('canvas')
      canvas.width = image.naturalWidth; canvas.height = image.naturalHeight
      const context = canvas.getContext('2d')
      if (!context || !image.parentElement) throw new Error('Logo geometry unavailable')
      context.drawImage(image, 0, 0)
      const {data} = context.getImageData(0, 0, canvas.width, canvas.height)
      let left = canvas.width, right = -1, top = canvas.height, bottom = -1
      for (let y = 0; y < canvas.height; y++) for (let x = 0; x < canvas.width; x++) {
        const offset = (y * canvas.width + x) * 4
        if (data[offset + 3] > 8 && Math.max(data[offset], data[offset + 1], data[offset + 2]) > 40) {
          left = Math.min(left, x); right = Math.max(right, x)
          top = Math.min(top, y); bottom = Math.max(bottom, y)
        }
      }
      const imageBox = image.getBoundingClientRect()
      const frame = image.parentElement.getBoundingClientRect()
      const scale = Math.min(imageBox.width / canvas.width, imageBox.height / canvas.height)
      const x = imageBox.left + (imageBox.width - canvas.width * scale) / 2
      const y = imageBox.top + (imageBox.height - canvas.height * scale) / 2
      return right >= left && x + left * scale >= frame.left &&
        x + (right + 1) * scale <= frame.right && y + top * scale >= frame.top &&
        y + (bottom + 1) * scale <= frame.bottom
    })
    expect(inkFits).toBe(true)

    await profile.hover()
    await expect.poll(async () => {
      const box = await profile.boundingBox()
      return box ? box.x + box.width : Infinity
    }).toBeLessThanOrEqual(width)
  })

  test(`Greeting and chat remain aligned with the enlarged home eye at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 844})
    await page.emulateMedia({reducedMotion: 'reduce'})
    const state = await mockAssistant(page)
    await page.goto('/')
    await expect(page.locator('.v26OfflineNotice')).toHaveCount(0)
    await page.evaluate(() => document.fonts.ready)
    const launcher = page.getByRole('button', {name: 'Kais AI', exact: true})
    const bubble = page.locator('[data-kais-greeting]')
    await expect(page.locator('.v26OfflineNotice')).toHaveCount(0)
    await page.evaluate(() => document.fonts.ready)
    await expect(bubble).toBeVisible()
    const anchor = await launcher.boundingBox()
    const greeting = await bubble.boundingBox()
    expect(anchor).not.toBeNull(); expect(greeting).not.toBeNull()
    expect(greeting!.x).toBeGreaterThanOrEqual(16)
    expect(greeting!.x + greeting!.width).toBeLessThanOrEqual(width - 16)
    const eyeBox = (await launcher.locator('.kaisEye').boundingBox())!
    expect(greeting!.y).toBeCloseTo(eyeBox.y + eyeBox.height + 10, 1)
    expect(greeting!.width).toBe(width <= 480 ? width - 32 : 300)
    const greetingArrow = await bubble.evaluate(element => Number.parseFloat(getComputedStyle(element).getPropertyValue('--kais-greeting-arrow')))
    expect(greeting!.x + greetingArrow).toBeCloseTo(anchor!.x + anchor!.width / 2, 1)
    await openChatFromHint(page)
    const dialog = page.getByRole('dialog', {name: 'Kais AI', exact: true})
    await expect(dialog).toBeVisible()
    await expect(dialog.locator('.assistantHeader > .kaisHeaderEye')).toHaveCSS('width', '36px')
    const panel = await dialog.boundingBox()
    expect(panel).not.toBeNull()
    expect(panel!.x).toBeGreaterThanOrEqual(0)
    expect(panel!.x + panel!.width).toBeLessThanOrEqual(width)
    expect(panel!.y + panel!.height).toBeLessThanOrEqual(844)
    if (width >= 768) {
      expect(panel!.y).toBeGreaterThanOrEqual(anchor!.y + anchor!.height)
      const arrow = await dialog.evaluate(element => Number.parseFloat(getComputedStyle(element).getPropertyValue('--assistant-arrow-left')))
      expect(panel!.x + arrow).toBeCloseTo(anchor!.x + anchor!.width / 2, 1)
    } else {
      expect(panel!.width).toBe(width)
      expect(panel!.height).toBe(633)
    }
    expect(state.chats).toHaveLength(0)
    expect(state.confirmations).toHaveLength(0)
  })
}
