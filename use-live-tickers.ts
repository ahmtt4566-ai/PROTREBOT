import { useEffect, useRef, useState } from 'react'

type TickerPayload = {c?:string;o?:string}
export type LiveTicker = {
  symbol: string
  price: number
  openPrice: number
  changePercent: number
  history: number[]
  direction: 'up'|'down'|null
  flash: 'up'|'down'|null
}
export type LiveTickerStatus = 'connecting'|'live'|'offline'
export type LiveTickerData = Record<string,LiveTicker>

const WS_BASE = 'wss://stream.binance.com:9443/stream?streams='
const REST_URL = 'https://api.binance.com/api/v3/ticker/24hr?symbols='

function buildTicker(symbol:string,price:number,openPrice:number,history:number[] = [],previous?:LiveTicker):LiveTicker {
  const changePercent = openPrice ? ((price - openPrice) / openPrice) * 100 : 0
  const direction = previous && price !== previous.price ? price > previous.price ? 'up' : 'down' : previous?.direction || null
  return {symbol,price,openPrice,changePercent,history:[...history.slice(-59),price],direction,flash:direction}
}

export function useLiveTickers(symbols:string[]):{data:LiveTickerData;status:LiveTickerStatus} {
  const [data,setData] = useState<LiveTickerData>({})
  const [status,setStatus] = useState<LiveTickerStatus>('connecting')
  const pendingRef = useRef(new Map<string,{price:number;openPrice:number}>())
  const dataRef = useRef<LiveTickerData>({})
  const socketRef = useRef<WebSocket|null>(null)
  const reconnectTimerRef = useRef<number|null>(null)
  const pollingTimerRef = useRef<number|null>(null)
  const flushTimerRef = useRef<number|null>(null)
  const attemptRef = useRef(0)
  const stoppedRef = useRef(false)

  useEffect(() => {
    const normalizedSymbols = symbols.map(symbol => symbol.toUpperCase())
    const stream = normalizedSymbols.map(symbol => `${symbol.toLowerCase()}@miniTicker`).join('/')
    stoppedRef.current = false

    const clearTimer = (ref:{current:number|null}) => { if (ref.current !== null) { window.clearTimeout(ref.current); ref.current = null } }
    const stopPolling = () => clearTimer(pollingTimerRef)
    const schedulePolling = () => {
      stopPolling()
      const poll = async () => {
        if (stoppedRef.current || document.hidden) return
        try {
          const response = await fetch(`${REST_URL}${encodeURIComponent(JSON.stringify(normalizedSymbols))}`)
          if (!response.ok) throw new Error('Binance REST unavailable')
          const payload = await response.json() as Array<{symbol:string;lastPrice:string;openPrice:string}>
          payload.forEach(item => queue(item.symbol,Number(item.lastPrice),Number(item.openPrice)))
          setStatus('live')
        } catch { setStatus('offline') }
        if (!stoppedRef.current && !document.hidden) pollingTimerRef.current = window.setTimeout(poll,10000)
      }
      void poll()
    }
    const flush = () => {
      flushTimerRef.current = null
      if (!pendingRef.current.size) return
      const pending = new Map(pendingRef.current)
      pendingRef.current.clear()
      setData(previous => {
        const next = {...previous}
        pending.forEach((value,symbol) => { next[symbol] = buildTicker(symbol,value.price,value.openPrice,previous[symbol]?.history,previous[symbol]) })
        dataRef.current = next
        return next
      })
      window.setTimeout(() => setData(previous => { const next = {...previous}; pending.forEach((_,symbol) => { if (next[symbol]) next[symbol] = {...next[symbol],flash:null} }); dataRef.current = next; return next }), 420)
    }
    function queue(symbol:string,price:number,openPrice:number) {
      if (!Number.isFinite(price) || !Number.isFinite(openPrice) || price <= 0) return
      pendingRef.current.set(symbol.toUpperCase(),{price,openPrice})
      if (flushTimerRef.current === null) flushTimerRef.current = window.setTimeout(flush,250)
    }
    const closeSocket = () => { socketRef.current?.close(); socketRef.current = null }
    const connect = () => {
      if (stoppedRef.current || document.hidden) return
      if (!Object.keys(dataRef.current).length) setStatus('connecting')
      try {
        const socket = new WebSocket(`${WS_BASE}${stream}`)
        socketRef.current = socket
        socket.onopen = () => { attemptRef.current = 0; stopPolling(); setStatus('live') }
        socket.onmessage = event => {
          try {
            const payload = JSON.parse(event.data) as {data?:TickerPayload;stream?:string}
            const symbol = payload.stream?.split('@')[0]?.toUpperCase() || ''
            if (symbol && payload.data?.c && payload.data.o) queue(symbol,Number(payload.data.c),Number(payload.data.o))
          } catch { /* ignore malformed market packets */ }
        }
        socket.onerror = () => { socket.close() }
        socket.onclose = () => {
          socketRef.current = null
          if (stoppedRef.current || document.hidden) return
          setStatus('offline')
          schedulePolling()
          const delay = Math.min(30000,1000 * 2 ** attemptRef.current++)
          reconnectTimerRef.current = window.setTimeout(connect,delay)
        }
      } catch { setStatus('offline'); schedulePolling() }
    }
    const onVisibilityChange = () => {
      if (document.hidden) { closeSocket(); stopPolling(); clearTimer(reconnectTimerRef); setStatus('offline') }
      else { attemptRef.current = 0; connect() }
    }
    document.addEventListener('visibilitychange',onVisibilityChange)
    connect()
    return () => {
      stoppedRef.current = true
      document.removeEventListener('visibilitychange',onVisibilityChange)
      closeSocket(); stopPolling(); clearTimer(reconnectTimerRef); clearTimer(flushTimerRef)
    }
  },[symbols.join(',')])

  return {data,status}
}
