import {expect, test} from '@playwright/test'
import {readFileSync} from 'node:fs'
import {PUBLIC_POLICIES, publicPolicyForPath} from '../../compliance-content'
import {mockAssistant} from './helpers/assistant-api'
import {PUBLIC_PAGE_METADATA,publicPageSchema} from '../../public-page-metadata'

test('home page publishes complete KaisTrade metadata before JavaScript in both entrypoints and builds', async ({request,page}) => {
  const title = 'KaisTrade | AI-Powered Crypto Trading Platform'
  const description = "Explore KaisTrade's AI-powered crypto analysis, market scanning and trading tools, with demo trading and risk controls for informed market decisions."
  for (const base of ['http://127.0.0.1:4173', 'http://127.0.0.1:4175', 'http://127.0.0.1:4176', 'http://127.0.0.1:4177']) {
    const response = await request.get(base)
    expect(response.status()).toBe(200)
    const html = await response.text()
    expect(html).toContain(`<title>${title}</title>`)
    expect(html).toContain('<meta property="og:site_name" content="KaisTrade"')
    const metadata = await page.evaluate(html => {
      const parsed = new DOMParser().parseFromString(html,'text/html')
      return {
        title:parsed.title,
        canonical:Array.from(parsed.querySelectorAll('link[rel="canonical"]'),element => element.getAttribute('href')),
        schemas:Array.from(parsed.querySelectorAll('script[type="application/ld+json"]'),element => JSON.parse(element.textContent || '')),
        icons:Array.from(parsed.querySelectorAll('link[rel="icon"],link[rel="apple-touch-icon"]'),element => ({
          href:element.getAttribute('href'),type:element.getAttribute('type'),sizes:element.getAttribute('sizes'),
        })),
        meta:Object.fromEntries(Array.from(parsed.querySelectorAll('meta[name],meta[property]'),element => [
          element.getAttribute('name') || element.getAttribute('property'),element.getAttribute('content'),
        ])),
      }
    },html)
    expect(metadata.title).toBe(title)
    expect(metadata.canonical).toEqual(['https://kaistrade.com/'])
    expect(metadata.meta).toMatchObject({
      description,
      'og:site_name':'KaisTrade','og:type':'website','og:title':title,'og:description':description,
      'og:url':'https://kaistrade.com/','og:image':'https://kaistrade.com/og-image.png',
      'og:image:alt':'KaisTrade crypto trading platform',
      'twitter:card':'summary_large_image','twitter:title':title,'twitter:description':description,
      'twitter:image':'https://kaistrade.com/og-image.png','twitter:image:alt':'KaisTrade crypto trading platform',
    })
    expect(metadata.meta.robots).toBeUndefined()
    const image = await request.get(`${base}/og-image.png`)
    expect(image.status()).toBe(200)
    expect(image.headers()['content-type']).toMatch(/^image\/png\b/i)
    expect(await image.body()).toEqual(readFileSync(new URL('../public/og-image.png',import.meta.url)))
    expect(metadata.schemas).toEqual([{
      '@context': 'https://schema.org', '@type': 'WebSite',
      name: 'KaisTrade', alternateName: 'Kais Trade', url: 'https://kaistrade.com/',
    }])
    expect(metadata.icons).toHaveLength(6)
    expect(metadata.icons).toContainEqual({href:'/favicon-48x48.png',type:'image/png',sizes:'48x48'})
    expect(metadata.icons).toContainEqual({href:'/favicon-512x512.png',type:'image/png',sizes:'512x512'})
    for (const icon of metadata.icons) {
      if (!icon.href) throw new Error('Icon URL missing')
      const response = await request.get(base+icon.href)
      expect(response.status()).toBe(200)
      expect(response.headers()['content-type']).toMatch(icon.href.endsWith('.ico') ? /^image\/(x-icon|vnd\.microsoft\.icon)\b/i : /^image\/png\b/i)
      expect(await response.body()).toEqual(readFileSync(new URL('../public'+icon.href,import.meta.url)))
    }
    const logo = await request.get(`${base}/kaistrade-logo.png`)
    expect(logo.status()).toBe(200)
    expect(logo.headers()['content-type']).toMatch(/^image\/png\b/i)
    expect(await logo.body()).toEqual(readFileSync(new URL('../public/kaistrade-logo.png',import.meta.url)))
  }
})

