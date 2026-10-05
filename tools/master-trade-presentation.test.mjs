import assert from 'node:assert/strict'
import {test} from 'node:test'
import {analysisChartLabels, analysisChartRange, analysisPrice, analysisValue, autoTradePresentation, masterLayoutV2Enabled, triggerPresentation} from '../master-trade-presentation.ts'
import {coinBaseAsset} from '../src/components/coin-symbol.ts'
import {previewAnalysis, previewCandles, previewPrice} from '../frontend/tests/helpers/master-trade-preview-data.ts'

test('Reference layout is the default at every responsive width with an explicit legacy escape hatch', () => {
  assert.equal(masterLayoutV2Enabled('', 1440), true)
  assert.equal(masterLayoutV2Enabled('?masterLayoutV2=0', 1440), false)
  assert.equal(masterLayoutV2Enabled('?masterLayoutV2=true', 1440), true)
  assert.equal(masterLayoutV2Enabled('?masterLayoutV2=1', 390), true)
  assert.equal(masterLayoutV2Enabled('?tab=analiz', 1024), true)
  assert.equal(masterLayoutV2Enabled('?masterLayoutV2=1', 1199), true)
  assert.equal(masterLayoutV2Enabled('?masterLayoutV2=1', 1200), true)
  assert.equal(masterLayoutV2Enabled('?masterLayoutV2=1&tab=canli', 1440), true)
})

test('Analysis prices always have two decimals and never invent unavailable data', () => {
  assert.equal(analysisPrice(85360.8), '$85,360.80')
  assert.equal(analysisPrice(0.01234), '$0.01234')
  assert.equal(analysisPrice(.00000012345), '$0.00000012345')
  assert.equal(analysisPrice(0), '$0.00')
  for (const value of [undefined, null, NaN, Infinity, -Infinity]) assert.equal(analysisPrice(value), '—')
})

test('Bundled coin symbols normalize quote currencies without changing unknown assets', () => {
  for (const [symbol, expected] of [['BTCUSDT', 'BTC'], [' eth/usdt ', 'ETH'], ['SOL-USDC', 'SOL'], ['BNB_BUSD', 'BNB'], ['XRPUSDT.P', 'XRP'], ['ADABTC', 'ADA'], ['DOGEFDUSD', 'DOGE'], ['AVAX', 'AVAX'], ['TRXUSD', 'TRX'], ['LINKETH', 'LINK'], ['UNKNOWNUSDT', 'UNKNOWN'], ['', '']]) assert.equal(coinBaseAsset(symbol), expected)
})

test('Small-price MACD values retain significant digits instead of becoming zero', () => {
  assert.equal(analysisValue(.0000014168), '0.0000014168')
  assert.equal(analysisValue(-.000002678), '-0.000002678')
  assert.equal(analysisValue(12.345), '12.35')
  assert.equal(analysisValue(0), '0.00')
  for (const value of [undefined, null, NaN, Infinity]) assert.equal(analysisValue(value), '—')
})

test('Trigger lifecycle presentation never invents waiting or hides actual lifecycle', () => {
  for (const [lifecycle, label, tone] of [['WAITING', 'Waiting', 'warning'], ['ARMED', 'Armed', 'warning'], ['TRIGGERED', 'Triggered', 'positive'], ['CONFIRMED', 'Confirmed', 'positive'], ['INVALIDATED', 'Invalidated', 'negative'], ['EXPIRED', 'Expired', 'neutral']]) {
    assert.deepEqual(triggerPresentation(lifecycle, true), {label, tone})
    assert.deepEqual(triggerPresentation(lifecycle, false), {label: '—', tone: 'neutral'})
  }
})

test('Read-only Auto Trade labels preserve channel identity and unknown state', () => {
  assert.deepEqual(autoTradePresentation('LIVE', false), {label: 'Kapalı', action: "Auto Trade'i aç"})
  assert.deepEqual(autoTradePresentation('LIVE', true), {label: "Live'da açık", action: "Auto Trade'e git"})
  assert.deepEqual(autoTradePresentation('DEMO', true), {label: "Demo'da açık", action: "Auto Trade'e git"})
  assert.equal(autoTradePresentation('LIVE', undefined).label, '—')
})

test('Offline preview has 200 valid candles, realistic range and non-collapsed target distances', () => {
  const candles = previewCandles('BTCUSDT', 1791201600000)
  assert.equal(candles.length, 200)
  const high = Math.max(...candles.map(candle => candle.high))
  const low = Math.min(...candles.map(candle => candle.low))
  const range = (high - low) / previewPrice * 100
  assert.ok(range >= 1 && range <= 2, `Preview range is ${range}%`)
  const analysis = previewAnalysis()
  for (const value of [analysis.tp1, analysis.tp2, analysis.tp3, analysis.stop_loss, analysis.support, analysis.resistance]) {
    const distance = Math.abs(value - analysis.entry) / analysis.entry * 100
    assert.ok(distance >= .3 - 1e-10 && distance <= 1.5 + 1e-10, `Level distance is ${distance}%`)
  }
  for (const candle of candles) {
    assert.ok(candle.low <= Math.min(candle.open, candle.close))
    assert.ok(candle.high >= Math.max(candle.open, candle.close))
    assert.ok(candle.volume > 0)
  }
  assert.equal(candles.at(-1).close, previewPrice)
})

test('Chart range uses only candle high/low with exactly eight percent padding', () => {
  assert.deepEqual(analysisChartRange([{low: 100, high: 110}, {low: 98, high: 105}]), {low: 97.04, high: 110.96})
  for (const price of [0, .000001, 100, -100]) {
    const range = analysisChartRange([{low: price, high: price}])
    assert.ok(range.low < price && range.high > price)
    assert.ok(Number.isFinite(range.low) && Number.isFinite(range.high))
  }
  for (const candles of [[], [{low: 1, high: NaN}], [{low: 2, high: 1}]]) assert.equal(analysisChartRange(candles), null)
})

test('Out-of-range levels pin to the edges without expanding the candle scale or colliding', () => {
  const range = analysisChartRange([{low: 98, high: 110}])
  const original = {...range}
  const labels = analysisChartLabels([
    {label: 'TP3', value: 10000, tone: 'tp'}, {label: 'TP2', value: 1000, tone: 'tp'},
    {label: 'ENTRY', value: 104, tone: 'entry'}, {label: 'SUPPORT', value: 104, tone: 'support'},
    {label: 'SL', value: 1, tone: 'stop'},
  ], range, 320)
  assert.deepEqual(range, original)
  assert.equal(labels[0].edge, 'above')
  assert.equal(labels.at(-1).edge, 'below')
  assert.equal(labels[0].y, 11)
  assert.equal(labels.at(-1).y, 309)
  for (let index = 1; index < labels.length; index++) assert.ok(labels[index].y - labels[index - 1].y >= 42)
  assert.equal(labels.find(label => label.label === 'ENTRY').edge, 'inside')
  assert.equal(labels.find(label => label.label === 'TP3').value, 10000)
})
