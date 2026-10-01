import { useEffect, useState } from 'react'

export type TopMover = {symbol:string;price:number;changePercent:number}
const TOP_MOVERS_URL = 'https://api.binance.com/api/v3/ticker/24hr'

export function useTopMovers():TopMover[] {
  const [movers,setMovers] = useState<TopMover[]>([])
  useEffect(() => {
    let stopped = false
    let timer:number|undefined
    const load = async () => {
      if (document.hidden) return
      try {
        const response = await fetch(TOP_MOVERS_URL)
        if (!response.ok) throw new Error('Top movers unavailable')
        const payload = await response.json() as Array<{symbol:string;lastPrice:string;priceChangePercent:string;quoteVolume:string}>
        const next = payload.filter(item => item.symbol.endsWith('USDT') && !/(UP|DOWN|BULL|BEAR)USDT$/.test(item.symbol) && Number(item.quoteVolume) >= 5000000).map(item => ({symbol:item.symbol.replace('USDT',''),price:Number(item.lastPrice),changePercent:Number(item.priceChangePercent)})).filter(item => Number.isFinite(item.price) && Number.isFinite(item.changePercent)).sort((left,right) => right.changePercent - left.changePercent).slice(0,5)
        if (!stopped && next.length) setMovers(next)
      } catch { /* preserve the last successful result */ }
      if (!stopped && !document.hidden) timer = window.setTimeout(load,30000)
    }
    const onVisibilityChange = () => { if (!document.hidden) { window.clearTimeout(timer); void load() } }
    document.addEventListener('visibilitychange',onVisibilityChange)
    void load()
    return () => { stopped = true; window.clearTimeout(timer); document.removeEventListener('visibilitychange',onVisibilityChange) }
  },[])
  return movers
}