for (const policy of ['privacy', 'terms', 'risk'] as const) {
  test(`${policy} is public in both entry points and the production build without auth requests`, async ({page,request}) => {
    await page.route('https://**', route => route.abort())
    await page.routeWebSocket('**/*', socket => socket.close())
    const requests:string[] = []
    await page.route('**/api/**', async route => {
      requests.push(route.request().url())
      await route.fulfill({status:401, json:{detail:'Not authenticated'}})
    })
    for (const base of ['http://127.0.0.1:4173', 'http://127.0.0.1:4175', 'http://127.0.0.1:4176']) {
      for (const suffix of ['', '/']) {
        const metadata = PUBLIC_PAGE_METADATA[policy]
        const raw = await request.get(`${base}/${policy}${suffix}`)
        expect(raw.status()).toBe(200)
        const head = await page.evaluate(html => {
          const parsed = new DOMParser().parseFromString(html,'text/html')
          return {
            title:parsed.title,
            description:Array.from(parsed.querySelectorAll('meta[name="description"]'),node => node.getAttribute('content')),
            canonical:Array.from(parsed.querySelectorAll('link[rel="canonical"]'),node => node.getAttribute('href')),
            schemas:Array.from(parsed.querySelectorAll('script[type="application/ld+json"]'),node => JSON.parse(node.textContent || '')),
            robots:parsed.querySelector('meta[name="robots"]')?.getAttribute('content'),
          }
        },await raw.text())
        expect(head).toEqual({title:metadata.title,description:[metadata.description],canonical:[metadata.url],schemas:[publicPageSchema(policy)],robots:undefined})
        const response = await page.goto(`${base}/${policy}${suffix}`)
        expect(response?.status()).toBe(200)
        await expect(page.getByRole('heading', {name:PUBLIC_POLICIES[policy].title, exact:true})).toBeVisible()
        for (const paragraph of PUBLIC_POLICIES[policy].paragraphs) {
          await expect(page.getByRole('article')).toContainText(paragraph)
        }
        await expect(page.locator('input[type="password"]')).toHaveCount(0)
        await expect(page.getByRole('button', {name:'Google ile devam et', exact:true})).toHaveCount(0)
        await expect(page).toHaveTitle(`${PUBLIC_POLICIES[policy].title} | KaisTrade`)
        await expect(page.locator('meta[name="description"]')).toHaveAttribute('content',metadata.description)
        await expect(page.locator('link[rel="canonical"]')).toHaveAttribute('href',metadata.url)
        await expect(page.locator('meta[property="og:title"],meta[name="twitter:title"]')).toHaveCount(2)
        for (const selector of ['meta[property="og:title"]','meta[name="twitter:title"]']) {
          await expect(page.locator(selector)).toHaveAttribute('content',metadata.title)
        }
        for (const selector of ['meta[property="og:description"]','meta[name="twitter:description"]']) {
          await expect(page.locator(selector)).toHaveAttribute('content',metadata.description)
        }
        await expect(page.locator('meta[property="og:url"]')).toHaveAttribute('content',metadata.url)
        await expect(page.locator('link[rel="icon"][href="/favicon-96x96.png"]')).toHaveCount(1)
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

test('SPA policy changes and unmount restore every head value without duplicate metadata', async ({page,request}) => {
  const base = 'http://127.0.0.1:4173'
  const shell = await (await request.get(base+'/')).text()
  expect(shell).toContain('/src/main.tsx')
  await page.route(base+'/metadata-navigation', route =>
    route.fulfill({contentType:'text/html',body:shell.replace('/src/main.tsx','/tests/fixtures/public-page-metadata.tsx')}))
  await page.route('https://**', route => route.abort())
  await page.routeWebSocket('**/*', socket => socket.close())
  await page.goto(base+'/metadata-navigation')
  for (const pageName of ['privacy','risk','terms','home'] as const) {
    await page.getByRole('button',{name:pageName,exact:true}).click()
    const metadata = PUBLIC_PAGE_METADATA[pageName]
    await expect(page).toHaveTitle(metadata.title)
    await expect(page.locator('meta[name="description"]')).toHaveAttribute('content',metadata.description)
    await expect(page.locator('link[rel="canonical"]')).toHaveAttribute('href',metadata.url)
    for (const selector of ['meta[property="og:title"]','meta[name="twitter:title"]']) {
      await expect(page.locator(selector)).toHaveAttribute('content',metadata.title)
    }
    for (const selector of ['meta[property="og:description"]','meta[name="twitter:description"]']) {
      await expect(page.locator(selector)).toHaveAttribute('content',metadata.description)
    }
    await expect(page.locator('meta[property="og:url"]')).toHaveAttribute('content',metadata.url)
    await expect(page.locator('script[type="application/ld+json"]')).toHaveCount(1)
    expect(await page.locator('script[type="application/ld+json"]').textContent()).toBe(JSON.stringify(publicPageSchema(pageName)))
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
  await expect(page.locator('.authWordmark')).toHaveText('KaisTrade')
  await expect(page.locator('.authLogoHeading img')).toHaveAttribute('alt','KaisTrade')
  const badges = page.locator('.authTrustBadges')
  await expect(badges).toBeVisible()
  await expect(badges).toContainText('Hesap yönetimi')
  await expect(badges).toContainText('E-posta ile giriş')
  await expect(badges).toContainText('Risk bilgilendirmesi')
  await expect(badges).not.toContainText(/256-bit|2 adımlı|7\/24|24\/7|2FA/)
})
