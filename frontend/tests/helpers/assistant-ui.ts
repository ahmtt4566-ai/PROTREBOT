import {type Page} from '@playwright/test'

export async function openChatFromHint(page: Page) {
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  await page.getByRole('button', {name: 'Sohbeti aç →', exact: true}).press('Enter')
}
