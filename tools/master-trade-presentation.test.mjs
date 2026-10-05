import assert from 'node:assert/strict'
import {test} from 'node:test'
import {analysisPrice, autoTradePresentation, masterLayoutV2Enabled} from '../master-trade-presentation.ts'

test('Restored layout is explicit opt-in and desktop only', () => {
  assert.equal(masterLayoutV2Enabled('', 1440), false)
  assert.equal(masterLayoutV2Enabled('?masterLayoutV2=0', 1440), false)
  assert.equal(masterLayoutV2Enabled('?masterLayoutV2=true', 1440), false)
  assert.equal(masterLayoutV2Enabled('?masterLayoutV2=1', 1199), false)
  assert.equal(masterLayoutV2Enabled('?masterLayoutV2=1', 1200), true)
  assert.equal(masterLayoutV2Enabled('?masterLayoutV2=1&tab=canli', 1440), true)
})

test('Analysis prices always have two decimals and never invent unavailable data', () => {
  assert.equal(analysisPrice(85360.8), '$85,360.80')
  assert.equal(analysisPrice(0.01234), '$0.01')
  assert.equal(analysisPrice(0), '$0.00')
  for (const value of [undefined, null, NaN, Infinity, -Infinity]) assert.equal(analysisPrice(value), '—')
})

test('Read-only Auto Trade labels preserve channel identity and unknown state', () => {
  assert.deepEqual(autoTradePresentation('LIVE', false), {label: 'Kapalı', action: "Auto Trade'i aç"})
  assert.deepEqual(autoTradePresentation('LIVE', true), {label: "Live'da açık", action: "Auto Trade'e git"})
  assert.deepEqual(autoTradePresentation('DEMO', true), {label: "Demo'da açık", action: "Auto Trade'e git"})
  assert.equal(autoTradePresentation('LIVE', undefined).label, '—')
})
