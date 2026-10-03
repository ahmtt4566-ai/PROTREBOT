import {readFileSync, readdirSync, existsSync, writeFileSync} from 'node:fs'
import {dirname, extname, join, relative, resolve} from 'node:path'
import {fileURLToPath, pathToFileURL} from 'node:url'
import parser from '@babel/eslint-parser'
import {hasPrivateMarker, jsxAttribute, literalValue, passwordType} from './private-field-helpers.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const options = {requireConfigFile: false, babelOptions: {babelrc: false, configFile: false, parserOpts: {plugins: ['typescript', 'jsx']}}}

function walk(node, visit, inheritedPrivate = false) {
  if (!node || typeof node !== 'object') return
  if (typeof node.type === 'string') visit(node, inheritedPrivate)
  const childrenPrivate = inheritedPrivate || (node.type === 'JSXElement' && hasPrivateMarker(node.openingElement))
  for (const [key, value] of Object.entries(node)) {
    if (['loc', 'range', 'tokens', 'comments', 'parent'].includes(key)) continue
    const nextPrivate = key === 'children' ? childrenPrivate : inheritedPrivate
    if (Array.isArray(value)) value.forEach(child => walk(child, visit, nextPrivate))
    else if (value && typeof value === 'object') walk(value, visit, nextPrivate)
  }
}

function sourceFiles(directory, recursive = false) {
  return readdirSync(directory, {withFileTypes: true}).flatMap(entry => {
    const file = join(directory, entry.name)
    if (entry.isDirectory()) return recursive ? sourceFiles(file, true) : []
    return /\.[jt]sx?$/.test(entry.name) ? [file] : []
  })
}

function resolveImport(file, specifier) {
  if (!specifier.startsWith('.')) return undefined
  const base = resolve(dirname(file), specifier)
  return [base, ...['.tsx', '.ts', '.jsx', '.js'].map(extension => base + extension),
    ...['index.tsx', 'index.ts', 'index.jsx', 'index.js'].map(name => join(base, name))]
    .find(candidate => /\.[jt]sx?$/.test(extname(candidate)) && existsSync(candidate))
}

export function inputInventory() {
  const files = [...sourceFiles(root), ...sourceFiles(join(root, 'frontend', 'src'), true),
    ...sourceFiles(join(root, 'frontend', 'tests', 'fixtures'), true)]
  const parsed = new Map()
  const parse = file => {
    if (!parsed.has(file)) {
      const source = readFileSync(file, 'utf8')
      parsed.set(file, {source, ast: parser.parseForESLint(source, {...options, filePath: file}).ast})
    }
    return parsed.get(file)
  }
  const active = new Set()
  const follow = file => {
    if (active.has(file)) return
    active.add(file)
    walk(parse(file).ast, node => {
      const source = node.type === 'ImportDeclaration' || node.type === 'ExportNamedDeclaration' || node.type === 'ExportAllDeclaration' || node.type === 'ImportExpression'
        ? node.source : node.type === 'CallExpression' && node.callee.type === 'Import' ? node.arguments[0] : undefined
      const specifier = literalValue(source)
      const target = typeof specifier === 'string' ? resolveImport(file, specifier) : undefined
      if (target) follow(target)
    })
  }
  follow(join(root, 'main.tsx'))
  const fields = []
  for (const file of files) {
    const {source, ast} = parse(file)
    walk(ast, (node, inheritedPrivate) => {
      if (node.type !== 'JSXOpeningElement' || node.name.type !== 'JSXIdentifier' || !['input', 'textarea'].includes(node.name.name)) return
      const type = jsxAttribute(node, 'type')?.value
      const marker = jsxAttribute(node, 'data-private')?.value
      const printed = attribute => attribute ? String(literalValue(attribute) ?? source.slice(...attribute.range)) : ''
      fields.push({file: relative(root, file), line: node.loc.start.line, column: node.loc.start.column + 1, element: node.name.name,
        type: node.name.name === 'textarea' ? 'textarea' : printed(type) || 'text (implicit)',
        private: hasPrivateMarker(node), inheritedPrivate, marker: printed(marker), password: passwordType(type), active: active.has(file)})
    })
  }
  return fields.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line)
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const fields = inputInventory()
  const summary = new Map()
  for (const field of fields) {
    const row = summary.get(field.file) ?? {file: field.file, active: field.active, inputs: 0, textareas: 0, passwords: 0, private: 0}
    row.inputs += Number(field.element === 'input')
    row.textareas += Number(field.element === 'textarea')
    row.passwords += Number(field.password)
    row.private += Number(field.private)
    summary.set(field.file, row)
  }
  console.table([...summary.values()])
  console.log(JSON.stringify({fields: fields.length, activeFields: fields.filter(field => field.active).length,
    passwordFields: fields.filter(field => field.password).length, privateFields: fields.filter(field => field.private).length}))
  const output = process.argv.indexOf('--out')
  if (output >= 0) {
    if (!process.argv[output + 1]) throw new Error('--out requires a filename')
    const columns = ['file', 'line', 'column', 'element', 'type', 'private', 'inheritedPrivate', 'marker', 'password', 'active']
    const csv = value => `"${String(value).replaceAll('"', '""')}"`
    writeFileSync(resolve(process.argv[output + 1]), [columns.join(','), ...fields.map(field => columns.map(column => csv(field[column])).join(','))].join('\n') + '\n')
  }
}
