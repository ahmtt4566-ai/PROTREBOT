import {expect, test, type Page} from '@playwright/test'
import {mkdir} from 'node:fs/promises'
import {join} from 'node:path'
import {mockAssistant} from './helpers/assistant-api'
import {previewAnalysis, previewCandles, previewMarkets} from './helpers/master-trade-preview-data'

const PRIVACY = 'Resmin sitemizde saklanmaz; analiz için yapay zeka sağlayıcımıza gönderilir.'
const FINAL = 'Genel yön\nGörünüm nötr.\n\nBu analiz yatırım tavsiyesi değildir.'

for (const mobile of [false, true]) test(`local design screenshots (${mobile ? 'mobile' : 'desktop'})`, async ({page}) => {
  const directory = process.env.CHART_AI_SCREENSHOT_DIR
  test.skip(!directory, 'Screenshots require an explicit local output directory.')
  if (!directory) return
  await mkdir(directory, {recursive: true})
  const state = await prepare(page, {mobile})
  const button = page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true})
  await expect(button).toBeEnabled()
  await button.scrollIntoViewIfNeeded()
  const name = mobile ? 'mobile' : 'desktop'
  await page.screenshot({path: join(directory, `${name}-button.png`), animations: 'disabled'})
  const dialog = await openAndCapture(page)
  await dialog.locator('.chartAiBody').evaluate(element => {element.scrollTop = 0})
  await page.screenshot({path: join(directory, `${name}-panel.png`), animations: 'disabled'})
  await dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true}).scrollIntoViewIfNeeded()
  await page.screenshot({path: join(directory, `${name}-panel-controls.png`), animations: 'disabled'})
  expect(state.generations).toHaveLength(0)
})

