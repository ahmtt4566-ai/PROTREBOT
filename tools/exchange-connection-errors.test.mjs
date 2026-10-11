import assert from 'node:assert/strict'
import {test} from 'node:test'
import {CONNECTION_REQUEST_TIMEOUT_MS, connectionFailureMessage, connectionHttpMessage, redactConnectionMessage} from '../frontend/src/exchange-connection-errors.ts'

test('verification timeout covers four default 15-second read-only checks', () => {
  assert.ok(CONNECTION_REQUEST_TIMEOUT_MS > 4 * 15000)
})

test('network, timeout and invalid response errors always have Turkish explanations', () => {
  assert.match(connectionFailureMessage(new TypeError('Failed to fetch'), []), /Bağlantı servisine ulaşılamadı/)
  assert.match(connectionFailureMessage(new DOMException('aborted', 'TimeoutError'), []), /zaman aşımına/)
  assert.match(connectionFailureMessage(new SyntaxError('bad json'), []), /geçerli bir yanıt/)
  assert.match(connectionFailureMessage(null, []), /Binance bağlantısı kurulamadı/)
})

test('server errors never reflect internal details or credentials', () => {
  assert.equal(connectionHttpMessage(500, 'private-key private-secret traceback', ['private-key', 'private-secret']),
    'Sunucu hatası (HTTP 500). Bağlantı kaydedilemedi veya doğrulanamadı; sunucu durumunu kontrol edin.')
  assert.equal(redactConnectionMessage('private-key-private-secret private-key', ['private-key', 'private-key-private-secret']),
    '[GİZLİ] [GİZLİ]')
  assert.match(connectionHttpMessage(502, 'private-key private-secret', ['private-key', 'private-secret']), /\[GİZLİ\] \[GİZLİ\]/)
})
