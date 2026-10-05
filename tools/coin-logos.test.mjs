import assert from 'node:assert/strict'
import {test} from 'node:test'
import {spawnSync} from 'node:child_process'
import {readFileSync} from 'node:fs'
import {coinAvatarColor, coinBaseAsset, coinLogoCandidates} from './coin-logo-symbol.mjs'
import {activeSymbols, refreshLogos, safeSvg, selectToken, svgLiteral, tarFiles} from './fetch-coin-logos.mjs'
import {runCoinLogoRefresh} from './run-coin-logo-refresh.mjs'

test('Logo multiplier normalization is shared, quote-safe and preserves numbered brands', () => {
  for (const [symbol, asset] of [['1000PEPEUSDT', 'PEPE'], ['1000SHIB/USDT', 'SHIB'], ['10000SATSUSDT', 'SATS'], ['1MBABYDOGEUSDT', 'BABYDOGE'], ['1000000MOGUSDT', 'MOG'], ['1INCHUSDT', '1INCH'], ['BTCDOMUSDT', 'BTCDOM']]) assert.equal(coinBaseAsset(symbol), asset)
})

test('Manifest takes precedence, explicit index proxy remains separate, then legacy fallback', () => {
  const candidates = coinLogoCandidates('BTCDOMUSDT', {BTC: {file: 'generated/btc.svg'}}, {BTCDOM: {asset: 'BTC', note: 'Index proxy'}})
  assert.deepEqual(candidates.map(row => [row.source, row.file]), [['manifest', 'generated/btc.svg'], ['legacy', 'btc.svg']])
  assert.equal(candidates[0].note, 'Index proxy')
  assert.equal(coinBaseAsset('BTCDOMUSDT'), 'BTCDOM')
  assert.throws(() => coinLogoCandidates('BTCUSDT', {BTC: {file: 'https://external.invalid/btc.svg'}}, {}))
})