async function prepare(page: Page, options: {customer?: boolean; disabled?: boolean; unavailable?: boolean; processing?: boolean; failed?: boolean; legacy?: boolean; mobile?: boolean; getFailures?: number; unconfirmed?: boolean; deferUsage?: boolean; missingApi?: boolean} = {}) {
  await page.setViewportSize({width: options.mobile ? 390 : 1440, height: 900})
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort())
  await page.routeWebSocket(/^wss?:\/\//, socket => socket.close())
  await page.route('**/api/**', route => route.fulfill({status: 501, json: {detail: 'Offline fixture missing'}}))
  await mockAssistant(page)
  const user = {id: 'chart-owner-fixture', role: options.customer ? 'CUSTOMER' : 'OWNER', active: true, email_verified: true}
  await page.route('**/api/v22/{profile,session}', route => route.fulfill({json: {user, access: {canAccessMasterTrade: true, isPremium: true}}}))
  await page.route('**/api/v25/status', route => route.fulfill({json: {connected: false, account: {wallet_balance: 777777}, plans: [], events: []}}))
  await page.route('**/api/v25/risk/preview', route => route.fulfill({json: {ok: false, executable: false, warnings: ['Offline preview']}}))
  await page.route('**/api/exchange-connections/status', route => route.fulfill({json: {connections: {LIVE: {configured: false, active: false}}}}))
  await page.route('**/api/markets**', route => route.fulfill({json: previewMarkets}))
  await page.route('**/api/analysis-universe**', route => route.fulfill({json: {results: previewMarkets.map(market => ({...market, direction: 'LONG', confidence: 89, final_decision_score: 89}))}}))
  await page.route('**/api/analysis/**', route => route.fulfill({json: previewAnalysis(new URL(route.request().url()).pathname.split('/').pop())}))
  await page.route('**/api/klines/**', route => route.fulfill({json: previewCandles(new URL(route.request().url()).pathname.split('/').pop())}))
  const writes: string[] = []
  const generations: Array<Record<string, unknown>> = []
  let usageCalls = 0, accepted = false, gets = 0, key = ''
  let usageReplies = 0
  let releaseUsage: (() => void) | undefined
  const usageReady = options.deferUsage ? new Promise<void>(resolve => {releaseUsage = resolve}) : Promise.resolve()
  let complete = !options.processing
  await page.route('**/api/chart-ai/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/usage')) {
      usageCalls++
      await usageReady
      if (options.disabled || options.missingApi) {
        await route.fulfill(options.missingApi
          ? {status: 404, json: {detail: 'Not Found'}}
          : {status: 503, json: {detail: {code: 'feature_disabled', message: 'Kapalı'}}})
        usageReplies++
        return
      }
      if (options.customer) return route.fulfill({status: 403, json: {detail: {code: 'owner_only', message: 'Grafik AI şu anda yalnız OWNER için açık.'}}})
      return route.fulfill({json: {
        analysis_enabled: !options.unavailable,
        quota: {remaining: null, unlimited: true},
        acknowledgement: {required: !accepted, policy_version: 'chart-ai-v1', text: `${PRIVACY}\nBu analiz yatırım tavsiyesi değildir.`},
        availability: {message: options.unavailable ? 'Analiz servisi şu an kullanılamıyor.' : 'Gönderimde bütçe doğrulanır.'},
      }})
    }
    if (path.endsWith('/acknowledgement')) {
      expect(route.request().postDataJSON()).toEqual({policy_version: 'chart-ai-v1', accepted: true})
      accepted = !options.unconfirmed
      return route.fulfill({json: {acknowledgement: {required: false}, quota_consumed: false}})
    }
    if (path.endsWith('/analyze')) {
      const data: Record<string, unknown> = route.request().postDataJSON()
      generations.push(data)
      key = String(data.request_key)
      return route.fulfill({status: options.failed ? 502 : 200, json: {
        request_key: key, status: options.failed ? 'failed' : complete ? 'succeeded' : 'processing',
        result_text: options.failed || !complete ? null : FINAL,
        error: options.failed ? {code: 'response_unsafe', message: 'Yanıt güvenlik kontrolünü geçemedi; hakkın kullanılmadı.'} : null,
      }})
    }
    gets++
    if (gets <= (options.getFailures ?? 0)) return route.fulfill({status: 503, json: {error: {code: 'storage_unavailable', message: 'Sonuç durumu okunamadı.'}}})
    return route.fulfill({json: {request_key: key, status: complete ? 'succeeded' : 'processing', result_text: complete ? FINAL : null, error: null}})
  })
  page.on('request', request => {
    const path = new URL(request.url()).pathname
    if (request.method() !== 'GET' && path.startsWith('/api/')) writes.push(path)
  })
  await page.goto(`/master-trade?tab=analiz${options.legacy ? '&masterLayoutV2=0' : ''}`)
  await expect(page.locator(`svg[data-chart-ai-capture="${options.legacy ? 'legacy' : 'reference'}"]`)).toBeVisible()
  return {generations, writes, usageCalls: () => usageCalls, usageReplies: () => usageReplies,
    releaseUsage: () => {if (!releaseUsage) throw new Error('Usage response was not deferred'); releaseUsage()},
    gets: () => gets, finish: () => {complete = true}}
}

async function openAndCapture(page: Page) {
  await page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: 'Grafik AI', exact: true})
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', {name: 'Grafiği yakala', exact: true}).click()
  await expect(dialog.getByAltText('Gönderilecek grafik önizlemesi')).toBeVisible()
  await dialog.getByRole('checkbox').check()
  return dialog
}

test('feature off hides the OWNER control without producing an analysis', async ({page}) => {
  const state = await prepare(page, {disabled: true})
  await expect.poll(state.usageCalls).toBeGreaterThan(0)
  await expect(page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true})).toHaveCount(0)
  expect(state.generations).toHaveLength(0)
  await page.unroute('**/api/chart-ai/**')
})

for (const customer of [false, true]) test(`old backend without chart API never exposes the control (${customer ? 'CUSTOMER' : 'OWNER'})`, async ({page}) => {
  const response = page.waitForResponse(reply => new URL(reply.url()).pathname.endsWith('/chart-ai/usage'))
  const state = await prepare(page, {customer, missingApi: true})
  await (await response).finished()
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
  await expect.poll(state.usageReplies).toBeGreaterThan(0)
  await expect(page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true})).toHaveCount(0)
  expect(state.generations).toHaveLength(0)
})

