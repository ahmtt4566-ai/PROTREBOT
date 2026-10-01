export type AuthMarketQuote = {
  symbol: string
  name: string
  price: string
  change: string
  positive: boolean
  sparkline: number[]
}

export const AUTH_MARKET_QUOTES: AuthMarketQuote[] = [
  {symbol:'BTC/USDT',name:'Bitcoin',price:'67,842.10',change:'+2.84%',positive:true,sparkline:[35,28,32,24,27,19,22,14,16,8]},
  {symbol:'ETH/USDT',name:'Ethereum',price:'3,482.76',change:'+1.62%',positive:true,sparkline:[28,31,26,29,22,24,18,21,16,18]},
  {symbol:'USDT/TRY',name:'Tether',price:'34.2180',change:'-0.18%',positive:false,sparkline:[22,18,21,16,18,20,17,19,14,16]},
]
