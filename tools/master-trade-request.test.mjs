import assert from 'node:assert/strict'
import {test} from 'node:test'
import {fetchWithTimeout} from '../master-trade-request.ts'

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
