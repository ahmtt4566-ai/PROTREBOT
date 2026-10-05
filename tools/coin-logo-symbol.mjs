export function coinBaseAsset(symbol) {
  const base = symbol.trim().toUpperCase().replace(/\.P$/, '').split(/[/_:-]/)[0]
  const quote = ['FDUSD', 'USDT', 'USDC', 'BUSD', 'TUSD', 'USD', 'EUR', 'GBP', 'BTC', 'ETH'].find(quote => base.endsWith(quote) && base.length > quote.length)
  return (quote ? base.slice(0, -quote.length) : base).replace(/^(?:1000000|10000|1000|1M)(?=[A-Z])/, '')
}

export function coinAvatarColor(symbol) {
  let hash = 2166136261
  for (const character of coinBaseAsset(symbol)) hash = Math.imul(hash ^ character.codePointAt(0), 16777619) >>> 0
  return ['#426879', '#655580', '#826038', '#376c67', '#7c485d'][hash % 5]
}

export function coinLogoCandidates(symbol, catalog, overrides) {
  const asset = coinBaseAsset(symbol)
  const override = overrides[asset]
  const canonical = override?.asset ?? asset
  const generated = catalog[asset] ?? catalog[canonical]
  const result = []
  if (generated) {
    if (!/^generated\/[a-z0-9$._-]+\.(?:svg|webp)$/.test(generated.file)) throw new Error('Invalid bundled logo manifest filename')
    result.push({file: generated.file, source: 'manifest', logoAsset: generated.logoAsset ?? canonical, note: override?.note ?? generated.note})
  }
  result.push({file: `${(override?.legacyAsset ?? canonical).toLowerCase()}.svg`, source: 'legacy', logoAsset: override?.legacyAsset ?? canonical, note: override?.note})
  return result
}
