export type LiveMarketConfig = {
  symbol: string
  displaySymbol: string
  name: string
  kind: 'crypto' | 'fiat'
}

export const LIVE_MARKET_CONFIG: LiveMarketConfig[] = [
  {symbol:'BTCUSDT',displaySymbol:'BTC',name:'Bitcoin',kind:'crypto'},
  {symbol:'ETHUSDT',displaySymbol:'ETH',name:'Ethereum',kind:'crypto'},
  {symbol:'BNBUSDT',displaySymbol:'BNB',name:'BNB',kind:'crypto'},
  {symbol:'SOLUSDT',displaySymbol:'SOL',name:'Solana',kind:'crypto'},
  {symbol:'XRPUSDT',displaySymbol:'XRP',name:'XRP',kind:'crypto'},
  {symbol:'ADAUSDT',displaySymbol:'ADA',name:'Cardano',kind:'crypto'},
  {symbol:'DOGEUSDT',displaySymbol:'DOGE',name:'Dogecoin',kind:'crypto'},
  {symbol:'AVAXUSDT',displaySymbol:'AVAX',name:'Avalanche',kind:'crypto'},
  {symbol:'TRXUSDT',displaySymbol:'TRX',name:'TRON',kind:'crypto'},
  {symbol:'USDTTRY',displaySymbol:'USDT/TRY',name:'Tether',kind:'fiat'},
]

export const AUTH_PANEL_MARKETS = LIVE_MARKET_CONFIG.filter(({symbol}) => ['BTCUSDT','ETHUSDT','USDTTRY'].includes(symbol))
