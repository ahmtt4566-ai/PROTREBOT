import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { CandlestickSeries, ColorType, createChart, HistogramSeries, LineSeries, type IPriceLine } from 'lightweight-charts'
import { Activity, ArrowUp, CircleDollarSign, Cloud, KeyRound, LockKeyhole, RadioTower, RefreshCw, ShieldCheck, TestTube2 } from 'lucide-react'
import { API_BASE } from './api'

const BinanceDemo = lazy(() => import('./BinanceDemo'))
const LiveTradingPanel = lazy(() => import('./LiveTradingPanel'))
const ExecutionCenter = lazy(() => import('./ExecutionCenter'))
const CloudOpsCenter = lazy(() => import('./CloudOpsCenter'))
const ExchangeConnections = lazy(() => import('./ExchangeConnections'))

type View = 'testnet'|'ops'|'live'|'setup'
type Market = {symbol:string;display:string;price:number;change:number;volume:number}
type Candle = {time:number;open:number;high:number;low:number;close:number;volume:number}
type Point = {time:number;value:number}
type Analysis = {
  direction:'LONG'|'SHORT'|'BEKLE';confidence:number;entry:number;stop_loss:number;tp1:number;tp2:number;tp3:number;
  support:number;resistance:number;trend:string;momentum:string;rsi:number;adx:number;volume_ratio:number;explanation:string;
  series:{ema20:Point[];ema50:Point[];ema200:Point[]}
}
type Health = {status:string;version:string;mode:string;testnet:string;live_guard:string;paper:string;database:string;cloud_evidence:string;web_access:string}

const format = (value:number) => value.toLocaleString('tr-TR',{maximumFractionDigits:value < 10 ? 5 : 2})

