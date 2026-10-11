import assert from 'node:assert/strict'
import {test} from 'node:test'
import {fetchPublicMarketData, fetchWithTimeout} from '../master-trade-request.ts'

test('Master requests never dispatch with already revoked authority', async () => {
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  const controller = new AbortController()
  controller.abort()
  let calls = 0
  globalThis.window = globalThis
  globalThis.fetch = async () => { calls++; return new Response('{}') }
  try {
    await assert.rejects(fetchWithTimeout('http://localhost/mock',{signal:controller.signal}),{name:'AbortError'})
    assert.equal(calls,0)
  } finally {
    globalThis.fetch = originalFetch
    globalThis.window = originalWindow
  }
})

test('Master requests time out explicitly without retrying a possibly accepted mutation', async () => {
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  let calls = 0
  globalThis.window = globalThis
  globalThis.fetch = (_input,init) => {
    calls++
    return new Promise((_resolve,reject) => init.signal.addEventListener('abort',() => reject(init.signal.reason),{once:true}))
  }
  try {
    await assert.rejects(fetchWithTimeout('http://localhost/mock',{method:'POST'},10),{name:'TimeoutError'})
    assert.equal(calls,1)
  } finally {
    globalThis.fetch = originalFetch
    globalThis.window = originalWindow
  }
})

test('Master timeout also covers a response body that never completes after headers', async () => {
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  let bodyController
  globalThis.window = globalThis
  globalThis.fetch = async (_input,init) => new Response(new ReadableStream({
    start(controller) {
      bodyController = controller
      init.signal.addEventListener('abort',() => controller.error(init.signal.reason),{once:true})
    },
  }))
  try {
    await assert.rejects(fetchWithTimeout('http://localhost/mock',{},10),{name:'TimeoutError'})
  } finally {
    bodyController?.error(new DOMException('Test cleanup','AbortError'))
    globalThis.fetch = originalFetch
    globalThis.window = originalWindow
  }
})

test('Public market backoff honors 180 seconds across paths without touching private requests', async () => {
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  const originalNow = Date.now
  let now = 1000000
  const calls = []
  globalThis.window = globalThis
  Date.now = () => now
  globalThis.fetch = async input => {
    calls.push(String(input))
    return calls.length === 1 ? Response.json({detail:'Rate limited'}, {status:429,headers:{'Retry-After':'180'}}) : Response.json({})
  }
  try {
    assert.equal((await fetchPublicMarketData('/api/markets?all=true')).status,429)
    now += 179000
    await assert.rejects(fetchPublicMarketData('/api/analysis/ETHUSDT?interval=1h'),/1 saniye bekleyin/)
    assert.equal(calls.length,1)
    assert.equal((await fetchWithTimeout('/api/v25/status')).status,200)
    assert.equal(calls.length,2)
    now += 1000
    assert.equal((await fetchPublicMarketData('/api/klines/BTCUSDT?interval=15m')).status,200)
    assert.equal(calls.length,3)
  } finally {
    globalThis.fetch = originalFetch
    globalThis.window = originalWindow
    Date.now = originalNow
  }
})

test('Public market helper cannot dispatch account, approval or order mutations', async () => {
  for (const [input,init] of [['/api/v25/status',{}],['/api/markets',{method:'POST'}],['/api/v25/auto/start',{method:'POST'}]]) {
    await assert.rejects(fetchPublicMarketData(input,init),/salt-okunur piyasa/)
  }
})

test('A symbol-specific 503 without Retry-After does not block a healthy symbol', async () => {
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  const calls = []
  globalThis.window = globalThis
  globalThis.fetch = async input => {
    calls.push(input)
    return Response.json({}, {status:input.includes('1000PEPEUSDT') ? 503 : 200})
  }
  try {
    assert.equal((await fetchPublicMarketData('/api/analysis/1000PEPEUSDT?interval=15m')).status,503)
    await assert.rejects(fetchPublicMarketData('/api/analysis/1000PEPEUSDT?interval=15m'),/saniye bekleyin/)
    assert.equal((await fetchPublicMarketData('/api/analysis/ETHUSDT?interval=15m')).status,200)
    assert.equal(calls.length,2)
  } finally {
    globalThis.fetch = originalFetch
    globalThis.window = originalWindow
  }
})
