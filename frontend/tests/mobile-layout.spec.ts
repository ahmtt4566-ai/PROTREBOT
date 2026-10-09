import {test, expect} from '@playwright/test'
import {mobileGeometry, mobileSurfaces, openMobileSurface} from './helpers/mobile-layout'

for (const width of [320, 390, 768]) for (const surface of mobileSurfaces) {
  test(`mobile ${surface.name} remains readable at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 844})
    const errors: string[] = []
    const mutations: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('request', request => {
      if (new URL(request.url()).pathname.startsWith('/api/') && !['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
        mutations.push(`${request.method()} ${new URL(request.url()).pathname}`)
      }
    })
    await openMobileSurface(page, surface)
    const geometry = await mobileGeometry(page)
    await testInfo.attach('mobile-geometry', {body: JSON.stringify(geometry, null, 2), contentType: 'application/json'})
    expect(geometry.documentWidth).toBeLessThanOrEqual(width)
    expect(geometry.bodyWidth).toBeLessThanOrEqual(width)
    expect(geometry.clipped).toEqual([])
    expect(geometry.overlaps).toEqual([])
    expect(geometry.undersized).toEqual([])
    expect(geometry.smallInputs).toEqual([])
    expect(geometry.metricOverflow).toEqual([])
    expect(errors).toEqual([])
    expect(mutations).toEqual(surface.name === 'owner-access' ? ['POST /api/web/access/logout'] : [])
    if (surface.name === 'demo-original') {
      const panel = page.getByRole('region', {name: 'Kais Original Demo profili'})
      await expect(panel.locator('dl > div').filter({has: page.getByText('Original profil anahtarı', {exact: true})}).locator('dd')).toHaveText('Kapalı')
      await expect(panel.locator('dl > div').filter({has: page.getByText('Original emir izni', {exact: true})}).locator('dd')).toHaveText('Kapalı')
      await expect(panel.locator('dl > div').filter({has: page.getByText('DEMO arm', {exact: true})}).locator('dd')).toHaveText('Kapalı')
      await expect(panel.getByRole('button', {name: 'Emirsiz tek karar döngüsü', exact: true})).toBeDisabled()
    }
    if (process.env.MOBILE_LAYOUT_SCREENSHOTS === '1') {
      await page.screenshot({path: testInfo.outputPath(`${surface.name}-${width}.png`), fullPage: true})
    }
  })
}

for (const viewport of [{width: 360, height: 640}, {width: 412, height: 915}, {width: 844, height: 390}]) {
  test(`mobile profile dialog fits ${viewport.width}x${viewport.height}`, async ({page}) => {
    await page.setViewportSize(viewport)
    const surface = mobileSurfaces.find(item => item.name === 'profile-dialog')
    expect(surface).toBeDefined()
    if (!surface) throw new Error('Missing profile dialog fixture')
    await openMobileSurface(page, surface)
    const dialog = page.locator('.accountDialog')
    const box = await dialog.boundingBox()
    expect(box).not.toBeNull()
    if (!box) throw new Error('Profile dialog has no layout box')
    expect(box.x).toBeGreaterThanOrEqual(0)
    expect(box.y).toBeGreaterThanOrEqual(0)
    expect(box.x + box.width).toBeLessThanOrEqual(viewport.width)
    expect(box.y + box.height).toBeLessThanOrEqual(viewport.height)
    await dialog.getByRole('button', {name: 'Pencereyi kapat', exact: true}).scrollIntoViewIfNeeded()
    expect((await mobileGeometry(page)).documentWidth).toBeLessThanOrEqual(viewport.width)
  })
}
