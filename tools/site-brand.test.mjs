import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

for (const entry of ['../index.html', '../frontend/index.html']) {
  test(`${entry} consistently exposes KaisTrade to search engines without running JavaScript`, () => {
    const html = readFileSync(new URL(entry, import.meta.url), 'utf8')
    const head = html.match(/<head\b[^>]*>([\s\S]*?)<\/head>/i)?.[1]
    assert.ok(head)
    assert.match(head, /<title>KaisTrade<\/title>/)
    const metas = Array.from(head.matchAll(/<meta\b[^>]*>/gi), match =>
      Object.fromEntries(Array.from(match[0].matchAll(/([\w-]+)="([^"]*)"/g), attr => [attr[1], attr[2]])))
    for (const [attribute, value] of [['name', 'application-name'], ['property', 'og:site_name'], ['property', 'og:title']]) {
      assert.equal(metas.find(meta => meta[attribute] === value)?.content, 'KaisTrade')
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
