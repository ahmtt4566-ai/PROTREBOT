import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'
import React from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import * as model from '../moderator-model.ts'
import {componentModule} from './moderator-component-test.mjs'
const {ModEmpty, ModError, ModResourceView} = await componentModule('moderator-ui.tsx', {'./moderator-model': model})
const {default: AdminModeratorAccess} = await componentModule('AdminModeratorAccess.tsx', {'./moderator-model': model, './account-settings-api': {accountRequest: () => {throw new Error('SSR must not fetch')}}})
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props))
const profile = {user_id: 'customer-id', email_masked: 'a***@example.test', role: 'CUSTOMER', active: true, created_at: null, email_verified: true, mfa_enabled: false}

test('Menu exposes only permitted areas, with Turkish labels', () => {
  assert.deepEqual(model.visibleModeratorSections([]).map(item => item.id), ['overview'])
  assert.deepEqual(model.visibleModeratorSections(['payments.view']).map(item => item.id), ['overview', 'payments'])
  assert.equal(model.visibleModeratorSections(model.MODERATOR_PERMISSIONS).length, 7)
  assert.deepEqual(model.visibleModeratorSections(['customers.view', 'subscriptions.view']).map(item => item.label), ['Genel Bakış', 'Müşteriler', 'Abonelikler'])
  assert.deepEqual(model.visibleModeratorSections(['support.manage']).map(item => item.id), ['overview'])
})

test('MFA-required component supplies the Turkish message and reachable profile link', () => {
  const html = render(ModError, {error: new model.ModeratorRequestError(403, 'mfa_required')})
  assert.match(html, /Devam etmek için profilinden iki adımlı doğrulamayı aç/)
  assert.match(html, /href="\/settings"/)
  assert.doesNotMatch(html, /Yeniden dene/)
})

test('404, 429 and unknown failures are actionable and do not reflect server secrets', () => {
  assert.match(render(ModError, {error: new model.ModeratorRequestError(429)}), /Bir dakika bekleyip yeniden deneyin/)
  assert.match(render(ModError, {error: new model.ModeratorRequestError(404)}), /Kullanıcı numarasını kontrol edin/)
  assert.doesNotMatch(render(ModError, {error: new Error('token=secret@example.test')}), /token|secret@example/)
})

test('Empty payment and subscription components show no fabricated values', () => {
  assert.match(render(ModEmpty), /Henüz veri yok/)
  const payments = render(ModResourceView, {resource: {kind: 'payments', data: {user_id: 'customer-id', payment_status: 'veri yok', last_failed_payment_at: null}}})
  const subscription = render(ModResourceView, {resource: {kind: 'subscription', data: {user_id: 'customer-id', plan: null, subscription_status: null, current_period_end: null, cancel_at_period_end: null}}})
  for (const html of [payments, subscription]) {
    assert.match(html, /Henüz veri yok/)
    assert.match(html, /veri yok/)
    assert.doesNotMatch(html, /₺|\$|0\.00|Ödendi|Aktif/)
  }
})

test('Profile allowlist strips forbidden fields and rejects unmasked email or staff targets', () => {
  const safe = model.parseCustomerProfile({...profile, email: 'private@example.test', token: 'secret-token', stripe_customer_id: 'private-id', preferences: {}})
  assert.deepEqual(Object.keys(safe).sort(), Object.keys(profile).sort())
  const html = render(ModResourceView, {resource: {kind: 'profile', data: safe}})
  assert.match(html, /a\*\*\*@example.test/)
  assert.doesNotMatch(html, /private@example|secret-token|private-id/)
  assert.throws(() => model.parseCustomerProfile({...profile, email_masked: 'private@example.test'}), model.ModeratorRequestError)
  assert.throws(() => model.parseCustomerProfile({...profile, role: 'OWNER'}), model.ModeratorRequestError)
  assert.equal(model.parseCustomerProfile({...profile, email_masked: '***'}).email_masked, '***')
})

test('Response parsers reject invalid data instead of displaying false successful defaults', () => {
  assert.throws(() => model.parseModeratorMe({role: 'MODERATOR', permissions: ['admin']}))
  assert.throws(() => model.parseCustomerPage({items: [], total: 0, limit: 51, offset: 0}))
  assert.throws(() => model.parseCustomerProfile({...profile, active: 'true'}))
  assert.throws(() => model.parseCustomerSubscription({user_id: 'customer-id', plan: 'PREMIUM', subscription_status: null, current_period_end: null, cancel_at_period_end: null}))
  assert.throws(() => model.parseCustomerPayments({user_id: 'customer-id', payment_status: 'PENDING', last_failed_payment_at: null}))
})

test('OWNER cannot be promoted or demoted; moderator role buttons reflect actual role', () => {
  const props = {id: 'user-id', onChanged: async () => {}, onBusyChange: () => {}}
  const owner = render(AdminModeratorAccess, {...props, role: 'OWNER'})
  assert.match(owner, /<button disabled=""/)
  assert.match(render(AdminModeratorAccess, {...props, role: 'CUSTOMER'}), /Moderatör yap/)
  const moderator = render(AdminModeratorAccess, {...props, role: 'MODERATOR'})
  assert.match(moderator, /Moderatörlüğü kaldır/)
  assert.match(moderator, /İzinler yükleniyor/)
  assert.doesNotMatch(moderator, /type="checkbox"/)
})

test('Customer moderator requests remain GET-only and admin styles have not acquired mod selectors', () => {
  const api = readFileSync(new URL('../moderator-api.ts', import.meta.url), 'utf8')
  assert.match(api, /method: 'GET'/)
  assert.doesNotMatch(api, /method: '(POST|PATCH|DELETE|PUT)'/)
  const css = readFileSync(new URL('../moderator-panel.css', import.meta.url), 'utf8')
  assert.match(css, /prefers-reduced-motion:reduce/)
  assert.match(css, /var\(--admin-bg\)/)
  assert.doesNotMatch(readFileSync(new URL('../admin.css', import.meta.url), 'utf8'), /\.mod-/)
})