function TestnetMarketChart({symbol,interval,onAnalysis,showLevels=true,showEma=true}:{symbol:string;interval:string;onAnalysis:(analysis:Analysis|null)=>void;showLevels?:boolean;showEma?:boolean}) {
  const host = useRef<HTMLDivElement>(null)
  const [stream,setStream] = useState<'YÜKLENİYOR'|'CANLI'|'HATA'>('YÜKLENİYOR')
  const [updated,setUpdated] = useState('—')
  const [error,setError] = useState('')

  useEffect(() => {
    if (!host.current) return
    let active = true
    let priceLines:IPriceLine[] = []
    const chart = createChart(host.current,{
      autoSize:true,
      layout:{background:{type:ColorType.Solid,color:'#fffef9'},textColor:'#49614f'},
      grid:{vertLines:{color:'#edf1e8'},horzLines:{color:'#edf1e8'}},
      rightPriceScale:{borderColor:'#d8e3d7'},timeScale:{borderColor:'#d8e3d7',timeVisible:true,secondsVisible:false},
      crosshair:{vertLine:{color:'#70a882'},horzLine:{color:'#70a882'}},
    })
    const candles = chart.addSeries(CandlestickSeries,{upColor:'#0caf62',downColor:'#ef594a',wickUpColor:'#0caf62',wickDownColor:'#ef594a',borderVisible:false})
    const volume = chart.addSeries(HistogramSeries,{priceFormat:{type:'volume'},priceScaleId:''})
    volume.priceScale().applyOptions({scaleMargins:{top:.82,bottom:0}})
    const ema20 = chart.addSeries(LineSeries,{color:'#16a560',lineWidth:2,priceLineVisible:false,lastValueVisible:false,title:'EMA20'})
    const ema50 = chart.addSeries(LineSeries,{color:'#f3a712',lineWidth:2,priceLineVisible:false,lastValueVisible:false,title:'EMA50'})
    const ema200 = chart.addSeries(LineSeries,{color:'#6f91ad',lineWidth:2,priceLineVisible:false,lastValueVisible:false,title:'EMA200'})

    const applyAnalysis = (analysis:Analysis) => {
      ema20.setData(showEma ? analysis.series.ema20.map(point => ({time:point.time as never,value:point.value})) : [])
      ema50.setData(showEma ? analysis.series.ema50.map(point => ({time:point.time as never,value:point.value})) : [])
      ema200.setData(showEma ? analysis.series.ema200.map(point => ({time:point.time as never,value:point.value})) : [])
      priceLines.forEach(line => candles.removePriceLine(line))
      const line = (price:number,color:string,title:string,width:1|2|3=2,style=2) => candles.createPriceLine({price,color,lineWidth:width,lineStyle:style,axisLabelVisible:true,title})
      priceLines = showLevels ? [
        line(analysis.entry,'#078b4c',`${analysis.direction} GİRİŞ`,3,0), line(analysis.stop_loss,'#ed4f42','STOP',3,0),
        line(analysis.tp1,'#28a657','TP1'),line(analysis.tp2,'#28a657','TP2'),line(analysis.tp3,'#28a657','TP3'),
        line(analysis.support,'#e96b5f','DESTEK',1,3),line(analysis.resistance,'#228d51','DİRENÇ',1,3),
      ] : []
      onAnalysis(analysis)
    }

    const load = async () => {
      try {
        const [candleResponse,analysisResponse] = await Promise.all([
          fetch(`${API_BASE}/klines/${symbol}?interval=${interval}&limit=500`),
          fetch(`${API_BASE}/analysis/${symbol}?interval=${interval}`),
        ])
        const candlePayload = await candleResponse.json().catch(() => null) as Candle[] | {detail?:unknown} | null
        const analysisPayload = await analysisResponse.json().catch(() => null) as Analysis | {detail?:unknown} | null
        const candleError = candlePayload && typeof candlePayload === 'object' && !Array.isArray(candlePayload) ? candlePayload.detail : null
        const analysisError = analysisPayload && typeof analysisPayload === 'object' && !Array.isArray(analysisPayload) ? (analysisPayload as {detail?:unknown}).detail : null
        if (!candleResponse.ok) throw new Error(typeof candleError === 'string' ? candleError : `Piyasa verisi alınamadı (HTTP ${candleResponse.status}).`)
        if (!analysisResponse.ok) throw new Error(typeof analysisError === 'string' ? analysisError : `Analiz alınamadı (HTTP ${analysisResponse.status}).`)
        const rows = candlePayload as Candle[]
        const analysis = analysisPayload as Analysis
        if (!active) return
        candles.setData(rows.map(row => ({time:row.time as never,open:row.open,high:row.high,low:row.low,close:row.close})))
        volume.setData(rows.map(row => ({time:row.time as never,value:row.volume,color:row.close >= row.open ? 'rgba(24,177,100,.32)' : 'rgba(239,89,74,.28)'})))
        applyAnalysis(analysis)
        chart.timeScale().fitContent()
        setStream('CANLI')
        setError('')
        setUpdated(new Date().toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit',second:'2-digit'}))
      } catch (reason) {
        if (active) {setStream('HATA');setError(reason instanceof Error ? reason.message : 'Piyasa verisi alınamadı.');onAnalysis(null)}
      }
    }
    void load()
    const timer = window.setInterval(() => void load(),15000)
    return () => {active=false;window.clearInterval(timer);chart.remove();onAnalysis(null)}
  },[symbol,interval,onAnalysis,showLevels,showEma])

  return <div className="v26ChartShell">
    <div className="v26ChartStatus"><span className={stream === 'CANLI' ? 'live' : stream === 'HATA' ? 'error' : ''}><i/>{stream}</span><em>{error || `Binance Futures piyasa verisi · 15 sn yenileme · ${updated}`}</em></div>
    <div className="v26Chart" ref={host}/>
  </div>
}

export default function TestnetFirstApp() {
  const [view,setView] = useState<View>('live')
  const [markets,setMarkets] = useState<Market[]>([])
  const [symbol,setSymbol] = useState('BTCUSDT')
  const [marketQuery,setMarketQuery] = useState('')
  const [interval,setInterval] = useState('15m')
  const [analysis,setAnalysis] = useState<Analysis|null>(null)
  const [health,setHealth] = useState<Health|null>(null)
  const [loading,setLoading] = useState(false)
  const [mobileNavVisible,setMobileNavVisible] = useState(true)
  const [showBackTop,setShowBackTop] = useState(false)

  const refresh = async () => {
    setLoading(true)
    try {
      const [marketResponse,healthResponse] = await Promise.all([
        fetch(`${API_BASE}/v21/markets?limit=100`),
        fetch(`${API_BASE}/health`),
      ])
      if (marketResponse.ok) {
        const payload = await marketResponse.json() as {markets?:Market[]}
        setMarkets(Array.isArray(payload.markets) ? payload.markets : [])
      } else {
        setMarkets([])
      }
      if (healthResponse.ok) setHealth(await healthResponse.json() as Health)
    } finally {setLoading(false)}
  }

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(),30000)
    return () => window.clearInterval(timer)
  },[])

  useEffect(() => {
    let previousY = window.scrollY
    const onScroll = () => {
      const currentY = window.scrollY
      if (currentY < 24) setMobileNavVisible(true)
      else if (Math.abs(currentY - previousY) > 4) setMobileNavVisible(currentY < previousY)
      setShowBackTop(currentY > 320)
      previousY = currentY
    }
    window.addEventListener('scroll',onScroll,{passive:true})
    return () => window.removeEventListener('scroll',onScroll)
  },[])

  const goHome = () => {
    setView('testnet')
    window.scrollTo({top:0,behavior:'smooth'})
  }

  const scrollToTop = () => window.scrollTo({top:0,behavior:'smooth'})

  return <main className="v26App" onCopy={event => {if (!(event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLSelectElement)) event.preventDefault()}} onCut={event => {if (!(event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement)) event.preventDefault()}} onContextMenu={event => event.preventDefault()} onDragStart={event => event.preventDefault()}>
    <header className="v26Header">
      <div className="v26Brand" role="button" tabIndex={0} onClick={goHome} onKeyDown={event => {if(event.key === 'Enter' || event.key === ' ') goHome()}}><span>X</span><div><b>PROTREBOT ELITE X</b><small>V28 · IN-APP EXCHANGE VAULT</small></div></div>
      <div className="v26HeaderSignals">
        <span className="ok"><i/>SUNUCU CANLI</span>
        <span className="ok"><i/>TESTNET ANA MOD</span>
        <span className={health?.cloud_evidence === 'KALICI' ? 'ok' : 'locked'}><Cloud/>{health?.cloud_evidence || 'KANIT BAĞLANIYOR'}</span>
        <span className={health?.live_guard === 'SALT OKUNUR BAĞLI' ? 'ok' : 'locked'}><LockKeyhole/>{health?.live_guard || 'CANLI API BEKLİYOR'}</span>
      </div>
      <button className="v26Refresh" onClick={refresh} disabled={loading}><RefreshCw className={loading ? 'spin' : ''}/>{loading ? 'YENİLENİYOR' : 'YENİLE'}</button>
    </header>

    <nav className={`v26Nav${mobileNavVisible ? '' : ' mobileNavHidden'}`}>
      <button className={view === 'testnet' ? 'active' : ''} onClick={() => setView('testnet')}><TestTube2/><span><b>TESTNET KOMUTA</b><small>Binance Futures Demo · Ana çalışma alanı</small></span></button>
      <button className={view === 'ops' ? 'active' : ''} onClick={() => setView('ops')}><Cloud/><span><b>OPERASYON & KANIT</b><small>Karar, pozisyon ve kalıcı PostgreSQL kaydı</small></span></button>
      <button className={view === 'live' ? 'active liveTab' : ''} onClick={() => setView('live')}><ShieldCheck/><span><b>CANLI HAZIRLIK</b><small>API yoksa kesin kilitli · Gerçek kanal</small></span></button>
      <button className={view === 'setup' ? 'active' : ''} onClick={() => setView('setup')}><KeyRound/><span><b>BORSA BAĞLANTILARI</b><small>Testnet & Gerçek API kasası</small></span></button>
    </nav>

    <button className={`mobileBackTop${showBackTop ? ' visible' : ''}`} onClick={scrollToTop} aria-label="En yukarı çık"><ArrowUp/></button>

    <section className="v26ModeBar">
      <div><small>AKTİF ÇALIŞMA ALANI</small><h1>{view === 'testnet' ? 'Binance Futures Demo Merkezi' : view === 'ops' ? 'Bulut Operasyon ve Kanıt Merkezi' : view === 'live' ? 'Gerçek Futures Hazırlık Merkezi' : 'Borsa Bağlantıları ve Şifreli Kasa'}</h1><p>{view === 'testnet' ? 'Gerçek Binance motoruna en yakın test ortamı; sanal bakiye, gerçek emir akışı ve borsa yanıtları.' : view === 'ops' ? 'Otonom taramanın son kararı, pozisyonlar ve yeniden başlatmaya dayanıklı PostgreSQL kanıt defteri.' : view === 'live' ? 'Gerçek hesap bağlantısı salt-okunur başlar; emir yetkisi bağımsız ve süreli risk kilitleriyle korunur.' : 'Testnet ve gerçek hesap API anahtarlarını programdan test et, şifreli kaydet, aktifleştir veya anında kapat.'}</p></div>
      <aside><span><CircleDollarSign/>GERÇEK PARA</span><b>{view === 'live' ? 'KİLİTLİ' : '0 USDT'}</b><em>Paper devre dışı</em></aside>
    </section>

    {view === 'testnet' && <>
      <section className="v26MarketBar">
        <div className="v26MarketTitle"><Activity/><span><small>SEÇİLİ TESTNET PAZARI</small><b>{symbol.replace('USDT','/USDT')}</b></span><strong className={analysis?.direction === 'SHORT' ? 'short' : analysis?.direction === 'LONG' ? 'long' : ''}>{analysis?.direction || 'HESAPLANIYOR'} <em>%{analysis?.confidence ?? 0}</em></strong></div>
        <div className="v26MarketPicker">
          <input aria-label="Testnet market search" placeholder="Sembol ara" value={marketQuery} onChange={event => setMarketQuery(event.target.value)} />
          {markets.filter(market => `${market.display} ${market.symbol}`.toUpperCase().includes(marketQuery.trim().toUpperCase())).map(market => <button key={market.symbol} className={market.symbol === symbol ? 'active' : ''} onClick={() => setSymbol(market.symbol)}><b>{market.display}</b><span>{format(market.price)}</span><em className={market.change >= 0 ? 'up' : 'down'}>{market.change >= 0 ? '+' : ''}{market.change.toFixed(2)}%</em></button>)}
        </div>
        <div className="v26Intervals">{['1m','5m','15m','1h','4h'].map(item => <button key={item} className={interval === item ? 'active' : ''} onClick={() => setInterval(item)}>{item}</button>)}</div>
      </section>
      <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Testnet merkezi hazırlanıyor…</div>}>
        <BinanceDemo active symbol={symbol} analysis={analysis} chart={<TestnetMarketChart symbol={symbol} interval={interval} onAnalysis={setAnalysis}/>}/>
      </Suspense>
    </>}

    {view === 'live' && <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Canlı güvenlik merkezi hazırlanıyor…</div>}><LiveTradingPanel active symbol={symbol} analysis={analysis} masterTrade={import.meta.env.MODE === 'test'}/></Suspense>}

    {view === 'ops' && <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Bulut operasyon merkezi hazırlanıyor…</div>}><CloudOpsCenter/></Suspense>}

    {view === 'setup' && <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Şifreli borsa kasası hazırlanıyor…</div>}><ExchangeConnections/></Suspense>}

    <footer className="v26Footer"><span><RadioTower/>API: <b>{health?.status === 'ok' ? 'BAĞLI' : 'KONTROL EDİLİYOR'}</b></span><span>Veritabanı: <b>{health?.database || '—'}</b></span><span>Kanıt defteri: <b>{health?.cloud_evidence || '—'}</b></span><span>Çalışma modu: <b>V28 · TESTNET FIRST</b></span><span>Paper: <b>DEVRE DIŞI</b></span><em>Kâr garantisi yoktur. Testnet sonucu gerçek piyasa sonucunu garanti etmez.</em></footer>
  </main>
}
