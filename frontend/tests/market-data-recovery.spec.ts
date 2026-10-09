import {expect, test, type Page} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

const quotes = [{symbol:'BTCUSDT',display:'BTC/USDT',price:60000,change:1,volume:1000000}]
const reason = 'Sentetik piyasa proxy bağlantısı kurulamadı'

async function prepare(page:Page) {
  await page.route('**/*',route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.fallback() : route.abort())
  await page.routeWebSocket('**/*',socket => socket.close())
  const state = await mockAssistant(page)
  const errors:string[] = []
  page.on('pageerror',error => errors.push(error.message))
  return {state,errors}
}

for (const root of ['http://127.0.0.1:4174','http://127.0.0.1:4176']) {
  test(`${root} trading market error preserves upstream reason and a healthy retry recovers without orders`,async ({page}) => {
    const {state,errors} = await prepare(page)
    let repaired = false
    await page.route('**/api/markets**',route => route.fulfill({status:repaired ? 200 : 503,json:repaired ? quotes : {detail:reason}}))
    await page.goto(root)
    await page.locator('.v26DashboardChoices button').filter({has:page.locator('b',{hasText:/^İŞLEM$/})}).click()
    await expect(page.locator('.v26MarketError')).toContainText(reason)
    await expect(page.locator('.v26MarketError')).toContainText('HTTP 503')
    repaired = true
    await page.getByRole('button',{name:'Market verisini yeniden dene',exact:true}).click()
    await expect(page.locator('.v26MarketError')).toHaveCount(0)
    expect(state.requests.filter(request => /\/order|\/arm|auto\/start/.test(request))).toEqual([])
    expect(errors).toEqual([])
  })

  test(`${root} stalled dashboard reads are bounded and do not overlap`,async ({page}) => {
    const {errors} = await prepare(page)
    await page.clock.install()
    let calls = 0
    await page.route('**/api/markets**',() => {calls++})
    await page.goto(root)
    await page.locator('.v26DashboardChoices button').filter({has:page.locator('b',{hasText:/^İŞLEM$/})}).click()
    await expect.poll(() => calls).toBeGreaterThan(0)
    const initial = calls
    await page.clock.fastForward(14000)
    expect(calls).toBe(initial)
    await page.clock.fastForward(1001)
    await expect(page.getByRole('button',{name:'Market verisini yeniden dene',exact:true})).toBeEnabled()
    await expect(page.locator('.v26MarketError')).toContainText('zaman aşımına')
    expect(errors).toEqual([])
  })

  for (const layout of ['0','1']) {
    test(`${root} Master Trade layout ${layout} preserves actual market failure and recovers read-only`,async ({page}) => {
      const {state,errors} = await prepare(page)
      let repaired = false
      await page.route('**/api/markets**',route => route.fulfill({status:repaired ? 200 : 503,json:repaired ? quotes : {detail:reason}}))
      await page.goto(`${root}/master-trade?masterLayoutV2=${layout}`)
      const panel = page.getByRole('tabpanel',{name:'Analiz',exact:true})
      await expect(panel).toBeVisible()
      await expect(panel).toContainText(reason)
      repaired = true
      await page.reload()
      await expect(panel).toBeVisible()
      await expect(panel).not.toContainText(reason)
      expect(state.requests.filter(request => /\/order|\/arm|auto\/start/.test(request))).toEqual([])
      expect(errors).toEqual([])
    })
  }
}