for (const legacy of [false, true]) for (const mobile of [false, true]) {
  test(`CUSTOMER sees only a locked pilot notice (${legacy ? 'legacy' : 'reference'}, ${mobile ? 'mobile' : 'desktop'})`, async ({page}) => {
    const state = await prepare(page, {customer: true, legacy, mobile})
    await page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true}).click()
    const dialog = page.getByRole('dialog', {name: 'Grafik AI', exact: true})
    await expect(dialog.getByRole('status')).toContainText('Grafik AI pilotu henüz hesabına açılmadı.')
    await expect(dialog.getByRole('button', {name: 'Grafiği yakala', exact: true})).toHaveCount(0)
    await expect(dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true})).toHaveCount(0)
    await expect(dialog.locator('input, textarea')).toHaveCount(0)
    expect(state.usageCalls()).toBeGreaterThan(0)
    expect(state.generations).toHaveLength(0)
    expect(state.writes.filter(path => path.startsWith('/api/chart-ai/'))).toEqual([])
  })
}

for (const legacy of [false, true]) {
  test(`unverified feature state never flashes an OWNER button (${legacy ? 'legacy' : 'reference'})`, async ({page}) => {
    const state = await prepare(page, {legacy, disabled: true, deferUsage: true})
    try {
      await expect.poll(state.usageCalls).toBeGreaterThan(0)
      await expect(page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true})).toHaveCount(0)
    } finally {
      state.releaseUsage()
    }
    await expect.poll(state.usageReplies).toBeGreaterThan(0)
    await expect(page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true})).toHaveCount(0)
    expect(state.generations).toHaveLength(0)
  })
  for (const mobile of [false, true]) test(`feature off never exposes the CUSTOMER control (${legacy ? 'legacy' : 'reference'}, ${mobile ? 'mobile' : 'desktop'})`, async ({page}) => {
    const state = await prepare(page, {legacy, mobile, customer: true, disabled: true})
    await expect.poll(state.usageReplies).toBeGreaterThan(0)
    await expect(page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true})).toHaveCount(0)
    expect(state.usageCalls()).toBeGreaterThan(0)
    expect(state.generations).toHaveLength(0)
  })
}

for (const legacy of [false, true]) test(`preview, consent, exact context and single explicit generation (${legacy ? 'legacy' : 'reference'})`, async ({page}) => {
  const state = await prepare(page, {legacy})
  const toolbar = page.locator(legacy ? '.chartToolbar' : '.refToolbar')
  expect(await toolbar.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true)
  const dialog = await openAndCapture(page)
  expect(state.generations).toHaveLength(0)
  await expect(dialog.getByText(PRIVACY, {exact: true})).toBeVisible()
  const imageData = await dialog.getByAltText('Gönderilecek grafik önizlemesi').evaluate(async node => {
    const image = node as HTMLImageElement
    await image.decode()
    const canvas = document.createElement('canvas')
    canvas.width = image.naturalWidth; canvas.height = image.naturalHeight
    const context = canvas.getContext('2d')!
    context.drawImage(image, 0, 0)
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data
    return {width: canvas.width, height: canvas.height, colours: new Set(Array.from(pixels)).size}
  })
  expect(imageData.width).toBeGreaterThanOrEqual(320)
  expect(imageData.colours).toBeGreaterThan(20)
  await dialog.getByRole('button', {name: 'Riskler neler?', exact: true}).click()
  await dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true}).evaluate(node => {
    const button = node as HTMLButtonElement
    button.click(); button.click()
  })
  await expect(dialog.getByRole('region', {name: 'Grafik AI sonucu'})).toContainText(FINAL)
  expect(state.generations).toHaveLength(1)
  const payload = state.generations[0]
  expect(Object.keys(payload).sort()).toEqual(['context', 'image', 'question', 'request_key', 'symbol', 'timeframe'])
  expect(payload.question).toBe('Riskler neler?')
  expect(payload.context).toMatchObject({symbol: payload.symbol, timeframe: payload.timeframe, price: previewMarkets[0].price,
    indicators: {rsi: 58}, levels: {entry: previewMarkets[0].price}})
  expect(payload.image).toMatchObject({source: 'screenshot', media_type: 'image/png'})
  expect(JSON.stringify(payload)).not.toMatch(/wallet|account|777777|api_key|secret/i)
  expect(state.writes.filter(path => !path.startsWith('/api/chart-ai/') && path !== '/api/v25/risk/preview')).toEqual([])
  await dialog.getByRole('button', {name: 'Grafik AI panelini kapat'}).click()
  await expect(dialog).not.toBeVisible()
})

