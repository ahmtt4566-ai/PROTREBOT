import assert from 'node:assert/strict'
import {test} from 'node:test'
import {BrowserRequestTimeoutError, withRequestDeadline} from '../browser-request.ts'

test('Deadline covers the complete response body and aborts its transport', async () => {
  let signal
  await assert.rejects(withRequestDeadline(async value => {
    signal = value
    await Promise.resolve({ok:true})
    return new Promise(() => {})
  },{timeoutMs:20}),BrowserRequestTimeoutError)
  assert.equal(signal.aborted,true)
})

test('A non-cooperative request cannot keep the caller pending past its deadline', async () => {
  let resolve
  const pending = new Promise(done => { resolve = done })
  await assert.rejects(withRequestDeadline(() => pending,{timeoutMs:20}),BrowserRequestTimeoutError)
  resolve('late-success')
})

test('Caller cancellation is forwarded without a timeout or successful fallback', async () => {
  const controller = new AbortController()
  const reason = new Error('navigation cancelled')
  let signal
  const pending = withRequestDeadline(async value => {
    signal = value
    return new Promise(() => {})
  },{signal:controller.signal,timeoutMs:500})
  await Promise.resolve()
  controller.abort(reason)
  await assert.rejects(pending,error => error === reason)
  assert.equal(signal.aborted,true)
  assert.equal(signal.reason,reason)
})

test('Already cancelled operations never run, and invalid deadlines fail explicitly', async () => {
  const controller = new AbortController()
  controller.abort(new Error('already cancelled'))
  let calls = 0
  await assert.rejects(withRequestDeadline(async () => { calls++; return true },{signal:controller.signal}),/already cancelled/)
  assert.equal(calls,0)
  for (const timeoutMs of [0,-1,NaN,Infinity]) {
    await assert.rejects(withRequestDeadline(async () => true,{timeoutMs}),RangeError)
  }
})

test('Success and explicit errors preserve their values without aborting a completed request', async () => {
  let signal
  const value = {authorized:false}
  assert.equal(await withRequestDeadline(async current => { signal = current; return value }),value)
  assert.equal(signal.aborted,false)
  const error = new Error('server denied access')
  await assert.rejects(withRequestDeadline(async () => { throw error }),actual => actual === error)
})
