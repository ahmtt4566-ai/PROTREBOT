import {createHash} from 'node:crypto'
import {mkdir, readFile, readdir, rename, writeFile} from 'node:fs/promises'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
import {gunzipSync} from 'node:zlib'
import {parseSync} from '@babel/core'
import {coinBaseAsset} from './coin-logo-symbol.mjs'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const COINS = resolve(ROOT, 'src', 'assets', 'coins')
const CATALOG = resolve(COINS, 'logo-manifest.json')
const REPOSITORY = '0xa3k5/web3icons'
const DAY = 86400000

export function activeSymbols(info) {
  if (!info || !Array.isArray(info.symbols)) throw new Error('Invalid Binance exchangeInfo')
  const symbols = info.symbols.filter(row => row.status === 'TRADING' && row.contractType === 'PERPETUAL' && row.quoteAsset === 'USDT').map(row => row.symbol)
  if (!symbols.length || symbols.some(symbol => typeof symbol !== 'string' || !symbol.endsWith('USDT')) || new Set(symbols).size !== symbols.length) throw new Error('Invalid active perpetual universe')
  return symbols.sort()
}

export function tarFiles(buffer) {
  const result = new Map()
  let longName = ''
  for (let offset = 0; offset + 512 <= buffer.length;) {
    const header = buffer.subarray(offset, offset + 512)
    if (header.every(byte => byte === 0)) break
    const text = (start, length) => header.subarray(start, start + length).toString().replace(/\0.*$/s, '')
    const checksum = Number.parseInt(text(148, 8).trim(), 8)
    const actual = header.reduce((sum, byte, index) => sum + (index >= 148 && index < 156 ? 32 : byte), 0)
    const size = Number.parseInt(text(124, 12).trim(), 8)
    if (checksum !== actual || !Number.isSafeInteger(size) || size < 0 || offset + 512 + size > buffer.length) throw new Error('Invalid source tar archive')
    const name = longName || [text(345, 155), text(0, 100)].filter(Boolean).join('/')
    const data = buffer.subarray(offset + 512, offset + 512 + size)
    const type = text(156, 1)
    longName = ''
    if (type === 'L') longName = data.toString().replace(/\0.*$/s, '')
    else if (type === 'x' || type === 'g') {
      const path = data.toString().match(/(?:^|\n)\d+ path=([^\n]+)\n/)
      if (path) longName = path[1]
    } else if (type === '0' || type === '') {
      if (name.startsWith('/') || name.split('/').includes('..')) throw new Error('Unsafe source tar path')
      result.set(name, data)
    }
    offset += 512 + Math.ceil(size / 512) * 512
  }
  return result
}

export function selectToken(asset, tokens, files, override = {}) {
  const canonical = override.asset ?? asset
  const candidates = tokens.filter(token => override.sourceId ? token.id === override.sourceId : token.symbol?.toUpperCase() === canonical)
    .map(token => {
      const file = token.filePath?.split(':')[1]
      if (!file || !/^[A-Z0-9$._-]+$/i.test(file)) return null
      const path = ['background', 'branded'].flatMap(variant => ['.svg', '.svg.js'].map(extension => `package/dist/svgs/tokens/${variant}/${file}${extension}`)).find(path => files.has(path))
      return path ? {token, path, canonical} : null
    }).filter(Boolean)
  if (candidates.length > 1) return {ambiguous: true}
  return candidates[0] ?? null
}

