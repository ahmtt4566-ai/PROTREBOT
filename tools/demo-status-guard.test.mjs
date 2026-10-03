import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'
import {runInNewContext} from 'node:vm'

test('Actual Demo notional expression guards missing limits without overriding valid caps', () => {
  const source = readFileSync(new URL('../BinanceDemo.tsx', import.meta.url), 'utf8')
  const match = source.match(/const previewNotionalCap = ([^\r\n]+)/)
  assert.ok(match)
  for (const [status, expected] of [
    [null, 200], [{}, 200], [{limits: {}}, 200],
    [{limits: {max_notional_usdt: 50}}, 50],
    [{limits: {max_notional_usdt: 500}}, 500],
  ]) assert.equal(runInNewContext(match[1], {status}), expected)
})