test('reload resumes only GET for the same pending UUID; image never goes to storage', async ({page}) => {
  const state = await prepare(page, {processing: true})
  const dialog = await openAndCapture(page)
  await dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true}).click()
  await expect.poll(() => state.generations.length).toBe(1)
  await expect.poll(state.gets).toBeGreaterThan(0)
  const saved = await page.evaluate(() => sessionStorage.getItem('kaistrade-chart-ai-request:chart-owner-fixture'))
  expect(saved).not.toBeNull()
  expect(Object.keys(JSON.parse(saved!)).sort()).toEqual(['request_key', 'symbol', 'timeframe'])
  expect(saved).not.toContain('data_base64')
  const prior = state.gets()
  await page.reload()
  await expect.poll(state.gets).toBeGreaterThan(prior)
  state.finish()
  await page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true}).click()
  await expect(page.getByRole('region', {name: 'Grafik AI sonucu'})).toContainText(FINAL)
  expect(state.generations).toHaveLength(1)
})

test('terminal 502 is a failed result, not a client regeneration', async ({page}) => {
  const state = await prepare(page, {failed: true})
  const dialog = await openAndCapture(page)
  await dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true}).click()
  await expect(dialog.getByRole('alert')).toContainText('hakkın kullanılmadı')
  await expect(dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true})).toBeEnabled()
  expect(state.generations).toHaveLength(1)
})

test('repeated result-query failures remain retryable without another generation', async ({page}) => {
  const state = await prepare(page, {processing: true, getFailures: 2})
  const dialog = await openAndCapture(page)
  await dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true}).click()
  for (let attempt = 1; attempt <= 2; attempt++) {
    await expect.poll(state.gets).toBe(attempt)
    await expect(dialog.getByRole('alert')).toContainText('Sonuç durumu okunamadı.')
    if (attempt === 2) state.finish()
    await dialog.getByRole('button', {name: 'Sonucu yeniden sorgula', exact: true}).click()
  }
  await expect(dialog.getByRole('region', {name: 'Grafik AI sonucu'})).toContainText(FINAL)
  expect(state.generations).toHaveLength(1)
})

test('unconfirmed acknowledgement never reaches analyze', async ({page}) => {
  const state = await prepare(page, {unconfirmed: true})
  const dialog = await openAndCapture(page)
  await dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true}).click()
  await expect(dialog.getByRole('alert')).toContainText('Kullanım onayı sunucuda doğrulanamadı')
  expect(state.generations).toHaveLength(0)
})

test('timeframe changes discard the old preview and require a fresh capture', async ({page}) => {
  const state = await prepare(page)
  const dialog = await openAndCapture(page)
  await dialog.getByRole('button', {name: 'Grafik AI panelini kapat'}).click()
  await page.getByRole('combobox', {name: 'Zaman dilimi', exact: true}).selectOption('1h')
  await page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true}).click()
  await expect(dialog.getByAltText('Gönderilecek grafik önizlemesi')).toHaveCount(0)
  await expect(dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true})).toBeDisabled()
  await dialog.getByRole('button', {name: 'Grafiği yakala', exact: true}).click()
  await expect(dialog.getByAltText('Gönderilecek grafik önizlemesi')).toBeVisible()
  await dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true}).click()
  await expect(dialog.getByRole('region', {name: 'Grafik AI sonucu'})).toBeVisible()
  expect(state.generations[0]).toMatchObject({timeframe: '1h', context: {timeframe: '1h'}})
})

