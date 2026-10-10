import {expect,test} from '@playwright/test'
import {readFileSync} from 'node:fs'
import rootDeployment from '../../vercel.json' with {type:'json'}
import frontendDeployment from '../vercel.json' with {type:'json'}
import {PUBLIC_POLICIES,publicPolicyForPath} from '../../compliance-content'

const expectedUrls = ['https://kaistrade.com/','https://kaistrade.com/privacy','https://kaistrade.com/terms','https://kaistrade.com/risk']
const publicDirectory = new URL('../public/',import.meta.url)
const sitemapSource = readFileSync(new URL('sitemap.xml',publicDirectory),'utf8')
const robotsSource = readFileSync(new URL('robots.txt',publicDirectory),'utf8')

function fallbackPattern(source:string) {
  const prefix = '/:path('
  expect(source.startsWith(prefix)).toBe(true)
  expect(source.endsWith(')')).toBe(true)
  return new RegExp(`^/${source.slice(prefix.length,-1)}$`)
}

test('crawler allowlist permits the homepage but not login variants or private application routes',() => {
  const directives = robotsSource.split(/\r?\n/).filter(line => /^(Allow|Disallow): /.test(line))
  expect(directives).toEqual([
    'Disallow: /','Allow: /$','Allow: /privacy$','Allow: /terms$','Allow: /risk$',
    'Allow: /assets/','Allow: /kaistrade-logo.png$','Allow: /og-image.png$','Allow: /sitemap.xml$','Allow: /robots.txt$',
  ])
  const allowedPaths = directives.filter(line => line.startsWith('Allow: ')).map(line => line.slice(7))
  const allowed = (path:string) => allowedPaths.some(rule => rule.endsWith('$') ? path === rule.slice(0,-1) : path.startsWith(rule))
  for (const path of ['/','/privacy','/terms','/risk','/assets/app.js','/og-image.png']) expect(allowed(path)).toBe(true)
  for (const path of ['/login','/register','/profile','/admin','/settings','/master-trade','/verify-email','/api/v22/session','/api/v22/auth/google/callback','/?google_login=success','/?token=private']) {
    expect(allowed(path)).toBe(false)
  }
})

test('Vercel serves SEO files outside the SPA fallback without changing API or OAuth routing',() => {
  expect(rootDeployment.rewrites[0]).toEqual({
    source:'/api/:path*',destination:'https://protrebot-rkpt.onrender.com/api/:path*',
  })
  expect(rootDeployment.env).toEqual({VITE_API_BASE:'/api'})
  expect(frontendDeployment.env).toEqual({VITE_API_URL:'https://protrebot-rkpt.onrender.com'})
  expect(rootDeployment.headers.find(rule => rule.source === '/(.*)')?.headers).toEqual([
    {key:'Content-Security-Policy',value:"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self' https://api.binance.com wss://stream.binance.com:9443; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; upgrade-insecure-requests"},
    {key:'Strict-Transport-Security',value:'max-age=31536000; includeSubDomains'},
    {key:'X-Content-Type-Options',value:'nosniff'},
    {key:'X-Frame-Options',value:'DENY'},
    {key:'Referrer-Policy',value:'no-referrer'},
    {key:'Permissions-Policy',value:'camera=(), microphone=(), geolocation=(), payment=()'},
  ])
  for (const deployment of [rootDeployment,frontendDeployment]) {
    const fallback = deployment.rewrites.at(-1)
    if (!fallback) throw new Error('SPA fallback missing')
    expect(fallback.destination).toBe('/index.html')
    const pattern = fallbackPattern(fallback.source)
    for (const path of ['/sitemap.xml','/robots.txt']) expect(pattern.test(path)).toBe(false)
    for (const path of ['/','/privacy','/terms','/risk','/profile','/verify-email','/master-trade','/api/v22/auth/google/callback']) {
      expect(pattern.test(path)).toBe(true)
    }
    expect(deployment.headers.find(rule => rule.source === '/sitemap.xml')?.headers)
      .toEqual([{key:'Content-Type',value:'application/xml; charset=utf-8'}])
    expect(deployment.headers.find(rule => rule.source === '/robots.txt')?.headers)
      .toEqual([{key:'Content-Type',value:'text/plain; charset=utf-8'}])
  }
  const resolveRootRoute = (pathname:string) => {
    for (const rule of rootDeployment.rewrites) {
      if (rule.source === '/api/:path*') {
        if (pathname.startsWith('/api/')) return rule.destination.replace(':path*',pathname.slice('/api/'.length))
      } else if (fallbackPattern(rule.source).test(pathname)) return rule.destination
    }
    return null
  }
  expect(resolveRootRoute('/api/v22/auth/google/callback')).toBe('https://protrebot-rkpt.onrender.com/api/v22/auth/google/callback')
  expect(resolveRootRoute('/api/v22/session')).toBe('https://protrebot-rkpt.onrender.com/api/v22/session')
  expect(resolveRootRoute('/verify-email')).toBe('/index.html')
  expect(resolveRootRoute('/sitemap.xml')).toBeNull()
  expect(resolveRootRoute('/robots.txt')).toBeNull()
})

