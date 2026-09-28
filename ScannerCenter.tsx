import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Activity, BarChart3, RefreshCw, SlidersHorizontal, Target, TrendingDown, TrendingUp, Volume2 } from 'lucide-react'
import { API_BASE } from './api'

type Market = {symbol:string;display:string;price:number;change:number;volume:number}
type Analysis = {
  direction?:string;trend?:string;momentum?:string;rsi?:number;macd?:number;adx?:number;atr?:number;volume_ratio?:number
  ema?:{ema20?:number;ema50?:number;ema200?:number}
}
type Candidate = {
  rank?:number;symbol:string;direction?:string;score?:number;confidence?:string|number;confidence_value?:number
  trend?:string;momentum?:string;volume_ratio?:number;rsi?:number;status?:string;reasons?:string[]
  mtf_trend?:string;macd_confirmation?:boolean;entry?:number;stop_loss?:number;tp1?:number;tp2?:number;tp3?:number
}
type ScannerState = {
  scan_status?:string;running?:boolean;last_scan_at?:string|null;next_scan_at?:string|null;coins_scanned?:number
  selected_count?:number;eligible_count?:number;last_error?:string|null;top_candidates?:Candidate[];all_candidates?:Candidate[]
  last_scan_timeframe?:string|null;last_scan_universe?:string|null;last_scan_symbols?:string[]
}
type Summary = {scanner?:ScannerState;stream?:{status?:string;last_error?:string|null}}
type Props = {
  active:boolean;markets:Market[];symbol:string;onSymbolChange:(symbol:string)=>void;analysis:Analysis|null;chart:ReactNode
  interval:string;onIntervalChange:(interval:string)=>void
}
type Filter = 'ALL'|'LONG'|'SHORT'|'STRONG'|'BREAKOUT'|'HIGH VOLUME'
type Sort = 'score'|'volume'|'momentum'|'symbol'

const safeNumber = (value:unknown):number|null => typeof value === 'number' && Number.isFinite(value) ? value : null
const textValue = (value:unknown, fallback='—') => typeof value === 'string' && value.trim() ? value : fallback
const formatNumber = (value:unknown, digits=2) => {
  const number = safeNumber(value)
  return number === null ? '—' : number.toLocaleString('en-US',{maximumFractionDigits:digits})
}
const formatTime = (value?:string|null) => value ? new Date(value).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—'
const candidateDirection = (candidate:Candidate) => ['LONG','SHORT'].includes(String(candidate.direction || '').toUpperCase()) ? String(candidate.direction).toUpperCase() : 'NEUTRAL'
const candidateScore = (candidate:Candidate) => safeNumber(candidate.score) ?? safeNumber(candidate.confidence_value) ?? 0
const momentumRank = (value:unknown) => /positive|bullish|pozitif|güçlü|strong/i.test(String(value)) ? 2 : /negative|bearish|negatif|zayıf|weak/i.test(String(value)) ? 0 : 1
const setupLabel = (candidate:Candidate) => {
  const reasons = (candidate.reasons || []).join(' ').toLowerCase()
  if (/breakout|kırılım/.test(reasons)) return 'Breakout'
  if (/breakdown|düşüş/.test(reasons) && candidateDirection(candidate) === 'SHORT') return 'Breakdown'
  if (/pullback|geri çekil/.test(reasons)) return 'Pullback'
  return candidateDirection(candidate) === 'NEUTRAL' ? 'No clear setup' : 'Trend continuation'
}
const hasHighVolume = (candidate:Candidate) => (safeNumber(candidate.volume_ratio) ?? 0) >= 1.15

