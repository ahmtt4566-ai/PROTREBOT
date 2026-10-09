import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {stripTypeScriptTypes} from 'node:module'
import {test} from 'node:test'
import {withRequestDeadline} from '../browser-request.ts'
import {demoAccount, demoStatus, demoSummary} from '../frontend/tests/helpers/demo-api.ts'

const source = readFileSync(new URL('../demo-request.ts',import.meta.url),'utf8')
const outputText = stripTypeScriptTypes(source).replace(/^import .+$/gm,'').replace(/^export /gm,'')
function helpers(fetch) {
  const buildDemoSavePayload = () => { throw new Error('This fixture uses existing credentials only.') }
  return new Function('withRequestDeadline','buildDemoSavePayload','fetch',
    `${outputText}\nreturn {checkDemoResponse,demoRequest,startDemoAutomation}`)(withRequestDeadline,buildDemoSavePayload,fetch)
}

const {checkDemoResponse} = helpers(() => { throw new Error('Network is forbidden in contract tests.') })

test('Complete Demo contracts are accepted and account snapshots may omit unrelated status limits',() => {
  checkDemoResponse(demoStatus,'status')
  checkDemoResponse(demoAccount,'account')
  checkDemoResponse(demoSummary,'summary')
  const {limits: _limits,...account} = demoAccount
  checkDemoResponse(account,'account')
})

for (const limits of [undefined,null,{},'limits',{...demoStatus.limits,max_open_positions:'5'},
  {...demoStatus.limits,max_open_positions:0},{...demoStatus.limits,max_notional_usdt:NaN}]) {
  test(`Invalid Demo limits ${JSON.stringify(limits)} cannot become status`,() => {
    assert.throws(() => checkDemoResponse({...demoStatus,limits},'status'),/Demo durum yanıtı/)
  })
}

test('Malformed summaries, scanner arrays and balances fail before rendering',() => {
  for (const payload of [{},null,{...demoSummary,scanner:null},
    {...demoSummary,scanner:{...demoSummary.scanner,all_candidates:undefined}},
    {...demoSummary,journal:[{created_at:null,message:'invalid'}]}]) {
    assert.throws(() => checkDemoResponse(payload,'summary'),/Demo merkez yanıtı/)
  }
  assert.throws(() => checkDemoResponse({...demoAccount,configured:true,wallet_balance:'0'},'account'),/Demo hesap yanıtı/)
  assert.throws(() => checkDemoResponse({...demoStatus,real_trading_locked:false},'status'),/Demo durum yanıtı/)
})

test('HTML success is an explicit error, not an empty successful response',async () => {
  const {demoRequest} = helpers(async () => new Response('<!doctype html>',{status:200}))
  await assert.rejects(demoRequest('/api/binance-demo','/status',undefined,String),/geçerli JSON yanıtı/)
})

test('Authentication errors preserve their server message and POSTs are never retried',async () => {
  let attempts = 0
  const {demoRequest} = helpers(async () => { attempts++;return Response.json({detail:'Demo oturumu gerekli'},{status:401}) })
  await assert.rejects(demoRequest('/api/binance-demo','/arm',{method:'POST'},String),/Demo oturumu gerekli/)
  assert.equal(attempts,1)
})

test('Caller cancellation aborts a non-cooperative Demo transport without a fallback',async () => {
  let signal
  const {demoRequest} = helpers(async (_url,options) => { signal = options.signal;return new Promise(() => {}) })
  const controller = new AbortController()
  const pending = demoRequest('/api/binance-demo','/status',{signal:controller.signal},String)
  await Promise.resolve()
  const reason = new Error('Navigation cancelled')
  controller.abort(reason)
  await assert.rejects(pending,error => error === reason)
  assert.equal(signal.aborted,true)
})

for (const flags of [{enabled:false,send_orders:false},{enabled:false,send_orders:true},{enabled:true,send_orders:false}]) {
  test(`Original flags ${JSON.stringify(flags)} reject explicit activation before any mutation`,async () => {
    let calls = 0
    const {startDemoAutomation} = helpers(async () => { calls++;throw new Error('No transport should run.') })
    await assert.rejects(startDemoAutomation('/api',null,'kais-original-v2-demo-v1',flags,String),/sunucu ayarında kapalı/)
    assert.equal(calls,0)
  })
}

test('One explicit start uses only TESTNET readiness, existing arm and canonical Original identity',async () => {
  const calls = []
  const {startDemoAutomation} = helpers(async (url,options) => {
    calls.push({url,body:options.body ? JSON.parse(options.body) : null})
    return Response.json(url.endsWith('/arm') ? {...demoStatus,configured:true,connected:true,armed:true}
      : url.endsWith('/auto/start') ? {...demoSummary,auto:{...demoSummary.auto,enabled:true}} : {ok:true})
  })
  await startDemoAutomation('/api',null,'kais-original-v2-demo-v1',{enabled:true,send_orders:true},String)
  assert.deepEqual(calls.map(call => call.url),[
    '/api/exchange-connections/test','/api/exchange-connections/activate',
    '/api/binance-demo/connect','/api/binance-demo/arm','/api/v21/auto/start',
  ])
  assert.deepEqual(calls[0].body,{mode:'TESTNET'})
  assert.deepEqual(calls[1].body,{mode:'TESTNET',confirmation:'TESTNET BAĞLANTIYI AÇ'})
  assert.deepEqual(calls[3].body,{confirmation:'DEMO'})
  assert.deepEqual(calls[4].body,{confirmation:'DEMO OTOMATİK',strategy_id:'kais-original-v2-demo-v1'})
})
