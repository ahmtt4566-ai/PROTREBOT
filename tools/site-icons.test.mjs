import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {inflateSync} from 'node:zlib'
import test from 'node:test'

const asset = name => readFileSync(new URL(`../frontend/public/${name}`, import.meta.url))
const pngSignature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])

function assertPng(buffer, size) {
  assert.deepEqual(buffer.subarray(0, 8), pngSignature)
  assert.equal(buffer.toString('ascii', 12, 16), 'IHDR')
  assert.equal(buffer.readUInt32BE(16), size)
  assert.equal(buffer.readUInt32BE(20), size)
}

function assertIconPng(buffer, size) {
  assertPng(buffer, size)
  assert.equal(buffer[24], 8)
  assert.equal(buffer[25], 6)
  const chunks = []
  for (let offset = 8; offset < buffer.length;) {
    const length = buffer.readUInt32BE(offset)
    if (buffer.toString('ascii', offset + 4, offset + 8) === 'IDAT') {
      chunks.push(buffer.subarray(offset + 8, offset + 8 + length))
    }
    offset += length + 12
  }
  const raw = inflateSync(Buffer.concat(chunks))
  const stride = size * 4
  const rows = []
  let offset = 0
  for (let y = 0; y < size; y++) {
    const filter = raw[offset++]
    assert.ok(filter >= 0 && filter <= 4)
    const row = Buffer.from(raw.subarray(offset, offset + stride))
    offset += stride
    for (let x = 0; x < stride; x++) {
      const left = x >= 4 ? row[x - 4] : 0
      const up = y ? rows[y - 1][x] : 0
      const upperLeft = y && x >= 4 ? rows[y - 1][x - 4] : 0
      if (filter === 1) row[x] = (row[x] + left) & 255
      if (filter === 2) row[x] = (row[x] + up) & 255
      if (filter === 3) row[x] = (row[x] + Math.floor((left + up) / 2)) & 255
      if (filter === 4) {
        const prediction = left + up - upperLeft
        const distances = [Math.abs(prediction - left), Math.abs(prediction - up), Math.abs(prediction - upperLeft)]
        const nearest = distances[0] <= distances[1] && distances[0] <= distances[2]
          ? left : distances[1] <= distances[2] ? up : upperLeft
        row[x] = (row[x] + nearest) & 255
      }
    }
    rows.push(row)
  }
  assert.equal(offset, raw.length)
  for (const row of rows) for (let x = 0; x < size; x++) assert.equal(row[x * 4 + 3], 255)
  const margin = Math.floor(size * .07)
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    if (x < margin || y < margin || x >= size - margin || y >= size - margin) {
      assert.deepEqual([...rows[y].subarray(x * 4, x * 4 + 4)], [255, 255, 255, 255])
    }
  }
  assert.ok(rows.some(row => {
    for (let x = 0; x < size; x++) if (row[x * 4 + 1] > 180 && row[x * 4] < 100) return true
    return false
  }), 'Existing green pulse artwork is present')
}

test('Both entrypoints declare stable same-origin favicon and touch icon URLs in the head', () => {
  const expected = [
    {rel: 'icon', href: '/favicon.ico', type: 'image/x-icon', sizes: '48x48'},
    {rel: 'icon', href: '/icon-512.png', type: 'image/png', sizes: '512x512'},
    {rel: 'apple-touch-icon', href: '/apple-touch-icon.png', sizes: '180x180'},
  ]
  for (const entry of ['../index.html', '../frontend/index.html']) {
    const html = readFileSync(new URL(entry, import.meta.url), 'utf8')
    const head = html.match(/<head\b[^>]*>([\s\S]*?)<\/head>/i)?.[1]
    assert.ok(head, `${entry} has a document head`)
    const links = Array.from(head.matchAll(/<link\b[^>]*>/gi), match =>
      Object.fromEntries(Array.from(match[0].matchAll(/([\w-]+)="([^"]*)"/g), attr => [attr[1], attr[2]])))
    assert.equal(links.filter(link => link.rel === 'icon' || link.rel === 'apple-touch-icon').length, 3)
    for (const icon of expected) {
      assert.ok(links.some(link => Object.entries(icon).every(([key, value]) => link[key] === value)),
        `${entry} declares ${icon.href}`)
      assert.ok(asset(icon.href.slice(1)).length > 0)
    }
  }
})

test('Search and Apple icons are real square PNGs with the declared sizes', () => {
  for (const size of [192, 512]) assertIconPng(asset(`icon-${size}.png`), size)
  assertIconPng(asset('apple-touch-icon.png'), 180)
})

test('Open Graph promotional PNG is exactly 1200 by 630 pixels', () => {
  const png = asset('og-image.png')
  assert.deepEqual(png.subarray(0, 8), pngSignature)
  assert.equal(png.readUInt32BE(16), 1200)
  assert.equal(png.readUInt32BE(20), 630)
})

test('Fallback ICO contains exactly one bounded 48px PNG entry', () => {
  const ico = asset('favicon.ico')
  assert.equal(ico.readUInt16LE(0), 0)
  assert.equal(ico.readUInt16LE(2), 1)
  assert.equal(ico.readUInt16LE(4), 1)
  let offset = 6 + 16
  for (const [index, size] of [48].entries()) {
    const entry = 6 + index * 16
    assert.equal(ico[entry], size)
    assert.equal(ico[entry + 1], size)
    assert.equal(ico.readUInt16LE(entry + 4), 1)
    assert.equal(ico.readUInt16LE(entry + 6), 32)
    const length = ico.readUInt32LE(entry + 8)
    assert.equal(ico.readUInt32LE(entry + 12), offset)
    assert.ok(length > 24 && offset + length <= ico.length)
    assertIconPng(ico.subarray(offset, offset + length), size)
    offset += length
  }
  assert.equal(offset, ico.length)
})
