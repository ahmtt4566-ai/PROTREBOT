import assert from 'node:assert/strict'
import test from 'node:test'
import {assetInlineLimit} from '../vite-asset-policy.ts'

test('Fonts remain same-origin files under font-src self', () => {
  for (const extension of ['woff', 'woff2', 'ttf', 'otf']) {
    for (const suffix of ['', '?v=1', '#font']) {
      assert.equal(assetInlineLimit(`C:\\assets\\font.${extension}${suffix}`), false)
      assert.equal(assetInlineLimit(`font.${extension.toUpperCase()}${suffix}`), false)
    }
  }
})

test('Other assets retain the default Vite inline threshold', () => {
  for (const file of ['coin.svg', 'logo.webp', 'picture.png', 'font.woff2.png', 'manifest.json']) {
    assert.equal(assetInlineLimit(file), undefined)
  }
})
