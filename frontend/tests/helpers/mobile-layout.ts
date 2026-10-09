import {expect, type Page} from '@playwright/test'
import {mockAssistant} from './assistant-api'
import {previewAnalysis, previewCandles, previewMarkets} from './master-trade-preview-data'

export const mobileSurfaces = [
  {name: 'login', path: '/login', selector: '.authCard', public: true},
  {name: 'register', path: '/register', selector: '.authCard', public: true},
  {name: 'forgot', path: '/forgot-password', selector: '.authCard', public: true},
  {name: 'reset', path: '/reset-password?token=offline-layout-proof-token', selector: '.authCard', public: true},
  {name: 'verify', path: '/verify-email', selector: '.authCard', public: true},
  {name: 'mfa', path: '/login?mfa_challenge=offline-layout-challenge', selector: '.authCard', public: true},
  {name: 'owner-access', path: '/', selector: '.webAccessCard', ownerDenied: true},
  {name: 'terms', path: '/terms', selector: '.publicPolicyPage', public: true},
  {name: 'privacy', path: '/privacy', selector: '.publicPolicyPage', public: true},
  {name: 'risk-policy', path: '/risk', selector: '.publicPolicyPage', public: true},
  {name: 'dashboard', path: '/', selector: '.v26DashboardChoices'},
  {name: 'billing', path: '/billing', selector: '.subscriptionStatusCard'},
  {name: 'pricing', path: '/pricing', selector: '.subscriptionPricing'},
  {name: 'settings', path: '/settings', selector: '.accountWorkspace'},
  {name: 'master-analysis', path: '/master-trade?masterLayoutV2=1', selector: '.masterReferenceAnalysis'},
  {name: 'master-legacy', path: '/master-trade?masterLayoutV2=0', selector: '.masterTradeWorkspace'},
  {name: 'master-live', path: '/master-trade?masterLayoutV2=1&tab=canli', selector: '.masterTradeTabs'},
  {name: 'master-positions', path: '/master-trade?masterLayoutV2=1&tab=pozisyonlar', selector: '.masterTradeTabs'},
  {name: 'master-connection', path: '/master-trade?masterLayoutV2=1&tab=baglanti', selector: '.masterTradeTabs'},
  {name: 'trading', path: '/', button: 'İŞLEM', selector: '.binanceDemoDeck'},
  {name: 'demo-original', path: '/', button: 'İŞLEM', selector: '[aria-label="Kais Original Demo profili"]'},
  {name: 'analyst', path: '/', button: 'ANALİST', selector: '.coinAnalysisCenter'},
  {name: 'scanner', path: '/', button: 'TARAMA', selector: '.scannerCenter'},
  {name: 'performance', path: '/', button: 'PERFORMANS', selector: '.binanceDemoDeck'},
  {name: 'risk', path: '/', button: 'RİSK', selector: '.riskOnlyDeck'},
  {name: 'live', path: '/', button: 'CANLI', selector: '.liveTradingPanel'},
  {name: 'operations', path: '/', button: 'OPERASYON', selector: '.v27Ops'},
  {name: 'connection', path: '/', button: 'AYARLAR', selector: '.connectionCenter'},
  {name: 'assistant', path: '/', selector: '.assistantDialog'},
  {name: 'profile-dialog', path: '/settings', selector: '.accountDialog'},
] as const

export type MobileSurface = typeof mobileSurfaces[number]

