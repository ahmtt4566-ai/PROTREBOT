import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

for (const entry of ['../index.html', '../frontend/index.html']) {
  test(`${entry} consistently exposes KaisTrade to search engines without running JavaScript`, () => {
    const html = readFileSync(new URL(entry, import.meta.url), 'utf8')
    const head = html.match(/<head\b[^>]*>([\s\S]*?)<\/head>/i)?.[1]
    assert.ok(head)
    assert.match(html, /<html lang="tr">/)
    const title = 'KaisTrade | AI-Powered Crypto Trading Platform'
    assert.ok(head.includes(`<title>${title}</title>`))
    const metas = Array.from(head.matchAll(/<meta\b[^>]*>/gi), match =>
      Object.fromEntries(Array.from(match[0].matchAll(/([\w-]+)="([^"]*)"/g), attr => [attr[1], attr[2]])))
    for (const [attribute, value] of [['name', 'application-name'], ['property', 'og:site_name']]) {
      assert.equal(metas.find(meta => meta[attribute] === value)?.content, 'KaisTrade')
    }
    const description = "Explore KaisTrade's AI-powered crypto analysis, market scanning and trading tools, with demo trading and risk controls for informed market decisions."
    const expected = [
      ['name', 'description', description],
      ['property', 'og:title', title],
      ['property', 'og:type', 'website'],
      ['property', 'og:description', description],
      ['property', 'og:image', 'https://kaistrade.com/og-image.png'],
      ['property', 'og:image:width', '1200'],
      ['property', 'og:image:height', '630'],
      ['property', 'og:url', 'https://kaistrade.com/'],
      ['property', 'og:locale', 'tr_TR'],
      ['name', 'twitter:card', 'summary_large_image'],
      ['name', 'twitter:title', title],
      ['name', 'twitter:description', description],
      ['name', 'twitter:image', 'https://kaistrade.com/og-image.png'],
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
      name: 'KaisTrade', alternateName: 'Kais Trade', url: 'https://kaistrade.com/',
    })
    assert.doesNotMatch(head, /KaiStrade|ProTreBot|PROTREBOT|KAISTrade/)
  })
}