export default function ScannerCenter({active,markets,symbol,onSymbolChange,analysis,chart,interval,onIntervalChange}:Props) {
  const [summary,setSummary] = useState<Summary|null>(null)
  const [loading,setLoading] = useState(true)
  const [scanning,setScanning] = useState(false)
  const [error,setError] = useState(false)
  const [filter,setFilter] = useState<Filter>('ALL')
  const [sort,setSort] = useState<Sort>('score')
  const [universe,setUniverse] = useState('TOP 20')
  const [direction,setDirection] = useState('BOTH')
  const [strength,setStrength] = useState('ANY')
  const [selectedSymbols,setSelectedSymbols] = useState<string[]>([])
  const [scanStale,setScanStale] = useState(false)
  const [customError,setCustomError] = useState(false)

  const loadSummary = async (showLoading=false) => {
    if (showLoading) setLoading(true)
    try {
      const response = await fetch(`${API_BASE}/v21/summary`)
      if (!response.ok) throw new Error('summary')
      const payload = await response.json() as Summary
      setSummary(payload);setError(false)
    } catch { setError(true) }
    finally { setLoading(false) }
  }
  const scan = async () => {
    if (scanning) return
    if (universe === 'CUSTOM' && !selectedSymbols.length) { setCustomError(true); return }
    setCustomError(false)
    setScanning(true);setError(false)
    try {
      const response = await fetch(`${API_BASE}/v21/scanner/scan`,{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({timeframe:interval,universe,symbols:universe === 'CUSTOM' ? selectedSymbols : undefined}),
      })
      if (!response.ok) throw new Error('scan')
      await loadSummary();setScanStale(false)
    } catch { setError(true) }
    finally { setScanning(false) }
  }

  useEffect(() => { if (active) void loadSummary(true) },[active])

  const candidates = summary?.scanner?.all_candidates || summary?.scanner?.top_candidates || []
  const marketBySymbol = useMemo(() => new Map(markets.map(market => [market.symbol,market])),[markets])
  const scopedCandidates = universe === 'TOP 20' ? candidates.slice(0,20) : universe === 'TOP 50' ? candidates.slice(0,50) : candidates.filter(candidate => selectedSymbols.includes(candidate.symbol))
  const visible = useMemo(() => scopedCandidates.filter(candidate => {
    const itemDirection = candidateDirection(candidate)
    const score = candidateScore(candidate)
    const setup = setupLabel(candidate).toUpperCase()
    if (direction !== 'BOTH' && itemDirection !== direction) return false
    if (strength === 'MEDIUM' && score < 60) return false
    if (strength === 'STRONG' && score < 75) return false
    if (filter === 'LONG' && itemDirection !== 'LONG') return false
    if (filter === 'SHORT' && itemDirection !== 'SHORT') return false
    if (filter === 'STRONG' && score < 75) return false
    if (filter === 'BREAKOUT' && !setup.includes('BREAKOUT')) return false
    if (filter === 'HIGH VOLUME' && !hasHighVolume(candidate)) return false
    return true
  }).sort((left,right) => {
    if (sort === 'symbol') return left.symbol.localeCompare(right.symbol)
    if (sort === 'volume') return (safeNumber(right.volume_ratio) ?? 0) - (safeNumber(left.volume_ratio) ?? 0)
    if (sort === 'momentum') return momentumRank(right.momentum) - momentumRank(left.momentum)
    return candidateScore(right) - candidateScore(left)
  }),[scopedCandidates,direction,filter,sort,strength])
  const selected = visible.find(candidate => candidate.symbol === symbol) || visible[0] || null
  useEffect(() => {
    if (active && visible.length && !visible.some(candidate => candidate.symbol === symbol)) onSymbolChange(visible[0].symbol)
  },[active,visible,symbol,onSymbolChange])

  const strongLong = visible.filter(candidate => candidateDirection(candidate) === 'LONG' && candidateScore(candidate) >= 75).length
  const strongShort = visible.filter(candidate => candidateDirection(candidate) === 'SHORT' && candidateScore(candidate) >= 75).length
  const breakout = visible.filter(candidate => setupLabel(candidate).toUpperCase() === 'BREAKOUT').length
  const highVolume = visible.filter(hasHighVolume).length
  const scannerStatus = error || summary?.scanner?.last_error ? 'ERROR' : scanning || summary?.scanner?.running ? 'UPDATING' : summary?.scanner?.last_scan_at ? 'LIVE' : 'OFFLINE'
  const scanMetadata = `${interval.toUpperCase()} · ${universe} · ${direction}${scanStale ? ' · SCAN AGAIN' : ''}`
  const analysisValue = selected?.symbol === symbol ? analysis : null
  const analysisDirection = String(analysisValue?.direction || candidateDirection(selected || {})).toUpperCase()
  const selectedDirection = analysisDirection === 'LONG' || analysisDirection === 'SHORT' ? analysisDirection : 'NO CLEAR SETUP'

  return <section className="scannerCenter" aria-label="Market Scanner">
    <header className="scannerCenterHeader">
      <div><span className="scannerEyebrow">MARKET ANALYSIS</span><h2>MARKET SCANNER</h2><p>Scan the market for technical setups and potential opportunities.</p></div>
      <div className="scannerHeaderActions"><span className={`scannerStatus scannerStatus-${scannerStatus.toLowerCase()}`}><i/>{scannerStatus === 'LIVE' ? 'LIVE MARKET' : scannerStatus}</span><small>Last scan: {formatTime(summary?.scanner?.last_scan_at)}</small><button type="button" onClick={() => void scan()} disabled={scanning}><RefreshCw className={scanning ? 'spin' : ''}/>{scanning ? 'SCANNING...' : 'SCAN NOW'}</button></div>
    </header>

    <section className="scannerSettings" aria-labelledby="scanner-settings-title">
      <div className="scannerSectionHeading"><div><span>CONTROL PANEL</span><h3 id="scanner-settings-title">SCAN SETTINGS</h3></div><SlidersHorizontal/></div>
      <div className="scannerSettingsGrid">
        <label>MARKET<select defaultValue="Binance Futures"><option>Binance Futures</option></select></label>
        <label>TIMEFRAME<select value={interval} onChange={event => {onIntervalChange(event.target.value);setScanStale(true)}}>{[['5m','5m'],['15m','15m'],['1h','1H'],['4h','4H']].map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label>SYMBOL UNIVERSE<select value={universe} onChange={event => {setUniverse(event.target.value);setScanStale(true);setCustomError(false)}}>{['TOP 20','TOP 50','CUSTOM'].map(item => <option key={item}>{item}</option>)}</select></label>
        <label>DIRECTION<select value={direction} onChange={event => setDirection(event.target.value)}><option>BOTH</option><option>LONG</option><option>SHORT</option></select></label>
        <label>SIGNAL STRENGTH<select value={strength} onChange={event => setStrength(event.target.value)}><option>ANY</option><option>MEDIUM</option><option>STRONG</option></select></label>
        <label>SORT BY<select value={sort} onChange={event => setSort(event.target.value as Sort)}><option value="score">Signal</option><option value="volume">Volume</option><option value="momentum">Momentum</option><option value="symbol">Symbol</option></select></label>
      </div>
      {universe === 'CUSTOM' && <div className="scannerCustomSymbols"><span>CUSTOM SYMBOLS</span><div>{markets.map(market => <label key={market.symbol}><input type="checkbox" aria-label={market.symbol} checked={selectedSymbols.includes(market.symbol)} onChange={event => {setSelectedSymbols(current => event.target.checked ? [...current,market.symbol] : current.filter(symbol => symbol !== market.symbol));setScanStale(true);setCustomError(false)}}/>{market.symbol}</label>)}</div>{!selectedSymbols.length && <small>SELECT AT LEAST ONE SYMBOL</small>}</div>}
      <button type="button" className="scannerPrimaryAction" onClick={() => void scan()} disabled={scanning || (universe === 'CUSTOM' && !selectedSymbols.length)}><Target/>{scanning ? 'SCANNING...' : 'SCAN MARKET'}</button>
    </section>

    <section className="scannerSummary" aria-label="Market Summary">
      {[[TrendingUp,'STRONG LONG',strongLong], [TrendingDown,'STRONG SHORT',strongShort], [BarChart3,'BREAKOUT',breakout], [Volume2,'HIGH VOLUME',highVolume]].map(([Icon,label,value]) => <article key={String(label)}><Icon/><span><small>{label}</small><b>{loading ? '—' : value}</b></span></article>)}
    </section>

    {error && <section className="scannerState scannerError" role="alert"><strong>MARKET DATA UNAVAILABLE</strong><span>Unable to retrieve market data. Please try again.</span><button type="button" onClick={() => void loadSummary(true)}>RETRY</button></section>}
    {!error && scanning && <section className="scannerState"><Activity className="spin"/><strong>Scanning market...</strong><span>Analysing symbols...</span></section>}
    {!error && !scanning && !loading && !visible.length && <section className="scannerState"><Activity/><strong>NO SETUPS FOUND</strong><span>Try changing the timeframe or scan filters.</span></section>}

    {!error && (loading || visible.length > 0) && <section className="scannerResults" aria-labelledby="scanner-results-title">
      <div className="scannerSectionHeading"><div><span>TECHNICAL OPPORTUNITIES</span><h3 id="scanner-results-title">SCANNER RESULTS</h3><small className="scannerResultMetadata">{scanMetadata}</small></div><div className="scannerFilters">{(['ALL','LONG','SHORT','STRONG','BREAKOUT','HIGH VOLUME'] as Filter[]).map(item => <button type="button" key={item} className={filter === item ? 'active' : ''} onClick={() => setFilter(item)}>{item}</button>)}</div></div>
      <div className="scannerTableWrap"><table><thead><tr><th>SYMBOL</th><th>PRICE</th><th>DIRECTION</th><th>SCORE</th><th>TREND</th><th>RSI</th><th>MACD</th><th>ADX</th><th>VOLUME</th><th>SETUP</th></tr></thead><tbody>{loading ? <tr><td colSpan={10} className="scannerTableLoading">Loading current market data...</td></tr> : visible.map(candidate => { const market = marketBySymbol.get(candidate.symbol); const itemDirection = candidateDirection(candidate); return <tr key={candidate.symbol} className={candidate.symbol === symbol ? 'selected' : ''} onClick={() => onSymbolChange(candidate.symbol)} tabIndex={0} onKeyDown={event => {if (event.key === 'Enter') onSymbolChange(candidate.symbol)}}><td><strong>{candidate.symbol}</strong></td><td>{formatNumber(market?.price)}</td><td><b className={`scannerDirection scannerDirection-${itemDirection.toLowerCase()}`}>{itemDirection}</b></td><td><strong>{formatNumber(candidateScore(candidate),0)}</strong></td><td>{textValue(candidate.trend)}</td><td>{formatNumber(candidate.rsi,1)}</td><td>{candidate.macd_confirmation === undefined ? '—' : candidate.macd_confirmation ? 'Bullish' : 'Bearish'}</td><td>—</td><td>{formatNumber(candidate.volume_ratio,2)}x</td><td>{setupLabel(candidate)}</td></tr> })}</tbody></table></div>
    </section>}

    <section className="scannerDetail" aria-labelledby="scanner-detail-title">
      <div className="scannerDetailCopy"><div className="scannerSectionHeading"><div><span>SELECTED SYMBOL</span><h3 id="scanner-detail-title">{selected?.symbol || symbol || '—'}</h3></div><b className="scannerCandidateLabel">{selectedDirection === 'NO CLEAR SETUP' ? selectedDirection : `${selectedDirection} CANDIDATE`}</b></div>
        <div className="scannerSnapshot"><h4>TECHNICAL SNAPSHOT</h4>{[['EMA 20',analysisValue?.ema?.ema20],['EMA 50',analysisValue?.ema?.ema50],['EMA 200',analysisValue?.ema?.ema200],['MACD',analysisValue?.macd],['RSI 14',analysisValue?.rsi],['ADX 14',analysisValue?.adx],['ATR 14',analysisValue?.atr],['Volume Ratio',analysisValue?.volume_ratio ?? selected?.volume_ratio]].map(([label,value]) => <span key={String(label)}><small>{label}</small><b>{typeof value === 'number' ? formatNumber(value,2) : '—'}</b></span>)}</div>
        <div className="scannerExplanation"><h4>SIGNAL EXPLANATION</h4><p><b>Trend</b> {textValue(analysisValue?.trend || selected?.trend)}</p><p><b>Momentum</b> {textValue(analysisValue?.momentum || selected?.momentum)}</p><p><b>Volume</b> {hasHighVolume(selected || {}) ? 'Above average' : 'No confirmation'}</p><p><b>Structure</b> {setupLabel(selected || {})}</p><button type="button" className="scannerMasterTradeAction" onClick={() => window.dispatchEvent(new CustomEvent('protrebot-navigate',{detail:'master-trade'}))} disabled={!selected}>OPEN IN MASTER TRADE</button></div>
      </div><div className="scannerChart"><div className="scannerChartHeader"><span>{selected?.symbol || symbol || '—'} · {interval.toUpperCase()}</span><small>MARKET DATA</small></div>{chart}</div>
    </section>
  </section>
}
