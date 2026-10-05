export const previewPrice = 85360.8
export const previewMarkets = [
  ['BTCUSDT', previewPrice, 1.24], ['ETHUSDT', 3212.8, 2.08], ['SOLUSDT', 145.07, -.42],
  ['BNBUSDT', 593.3, .83], ['XRPUSDT', .5298, -.64], ['ADAUSDT', .413, 1.13],
  ['DOGEUSDT', .1492, .98], ['AVAXUSDT', 28.15, -1.05], ['TRXUSDT', .1254, -.36],
  ['LINKUSDT', 13.91, .72],
].map(([symbol, price, change]) => ({
  symbol: String(symbol), display: String(symbol).replace('USDT', '/USDT'), price: Number(price),
  change: Number(change), volume: 1000000,
}))

export function previewAnalysis(symbol = 'BTCUSDT') {
  const price = previewUniverse().find(market => market.symbol === symbol)?.price
  if (price === undefined) throw new Error(`Unknown preview symbol: ${symbol}`)
  return {direction: 'LONG', confidence: 89, entry: price, stop_loss: price * .994,
    tp1: price * 1.005, tp2: price * 1.01, tp3: price * 1.015,
    support: price * .995, resistance: price * 1.003, risk_reward: 2.5,
    trend: 'Güçlü yükseliş', momentum: 'POSITIVE', rsi: 58, macd: price * .00014,
    adx: 24, atr: price * .0047, volume_ratio: .75}
}

export function previewCandles(symbol = 'BTCUSDT', now = Date.now()) {
  const price = previewUniverse().find(market => market.symbol === symbol)?.price
  if (price === undefined) throw new Error(`Unknown preview symbol: ${symbol}`)
  return Array.from({length: 200}, (_, index) => {
    const close = price * (.986 + index / 199 * .014 + Math.sin(index * .35) * .0014 * (1 - index / 199))
    const open = close - price * Math.cos(index * .8) * .00035
    return {time: Math.floor(now / 1000) - (199 - index) * 900, open,
      high: Math.max(open, close) * 1.0004, low: Math.min(open, close) * .9996, close,
      volume: 1200 + (1 + Math.sin(index * .6)) * 700 + index * 4}
  })
}

export function previewUniverse(count = 650) {
  const additional = [
    {symbol: 'BTCDOMUSDT', display: 'BTCDOM/USDT', price: 3670.12, change: -.3, volume: 3000000},
    {symbol: '1000SHIBUSDT', display: '1000SHIB/USDT', price: .01913, change: 2.12, volume: 45000000},
    {symbol: '1000PEPEUSDT', display: '1000PEPE/USDT', price: .01012, change: -1.2, volume: 65000000},
  ]
  return [...previewMarkets, ...additional, ...Array.from({length: Math.max(0, count - previewMarkets.length - additional.length)}, (_, index) => ({
    symbol: `TOKEN${String(index).padStart(4, '0')}USDT`, display: `TOKEN${String(index).padStart(4, '0')}/USDT`,
    test: true,
    price: 1 + index / 10, change: (index % 21 - 10) / 2, volume: 100000 + index * 1000,
  }))].map(market => ({...market, status: 'TRADING', contractType: 'PERPETUAL', quoteAsset: 'USDT'}))
}