for (const base of ['http://127.0.0.1:4173','http://127.0.0.1:4175','http://127.0.0.1:4176','http://127.0.0.1:4177']) {
  test(`${base} sitemap and robots are real HTTP static files with public-only links`,async ({page,request}) => {
    await page.route('**/*',route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.fallback() : route.abort())
    await page.routeWebSocket('**/*',socket => socket.close())
    const authRequests:string[] = []
    await page.route('**/api/**',route => {
      authRequests.push(route.request().url())
      return route.fulfill({status:401,json:{detail:'Not authenticated'}})
    })
    const sitemap = await request.get(`${base}/sitemap.xml`)
    expect(sitemap.status()).toBe(200)
    expect(sitemap.headers()['content-type']).toMatch(/^(application|text)\/xml\b/i)
    const xml = await sitemap.text()
    expect(xml).toBe(sitemapSource)
    expect(xml).not.toMatch(/<!doctype html|<html[\s>]|<div[^>]*id=["']root/i)
    const robots = await request.get(`${base}/robots.txt`)
    expect(robots.status()).toBe(200)
    expect(robots.headers()['content-type']).toMatch(/^text\/plain\b/i)
    expect(await robots.text()).toBe(robotsSource)
    expect(robotsSource).not.toMatch(/<!doctype html|<html[\s>]/i)
    await page.goto(`${base}/sitemap.xml`)
    const parsed = await page.evaluate(xml => {
      const documentXml = new DOMParser().parseFromString(xml,'application/xml')
      return {
        errors:documentXml.getElementsByTagName('parsererror').length,
        namespace:documentXml.documentElement.namespaceURI,
        root:documentXml.documentElement.localName,
        urls:Array.from(documentXml.getElementsByTagNameNS('http://www.sitemaps.org/schemas/sitemap/0.9','loc'),element => element.textContent),
      }
    },xml)
    expect(parsed).toEqual({errors:0,namespace:'http://www.sitemaps.org/schemas/sitemap/0.9',root:'urlset',urls:expectedUrls})
    for (const url of expectedUrls) {
      const {pathname} = new URL(url)
      if (pathname === '/') {
        const home = await request.get(base+'/')
        expect(home.status()).toBe(200)
        expect(await home.text()).toContain('<title>KaisTrade | AI-Powered Crypto Trading Platform</title>')
        continue
      }
      const policy = publicPolicyForPath(pathname)
      if (!policy) throw new Error(`Sitemap contains a non-public route: ${pathname}`)
      const response = await page.goto(base+pathname)
      expect(response?.status()).toBe(200)
      await expect(page.getByRole('heading',{name:PUBLIC_POLICIES[policy].title,exact:true})).toBeVisible()
      await expect(page.locator('input[type="password"]')).toHaveCount(0)
    }
    expect(authRequests).toEqual([])
    expect(robotsSource).toContain('Disallow: /')
    for (const url of expectedUrls) expect(robotsSource).toContain(`Allow: ${new URL(url).pathname}$`)
    expect(robotsSource).toContain('Allow: /assets/')
    expect(robotsSource).toContain('Sitemap: https://kaistrade.com/sitemap.xml')
    expect(robotsSource).not.toMatch(/\/api\/|\/admin|\/profile|\/settings|[?&]token=/i)
  })
}
