import assert from 'node:assert/strict'
import {test} from 'node:test'
import {demoAccount, demoHistory, demoPerformance, demoReadFixtures, demoStatus, demoSummary} from '../frontend/tests/helpers/demo-api.ts'

test('Demo status and account retain complete limits and locked execution', () => {
  assert.equal(demoReadFixtures['/api/binance-demo/status'], demoStatus)
  assert.equal(demoReadFixtures['/api/binance-demo/account'], demoAccount)
  assert.equal(demoStatus.limits.max_notional_usdt, 200)
  for (const value of Object.values(demoStatus.limits)) assert.ok(Number.isFinite(value) && value > 0)
  for (const row of [demoStatus, demoAccount]) {
    assert.equal(row.configured, false)
    assert.equal(row.connected, false)
    assert.equal(row.armed, false)
    assert.equal(row.real_trading_locked, true)
  }
  for (const field of ['positions', 'open_orders', 'open_algo_orders', 'plans']) assert.deepEqual(demoAccount[field], [])
})

test('V21 summary supplies scanner, settings, stream and certificate read contracts', () => {
  assert.equal(demoReadFixtures['/api/v21/summary'], demoSummary)
  assert.equal(demoReadFixtures['/api/v21/settings'], demoSummary.settings)
  assert.equal(demoSummary.auto.enabled, false)
  assert.equal(demoSummary.scanner.active, false)
  assert.equal(demoSummary.scanner.next_scan_at, null)
  assert.deepEqual(demoSummary.scanner.top_candidates, [])
  assert.deepEqual(demoSummary.scanner.selected_symbols, [])
  assert.deepEqual(demoSummary.journal, [])
  assert.deepEqual(demoSummary.certificate.gates, [])
  assert.equal(demoSummary.stream.status, 'DISCONNECTED')
  assert.equal(demoSummary.real_trading_locked, true)
  for (const value of Object.values(demoSummary.settings)) {
    if (typeof value === 'number') assert.ok(Number.isFinite(value))
  }
})

test('Performance, scanner candidates and history expose complete empty collections', () => {
  assert.equal(demoReadFixtures['/api/v21/performance'], demoPerformance)
  assert.deepEqual(demoPerformance.equity_curve, [])
  assert.deepEqual(Object.keys(demoPerformance.directional), ['LONG', 'SHORT'])
  assert.equal(demoPerformance.demo_only, true)
  assert.equal(demoPerformance.read_only, true)
  assert.deepEqual(demoReadFixtures['/api/v21/scanner/candidates'], {top_candidates: [], candidates: []})
  assert.deepEqual(demoReadFixtures['/api/v21/journal'], {items: []})
  assert.deepEqual(demoHistory, {orders: [], algo_orders: [], trades: []})
})

test('Shared fixture map does not fabricate execution or ARM responses', () => {
  for (const endpoint of ['arm', 'order', 'connect', 'auto/start']) {
    assert.equal(Object.keys(demoReadFixtures).some(path => path.endsWith(`/${endpoint}`)), false)
  }
})
