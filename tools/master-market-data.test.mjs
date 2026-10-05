import assert from 'node:assert/strict'
import {test} from 'node:test'
import {filterMarketQuotes, marketPrice, marketWindow, parseMarketQuotes} from '../master-market-data.ts'
import {coinBaseAsset} from '../src/components/coin-symbol.ts'

const rows = [
  {symbol: 'BTCUSDT', display: 'BTC/USDT', name: 'Bitcoin', price: 100, change: 2, volume: 1000},
  {symbol: 'BTCDOMUSDT', display: 'BTCDOM/USDT', price: 200, change: -3, volume: 2000},
  {symbol: 'ETHUSDT', display: 'ETH/USDT', price: null, change: null, volume: null},
]
const scores = new Map([['BTCUSDT', {symbol: 'BTCUSDT', score: 94, direction: 'LONG'}]])
const favorites = new Set(['ETHUSDT'])

test('Universe filters preserve unscored rows and use actual optional quote values', () => {
  const select = (query, filter, sort) => filterMarketQuotes(rows, scores, favorites, query, filter, sort).map(row => row.symbol)
  assert.deepEqual(select('btc', 'Tümü', 'A-Z'), ['BTCDOMUSDT', 'BTCUSDT'])
  assert.deepEqual(select('bitcoin', 'Tümü', 'Skor'), ['BTCUSDT'])
  assert.deepEqual(select('', 'Skorlu', 'Skor'), ['BTCUSDT'])
  assert.deepEqual(select('', 'Favoriler', 'A-Z'), ['ETHUSDT'])
  assert.deepEqual(select('', 'Yükselenler', '24s %'), ['BTCUSDT'])
  assert.deepEqual(select('', 'Düşenler', '24s %'), ['BTCDOMUSDT'])
  assert.deepEqual(select('', 'Tümü', 'Hacim'), ['BTCDOMUSDT', 'BTCUSDT', 'ETHUSDT'])
  assert.equal(rows[0].symbol, 'BTCUSDT')
})

test('Fixed-height virtual windows remain bounded throughout 650+ rows', () => {
  for (const offset of [0, 5000, 33000]) {
    const {start, end} = marketWindow(offset, 520, 650)
    assert.ok(end - start <= 15)
    assert.ok(start >= 0 && end <= 650)
  }
  assert.deepEqual(marketWindow(0, 520, 0), {start: 0, end: 0})
})

test('Market payload validation rejects malformed data rather than fabricating prices', () => {
  assert.equal(parseMarketQuotes(rows).length, 3)
  assert.equal(parseMarketQuotes(rows)[2].price, null)
  for (const payload of [{}, [{...rows[0], price: NaN}], [{...rows[0], symbol: 'BTCUSD'}], [rows[0], rows[0]]]) assert.throws(() => parseMarketQuotes(payload))
  assert.equal(parseMarketQuotes([{...rows[0], test: true}])[0].test, true)
  assert.equal(parseMarketQuotes(rows)[0].test, undefined)
  assert.throws(() => parseMarketQuotes([{...rows[0], test: 'yes'}]))
})

test('Multiplier logo normalization preserves real numbered assets', () => {
  for (const [symbol, asset] of [['1000PEPEUSDT', 'PEPE'], ['1000SHIB/USDT', 'SHIB'], ['1000000MOGUSDT', 'MOG'], ['1INCHUSDT', '1INCH'], ['BTCUSDT', 'BTC']]) assert.equal(coinBaseAsset(symbol), asset)
})

test('Market quote formatting keeps meaningful low-price digits without fabricated numbers', () => {
  assert.equal(marketPrice(null), '—')
  assert.equal(marketPrice(85360.8), '$85,360.80')
  assert.equal(marketPrice(.01913), '$0.01913')
  assert.equal(marketPrice(.000001234), '$0.000001234')
})