test('Avatar colors are deterministic and normalized without shared mutable state', () => {
  assert.equal(coinAvatarColor('1000PEPEUSDT'), coinAvatarColor('PEPE/USDT'))
  assert.equal(coinAvatarColor('UNKNOWNUSDT'), coinAvatarColor('UNKNOWNUSDT'))
  assert.match(coinAvatarColor('币安人生USDT'), /^#[0-9a-f]{6}$/)
})

test('Logo universe accepts only active USDT perpetual contracts', () => {
  const contract = {symbol: 'BTCUSDT', status: 'TRADING', contractType: 'PERPETUAL', quoteAsset: 'USDT'}
  assert.deepEqual(activeSymbols({symbols: [contract, {...contract, status: 'BREAK'}, {...contract, contractType: 'CURRENT_QUARTER'}, {...contract, quoteAsset: 'USDC'}]}), ['BTCUSDT'])
  assert.throws(() => activeSymbols({symbols: []}))
})

test('Offline and fresh cache never access the network', async () => {
  const original = globalThis.fetch
  const now = Date.now
  const bundled = JSON.parse(readFileSync(new URL('../src/assets/coins/logo-manifest.json', import.meta.url), 'utf8'))
  let calls = 0
  Date.now = () => Date.parse(bundled.generatedAt) + 1000
  globalThis.fetch = async () => {calls++; throw new Error('Network access forbidden')}
  try {
    const offline = await refreshLogos({offline: true})
    const cached = await refreshLogos()
    assert.equal(calls, 0)
    assert.equal(offline.generatedAt, cached.generatedAt)
  } finally {
    globalThis.fetch = original
    Date.now = now
  }
})

test('Real coverage matches physical, self-contained 64px assets with explicit identity mappings', () => {
  const catalog = JSON.parse(readFileSync(new URL('../src/assets/coins/logo-manifest.json', import.meta.url), 'utf8'))
  const overrides = JSON.parse(readFileSync(new URL('../src/assets/coins/logo-overrides.json', import.meta.url), 'utf8'))
  const files = new Set()
  const missing = catalog.symbols.filter(symbol => {
    assert.doesNotMatch(symbol, /^TOKEN\d{4}USDT$/)
    return !coinLogoCandidates(symbol, catalog.assets, overrides).some(candidate => {
      try {readFileSync(new URL(`../src/assets/coins/${candidate.file}`, import.meta.url)); return true}
      catch (error) {if (error.code === 'ENOENT') return false; throw error}
    })
  })
  assert.deepEqual(missing, catalog.coverage.missing)
  assert.equal(catalog.symbols.length - missing.length, catalog.coverage.covered)
  for (const entry of Object.values(catalog.assets)) {
    const svg = readFileSync(new URL(`../src/assets/coins/${entry.file}`, import.meta.url), 'utf8')
    safeSvg(svg)
    assert.match(svg, /<svg\b[^>]*\bwidth="64"/)
    assert.match(svg, /<svg\b[^>]*\bheight="64"/)
    assert.match(svg, /<svg\b[^>]*\bviewBox="/)
    files.add(entry.file)
  }
  assert.ok(files.size >= 218)
  assert.equal(catalog.assets.PEPE.sourceId, 'pepe')
  assert.equal(catalog.assets.SHIB.sourceId, 'shiba-inu')
  assert.equal(catalog.assets.BTCDOM.logoAsset, 'BTC')
  assert.equal(catalog.assets.LUNA2.sourceId, 'terra-luna-2')
  assert.equal(coinLogoCandidates('LUNA2USDT', {}, overrides)[0].file, 'luna2.svg')
})

test('Ambiguous provider symbols require a manual identity override', () => {
  const tokens = [{id: 'real', symbol: 'A', filePath: 'token:A'}, {id: 'different', symbol: 'A', filePath: 'token:OTHER'}]
  const files = new Map([['package/dist/svgs/tokens/background/A.svg', ''], ['package/dist/svgs/tokens/background/OTHER.svg', '']])
  assert.deepEqual(selectToken('A', tokens, files), {ambiguous: true})
  assert.equal(selectToken('A', tokens, files, {sourceId: 'real'}).token.id, 'real')
})

test('SVG source accepts local gradient references, rejects external resources and active content', () => {
  safeSvg('<svg><path fill="url(#a)"/></svg>')
  safeSvg(`<svg><path fill='url("#a")'/></svg>`)
  for (const svg of ['<svg><script>1</script></svg>', '<svg onload="1"/>', '<svg><image href="https://remote.invalid/a.png"/></svg>', '<svg><path fill="url(https://remote.invalid/a)"/></svg>', '<!DOCTYPE svg><svg/>']) assert.throws(() => safeSvg(svg))
})

test('Corrupt or truncated archives cannot be parsed as valid logos', () => {
  assert.throws(() => tarFiles(Buffer.alloc(512, 1)))
})

test('Published SVG modules are parsed as literals, never executed', () => {
  assert.equal(svgLiteral(`var PEPE = '<svg/>'; export { PEPE as default };`, 'PEPE.svg.js'), '<svg/>')
  assert.throws(() => svgLiteral(`var PEPE = fetch('https://external.invalid'); export { PEPE as default };`, 'PEPE.svg.js'))
  assert.throws(() => svgLiteral(`var PEPE = '<svg/>'; globalThis.bad = true; export { PEPE as default };`, 'PEPE.svg.js'))
})

test('Refresh outage is nonfatal and leaves the current manifest byte-for-byte intact', () => {
  const manifest = new URL('../src/assets/coins/logo-manifest.json', import.meta.url)
  const before = readFileSync(manifest, 'utf8')
  const module = new URL('./fetch-coin-logos.mjs', import.meta.url).href
  const code = `import {fileURLToPath} from 'node:url'; globalThis.fetch = async () => new Response('', {status:429,headers:{'Retry-After':'60'}}); process.argv=[process.execPath,fileURLToPath(${JSON.stringify(module)}),'--refresh']; await import(${JSON.stringify(module)});`
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', code], {encoding: 'utf8'})
  assert.equal(result.status, 0)
  assert.match(result.stderr, /Refresh failed:.*HTTP 429.*Build continues/)
  assert.equal(readFileSync(manifest, 'utf8'), before)
})

test('Both build integrations survive child startup crashes and report explicit diagnostics', () => {
  const warnings = []
  const warn = console.warn
  console.warn = message => warnings.push(message)
  try {
    assert.equal(runCoinLogoRefresh({spawn: () => ({status: 1})}), false)
    assert.equal(runCoinLogoRefresh({spawn: () => ({status: null, error: new Error('Missing logo tool dependency')})}), false)
    assert.equal(runCoinLogoRefresh({spawn: () => ({status: 0})}), true)
    assert.equal(warnings.length, 2)
    assert.match(warnings[0], /exit 1.*build continues/)
    assert.match(warnings[1], /Missing logo tool dependency.*build continues/)
  } finally {
    console.warn = warn
  }
})
