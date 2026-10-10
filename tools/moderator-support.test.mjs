import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {componentModule} from './moderator-component-test.mjs'
import * as moderator from '../moderator-model.ts'

const model = await componentModule('support-model.ts', {'./moderator-model': moderator})
const ui = await componentModule('moderator-ui.tsx', {'./moderator-model': moderator})
const {SupportDetailCards, SupportPriorityBadge, SupportStatusBadge, SupportSummaryCards} = await componentModule('support-ui.tsx', {'./moderator-ui': ui, './moderator-model': moderator, './support-model': model})
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props))
const profile = {user_id: 'customer-id', email_masked: 'c***@example.test', role: 'CUSTOMER', active: true, created_at: null, email_verified: true, mfa_enabled: true}
const detail = {
  id: 'case-id', user_id: 'customer-id', subject: 'Destek talebi', message: '<script>alert(1)</script>',
  priority: 'HIGH', case_status: 'OPEN', assignee_user_id: null, version: 1, assigned_to_me: false,
  created_at: '2026-10-10T10:00:00+00:00', legacy_status: 'OPEN', legacy_response_note: '',
  customer: profile, notes: [],
}
const props = {detail, manage: false, busy: false, note: '', status: 'OPEN', onNoteChange: () => {}, onStatusChange: () => {},
  onTake: () => {}, onRelease: () => {}, onSaveStatus: () => {}, onAddNote: () => {}}

test('Status and priority pills are Turkish and carry text, not just color', () => {
  assert.match(render(SupportStatusBadge, {status: 'WAITING'}), /Beklemede/)
  assert.match(render(SupportStatusBadge, {status: 'RESOLVED'}), /Çözüldü/)
  assert.match(render(SupportPriorityBadge, {priority: 'HIGH'}), /Yüksek/)
})

test('Support empty notes and absent summary do not invent counts', () => {
  assert.match(render(SupportDetailCards, props), /Henüz ekip notu yok/)
  const html = render(SupportSummaryCards, {summary: null})
  assert.equal((html.match(/veri yok/g) || []).length, 3)
  assert.doesNotMatch(html, />0</)
  assert.match(render(SupportSummaryCards, {summary: {open_cases: 0, unassigned: 2, assigned_to_me: 3}}), />0</)
})

test('Read-only, unassigned, other-assignee and no-manage cases disable note/status edits', () => {
  for (const input of [props, {...props, manage: true}, {...props, detail: {...detail, assignee_user_id: 'other'}},
    {...props, detail: {...detail, assignee_user_id: 'moderator', assigned_to_me: true}}]) {
    const html = render(SupportDetailCards, input)
    assert.match(html, /Salt okunur/)
    assert.match(html, /<fieldset[^>]*disabled=""/)
    assert.match(html, /<textarea[^>]*disabled=""/)
  }
  const assigned = {...detail, assignee_user_id: 'moderator', assigned_to_me: true}
  const html = render(SupportDetailCards, {...props, detail: assigned, manage: true, note: 'Ekip notu'})
  assert.doesNotMatch(html, /Salt okunur/)
  assert.doesNotMatch(html, /<textarea[^>]*disabled=/)
  assert.match(html, />Bırak</)
  assert.equal(model.supportCanEdit(assigned, false), false)
})

test('Messages and HTML notes render escaped plain text with no XSS interpretation', () => {
  const html = render(SupportDetailCards, {...props, detail: {...detail, notes: [{id: 'note-id', author_user_id: 'moderator', body: '<img src=x onerror=alert(1)>', created_at: detail.created_at}]}})
  assert.match(html, /&lt;script&gt;/)
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/)
  assert.doesNotMatch(html, /<script>|<img src=x/)
})

test('409 supplies the refresh instruction without replaying a mutation', () => {
  const html = render(ui.ModError, {error: new moderator.ModeratorRequestError(409), onRetry: () => {}})
  assert.match(html, /Bu talep başka biri tarafından güncellendi, yenile/)
  const source = readFileSync(new URL('../ModeratorSupport.tsx', import.meta.url), 'utf8')
  assert.match(source, /onRetry=\{\(\) => setVersion/)
  assert.match(source, /expected_version: detail.version/)
})

test('Support response parsers project only safe fields and reject invalid shapes', () => {
  const parsed = model.parseSupportDetail({...detail, token: 'secret', raw_payment: {}})
  assert.deepEqual(Object.keys(parsed).sort(), Object.keys(detail).sort())
  assert.throws(() => model.parseSupportDetail({...detail, customer: {...profile, role: 'OWNER'}}))
  assert.throws(() => model.parseSupportCase({...detail, assigned_to_me: 'true'}))
  assert.throws(() => model.parseSupportCase({...detail, version: 0}))
  assert.throws(() => model.parseSupportPage({items: [], total: 0, limit: 51, offset: 0}))
  assert.throws(() => model.parseSupportSummary({open_cases: -1, unassigned: 0, assigned_to_me: 0}))
  assert.deepEqual(model.parseSupportSummary({open_cases: null, unassigned: null, assigned_to_me: null}), {open_cases: null, unassigned: null, assigned_to_me: null})
})

test('Filter query has bounded pagination and exact state/assignment fields', () => {
  assert.equal(new URLSearchParams(model.supportFilterQuery('mine', 'HIGH', 25)).get('assignment'), 'mine')
  const query = new URLSearchParams(model.supportFilterQuery('waiting', '', 0))
  assert.equal(query.get('limit'), '25')
  assert.equal(query.get('status'), 'WAITING')
  assert.equal(query.has('priority'), false)
})
