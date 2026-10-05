export type MarketQuote = {symbol: string; display: string; name?: string; test?: boolean; price: number | null; change: number | null; volume: number | null}
export type MarketScore = {symbol: string; score: number; direction: string}
export type MarketFilter = 'Tümü' | 'Favoriler' | 'Skorlu' | 'Yükselenler' | 'Düşenler'
export type MarketSort = 'Skor' | '24s %' | 'Hacim' | 'A-Z'

export function parseMarketQuotes(payload: unknown): MarketQuote[] {
  if (!Array.isArray(payload)) throw new Error('Piyasa yanıtı geçerli bir liste değil.')
  const rows: MarketQuote[] = []
  for (const item of payload) {
    if (typeof item !== 'object' || item === null || !('symbol' in item) || typeof item.symbol !== 'string' || !item.symbol.endsWith('USDT') || !('display' in item) || typeof item.display !== 'string') throw new Error('Piyasa sembolü geçersiz.')
    const nullable = (field: string): number | null => {
      const value: unknown = Reflect.get(item, field)
      if (value === null) return null
      if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`Piyasa ${field} değeri geçersiz: ${item.symbol}`)
      return value
    }
    const test: unknown = Reflect.get(item, 'test')
    if (test !== undefined && typeof test !== 'boolean') throw new Error('Piyasa test etiketi geçersiz.')
    rows.push({symbol: item.symbol, display: item.display, price: nullable('price'), change: nullable('change'), volume: nullable('volume'), ...(test === true ? {test: true} : {})})
  }
  if (new Set(rows.map(row => row.symbol)).size !== rows.length) throw new Error('Piyasa listesinde tekrar eden sembol var.')
  return rows
}

export function marketWindow(offset: number, extent: number, count: number, size = 52) {
  const start = Math.max(0, Math.min(count, Math.floor(offset / size) - 2))
  return {start, end: Math.min(count, Math.ceil((offset + extent) / size) + 2)}
}

export function filterMarketQuotes(rows: readonly MarketQuote[], scores: ReadonlyMap<string, MarketScore>, favorites: ReadonlySet<string>, query: string, filter: MarketFilter, sort: MarketSort) {
  const search = query.trim().toUpperCase().replace(/[^A-Z0-9]/g, '')
  const result = rows.filter(row => (!search || `${row.symbol}${row.display}${row.name ?? ''}`.toUpperCase().replace(/[^A-Z0-9]/g, '').includes(search))
    && (filter === 'Tümü' || filter === 'Favoriler' && favorites.has(row.symbol) || filter === 'Skorlu' && scores.has(row.symbol) || filter === 'Yükselenler' && row.change !== null && row.change > 0 || filter === 'Düşenler' && row.change !== null && row.change < 0))
  const value = (row: MarketQuote) => sort === 'Skor' ? scores.get(row.symbol)?.score : sort === '24s %' ? row.change : row.volume
  return result.sort((left, right) => {
    if (sort !== 'A-Z') {
      const a = value(left), b = value(right)
      if (a == null && b != null) return 1
      if (a != null && b == null) return -1
      if (a != null && b != null && a !== b) return b - a
    }
    return left.symbol.localeCompare(right.symbol, 'en')
  })
}
export function marketPrice(value: number | null): string {
  if (value === null) return '—'
  const decimals = value === 0 ? 2 : Math.min(20, Math.max(2, 3 - Math.floor(Math.log10(Math.abs(value)))))
  return `$${value.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: decimals})}`
}
