import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {componentModule} from './moderator-component-test.mjs'
import * as moderator from '../moderator-model.ts'

const model = await componentModule('campaign-model.ts', {'./moderator-model': moderator})
const ui = await componentModule('moderator-ui.tsx', {'./moderator-model': moderator})
const campaignUi = await componentModule('campaign-ui.tsx', {'./moderator-model': moderator, './moderator-ui': ui, './campaign-model': model})
const approval = await componentModule('approval-model.ts', {'./moderator-model': moderator, './campaign-model': model})
const approvalUi = await componentModule('CampaignApprovalPreview.tsx', {'./campaign-ui': campaignUi, './moderator-ui': ui})
const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props))
const preview = {subject: '<script>Bilgilendirme</script>', text: '<img src=x onerror=alert(1)>', audience: 'all_users',
  content_hash: 'a'.repeat(64), recipient_count: 4, opt_out_count: 1, warnings: ['Yatırım vaadi gibi görünebilir'],
  approximate_schedule: true, personalized_footer: true, delivery_enabled: false}
const request = {id: 'approval', action_type: 'campaign.send', target_user_id: null, requester_user_id: 'mod',
  requester_role: 'MODERATOR', target_snapshot: {}, payload: {campaign_id: 'campaign', content_hash: 'a'.repeat(64)},
  reason: 'Bilgilendirme duyurusu', status: 'pending', decided_by: null, decided_at: null, decision_note: null,
  executed_at: null, result_code: null, expires_at: '2026-10-13T12:00:00Z', version: 1, created_at: '2026-10-10T12:00:00Z', needs_review: false}

test('Announcement status pills and audience choices are fixed, Turkish and info-only', () => {
  for (const status of model.CAMPAIGN_STATUSES) assert.match(render(campaignUi.CampaignBadge, {status}), new RegExp(model.campaignStatusLabels[status]))
  assert.deepEqual(model.AUDIENCES, ['all_users', 'premium_users', 'team_only'])
  assert.ok(moderator.MODERATOR_PERMISSIONS.includes('campaigns.manage'))
  assert.ok(moderator.visibleModeratorSections(['campaigns.manage']).some(s => s.label === 'Duyurular'))
})

test('Preview escapes plain text and exposes warnings, opt-outs, approximate scheduling and disabled delivery', () => {
  const html = render(campaignUi.CampaignPreviewView, {preview})
  assert.match(html, /&lt;script&gt;/)
  assert.match(html, /&lt;img/)
  assert.doesNotMatch(html, /<script>|<img /)
  assert.match(html, /Yatırım vaadi gibi görünebilir/)
  assert.match(html, /4 alıcı/)
  assert.match(html, /Zamanlama yaklaşık/)
  assert.match(html, /gönderildi sayılmaz/)
})

test('Recipient confirmation has exact count and all controls disabled while busy', () => {
  const html = render(campaignUi.CampaignConfirmation, {preview, busy: true, onConfirm() {}, onCancel() {}})
  assert.match(html, /role="dialog"/)
  assert.match(html, /4 alıcı/)
  assert.match(html, /<button disabled="">İşlem sürüyor/)
  assert.match(html, /<button disabled="">Vazgeç/)
})

test('Campaign approval is a separate typed action without any invented user target', () => {
  const value = approval.parseApproval(request)
  assert.equal(value.action_type, 'campaign.send')
  assert.equal(value.target_snapshot, null)
  assert.throws(() => approval.parseApproval({...request, target_user_id: 'invented'}))
  assert.throws(() => approval.parseApproval({...request, payload: {...request.payload, content_hash: 'wrong'}}))
  assert.throws(() => approval.parseApproval({...request, action_type: 'account.delete'}))
})

test('OWNER preview keeps investment warnings and stale hash disables approval', () => {
  const html = render(approvalUi.default, {detail: {request: approval.parseApproval(request), campaign_preview: preview,
    target_changed: true, current_target: null, active_subscription: false}, busy: false, note: '', onNote() {}, onApprove() {}, onReject() {}})
  assert.match(html, /Yatırım vaadi gibi görünebilir/)
  assert.match(html, /İçerik değişti/)
  assert.match(html, /<button disabled="">Onayla/)
  assert.match(html, /<button disabled="">Reddet/)
})

test('Preview parser projects data and rejects malformed counts or audiences', () => {
  assert.deepEqual(model.parseCampaignPreview({...preview, recipient_email: 'private@example.test'}), preview)
  assert.throws(() => model.parseCampaignPreview({...preview, recipient_count: -1}))
  assert.throws(() => model.parseCampaignPreview({...preview, audience: 'arbitrary_sql'}))
  assert.throws(() => model.parseCampaignPreview({...preview, warnings: [{}]}))
})

test('Write controls use an in-flight latch and no authentication or subscription mail endpoints', () => {
  const source = readFileSync(new URL('../ModeratorCampaigns.tsx', import.meta.url), 'utf8')
  assert.match(source, /if \(inFlight.current\) return/)
  assert.match(source, /inFlight.current = true/)
  assert.match(source, /confirmed_recipient_count: preview.recipient_count/)
  assert.match(source, /content_hash: preview.content_hash/)
  assert.doesNotMatch(source, /\/auth\/|\/subscriptions|\/customers\/.*\/status/)
})

test('Canonical unsubscribe GET and POST are routed to backend before SPA on both builds', () => {
  for (const path of ['../vercel.json', '../frontend/vercel.json']) {
    const routes = JSON.parse(readFileSync(new URL(path, import.meta.url), 'utf8')).rewrites
    assert.equal(routes[0].source, '/announcements/unsubscribe')
    assert.match(routes[0].destination, /\/announcements\/unsubscribe$/)
  }
})
