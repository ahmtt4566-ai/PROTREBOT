import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'

const source = readFileSync(new URL('../frontend/src/BinanceDemo.tsx', import.meta.url), 'utf8')

test('Observer error reset exposes a warning and uses the existing confirmation dialog', () => {
  assert.match(source, /result_observer\?\.reset_required && <div[^>]*role="alert"/)
  assert.match(source, /expected:'DEMO HATAYI SIFIRLA'/)
  assert.match(source, /disabled=\{busy \|\| v21Busy \|\| !status\?\.armed \|\| v21\.auto\.enabled\}/)
  assert.match(source, /ardışık zarar sayacı ve kilidi değişmez/)
})

test('Opening reset confirmation makes no API call; confirmed action posts acknowledgement only', () => {
  const reset = source.slice(source.indexOf('const resetObserverError ='), source.indexOf('const toggleAuto ='))
  assert.match(reset, /setConfirmation\(\{/)
  assert.match(reset, /action:\(\) => runV21\(\(\) => v21Call<V21Summary>\('\/auto\/errors\/reset'/)
  assert.match(reset, /acknowledged:true/)
  assert.doesNotMatch(reset, /\/auto\/start|\/order|execute_demo_order/)
})
