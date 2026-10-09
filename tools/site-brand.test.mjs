import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

for (const entry of ['../index.html', '../frontend/index.html']) {
  test(`${entry} consistently exposes KaisTrade to search engines without running JavaScript`, () => {
    const html = readFileSync(new URL(entry, import.meta.url), 'utf8')
    const head = html.match(/<head\b[^>]*>([\s\S]*?)<\/head>/i)?.[1]
    assert.ok(head)
    assert.match(html, /<html lang="tr">/)
    assert.match(head, /<title>KaisTrade – Binance Trading Bot<\/title>/)
    const metas = Array.from(head.matchAll(/<meta\b[^>]*>/gi), match =>
      Object.fromEntries(Array.from(match[0].matchAll(/([\w-]+)="([^"]*)"/g), attr => [attr[1], attr[2]])))
    for (const [attribute, value] of [['name', 'application-name'], ['property', 'og:site_name']]) {
      assert.equal(metas.find(meta => meta[attribute] === value)?.content, 'KaisTrade')
    }
    const description = 'KaisTrade: Binance için otomatik al-sat botu. Sinyaller, risk yönetimi, demo ve canlı işlem.'
    const expected = [
      ['name', 'description', description],
      ['property', 'og:title', 'KaisTrade – Binance Trading Bot'],
      ['property', 'og:description', description],
      ['property', 'og:image', 'https://kaistrade.com/og-image.png'],
      ['property', 'og:image:width', '1200'],
      ['property', 'og:image:height', '630'],
      ['property', 'og:url', 'https://kaistrade.com'],
      ['property', 'og:locale', 'tr_TR'],
    ]
    for (const [attribute, key, value] of expected) {
      const matching = metas.filter(meta => meta[attribute] === key)
      assert.equal(matching.length, 1)
      assert.equal(matching[0].content, value)
    }
    const schemas = Array.from(head.matchAll(/<script\b[^>]*type="application\/ld\+json"[^>]*>([\s\S]*?)<\/script>/gi),
      match => JSON.parse(match[1]))
    assert.equal(schemas.length, 1)
    assert.deepEqual(schemas[0], {
      '@context': 'https://schema.org', '@type': 'WebSite',
      name: 'KaisTrade', url: 'https://kaistrade.com/',
    })
    assert.doesNotMatch(head, /KaiStrade|ProTreBot|PROTREBOT|KAISTrade/)
  })
}
