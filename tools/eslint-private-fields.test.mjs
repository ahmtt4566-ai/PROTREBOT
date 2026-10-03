import assert from 'node:assert/strict'
import {test} from 'node:test'
import {join} from 'node:path'
import {Linter} from 'eslint'
import parser from '@babel/eslint-parser'
import rule from './eslint-private-fields.mjs'
import {inputInventory} from './input-inventory.mjs'

const linter = new Linter()
const config = [{
  files: ['**/*.tsx'],
  languageOptions: {parser, parserOptions: {requireConfigFile: false, babelOptions: {babelrc: false, configFile: false, parserOpts: {plugins: ['typescript', 'jsx']}}}},
  plugins: {kais: {rules: {'private-password': rule}}},
  rules: {'kais/private-password': 'error'},
}]
const verify = code => linter.verify(code, config, {filename: 'sample.tsx'})

for (const attributes of [
  'type="password"',
  'type={reveal ? "text" : "password"}',
  'type={reveal ? "password" : "text"}',
  'type={"password"} data-private="false"',
  'type="password" data-private',
  'type="password" data-private=""',
  'type="password" data-private={false}',
  'type="password" data-private={condition}',
]) {
  test(`Requires the exact private marker: ${attributes}`, () => {
    const code = `const field = <input ${attributes}/>`
    const messages = verify(code)
    assert.equal(messages.length, 1)
    assert.equal(messages[0].ruleId, 'kais/private-password')
    const fixed = linter.verifyAndFix(code, config, {filename: 'sample.tsx'})
    assert.equal(fixed.fixed, true)
    assert.match(fixed.output, /data-private="true"/)
    assert.deepEqual(verify(fixed.output), [])
  })
}

for (const attributes of [
  'type="password" data-private="true"',
  'type={reveal ? "text" : "password"} data-private="true"',
  'type="password" data-private={true}',
  'type="text"',
  'type="search" data-private="false"',
]) {
  test(`Accepts safe or non-password declarations: ${attributes}`, () => {
    assert.deepEqual(verify(`const field = <input ${attributes}/>`), [])
  })
}

test('Every application password declaration, including reveal toggles and compatibility copies, is marked', () => {
  const fields = inputInventory()
  const passwords = fields.filter(field => field.password)
  assert.ok(passwords.length > 0)
  assert.deepEqual(passwords.filter(field => !field.private), [])
  assert.equal(fields.some(field => field.file === 'TestnetFirstApp.tsx' && field.active), true)
  assert.equal(fields.some(field => field.file === join('frontend', 'src', 'LiveTradingPanel.tsx') && field.active), true)
  assert.equal(fields.some(field => field.file === join('frontend', 'src', 'TestnetFirstApp.tsx') && field.active), false)
})
