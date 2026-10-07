import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'
import parser from '@babel/eslint-parser'

const source = relative => readFileSync(new URL(`../${relative}`, import.meta.url), 'utf8')
const live = source('frontend/src/LiveTradingPanel.tsx')
const master = source('MasterTrade.tsx')
const reference = source('MasterTradeReference.tsx')
const parse = text => parser.parseForESLint(text, {
  requireConfigFile: false,
  babelOptions: {babelrc: false, configFile: false, parserOpts: {plugins: ['typescript', 'jsx']}},
}).ast

test('Latest signal risk displays the unchanged policy value in USDT, not percent', () => {
  assert.match(live, /<small>RISK<\/small><b>\{numericPolicy\('max_loss_per_trade', 1\)\} USDT<\/b>/)
  assert.doesNotMatch(live, /<small>RISK<\/small><b>\{numericPolicy\('max_loss_per_trade', 1\)\}%/)
})

test('Both analysis layouts explicitly identify a presentation decision and LIVE target allocation', () => {
  assert.match(master, /Presentation score only — not an order decision\./)
  assert.match(reference, /Final Decision is a presentation score; it does not send orders or grant Auto Trade eligibility\./)
  for (const text of [master, reference]) assert.match(text, /Target R\/R; LIVE: TP1 60%, remainder TP3\./)
})

test('Effective trap display applies min(35, policy) without changing a policy field', () => {
  const ast = parse(live)
  const matches = []
  const walk = node => {
    if (!node || typeof node !== 'object') return
    if (node.type === 'CallExpression' && node.callee.type === 'MemberExpression'
      && node.callee.object.name === 'Math' && node.callee.property.name === 'min'
      && node.arguments[1]?.type === 'CallExpression'
      && node.arguments[1].callee.name === 'numericPolicy'
      && node.arguments[1].arguments[0].value === 'max_trap_score') matches.push(node)
    for (const [key, value] of Object.entries(node)) {
      if (['loc', 'range', 'tokens', 'comments'].includes(key)) continue
      if (Array.isArray(value)) value.forEach(walk)
      else if (value && typeof value === 'object') walk(value)
    }
  }
  walk(ast)
  assert.equal(matches.length, 1)
  assert.equal(matches[0].arguments[0].value, 35)
  assert.equal(matches[0].arguments[1].arguments[1].value, 35)
  for (const [policy, expected] of [[10, 10], [35, 35], [60, 35]]) {
    assert.equal(Math.min(matches[0].arguments[0].value, policy), expected)
  }
})
