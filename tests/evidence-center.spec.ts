import { expect, test } from '@playwright/test'

test('Evidence Center keeps history and lower sections rendered', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /OPERASYON/i }).click()

  await expect(page.getByRole('heading', { name: 'Kanıt Geçmişi' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Canlı Kanıt Monitörü' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Testnet Kanıt Sertifikası' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Aktif Testnet Pozisyonu' })).toBeVisible()

  const timeline = page.locator('.v27TimelineItem')
  await expect(timeline.first()).toBeVisible()
  await expect(timeline.first()).toContainText(/.+/)
  await expect(page.locator('.v27HistoryPanel')).toHaveJSProperty('clientHeight', expect.any(Number))
  expect(await page.locator('.v27HistoryPanel').evaluate(element => element.getBoundingClientRect().height)).toBeGreaterThan(120)
})