import assert from 'node:assert/strict'
import {test} from 'node:test'
import {readFileSync} from 'node:fs'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {componentModule} from './moderator-component-test.mjs'
import * as moderator from '../moderator-model.ts'

const model = await componentModule('notification-model.ts', {'./moderator-model': moderator})
const {NotificationSummaryLine} = await componentModule('notification-ui.tsx')
const render = summary => renderToStaticMarkup(React.createElement(NotificationSummaryLine, {summary}))

test('Notification summary displays real counts and no invented fallback zeros', () => {
  assert.match(render({pending: 4, failed: 2}), /E-posta bildirimi: 4 bekliyor, 2 gönderilemedi/)
  assert.match(render({pending: 0, failed: 0}), /0 bekliyor, 0 gönderilemedi/)
  assert.equal((render(null).match(/veri yok/g) || []).length, 2)
  assert.equal((render({pending: null, failed: null}).match(/veri yok/g) || []).length, 2)
})
test('Notification parser rejects invalid counts and strips non-count data', () => {
  assert.deepEqual(model.parseNotificationSummary({pending: 1, failed: 2, email: 'private@example.test'}), {pending: 1, failed: 2})
  for (const row of [{pending: -1, failed: 0}, {pending: '1', failed: 0}, {}, {pending: 1.5, failed: 0}]) {
    assert.throws(() => model.parseNotificationSummary(row))
  }
})
test('Notification summary is connected only to the dedicated owner approvals area', () => {
  const source = readFileSync(new URL('../AdminApprovals.tsx', import.meta.url), 'utf8')
  assert.match(source, /<AdminNotificationSummary refresh=\{query.version\}/)
  const component = readFileSync(new URL('../AdminNotificationSummary.tsx', import.meta.url), 'utf8')
  assert.match(component, /approvalRequest\(true, '\/notifications\/summary'/)
  assert.match(component, /role="alert"/)
})
