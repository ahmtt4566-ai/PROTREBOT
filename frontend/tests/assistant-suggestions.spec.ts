import {expect, test} from '@playwright/test'
import {mockAssistant} from './helpers/assistant-api'

test.use({baseURL: 'http://127.0.0.1:4174'})

const questions = {
  tr: ['Premium üyelik ne kadar?', 'Günlük kullanım limitim ne kadar?', 'API anahtarlarımı nasıl girerim?', 'BTC için analiz durumu ne?'],
  en: ['How much is Premium?', 'What is my daily usage limit?', 'How do I enter my API keys?', 'What is the analysis status for BTC?'],
}

for (const language of ['tr', 'en'] as const) {
  for (const width of [1440, 390]) {
    test(`Exactly four ${language} suggestions send their displayed text at ${width}px`, async ({page}) => {
      await page.setViewportSize({width, height: 844})
      const state = await mockAssistant(page)
      state.chatBody = {reply: 'Suggestion fixture response.', language, sources: []}
      await page.goto('/')
      await page.evaluate(language => { document.documentElement.lang = language }, language)
      await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
      const dialog = page.getByRole('dialog', {name: 'Kais AI', exact: true})
      const suggestions = dialog.locator('.assistantSuggestions')
      await expect(suggestions).toHaveAttribute('aria-label', language === 'tr' ? 'Hazır sorular' : 'Suggested questions')
      await expect(suggestions.getByRole('button')).toHaveText(questions[language])
      for (const [index, question] of questions[language].entries()) {
        const button = suggestions.getByRole('button', {name: question, exact: true})
        await expect(button).toBeEnabled()
        await button.click()
        await expect.poll(() => state.chats.length).toBe(index + 1)
        expect(state.chats[index].body.message).toBe(question)
        await expect(dialog.locator('.assistantMessage.user .assistantText')).toHaveText(question)
        await expect(dialog.locator('.assistantMessage.assistant')).toHaveCount(1)
        await expect(suggestions).toHaveCount(0)
        await expect(dialog.locator('.assistantTyping')).toHaveCount(0)
        await dialog.getByRole('button', {name: language === 'tr' ? 'Kais AI sohbetini temizle' : 'Clear Kais AI chat', exact: true}).click()
        await expect(suggestions.getByRole('button')).toHaveText(questions[language])
      }
      expect(state.chats.map(call => call.body.message)).toEqual(questions[language])
      expect(state.confirmations).toHaveLength(0)
    })
  }
}