export async function openMobileSurface(page: Page, surface: MobileSurface) {
  await page.emulateMedia({reducedMotion: 'reduce'})
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.fallback() : route.abort())
  await page.routeWebSocket('**/*', socket => socket.close())
  const state = await mockAssistant(page)
  if ('public' in surface) state.sessionStatus = 401
  if ('ownerDenied' in surface) state.ownerStatus = 401
  await page.addInitScript(() => sessionStorage.setItem('protrebot-v25-session', 'cookie-session:assistant-member'))
  await page.route('**/api/markets**', route => route.fulfill({json: previewMarkets}))
  await page.route('**/api/analysis/**', route => route.fulfill({json: {...previewAnalysis(), series: {ema20: [], ema50: [], ema200: []}}}))
  await page.route('**/api/klines/**', route => route.fulfill({json: previewCandles()}))
  await page.route('**/api/analyst/credits', route => route.fulfill({json: {
    remaining: 87, total: 100, analysis_cost: 10, resetsAt: '2027-01-01T00:00:00Z', unlimited: false,
  }}))
  await page.route('**/api/v22/subscription', route => route.fulfill({json: {
    status: 'ACTIVE', plan: 'MASTER_MODE', master_trade_access: true, current_period_end: '2027-01-01T00:00:00Z', next_payment_amount: 119.9,
  }}))
  await page.route('**/api/v22/subscription/history', route => route.fulfill({json: {items: []}}))
  await page.route('**/api/v21/original-v2/status', route => route.fulfill({json: {
    strategy_id: 'kais-original-v2-demo-v1', flags: {enabled: false, send_orders: false},
    selected_strategy_id: null, profile_hash: 'a'.repeat(64), policy_hash: 'a'.repeat(64), dry_run_plans: [],
  }}))
  await page.route('**/api/v22/account/overview', route => route.fulfill({json: {
    user: {
      id: state.userId, display_name: 'Uzun Kullanıcı Adı Mobil Görünüm Kontrolü', email: 'mobile-layout-only@example.test',
      role: 'CUSTOMER', active: true, email_verified: true, created_at: '2026-10-01T12:00:00Z',
      last_login: '2026-10-05T12:00:00Z', password_changed_at: null, auth_methods: ['password'],
    },
    subscription: {
      plan: 'FREE', status: 'NONE', expires_at: null, is_premium: false,
      features: [{key: 'scanner', label: 'Temel Scanner', included: true}, {key: 'auto', label: 'Auto Trade', included: false}],
    },
    preferences: {trading_mode: 'MANUAL', timeframe: '15m', exchange: 'BINANCE', risk_per_trade: 1, symbols: ['BTCUSDT', 'ETHUSDT']},
    security: {two_factor_enabled: false, active_sessions: 1, email_delivery_available: true, can_close_account: true, close_blocker: null},
    pending_email: null,
    sessions: [{
      id: 'current-session', current: true, device: 'Android', browser: 'Chrome', created_at: '2026-10-05T12:00:00Z',
      last_seen_at: '2026-10-05T12:10:00Z', expires_at: '2026-10-06T12:00:00Z',
    }],
    activity: [{id: 'login-event', kind: 'LOGIN', message: 'Hesaba giriş yapıldı', created_at: '2026-10-05T12:00:00Z'}],
  }}))
  await page.goto(`http://127.0.0.1:4176${surface.path}`, {waitUntil: 'domcontentloaded'})
  if ('button' in surface) {
    await expect(page.locator('.v26DashboardChoices')).toBeVisible()
    await page.locator('.v26DashboardChoices button').filter({
      has: page.locator('b', {hasText: new RegExp(`^${surface.button}$`)}),
    }).click()
  }
  if (surface.name === 'assistant') await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  if (surface.name === 'profile-dialog') await page.getByRole('button', {name: 'Profili düzenle', exact: true}).click()
  if (surface.name === 'demo-original') {
    await page.getByRole('button', {name: /OTOMASYON/}).first().click()
    await page.getByLabel('Demo stratejisi').selectOption('kais-original-v2-demo-v1')
  }
  await expect(page.locator(surface.selector)).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  return state
}

export async function mobileGeometry(page: Page) {
  return page.evaluate(() => {
    const clipped: string[] = []
    const overlaps: string[] = []
    const undersized: string[] = []
    const smallInputs: string[] = []
    const metricOverflow: string[] = []
    const describe = (element: HTMLElement) => `${element.tagName}.${element.className}: ${element.innerText.slice(0, 80)}`
    for (const element of document.querySelectorAll<HTMLElement>('button,h1,h2,h3,h4,label,summary')) {
      if (!element.checkVisibility() || element.closest('[aria-hidden="true"]')) continue
      const box = element.getBoundingClientRect()
      const css = getComputedStyle(element)
      if (box.width <= 0 || box.height <= 0) continue
      const screenReaderOnly = box.width <= 1 && box.height <= 1 && (css.clip !== 'auto' || css.clipPath !== 'none')
      if (!screenReaderOnly && element.scrollWidth > element.clientWidth + 2) clipped.push(describe(element))
    }
    for (const header of document.querySelectorAll<HTMLElement>('.v26ModeBar,.v26HomeHeader,.authCardHead,.subscriptionPageHeader,.accountPageHead')) {
      const children = Array.from(header.children).filter((element): element is HTMLElement =>
        element instanceof HTMLElement && element.checkVisibility())
      for (let a = 0; a < children.length; a++) for (let b = a + 1; b < children.length; b++) {
        const first = children[a].getBoundingClientRect()
        const second = children[b].getBoundingClientRect()
        if (Math.min(first.right, second.right) - Math.max(first.left, second.left) > 2 &&
            Math.min(first.bottom, second.bottom) - Math.max(first.top, second.top) > 2) {
          overlaps.push(`${describe(children[a])} / ${describe(children[b])}`)
        }
      }
    }
    const primaryControls = [
      '.authSubmit', '.authPassword button', '.authLinks button', '.authCheck',
      '.v26HeaderRefresh', '.v26HeaderBell', '.v26HeaderProfile', '.marketPulseChip',
      '.masterTradeRoute [role="tab"]', '.refMarketSearch input',
      '.refMarketSort select', '.refMarketFilters button', '.refMarketFavorite',
      '.demoHeroStop', '.connectionCenter .secretInput button',
    ].join(',')
    for (const element of document.querySelectorAll<HTMLElement>(primaryControls)) {
      if (!element.checkVisibility()) continue
      const box = element.getBoundingClientRect()
      if (box.height > 0 && box.height < 43.5) undersized.push(`${describe(element)} (${box.height}px)`)
    }
    for (const element of document.querySelectorAll<HTMLInputElement>('.authCard input:not([type="checkbox"]):not([type="radio"]),.accountWorkspace input,.connectionCenter input')) {
      if (element.checkVisibility() && parseFloat(getComputedStyle(element).fontSize) < 16) smallInputs.push(element.className || element.type)
    }
    for (const element of document.querySelectorAll<HTMLElement>('.refIndicatorGrid')) {
      if (element.checkVisibility() && element.scrollWidth > element.clientWidth) metricOverflow.push(describe(element))
    }
    return {
      viewport: innerWidth, documentWidth: document.documentElement.scrollWidth, bodyWidth: document.body.scrollWidth,
      clipped, overlaps, undersized, smallInputs, metricOverflow,
    }
  })
}
