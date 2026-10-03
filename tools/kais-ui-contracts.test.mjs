import assert from 'node:assert/strict'
import {readFileSync, existsSync} from 'node:fs'
import {dirname, extname, join, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
import {test} from 'node:test'
import parser from '@babel/eslint-parser'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const options = {requireConfigFile: false, babelOptions: {babelrc: false, configFile: false, parserOpts: {plugins: ['typescript', 'jsx']}}}
const parse = file => parser.parseForESLint(readFileSync(file, 'utf8'), {...options, filePath: file}).ast

for (const file of ['vite.config.ts', join('frontend', 'vite.config.ts')]) {
  test(`${file} keeps shared components on one React runtime`, () => {
    const ast = parse(join(root, file))
    const exported = ast.body.find(node => node.type === 'ExportDefaultDeclaration')
    const config = exported.declaration.arguments[0]
    const resolve = config.properties.find(property => property.key.name === 'resolve').value
    const dedupe = resolve.properties.find(property => property.key.name === 'dedupe').value.elements.map(element => element.value)
    assert.ok(dedupe.includes('react'))
    assert.ok(dedupe.includes('react-dom'))
  })
}

function walk(node, visit) {
  if (!node || typeof node !== 'object') return
  if (typeof node.type === 'string') visit(node)
  for (const [key, value] of Object.entries(node)) {
    if (['loc', 'range', 'tokens', 'comments', 'parent'].includes(key)) continue
    if (Array.isArray(value)) value.forEach(child => walk(child, visit))
    else if (value && typeof value === 'object') walk(value, visit)
  }
}

function activeSources() {
  const files = new Map()
  const follow = file => {
    if (files.has(file)) return
    const source = readFileSync(file, 'utf8')
    files.set(file, source)
    if (!/\.[jt]sx?$/.test(file)) return
    walk(parse(file), node => {
      const imported = ['ImportDeclaration', 'ExportNamedDeclaration', 'ExportAllDeclaration', 'ImportExpression'].includes(node.type)
        ? node.source : node.type === 'CallExpression' && node.callee.type === 'Import' ? node.arguments[0] : null
      if (typeof imported?.value !== 'string' || !imported.value.startsWith('.')) return
      const base = resolve(dirname(file), imported.value)
      const target = [base, ...['.tsx', '.ts', '.jsx', '.js', '.json', '.css'].map(extension => base + extension),
        ...['index.tsx', 'index.ts', 'index.jsx', 'index.js'].map(name => join(base, name))]
        .find(candidate => existsSync(candidate) && /\.(?:[jt]sx?|css|json)$/.test(candidate))
      if (target) follow(target)
    })
  }
  follow(join(root, 'main.tsx'))
  return files
}

test('Active application UI strings contain no former assistant name', () => {
  const sources = activeSources()
  assert.ok(sources.has(join(root, 'TestnetFirstApp.tsx')))
  assert.ok(sources.has(join(root, 'AssistantChat.tsx')))
  assert.ok(sources.has(join(root, 'frontend', 'src', 'LiveTradingPanel.tsx')))
  assert.equal(sources.has(join(root, 'frontend', 'src', 'TestnetFirstApp.tsx')), false)
  const violations = []
  const formerName = /M\u00fc\u015fteri Asistan\u0131|Customer Assistant/i
  for (const [file, source] of sources) {
    if (['.css', '.json'].includes(extname(file))) {
      if (formerName.test(source.replace(/\/\*[\s\S]*?\*\//g, ''))) violations.push(file)
      continue
    }
    walk(parse(file), node => {
      const text = node.type === 'TemplateElement' ? node.value.cooked
        : ['Literal', 'StringLiteral', 'JSXText'].includes(node.type) ? node.value : undefined
      if (typeof text === 'string' && formerName.test(text)) violations.push(`${file}:${node.loc.start.line}`)
    })
  }
  assert.deepEqual(violations, [])
})

test('Eye and its hooks cannot access input values or page content', () => {
  const forbidden = new Set(['value', 'valueAsNumber', 'valueAsDate', 'checked', 'files', 'innerHTML', 'outerHTML', 'textContent', 'innerText'])
  const violations = []
  for (const name of ['KaisEye.tsx', 'useKaisEye.ts', 'useKaisPrivacy.ts', 'useAssistantPresentation.ts', 'useKaisPageReactions.ts', 'kais-reactions.ts']) {
    walk(parse(join(root, name)), node => {
      const property = node.computed ? node.property?.value : node.property?.name
      const member = ['MemberExpression', 'OptionalMemberExpression'].includes(node.type)
      const getter = node.type === 'CallExpression' && node.callee.type === 'MemberExpression' &&
        node.callee.property.name === 'getAttribute' && forbidden.has(node.arguments[0]?.value)
      const destructure = node.type === 'ObjectPattern' && node.properties.some(item =>
        forbidden.has(item.key?.name ?? item.key?.value))
      if ((member && forbidden.has(property)) || getter || destructure) violations.push(`${name}:${node.loc.start.line}`)
    })
  }
  assert.deepEqual(violations, [])
})
