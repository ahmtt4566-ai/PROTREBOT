import assert from 'node:assert/strict'
import {test} from 'node:test'
import {MarketDataFailure, marketDataFailure, marketDataResponseError} from '../master-trade-data-error.ts'

for (const [status, detail, kind, text] of [
  [422, 'Analiz için yeterli mum verisi yok', 'history', 'yeterli veri yok'],
  [400, 'Invalid symbol.', 'inactive', 'borsada aktif sembol bulunamadı'],
  [503, 'Binance Futures sunucusuna ulaşılamadı', 'backend', 'HTTP 503'],
  [401, 'Oturum gerekli', 'session', 'Oturum gerekli'],
  [403, 'Premium üyelik gerekli', 'session', 'Premium üyelik gerekli'],
  [404, 'Not Found', 'backend', 'HTTP 404'],
]) test(`Selected data HTTP ${status} retains its actual reason and classification: ${kind}`, async () => {
  const error = await marketDataResponseError(Response.json({detail}, {status}))
  assert.equal(error.kind, kind)
  assert.ok(error.message.includes(text))
})

test('Explicit preview marker never claims the real contract lacks candle history', async () => {
  const error = await marketDataResponseError(Response.json({code: 'PREVIEW_DATA_UNAVAILABLE'}, {status: 422}))
  assert.equal(error.kind, 'preview')
  assert.equal(error.message, 'Önizleme: bu sembol için örnek veri yok')
})

test('Invalid responses and network errors remain failures without success-shaped fallback', async () => {
  const error = await marketDataResponseError(new Response('<html>Unavailable</html>', {status: 502}))
  assert.equal(error.kind, 'backend')
  assert.match(error.message, /geçersiz yanıt/)
  assert.equal(marketDataFailure(new DOMException('Timeout', 'TimeoutError')).kind, 'frontend')
  assert.equal(marketDataFailure(new TypeError('Failed to fetch')).kind, 'backend')
  const original = new MarketDataFailure('history', 'No candle history')
  assert.equal(marketDataFailure(original), original)
})