export function safeSvg(svg) {
  if (!/<svg\b/i.test(svg) || /<(?:script|foreignObject|image)\b|\bon[a-z]+\s*=|<!DOCTYPE|<!ENTITY|@import\b|@font-face\b/i.test(svg)) throw new Error('Unsafe or externally dependent source SVG')
  const links = [...svg.matchAll(/\bhref\s*=\s*(["'])(.*?)\1/gi)].map(match => match[2])
  const urls = [...svg.matchAll(/\burl\(\s*(["']?)(.*?)\1\s*\)/gi)].map(match => match[2])
  if ([...links, ...urls].some(value => !value.trim().startsWith('#'))) throw new Error('External SVG resource rejected')
}

export function svgLiteral(source, path) {
  if (path.endsWith('.svg')) return source
  const body = parseSync(source, {sourceType: 'module', configFile: false, babelrc: false}).program.body
  const declaration = body[0]
  const exported = body[1]
  if (body.length !== 2 || declaration?.type !== 'VariableDeclaration' || declaration.declarations.length !== 1 || declaration.declarations[0].init?.type !== 'StringLiteral'
    || exported?.type !== 'ExportNamedDeclaration' || exported.specifiers.length !== 1 || exported.specifiers[0].exported.name !== 'default'
    || exported.specifiers[0].local.name !== declaration.declarations[0].id.name) throw new Error('SVG module is not a static string export')
  return declaration.declarations[0].init.value
}

async function download(url, limit = 20000000) {
  const response = await fetch(url, {signal: AbortSignal.timeout(20000), headers: {'User-Agent': 'protrebot-local-coin-logo-build/1.0'}})
  if (!response.ok) throw new Error(`Logo source HTTP ${response.status}; Retry-After=${response.headers.get('Retry-After') ?? 'not provided'}`)
  const chunks = []
  let length = 0
  for await (const chunk of response.body) {
    length += chunk.length
    if (length > limit) throw new Error('Logo source response exceeds size limit')
    chunks.push(chunk)
  }
  return Buffer.concat(chunks)
}

async function json(url) {
  return JSON.parse((await download(url, 5000000)).toString())
}

async function atomicWrite(path, data) {
  const temporary = `${path}.${process.pid}.tmp`
  await writeFile(temporary, data)
  await rename(temporary, path)
}

export async function refreshLogos({force = false, offline = false} = {}) {
  const previous = JSON.parse(await readFile(CATALOG, 'utf8'))
  if (offline) {
    console.warn('[coin-logos] Offline build: preserving bundled logos.')
    return previous
  }
  if (!force && previous.generatedAt && Date.now() - Date.parse(previous.generatedAt) < DAY) {
    console.log(`[coin-logos] Cached manifest: ${previous.coverage?.covered ?? 0}/${previous.symbols.length} real symbols; no network refresh.`)
    return previous
  }
  const overrides = JSON.parse(await readFile(resolve(COINS, 'logo-overrides.json'), 'utf8'))
  for (const entry of Object.values(overrides)) if (!entry || typeof entry.asset !== 'string' || /[/\\]/.test(entry.asset)) throw new Error('Invalid manual logo override')
  const symbols = activeSymbols(await json('https://fapi.binance.com/fapi/v1/exchangeInfo'))
  const commit = await json(`https://api.github.com/repos/${REPOSITORY}/commits/main`)
  if (!/^[a-f0-9]{40}$/.test(commit.sha)) throw new Error('Invalid source revision')
  const raw = `https://raw.githubusercontent.com/${REPOSITORY}/${commit.sha}`
  const sourcePackage = await json(`${raw}/packages/core/package.json`)
  if (sourcePackage.license !== 'MIT') throw new Error('Source license changed; existing licensed assets retained')
  const license = (await download(`${raw}/LICENCE`)).toString()
  if (!license.startsWith('MIT License') || !license.includes('Copyright (c) 2024 0xa3k5')) throw new Error('Source license could not be verified')
  const tokens = await json(`${raw}/packages/common/src/metadata/tokens.json`)
  if (!Array.isArray(tokens)) throw new Error('Invalid source token metadata')
  const release = await json('https://registry.npmjs.org/@web3icons%2fcore/latest')
  if (release.version !== sourcePackage.version || release.license !== 'MIT' || !release.dist?.tarball?.startsWith('https://registry.npmjs.org/')) throw new Error('Source release/revision mismatch')
  const archive = await download(release.dist.tarball)
  const integrity = `sha512-${createHash('sha512').update(archive).digest('base64')}`
  if (integrity !== release.dist.integrity) throw new Error('Source archive integrity mismatch')
  const files = tarFiles(gunzipSync(archive, {maxOutputLength: 96000000}))
  if (![...files.keys()].some(path => /^package\/dist\/svgs\/tokens\/(?:background|branded)\/.+\.svg(?:\.js)?$/.test(path))) throw new Error('Source archive contains no supported SVG paths')
  const {optimize} = await import('svgo')
  await mkdir(resolve(COINS, 'generated'), {recursive: true})
  const legacy = new Set((await readdir(COINS)).filter(file => file.endsWith('.svg')).map(file => file.slice(0, -4).toUpperCase()))
  const assets = {...previous.assets}
  const written = new Map()
  const ambiguous = [], failures = []
  for (const asset of new Set(symbols.map(coinBaseAsset))) {
    const override = overrides[asset] ?? {}
    const chosen = selectToken(asset, tokens, files, override)
    if (chosen?.ambiguous) {ambiguous.push(asset); continue}
    if (!chosen) continue
    try {
      const canonical = chosen.canonical
      let file = written.get(canonical)
      if (!file) {
        const svg = svgLiteral(files.get(chosen.path).toString(), chosen.path)
        if (svg.length > 1000000) throw new Error('Source SVG exceeds size limit')
        safeSvg(svg)
        const optimized = optimize(svg, {multipass: true, plugins: ['preset-default', 'removeDimensions', {
          name: 'fixed-local-logo-size', fn: () => ({element: {enter: node => {
            if (node.name === 'svg') {node.attributes.width = '64'; node.attributes.height = '64'}
          }}}),
        }]})
        safeSvg(optimized.data)
        file = `generated/${canonical.toLowerCase()}.svg`
        await atomicWrite(resolve(COINS, file), optimized.data)
        written.set(canonical, file)
      }
      assets[asset] = {file, name: chosen.token.name, sourceId: chosen.token.id, logoAsset: canonical, license: 'MIT', ...(override.note ? {note: override.note} : {})}
    } catch (error) {
      const message = `${asset}: ${error instanceof Error ? error.message : String(error)}`
      console.warn(`[coin-logos] ${message}; preserving previous logo.`)
      failures.push(message)
    }
  }
  if (!written.size) throw new Error('Source refresh produced no valid icons; old manifest retained')
  const missing = symbols.filter(symbol => {
    const asset = coinBaseAsset(symbol)
    return !assets[asset] && !legacy.has(overrides[asset]?.legacyAsset ?? overrides[asset]?.asset ?? asset)
  })
  const catalog = {
    schema: 1, generatedAt: new Date().toISOString(),
    source: {repository: REPOSITORY, revision: commit.sha, updatedAt: commit.commit.committer.date, packageVersion: release.version, archiveIntegrity: release.dist.integrity, license: 'MIT'},
    symbols, assets, coverage: {total: symbols.length, covered: symbols.length - missing.length, fallback: missing.length, missing, ambiguous, failures},
  }
  await atomicWrite(resolve(COINS, 'LICENSE-web3icons.txt'), license)
  await atomicWrite(CATALOG, `${JSON.stringify(catalog, null, 2)}\n`)
  console.log(`[coin-logos] ${catalog.coverage.covered}/${symbols.length} real symbols have logos; ${missing.length} letter fallbacks; ${written.size} optimized local SVGs.`)
  return catalog
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    await refreshLogos({force: process.argv.includes('--refresh'), offline: process.argv.includes('--offline') || process.env.COIN_LOGO_OFFLINE === '1'})
  } catch (error) {
    console.warn(`[coin-logos] Refresh failed: ${error instanceof Error ? error.message : String(error)}. Build continues with existing bundled logos.`)
  }
}
