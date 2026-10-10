import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {componentModule} from './moderator-component-test.mjs'
import * as moderator from '../moderator-model.ts'

const model = await componentModule('approval-model.ts', {'./moderator-model': moderator})
const ui = await componentModule('moderator-ui.tsx', {'./moderator-model': moderator})
const approvalUi = await componentModule('approval-ui.tsx', {'./moderator-model': moderator, './moderator-ui': ui, './approval-model': model})
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props))
const row = {
  id: 'approval-id', action_type: 'account.deactivate', target_user_id: 'customer-id', requester_user_id: 'moderator-id',
  requester_role: 'MODERATOR', target_snapshot: {active: true, role: 'CUSTOMER', auth_version: 1},
  reason: '<script>alert(1)</script>', status: 'pending', decided_by: null, decided_at: null, decision_note: null, executed_at: null,
  result_code: null, expires_at: '2026-10-13T10:00:00Z', version: 1, created_at: '2026-10-10T10:00:00Z', needs_review: false,
}
const detail = {request: row, current_target: row.target_snapshot, target_changed: false, active_subscription: true}
const props = {detail, busy: false, note: '', onNote: () => {}, onApprove: () => {}, onReject: () => {}, onRetry: () => {}}

test('Approval pills expose every Turkish state and explicit intervention state', () => {
  for (const status of model.APPROVAL_STATUSES) assert.match(render(approvalUi.ApprovalBadge, {status}), new RegExp(model.approvalLabels[status]))
  assert.match(render(approvalUi.ApprovalBadge, {status: 'failed', needsReview: true}), /Kontrol gerekli/)
})

test('Empty approvals use no fabricated numbers; pending cancellation is the only moderator write control', () => {
  assert.match(render(approvalUi.ApprovalCards, {items: []}), /Henüz onay talebi yok/)
  const html = render(approvalUi.ApprovalCards, {items: [row]})
  assert.match(html, /Talebi iptal et/)
  assert.doesNotMatch(html, /Onayla|Reddet/)
  assert.doesNotMatch(render(approvalUi.ApprovalCards, {items: [{...row, status: 'executed'}]}), /Talebi iptal et/)
})

test('Reason and decision notes remain escaped text, never HTML', () => {
  const html = render(approvalUi.ApprovalCards, {items: [{...row, decision_note: '<img src=x onerror=alert(1)>'}]})
  assert.match(html, /&lt;script&gt;/)
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/)
  assert.doesNotMatch(html, /<script>|<img src=x/)
})

test('Reason and rejection forms enforce lengths and disable all actions while busy', () => {
  assert.match(render(approvalUi.ApprovalReasonForm, {reason: 'short', busy: false, onReason: () => {}, onSubmit: () => {}}), /minLength="10"/)
  assert.match(render(approvalUi.ApprovalReasonForm, {reason: 'Valid reason text', busy: true, onReason: () => {}, onSubmit: () => {}}), /<textarea[^>]*disabled=""/)
  const html = render(approvalUi.ApprovalOwnerDetail, {...props, busy: true, note: 'Not'})
  assert.match(html, /<button disabled="">İşlem sürüyor/)
  assert.match(html, /<button disabled="">Reddet/)
  assert.match(render(approvalUi.ApprovalOwnerDetail, props), /<button disabled="">Reddet/)
})

test('Owner detail warns about real subscriptions and stale target prevents approve', () => {
  const html = render(approvalUi.ApprovalOwnerDetail, {...props, detail: {...detail, target_changed: true}})
  assert.match(html, /aktif aboneliği var/)
  assert.match(html, /<button disabled="">Onayla/)
})

test('Agent cleanup retry is explicit and never offers canonical replay of executing requests', () => {
  const failed = {...row, status: 'failed', result_code: 'agents_revoke_pending', needs_review: true}
  assert.equal(model.canRetryAgents(failed), true)
  assert.equal(model.canRetryAgents({...failed, result_code: 'execution_failed'}), false)
  assert.match(render(approvalUi.ApprovalOwnerDetail, {...props, detail: {...detail, request: failed}}), /Yalnız ajan iptalini yeniden dene/)
  const html = render(approvalUi.ApprovalOwnerDetail, {...props, detail: {...detail, request: {...row, status: 'executing'}}})
  assert.doesNotMatch(html, />Onayla<|yeniden dene/)
})

test('Approval error responses are Turkish and raw server messages are never displayed', () => {
  for (const status of [404, 409, 429]) {
    const html = render(approvalUi.ApprovalError, {error: new moderator.ModeratorRequestError(status), onRefresh: () => {}})
    assert.match(html, /Yenile/)
  }
  assert.match(model.approvalErrorMessage(new moderator.ModeratorRequestError(403, 'mfa_required')), /iki adımlı doğrulamayı aç/)
  assert.doesNotMatch(model.approvalErrorMessage(new Error('private@example.test token')), /private@example|token/)
})

test('Approval parsers only project allowed fields and reject unsafe DTO shapes', () => {
  assert.deepEqual(model.parseApproval({...row, token: 'private'}), row)
  assert.throws(() => model.parseApproval({...row, action_type: 'account.delete'}))
  assert.throws(() => model.parseApproval({...row, target_snapshot: {...row.target_snapshot, auth_version: '1'}}))
  assert.throws(() => model.parseApproval({...row, result_code: 'private exception'}))
  assert.throws(() => model.parseApproval({...row, status: 'unknown'}))
  assert.throws(() => model.parseApprovalPage({items: [], limit: 51, offset: 0, total: 0, pending_count: 0}))
})

test('New admin integration is isolated and moderator shortcut cannot call old status endpoint', () => {
  const admin = readFileSync(new URL('../AdminPanel.tsx', import.meta.url), 'utf8')
  assert.match(admin, /section === 'approvals' && <AdminApprovals/)
  const moderatorSource = readFileSync(new URL('../ModeratorApprovals.tsx', import.meta.url), 'utf8')
  assert.doesNotMatch(moderatorSource, /\/customers\/.*\/status|\/subscriptions|\/payments/)
  assert.match(moderatorSource, /inFlight\.current = true/)
  const ownerSource = readFileSync(new URL('../AdminApprovals.tsx', import.meta.url), 'utf8')
  assert.match(ownerSource, /inFlight\.current = true/)
  assert.match(ownerSource, /window\.confirm/)
})
