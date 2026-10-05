import {expect, test} from '@playwright/test'
import {PUBLIC_POLICIES, publicPolicyForPath} from '../../compliance-content'
import {mockAssistant} from './helpers/assistant-api'

test('home page publishes the KaisTrade site name before JavaScript in both entrypoints and production', async ({request}) => {
  for (const base of ['http://127.0.0.1:4173', 'http://127.0.0.1:4175', 'http://127.0.0.1:4176']) {
    const response = await request.get(base)
    expect(response.status()).toBe(200)
    const html = await response.text()
    expect(html).toContain('<title>KaisTrade</title>')
    expect(html).toContain('<meta property="og:site_name" content="KaisTrade"')
    const schema = html.match(/<script\b[^>]*type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/)?.[1]
    if (!schema) throw new Error('Home page is missing its WebSite structured data')
    expect(JSON.parse(schema)).toEqual({
      '@context': 'https://schema.org', '@type': 'WebSite',
      name: 'KaisTrade', url: 'https://kaistrade.com/',
    })
  }
})

for (const policy of ['privacy', 'terms', 'risk'] as const) {
  test(`${policy} is public in both entry points and the production build without auth requests`, async ({page}) => {
    const requests:string[] = []
    await page.route('**/api/**', async route => {
      requests.push(route.request().url())
      await route.fulfill({status:401, json:{detail:'Not authenticated'}})
    })
    for (const base of ['http://127.0.0.1:4173', 'http://127.0.0.1:4175', 'http://127.0.0.1:4176']) {
      for (const suffix of ['', '/']) {
        const response = await page.goto(`${base}/${policy}${suffix}`)
        expect(response?.status()).toBe(200)
        await expect(page.getByRole('heading', {name:PUBLIC_POLICIES[policy].title, exact:true})).toBeVisible()
        for (const paragraph of PUBLIC_POLICIES[policy].paragraphs) {
          await expect(page.getByRole('article')).toContainText(paragraph)
        }
        await expect(page.locator('input[type="password"]')).toHaveCount(0)
        await expect(page.getByRole('button', {name:'Google ile devam et', exact:true})).toHaveCount(0)
        await expect(page).toHaveTitle(`${PUBLIC_POLICIES[policy].title} | KaisTrade`)
      }
    }
    expect(requests).toEqual([])
  })
}

test('public policy path matching does not bypass gates for other routes', () => {
  for (const path of ['/', '/register', '/profile', '/admin', '/privacy/extra', '/privacy//', '/Privacy', '/api/v22/session']) {
    expect(publicPolicyForPath(path)).toBeNull()
  }
})

test('ordinary login remains protected and badges do not claim encryption strength, 2FA or 24/7 support', async ({page}) => {
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('wss://**', socket => socket.close())
  const state = await mockAssistant(page)
  state.sessionStatus = 401
  state.profileStatus = 401
  await page.goto('http://127.0.0.1:4175/')
  await expect(page.getByRole('button', {name:'Google ile devam et', exact:true})).toBeEnabled()
  await expect(page.locator('.authCard input[autocomplete="current-password"]')).toBeVisible()
  const badges = page.locator('.authTrustBadges')
  await expect(badges).toBeVisible()
  await expect(badges).toContainText('Hesap yönetimi')
  await expect(badges).toContainText('E-posta ile giriş')
  await expect(badges).toContainText('Risk bilgilendirmesi')
  await expect(badges).not.toContainText(/256-bit|2 adımlı|7\/24|24\/7|2FA/)
})
