import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {componentModule} from './moderator-component-test.mjs'
import * as moderator from '../moderator-model.ts'

const model = await componentModule('customer-event-model.ts', {'./moderator-model': moderator})
const ui = await componentModule('moderator-ui.tsx', {'./moderator-model': moderator})
const events = await componentModule('CustomerEvents.tsx', {
  './moderator-ui': ui, './moderator-model': moderator, './customer-event-model': model,
  './moderator-api': {moderatorRequest: () => {throw new Error('SSR must not fetch')}},
})
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props))
const row = {kind: 'auth.login_failed', code: 'invalid_credentials', feature: 'auth', http_status: 401,
  count: 5, first_at: '2026-10-10T12:00:00Z', last_at: '2026-10-10T12:09:00Z',
  request_ref: '12345678', source: 'customer_event', severity: 'WARNING', resolved: null}
const page = {items: [row], total: 1, limit: 25, offset: 0}

test('Fixed Turkish dictionary covers every event and unknown codes reveal no raw text', () => {
  for (const kind of model.EVENT_KINDS) assert.ok(model.eventKindLabels[kind])
  assert.equal(model.eventCodeLabel('invalid_credentials'), 'Geçersiz giriş bilgileri')
  assert.equal(model.eventCodeLabel('<script>secret</script>'), 'Diğer olay')
  assert.equal(model.eventCodeLabel('__proto__'), 'Diğer olay')
})

test('Timeline has empty state, real repeated counts and safe labels without raw code', () => {
  assert.match(render(events.EventTimeline, {items: []}), /veri yok/)
  const html = render(events.EventTimeline, {items: [row]})
  assert.match(html, /Giriş hatası/)
  assert.match(html, /×5/)
  assert.match(html, /5 tekrar kaydedildi/)
  assert.doesNotMatch(html, /invalid_credentials|12345678/)
  assert.match(render(events.EventTimeline, {items: [{...row, code: '<img src=x onerror=alert(1)>'}]}), /Diğer olay/)
})

test('Overflow and system rows are accurately labelled, not attributed login counts', () => {
  const html = render(events.EventTimeline, {items: [{...row, kind: 'api.error', code: 'event_limit', feature: 'events'},
    {...row, kind: 'api.error', code: 'server_error', source: 'system_error', count: 1, resolved: true}]})
  assert.match(html, /Ek olaylar/)
  assert.match(html, /5 ek olay sayaçta toplandı/)
  assert.match(html, /Sistem hatası kaynağı/)
  assert.match(html, /Çözüldü/)
})

test('DTO parser strictly projects fields and rejects private or malformed data', () => {
  assert.deepEqual(model.parseCustomerEvents({...page, items: [{...row, context: {token: 'secret'}, email: 'private@example.test'}]}), page)
  for (const item of [{...row, code: 'private@example.test'}, {...row, kind: 'unknown'}, {...row, count: 0},
    {...row, feature: '/api/private'}, {...row, request_ref: 'Bearer secret'}]) {
    assert.throws(() => model.parseCustomerEvents({...page, items: [item]}))
  }
  assert.throws(() => model.parseCustomerEvents({...page, limit: 51}))
})

test('OWNER notice links to actual user management and offers no moderator create action', () => {
  const html = render(events.OwnerApprovalNotice, {})
  assert.match(html, /Yönetici olarak işlemi doğrudan yapabilirsiniz/)
  assert.match(html, /href="\/admin\?section=users"/)
  assert.doesNotMatch(html, /<form|Talep oluştur/)
})

test('OWNER panel gates creation and own moderator overview, preserves support profile navigation', () => {
  const panel = readFileSync(new URL('../ModeratorPanel.tsx', import.meta.url), 'utf8')
  assert.match(panel, /canRequest=\{!owner && permissions.includes\('approvals.create'\)\}/)
  assert.match(panel, /me.role === 'MODERATOR' && me.permissions.includes\('approvals.create'\)/)
  assert.match(panel, /me.role === 'OWNER' \? <OwnerApprovalNotice\/> : <ModeratorApprovals\/>/)
  assert.match(panel, /Admin paneline dön/)
  assert.match(panel, /id => navigate\('customers', id\)/)
  const admin = readFileSync(new URL('../AdminPanel.tsx', import.meta.url), 'utf8')
  assert.match(admin, /'moderator','Moderatör Paneli',Users/)
  assert.match(admin, /window.location.assign\('\/moderator'\)/)
})
