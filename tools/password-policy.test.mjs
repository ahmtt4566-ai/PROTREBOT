import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {stripTypeScriptTypes} from 'node:module'
import {test} from 'node:test'
import {runInNewContext} from 'node:vm'

const policy = JSON.parse(readFileSync(new URL('../backend/app/password_policy.json', import.meta.url), 'utf8'))
const vectors = JSON.parse(readFileSync(new URL('../backend/tests/auth_password_vectors.json', import.meta.url), 'utf8'))
const source = readFileSync(new URL('../password-policy.ts', import.meta.url), 'utf8').replace(/^import .*$/m, '')
const code = stripTypeScriptTypes(source).replace(/\bexport (?=function|const)/g, '') +
  '\nObject.assign(exports, {passwordRules, passwordPolicyError})'
const exports = {}
runInNewContext(code, {policy, exports})

for (const [index, vector] of vectors.entries()) {
  test(`Shared Python/browser password policy vector ${index + 1}`, () => {
    const password = vector.password + (vector.repeat ?? '').repeat(vector.count ?? 0)
    assert.equal(exports.passwordRules(password).every(([, passed]) => passed), vector.valid)
    assert.equal(exports.passwordPolicyError(password) === null, vector.valid)
    if (!vector.valid) assert.ok(!exports.passwordPolicyError(password).includes(password))
  })
}
