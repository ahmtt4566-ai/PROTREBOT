import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

const asset = name => readFileSync(new URL(`../frontend/public/${name}`, import.meta.url))
const pngSignature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])

function assertPng(buffer, size) {
  assert.deepEqual(buffer.subarray(0, 8), pngSignature)
  assert.equal(buffer.toString('ascii', 12, 16), 'IHDR')
  assert.equal(buffer.readUInt32BE(16), size)
  assert.equal(buffer.readUInt32BE(20), size)
}

test('Both entrypoints declare stable same-origin favicon and touch icon URLs in the head', () => {
  const expected = [
    {rel: 'icon', href: '/favicon.ico', type: 'image/x-icon', sizes: '16x16 32x32 48x48 96x96'},
    {rel: 'icon', href: '/favicon-96x96.png', type: 'image/png', sizes: '96x96'},
    {rel: 'apple-touch-icon', href: '/apple-touch-icon.png', sizes: '180x180'},
  ]
  for (const entry of ['../index.html', '../frontend/index.html']) {
    const html = readFileSync(new URL(entry, import.meta.url), 'utf8')
    const head = html.match(/<head\b[^>]*>([\s\S]*?)<\/head>/i)?.[1]
    assert.ok(head, `${entry} has a document head`)
    const links = Array.from(head.matchAll(/<link\b[^>]*>/gi), match =>
      Object.fromEntries(Array.from(match[0].matchAll(/([\w-]+)="([^"]*)"/g), attr => [attr[1], attr[2]])))
    for (const icon of expected) {
      assert.ok(links.some(link => Object.entries(icon).every(([key, value]) => link[key] === value)),
        `${entry} declares ${icon.href}`)
      assert.ok(asset(icon.href.slice(1)).length > 0)
    }
  }
})

test('Search and Apple icons are real square PNGs with the declared sizes', () => {
  assertPng(asset('favicon-96x96.png'), 96)
  assertPng(asset('apple-touch-icon.png'), 180)
})

test('Fallback ICO contains bounded PNG entries at every declared browser size', () => {
  const ico = asset('favicon.ico')
  assert.equal(ico.readUInt16LE(0), 0)
  assert.equal(ico.readUInt16LE(2), 1)
  assert.equal(ico.readUInt16LE(4), 4)
  let offset = 6 + 4 * 16
  for (const [index, size] of [16, 32, 48, 96].entries()) {
    const entry = 6 + index * 16
    assert.equal(ico[entry], size)
    assert.equal(ico[entry + 1], size)
    assert.equal(ico.readUInt16LE(entry + 4), 1)
    assert.equal(ico.readUInt16LE(entry + 6), 32)
    const length = ico.readUInt32LE(entry + 8)
    assert.equal(ico.readUInt32LE(entry + 12), offset)
    assert.ok(length > 24 && offset + length <= ico.length)
    assertPng(ico.subarray(offset, offset + length), size)
    offset += length
  }
  assert.equal(offset, ico.length)
})
