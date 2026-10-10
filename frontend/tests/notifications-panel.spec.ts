import {expect, test, type Page, type Route} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

test.use({baseURL:'http://127.0.0.1:4174'})

const item = (id='notification-a', read=false) => ({
  id,type:'SCAN_STARTED',severity:'info',title:'Piyasa taraması başladı',
  message:`Deneme bildirimi ${id}`,timestamp:'2026-10-10T19:00:00+00:00',read,target:'system-health',
})
const panel = (page:Page) => page.getByRole('dialog',{name:'Bildirimler',exact:true})
const cors = {'Access-Control-Allow-Origin':'*','Access-Control-Allow-Headers':'*','Access-Control-Allow-Methods':'GET, POST, OPTIONS'}
async function mockNotifications(page:Page,handler:(route:Route) => Promise<void>) {
  await page.route('**/api/v21/notifications**',route => route.request().method() === 'OPTIONS'
    ? route.fulfill({status:204,headers:cors})
    : handler(route))
}
const respond = (route:Route,items= [item()],status=200) => route.fulfill({
  status,json:{items,unread:items.filter(entry => !entry.read).length},
  headers:cors,
})
async function open(page:Page) {
  await page.goto('/')
  await page.getByRole('button',{name:/^Bildirimler(?:,|$)/}).click()
  await expect(panel(page)).toBeVisible()
}

test('Loading and confirmed empty are separate states', async ({page}) => {
  await mockAssistant(page)
  let release!:() => void
  const held = new Promise<void>(resolve => {release=resolve})
  await mockNotifications(page,async route => {await held;await respond(route,[])})
  await open(page)
  await expect(panel(page)).toHaveAttribute('data-state','loading')
  await expect(panel(page).getByRole('status')).toContainText('Bildirimler yükleniyor')
  await expect(panel(page).getByText('Bildirim yok',{exact:true})).toHaveCount(0)
  release()
  await expect(panel(page)).toHaveAttribute('data-state','empty')
  await expect(panel(page).getByText('Bildirim yok',{exact:true})).toBeVisible()
})

for (const failure of ['http','invalid']) {
  test(`Initial ${failure} failure is an error, not an empty panel`,async ({page}) => {
    await mockAssistant(page)
    await mockNotifications(page,route => failure === 'http'
      ? respond(route,[],503)
      : route.fulfill({json:{items:'invalid'},headers:cors}))
    await open(page)
    await expect(panel(page)).toHaveAttribute('data-state','error')
    await expect(panel(page).getByRole('alert')).toContainText('Bildirimler yüklenemedi')
    await expect(panel(page).getByText('Bildirim yok',{exact:true})).toHaveCount(0)
  })
}

test('Failed refresh preserves the list and successful retry clears the warning',async ({page}) => {
  await mockAssistant(page)
  let failRefresh = false
  await mockNotifications(page,route => respond(route,[item()],failRefresh ? 503 : 200))
  await open(page)
  await expect(panel(page)).toHaveAttribute('data-state','data')
  await expect(panel(page).getByText('BİLGİ',{exact:true})).toBeVisible()
  failRefresh = true
  await panel(page).getByRole('button',{name:'Bildirimleri yenile'}).click()
  await expect(panel(page).getByRole('alert')).toContainText('önceki liste gösteriliyor')
  await expect(panel(page).locator('.v26NotificationItem')).toHaveCount(1)
  await expect(panel(page).getByText('Bildirim yok',{exact:true})).toHaveCount(0)
  failRefresh = false
  await panel(page).getByRole('button',{name:'YENİDEN DENE'}).click()
  await expect(panel(page)).toHaveAttribute('data-state','data')
  await expect(panel(page).getByRole('alert')).toHaveCount(0)
})

for (const failure of ['http','network']) {
  test(`Single read ${failure} failure restores unread state and shows the error`,async ({page}) => {
    await mockAssistant(page)
    await mockNotifications(page,route => {
      if (route.request().method() === 'POST') {
        return failure === 'network' ? route.abort() : respond(route,[item()],503)
      }
      return respond(route)
    })
    await open(page)
    await expect(panel(page).locator('.v26NotificationItem')).toHaveCount(1)
    await panel(page).locator('.v26NotificationItem').click()
    await expect(panel(page)).toBeVisible()
    await expect(panel(page).getByRole('alert')).toContainText('değişiklik geri alındı')
    await expect(panel(page).locator('.v26NotificationItem.isRead')).toHaveCount(0)
    await expect(page.getByRole('button',{name:'Bildirimler, 1 okunmamış',exact:true})).toBeVisible()
  })
}

test('Failed bulk read restores only the prior read flags',async ({page}) => {
  await mockAssistant(page)
  const items = [item('already-read',true),item('unread-a'),item('unread-b')]
  await mockNotifications(page,route => respond(route,items,route.request().method() === 'POST' ? 503 : 200))
  await open(page)
  await expect(panel(page).locator('.v26NotificationItem')).toHaveCount(3)
  await panel(page).getByRole('button',{name:'TÜMÜ OKUNDU'}).click()
  await expect(panel(page).getByRole('alert')).toContainText('değişiklik geri alındı')
  await expect(panel(page).locator('.v26NotificationItem.isRead')).toHaveCount(1)
  await expect(page.getByRole('button',{name:'Bildirimler, 2 okunmamış',exact:true})).toBeVisible()
})

for (const action of ['single','bulk']) {
  test(`Successful ${action} read keeps the read state`,async ({page}) => {
    await mockAssistant(page)
    let read = false
    await mockNotifications(page,route => {
      if (route.request().method() === 'POST') read = true
      return respond(route,[item('notification-a',read)])
    })
    await open(page)
    await expect(panel(page).locator('.v26NotificationItem')).toHaveCount(1)
    if (action === 'single') {
      await panel(page).locator('.v26NotificationItem').click()
      await expect(panel(page)).toBeHidden()
      await page.getByRole('button',{name:'Bildirimler',exact:true}).click()
    } else {
      await panel(page).getByRole('button',{name:'TÜMÜ OKUNDU'}).click()
    }
    await expect(panel(page).locator('.v26NotificationItem.isRead')).toHaveCount(1)
    await expect(panel(page).getByRole('alert')).toHaveCount(0)
    await expect(page.getByRole('button',{name:'Bildirimler',exact:true})).toBeVisible()
  })
}

test('A stale poll cannot overwrite rollback while a read is pending',async ({page}) => {
  await mockAssistant(page)
  let holdRefresh = false
  let release!:() => void
  const held = new Promise<void>(resolve => {release=resolve})
  let lateResponse!:() => void
  const completed = new Promise<void>(resolve => {lateResponse=resolve})
  await mockNotifications(page,async route => {
    if (route.request().method() === 'POST') {await respond(route,[item()],503);return}
    if (holdRefresh) {
      holdRefresh = false
      await held
      await respond(route,[item('notification-a',true)])
      lateResponse()
      return
    }
    await respond(route)
  })
  await open(page)
  await expect(panel(page).locator('.v26NotificationItem')).toHaveCount(1)
  holdRefresh = true
  await panel(page).getByRole('button',{name:'Bildirimleri yenile'}).click()
  await expect(panel(page)).toHaveAttribute('data-state','loading')
  await panel(page).locator('.v26NotificationItem').click()
  await expect(panel(page).getByRole('alert')).toContainText('değişiklik geri alındı')
  release()
  await completed
  await expect(panel(page).locator('.v26NotificationItem.isRead')).toHaveCount(0)
})
