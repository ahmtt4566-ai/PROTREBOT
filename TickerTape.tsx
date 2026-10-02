import { useState } from 'react'
import { Pause, Play } from 'lucide-react'
import { LIVE_MARKET_CONFIG } from './live-market-config'
import type { LiveTickerData, LiveTickerStatus } from './use-live-tickers'

type Props = {data:LiveTickerData;status:LiveTickerStatus}

function formatPrice(symbol:string,price:number) {
  return symbol === 'USDTTRY' ? `₺${price.toFixed(4)}` : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:2}).format(price)
}

function TapeItems({data}:{data:LiveTickerData}) {
  return <>{LIVE_MARKET_CONFIG.map(config => { const ticker = data[config.symbol]; return <span className="tickerTapeItem" key={config.symbol}><b>{config.displaySymbol}</b>{ticker ? <><strong aria-hidden="true">{formatPrice(config.symbol,ticker.price)}</strong><em className={ticker.changePercent >= 0 ? 'isPositive' : 'isNegative'}>{ticker.changePercent >= 0 ? '▲' : '▼'} {Math.abs(ticker.changePercent).toFixed(2)}%</em></> : <i className="tickerTapeSkeleton" aria-hidden="true"/>}</span> })}</>
}

export default function TickerTape({data,status}:Props) {
  const [paused,setPaused] = useState(false)
  return <div className="tickerTape" role="region" aria-label="Canlı fiyat şeridi"><div className={`tickerTapeStatus status-${status}`} aria-hidden="true">{status === 'offline' ? 'Bağlantı yok' : <i/>}</div><div className="tickerTapeViewport" aria-hidden="true"><div className="tickerTapeTrack" data-paused={paused}><div className="tickerTapeGroup"><TapeItems data={data}/></div><div className="tickerTapeGroup" aria-hidden="true"><TapeItems data={data}/></div></div></div><button type="button" className="tickerTapeToggle" onClick={() => setPaused(value => !value)} aria-label={paused ? 'Fiyat şeridini devam ettir' : 'Fiyat şeridini duraklat'} title={paused ? 'Devam et' : 'Duraklat'} aria-pressed={paused}>{paused ? <Play/> : <Pause/>}</button></div>
}
