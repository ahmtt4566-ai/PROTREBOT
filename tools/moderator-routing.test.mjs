import assert from 'node:assert/strict'
import {test} from 'node:test'
import {authenticatedPath, accountRoleLabel} from '../account-role.ts'

test('Authenticated landing routes follow the canonical session role', () => {
  for (const path of ['/login', '/register', '/verify-email']) {
    assert.equal(authenticatedPath('OWNER', path), '/admin')
    assert.equal(authenticatedPath('MODERATOR', path), '/moderator')
    assert.equal(authenticatedPath('CUSTOMER', path), '/dashboard')
  }
})

test('Moderator cannot render admin; customer cannot render moderator', () => {
  assert.equal(authenticatedPath('MODERATOR', '/admin/users'), '/moderator')
  assert.equal(authenticatedPath('CUSTOMER', '/moderator'), '/dashboard')
  assert.equal(authenticatedPath('OWNER', '/moderator'), '/moderator')
  assert.equal(authenticatedPath('CUSTOMER', '/admin'), '/admin')
})

test('Profile, MFA setup and normal customer pages retain their routes', () => {
  for (const role of ['OWNER', 'MODERATOR', 'CUSTOMER']) {
    for (const path of ['/settings', '/profile', '/master-trade']) assert.equal(authenticatedPath(role, path), path)
  }
  assert.equal(accountRoleLabel('MODERATOR'), 'Moderatör')
  assert.equal(accountRoleLabel('OWNER'), 'Yönetici')
  assert.equal(accountRoleLabel('CUSTOMER'), 'Kullanıcı')
})
