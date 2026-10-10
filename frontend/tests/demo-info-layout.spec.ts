import {expect,test} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'
import {demoAccount,demoStatus,demoSummary} from './helpers/demo-api'

declare global {
  interface Window {
    demoCanvasObservation:{text:string[];colors:string[]}
  }
}

const titles = ['Bakiye','Kullanılabilir','Gerçekleşmemiş K/Z','Açık Pozisyon','Akış','Otomasyon','Günlük Demo','Risk Bütçesi','Demo Kanıt']

for (const root of ['http://127.0.0.1:4173','http://127.0.0.1:4176']) {
  for (const [width,columns] of [[1440,5],[1000,3],[390,2]]) {
    test(`${root} Demo summary ${width}px has nine equal single-layer cards`,async ({page},testInfo) => {
      await page.setViewportSize({width,height:900})
      const errors:string[] = []
      page.on('pageerror',error => errors.push(error.message))
      await page.route('**/*',route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.fallback() : route.abort())
      await page.routeWebSocket('**/*',socket => socket.close())
      const state = await mockAssistant(page)
      const mutations:string[] = []
      page.on('request',request => {
        if (request.url().includes('/api/') && !['GET','OPTIONS'].includes(request.method())) mutations.push(request.url())
      })
      await page.route('**/api/binance-demo/status',route => route.fulfill({json:{...demoStatus,configured:true,connected:true}}))
      await page.route('**/api/binance-demo/account',route => route.fulfill({json:{
        ...demoAccount,configured:true,connected:true,wallet_balance:1234.5,available_balance:987.6,unrealized_pnl:-12.34,
        reconciliation:{reconciled_active_positions:2},
      }}))
      await page.route('**/api/v21/summary',route => route.fulfill({json:{
        ...demoSummary,daily:{...demoSummary.daily,auto_entries:3,remaining_loss_budget:23.5},
        certificate:{...demoSummary.certificate,score:78},
      }}))
      await page.goto(root)
      if (root.endsWith(':4173')) await page.getByRole('button',{name:/TESTNET KOMUTA/}).click()
      else await page.locator('.v26DashboardChoices button').filter({has:page.locator('b',{hasText:/^İŞLEM$/})}).click()
      const grid = page.getByRole('region',{name:'Demo hesap ve durum özeti'})
      const cards = grid.locator(':scope > article')
      await expect(cards).toHaveCount(9)
      await expect(cards.locator(':scope > small')).toHaveText(titles)
      await expect(cards.nth(0).locator('b')).toHaveText('1.234,5 USDT')
      await expect(cards.nth(1).locator('b')).toHaveText('987,6 USDT')
      await expect(cards.nth(2).locator('b')).toHaveText('-12,34 USDT')
      await expect(cards.nth(2).locator('b')).toHaveClass('demoLoss')
      await expect(cards.nth(3).locator('b')).toHaveText('2 / 5')
      await expect(cards.nth(4).locator('b')).toHaveText('DISCONNECTED')
      await expect(cards.nth(5).locator('b')).toHaveText('KAPALI')
      await expect(cards.nth(6).locator('b')).toHaveText('3 / 30')
      await expect(cards.nth(7).locator('b')).toHaveText('23,5 USDT')
      await expect(cards.nth(8).locator('b')).toHaveText('%78')
      await expect(cards.first()).toHaveCSS('border-top-color','rgba(255, 255, 255, 0.08)')
      if (root.endsWith(':4176')) await expect(page.locator('.demoHero')).toHaveCSS('background-image','none')
      expect(await grid.evaluate(element => ({
        columns:getComputedStyle(element).gridTemplateColumns.split(' ').length,
        overflow:element.scrollWidth > element.clientWidth + 1,
        border:getComputedStyle(element).borderTopWidth,
        background:getComputedStyle(element).backgroundImage,
      }))).toEqual({columns,overflow:false,border:'0px',background:'none'})
      const bounds = await cards.evaluateAll(elements => elements.map(element => {
        const rect = element.getBoundingClientRect()
        return {x:rect.x,y:rect.y,width:rect.width,height:rect.height}
      }))
      expect(Math.max(...bounds.map(rect => rect.width))-Math.min(...bounds.map(rect => rect.width))).toBeLessThan(1)
      expect(Math.max(...bounds.map(rect => rect.height))-Math.min(...bounds.map(rect => rect.height))).toBeLessThan(1)
      for (const rect of bounds) {
        expect(rect.x).toBeGreaterThanOrEqual(0)
        expect(rect.x+rect.width).toBeLessThanOrEqual(width+1)
      }

      await expect(page.locator('.binanceDemoDeck > .v21Pulse')).toHaveCount(0)
      await expect(page.locator('.binanceDemoDeck > .demoAccountStrip')).toHaveCount(0)
      await expect(grid.locator('article article,fieldset,legend')).toHaveCount(0)
      expect(state.requests.filter(request => /order|auto\/start|\/arm/.test(request))).toEqual([])
      expect(mutations).toEqual([])
      expect(errors).toEqual([])
      await grid.screenshot({path:testInfo.outputPath(`demo-summary-${width}.png`)})
      await page.screenshot({path:testInfo.outputPath(`demo-desk-${width}.png`)})
    })
  }
}