test('switching OWNER identity unmounts the pending panel without transferring its image or UUID', async ({page}) => {
  const state = await prepare(page, {processing: true})
  const dialog = await openAndCapture(page)
  await dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true}).click()
  await expect.poll(state.gets).toBeGreaterThan(0)
  await page.route('**/api/v22/profile', route => route.fulfill({json: {
    user: {id: 'different-owner', role: 'OWNER', active: true, email_verified: true},
    access: {canAccessMasterTrade: true, isPremium: true},
  }}))
  await page.evaluate(() => window.dispatchEvent(new Event('protrebot-access-refresh')))
  await expect(dialog).not.toBeVisible()
  await page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true}).click()
  await expect(dialog).toBeVisible()
  await expect(dialog.getByAltText('Gönderilecek grafik önizlemesi')).toHaveCount(0)
  await expect(dialog.getByRole('region', {name: 'Grafik AI sonucu'})).toHaveCount(0)
  expect(await page.evaluate(() => sessionStorage.getItem('kaistrade-chart-ai-request:different-owner'))).toBeNull()
  expect(state.generations).toHaveLength(1)
})

test('same-account OWNER role removal clears the old panel and shows only the pilot notice', async ({page}) => {
  const state = await prepare(page, {processing: true})
  const dialog = await openAndCapture(page)
  await dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true}).click()
  await expect.poll(state.gets).toBeGreaterThan(0)
  await page.route('**/api/chart-ai/usage', route => route.fulfill({status: 403, json: {
    detail: {code: 'owner_only', message: 'Grafik AI şu anda yalnız OWNER için açık.'},
  }}))
  await page.route('**/api/v22/profile', route => route.fulfill({json: {
    user: {id: 'chart-owner-fixture', role: 'CUSTOMER', active: true, email_verified: true},
    access: {canAccessMasterTrade: true, isPremium: true},
  }}))
  await page.evaluate(() => window.dispatchEvent(new Event('protrebot-access-refresh')))
  await expect(dialog).not.toBeVisible()
  await page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true}).click()
  await expect(dialog.getByRole('status')).toContainText('Grafik AI pilotu henüz hesabına açılmadı.')
  await expect(dialog.getByAltText('Gönderilecek grafik önizlemesi')).toHaveCount(0)
  await expect(dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true})).toHaveCount(0)
  expect(state.generations).toHaveLength(1)
})

test('unavailable provider is explicit and cannot submit', async ({page}) => {
  const state = await prepare(page, {unavailable: true})
  const dialog = await openAndCapture(page)
  await expect(dialog.getByRole('status')).toContainText('kullanılamıyor')
  await expect(dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true})).toBeDisabled()
  expect(state.generations).toHaveLength(0)
})

test('mobile panel and upload validation preserve isolation', async ({page}) => {
  const state = await prepare(page, {mobile: true})
  await page.getByRole('button', {name: 'Yapay Zekaya Sor', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: 'Grafik AI', exact: true})
  await dialog.locator('input[type=file]').setInputFiles({name: 'bad.svg', mimeType: 'image/svg+xml', buffer: Buffer.from('<svg/>')})
  await expect(dialog.getByRole('alert')).toContainText('PNG, JPEG veya WebP')
  await dialog.locator('input[type=file]').setInputFiles({name: 'oversize.png', mimeType: 'image/png', buffer: Buffer.alloc(5 * 1024 * 1024 + 1)})
  await expect(dialog.getByRole('alert')).toContainText('en fazla 5 MiB')
  await dialog.locator('input[type=file]').setInputFiles({name: 'broken.png', mimeType: 'image/png', buffer: Buffer.from('not a PNG')})
  await expect(dialog.getByRole('alert')).toContainText('Görsel çözümlenemedi')
  const base64 = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 320; canvas.height = 180
    const context = canvas.getContext('2d')!; context.fillStyle = '#4977aa'; context.fillRect(0, 0, 320, 180)
    return canvas.toDataURL('image/png').split(',')[1]
  })
  await dialog.locator('input[type=file]').setInputFiles({name: 'synthetic.png', mimeType: 'image/png', buffer: Buffer.from(base64, 'base64')})
  await expect(dialog.getByAltText('Gönderilecek grafik önizlemesi')).toBeVisible()
  await dialog.getByRole('checkbox').check()
  await dialog.getByRole('button', {name: 'Grafiği analiz et', exact: true}).click()
  await expect(dialog.getByRole('region', {name: 'Grafik AI sonucu'})).toBeVisible()
  expect(state.generations[0].image).toMatchObject({source: 'upload', media_type: 'image/png'})
  const box = await dialog.boundingBox()
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(391)
})