for (const root of ['http://127.0.0.1:4173','http://127.0.0.1:4176']) {
  test(`${root} Demo chart keeps EMA curves but only entry, stop and resistance axis labels`,async ({page},testInfo) => {
    await page.setViewportSize({width:1440,height:1000})
    await page.route('**/*',route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.fallback() : route.abort())
    await page.routeWebSocket('**/*',socket => socket.close())
    const state = await mockAssistant(page)
    await page.addInitScript(() => {
      const observed = {text:[] as string[],colors:[] as string[]}
      window.demoCanvasObservation = observed
      const originalText = CanvasRenderingContext2D.prototype.fillText
      CanvasRenderingContext2D.prototype.fillText = function(text,x,y,maxWidth) {
        observed.text.push(text)
        if (maxWidth === undefined) originalText.call(this,text,x,y)
        else originalText.call(this,text,x,y,maxWidth)
      }
      const originalRect = CanvasRenderingContext2D.prototype.fillRect
      CanvasRenderingContext2D.prototype.fillRect = function(x,y,width,height) {
        observed.colors.push(String(this.fillStyle))
        originalRect.call(this,x,y,width,height)
      }
      const originalStroke = CanvasRenderingContext2D.prototype.stroke
      CanvasRenderingContext2D.prototype.stroke = function(path?:Path2D) {
        observed.colors.push(String(this.strokeStyle))
        if (path) originalStroke.call(this,path)
        else Reflect.apply(originalStroke,this,[])
      }
    })
    const candles = Array.from({length:20},(_,index) => ({
      time:1700000000+index*900,open:60000+index*10,high:63000,low:58000,close:60100+index*10,volume:100,
    }))
    const series = (value:number) => candles.map(candle => ({time:candle.time,value}))
    await page.route('**/api/klines/**',route => route.fulfill({json:candles}))
    await page.route('**/api/analysis/**',route => route.fulfill({json:{
      symbol:'BTCUSDT',direction:'LONG',confidence:82,entry:60000,stop_loss:59000,tp1:61000,tp2:62000,tp3:62500,
      support:58500,resistance:61500,rsi:50,adx:20,volume_ratio:1,trend:'UP',momentum:'UP',
      explanation:'Synthetic chart fixture',series:{ema20:series(60300),ema50:series(59600),ema200:series(59100)},
    }}))
    await page.goto(root)
    if (root.endsWith(':4173')) await page.getByRole('button',{name:/TESTNET KOMUTA/}).click()
    else await page.locator('.v26DashboardChoices button').filter({has:page.locator('b',{hasText:/^İŞLEM$/})}).click()
    const observe = () => page.evaluate(() => window.demoCanvasObservation)
    await expect.poll(async () => (await observe()).text).toEqual(expect.arrayContaining(['LONG GİRİŞ','STOP','DİRENÇ']))
    const observed = await observe()
    expect(observed.text.filter(text => /EMA20|EMA50|EMA200|^TP[123]$|^DESTEK$/.test(text))).toEqual([])
    const gridColor = root.endsWith(':4173') ? 'rgba(237, 241, 232, 0.22)' : 'rgba(39, 42, 34, 0.22)'
    expect(observed.colors).toContain(gridColor)
    for (const color of ['#16a560','#f3a712',root.endsWith(':4173') ? '#6f91ad' : '#8063d9']) {
      expect(observed.colors).toContain(color)
    }
    expect(state.requests.filter(request => /order|auto\/start|\/arm/.test(request))).toEqual([])
    await page.locator('.demoLiveChart').screenshot({path:testInfo.outputPath('demo-chart-1440.png')})
  })

  test(`${root} pending Demo snapshots show no invented balances or scores`,async ({page}) => {
    await page.route('**/*',route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.fallback() : route.abort())
    await page.routeWebSocket('**/*',socket => socket.close())
    await mockAssistant(page)
    await page.route('**/api/binance-demo/account',route => route.fulfill({status:503,json:{detail:'Synthetic account outage'}}))
    await page.route('**/api/v21/summary',route => route.fulfill({status:503,json:{detail:'Synthetic summary outage'}}))
    await page.goto(root)
    if (root.endsWith(':4173')) await page.getByRole('button',{name:/TESTNET KOMUTA/}).click()
    else await page.locator('.v26DashboardChoices button').filter({has:page.locator('b',{hasText:/^İŞLEM$/})}).click()
    await expect(page.locator('.demoMessage[role="alert"]')).toBeVisible()
    await expect(page.locator('.demoInfoCard > b')).toHaveText(Array(9).fill('—'))
  })
}
