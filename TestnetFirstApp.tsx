import { lazy, Suspense, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { CandlestickSeries, ColorType, createChart, HistogramSeries, LineSeries, type IPriceLine } from 'lightweight-charts'
import { Activity, ArrowUp, Bell, CheckCircle2, CircleDollarSign, Cloud, CloudCog, KeyRound, LockKeyhole, Menu, RadioTower, RefreshCw, Save, ShieldCheck, Sparkles, TestTube2, X } from 'lucide-react'
import { API_BASE, buildDemoSavePayload, userSessionToken } from './api'
import CoinAnalysisCenter from './CoinAnalysisCenter'

const BinanceDemo = lazy(() => import('./BinanceDemo'))
const LiveTradingPanel = lazy(() => import('./frontend/src/LiveTradingPanel'))
const CommercialHub = lazy(() => import('./CommercialHub'))
const CloudOpsCenter = lazy(() => import('./CloudOpsCenter'))
const SubscriptionCenter = lazy(() => import('./SubscriptionCenter'))
const MasterTrade = lazy(() => import('./MasterTrade'))
const BUILD_COMMIT = import.meta.env.VITE_BUILD_COMMIT

type View = 'testnet'|'ops'|'live'|'setup'|'pricing'|'billing'|'master-trade'
type Market = {symbol:string;display:string;price:number;change:number;volume:number}
type Candle = {time:number;open:number;high:number;low:number;close:number;volume:number}
type Point = {time:number;value:number}
type Analysis = {
  direction:'LONG'|'SHORT'|'BEKLE';confidence:number;entry:number;stop_loss:number;tp1:number;tp2:number;tp3:number;
  support:number;resistance:number;trend:string;momentum:string;rsi:number;adx:number;volume_ratio:number;explanation:string;
  series:{ema20:Point[];ema50:Point[];ema200:Point[]}
}
type Health = {status:string;version:string;mode:string;testnet:string;live_guard:string;paper:string;database:string;cloud_evidence:string;web_access:string}
type ConnectionStatus = {connections?:Record<'TESTNET'|'LIVE',{configured:boolean;active:boolean;last_test_ok:boolean;last_error?:string|null;storage?:string;account?:{active_positions?:number}|null}>;vault?:{ready:boolean;reason?:string|null}}
type NotificationItem = {id:string;title:string;description:string;kind:'success'|'warning'|'error'|'info'}
const ANALYSIS_TIMEOUT_MS = 30000

const notificationKind = (value:string):NotificationItem['kind'] => {
  if (/error|hata|failed|down|unavailable/i.test(value)) return 'error'
  if (/bek|kontrol|connecting|waiting|locked|kilit/i.test(value)) return 'warning'
  if (/ok|bağlı|active|canlı|kalıcı|hazır/i.test(value)) return 'success'
  return 'info'
}

const healthNotifications = (health:Health|null):NotificationItem[] => {
  if (!health) return []
  return [
    ['api', 'API status', health.status],
    ['database', 'Database status', health.database],
    ['mode', 'Execution mode', health.mode],
    ['testnet', 'Testnet status', health.testnet],
    ['live-guard', 'Live Guard status', health.live_guard],
    ['paper', 'Paper status', health.paper],
    ['evidence', 'Evidence status', health.cloud_evidence],
  ].filter(([, , value]) => Boolean(value)).map(([id,title,description]) => ({
    id,title,description,kind:notificationKind(description),
  }))
}

const format = (value:number) => value.toLocaleString('tr-TR',{maximumFractionDigits:value < 10 ? 5 : 2})

const gateInteraction = (target:View,eventName?:string) => ({
  role:'button' as const,
  tabIndex:0,
  style:{cursor:'pointer'},
  onClick:() => {window.dispatchEvent(new CustomEvent('protrebot-navigate',{detail:target}));if (eventName) window.setTimeout(() => window.dispatchEvent(new Event(eventName)),0)},
  onKeyDown:(event:KeyboardEvent<HTMLElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      window.dispatchEvent(new CustomEvent('protrebot-navigate',{detail:target}))
      if (eventName) window.setTimeout(() => window.dispatchEvent(new Event(eventName)),0)
    }
  },
})

function TestnetMarketChart({symbol,interval,onAnalysis,onAnalysisProgress,showLevels=true,showEma=true}:{symbol:string;interval:string;onAnalysis:(analysis:Analysis|null)=>void;onAnalysisProgress?:(progress:number)=>void;showLevels?:boolean;showEma?:boolean}) {
  const host = useRef<HTMLDivElement>(null)
  const [stream,setStream] = useState<'YÜKLENİYOR'|'CANLI'|'HATA'>('YÜKLENİYOR')
  const [updated,setUpdated] = useState('—')

  useEffect(() => {
    if (!host.current) return
    let active = true
    let activeController:AbortController|null = null
    let priceLines:IPriceLine[] = []
    const chart = createChart(host.current,{
      autoSize:true,
      layout:{background:{type:ColorType.Solid,color:'#111310'},textColor:'#a49f91'},
      grid:{vertLines:{color:'#272a22'},horzLines:{color:'#272a22'}},
      rightPriceScale:{borderColor:'#3c4034'},timeScale:{borderColor:'#3c4034',timeVisible:true,secondsVisible:false},
      crosshair:{vertLine:{color:'#8b8a52'},horzLine:{color:'#8b8a52'}},
    })
    const candles = chart.addSeries(CandlestickSeries,{upColor:'#0caf62',downColor:'#ef594a',wickUpColor:'#0caf62',wickDownColor:'#ef594a',borderVisible:false})
    const volume = chart.addSeries(HistogramSeries,{priceFormat:{type:'volume'},priceScaleId:''})
    volume.priceScale().applyOptions({scaleMargins:{top:.82,bottom:0}})
    const ema20 = chart.addSeries(LineSeries,{color:'#16a560',lineWidth:2,priceLineVisible:false,lastValueVisible:false,title:'EMA20'})
    const ema50 = chart.addSeries(LineSeries,{color:'#f3a712',lineWidth:2,priceLineVisible:false,lastValueVisible:false,title:'EMA50'})
    const ema200 = chart.addSeries(LineSeries,{color:'#8063d9',lineWidth:2,priceLineVisible:false,lastValueVisible:false,title:'EMA200'})

    const applyAnalysis = (analysis:Analysis) => {
      ema20.setData(showEma ? analysis.series.ema20.map(point => ({time:point.time as never,value:point.value})) : [])
      ema50.setData(showEma ? analysis.series.ema50.map(point => ({time:point.time as never,value:point.value})) : [])
      ema200.setData(showEma ? analysis.series.ema200.map(point => ({time:point.time as never,value:point.value})) : [])
      priceLines.forEach(line => candles.removePriceLine(line))
      const line = (price:number,color:string,title:string,width:1|2|3=2,style=2) => candles.createPriceLine({price,color,lineWidth:width,lineStyle:style,axisLabelVisible:true,title})
      priceLines = showLevels ? [
        line(analysis.entry,'#078b4c',`${analysis.direction} GİRİŞ`,3,0),
        line(analysis.stop_loss,'#ed4f42','STOP',3,0),
        line(analysis.tp1,'#28a657','TP1'),line(analysis.tp2,'#28a657','TP2'),line(analysis.tp3,'#28a657','TP3'),
        line(analysis.support,'#e96b5f','DESTEK',1,3),line(analysis.resistance,'#228d51','DİRENÇ',1,3),
      ] : []
      onAnalysis(analysis)
    }

    const load = async () => {
      activeController?.abort()
      const controller = new AbortController()
      activeController = controller
      const timeout = window.setTimeout(() => controller.abort(),ANALYSIS_TIMEOUT_MS)
      onAnalysisProgress?.(5)
      try {
        const [candleResponse,analysisResponse] = await Promise.all([
          fetch(`${API_BASE}/klines/${symbol}?interval=${interval}&limit=500`,{signal:controller.signal}),
          fetch(`${API_BASE}/analysis/${symbol}?interval=${interval}`,{signal:controller.signal}),
        ])
        if (!candleResponse.ok || !analysisResponse.ok) throw new Error('Piyasa verisi alınamadı')
        const rows = await candleResponse.json() as Candle[]
        onAnalysisProgress?.(45)
        const analysis = await analysisResponse.json() as Analysis
        if (!active) return
        candles.setData(rows.map(row => ({time:row.time as never,open:row.open,high:row.high,low:row.low,close:row.close})))
        volume.setData(rows.map(row => ({time:row.time as never,value:row.volume,color:row.close >= row.open ? 'rgba(24,177,100,.32)' : 'rgba(239,89,74,.28)'})))
        onAnalysisProgress?.(85)
        applyAnalysis(analysis)
        onAnalysisProgress?.(100)
        chart.timeScale().fitContent()
        setStream('CANLI')
        setUpdated(new Date().toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit',second:'2-digit'}))
      } catch {
        if (active && activeController === controller) {setStream('HATA');onAnalysis(null);onAnalysisProgress?.(-1)}
      } finally {
        window.clearTimeout(timeout)
        if (activeController === controller) activeController = null
      }
    }
    void load()
    const timer = window.setInterval(() => void load(),15000)
    return () => {active=false;activeController?.abort();window.clearInterval(timer);chart.remove();onAnalysis(null)}
  },[symbol,interval,onAnalysis,showLevels,showEma])

  return <div className="v26ChartShell">
    <div className="v26ChartStatus"><span className={stream === 'CANLI' ? 'live' : stream === 'HATA' ? 'error' : ''}><i/>{stream}</span><em>Binance piyasa verisi · 15 sn yenileme · {updated}</em></div>
    <div className="v26Chart" ref={host}/>
  </div>
}

export default function TestnetFirstApp() {
  const initialView = ():View => window.location.pathname === '/pricing' ? 'pricing' : window.location.pathname === '/billing' ? 'billing' : window.location.pathname === '/master-trade' ? 'master-trade' : 'testnet'
  const [view,setView] = useState<View>(initialView)
  const [masterTradeAccess,setMasterTradeAccess] = useState<'loading'|'granted'|'locked'|'unauthenticated'>('loading')
  const [markets,setMarkets] = useState<Market[]>([])
  const [symbol,setSymbol] = useState('BTCUSDT')
  const [marketQuery,setMarketQuery] = useState('')
  const [marketPickerOpen,setMarketPickerOpen] = useState(false)
  const [interval,setInterval] = useState('15m')
  const [analysis,setAnalysis] = useState<Analysis|null>(null)
  const [analysisProgress,setAnalysisProgress] = useState(0)
  const [health,setHealth] = useState<Health|null>(null)
  const [loading,setLoading] = useState(false)
  const [marketError,setMarketError] = useState(false)
  const [credentials,setCredentials] = useState({demoApiKey:'',demoSecretKey:'',liveApiKey:'',liveSecretKey:''})
  const [demoVerification,setDemoVerification] = useState({busy:false,kind:'info',message:''})
  const [connectionStatus,setConnectionStatus] = useState<ConnectionStatus|null>(null)
  const [notificationsOpen,setNotificationsOpen] = useState(false)
  const [headerHidden,setHeaderHidden] = useState(false)
  const [showBackToTop,setShowBackToTop] = useState(false)
  const [mobileMenuOpen,setMobileMenuOpen] = useState(false)
  const [complianceOpen,setComplianceOpen] = useState(false)
  const [complianceTab,setComplianceTab] = useState<'risk'|'privacy'|'terms'|'support'>('risk')
  const notificationRef = useRef<HTMLDivElement>(null)
  const marketPickerRef = useRef<HTMLDivElement>(null)
  const notifications = healthNotifications(health)
  const selectedMarket = markets.find(market => market.symbol === symbol)

  const navigate = (target:View) => {
    setView(target)
    setMobileMenuOpen(false)
    if (target === 'pricing' || target === 'billing' || target === 'master-trade') {
      window.history.pushState({},'', `/${target}`)
    } else if (window.location.pathname === '/pricing' || window.location.pathname === '/billing' || window.location.pathname === '/master-trade') {
      window.history.pushState({},'', '/')
    }
  }

  useEffect(() => {
    const onNavigate = (event:Event) => navigate((event as CustomEvent<View>).detail)
    const onHistory = () => setView(initialView())
    window.addEventListener('protrebot-navigate',onNavigate)
    window.addEventListener('popstate',onHistory)
    return () => { window.removeEventListener('protrebot-navigate',onNavigate);window.removeEventListener('popstate',onHistory) }
  },[])

  useEffect(() => {
    if (view !== 'master-trade') return
    const token = userSessionToken()
    if (!token) {
      window.location.assign('/login')
      return
    }
    let active = true
    setMasterTradeAccess('loading')
    const headers = new Headers({Authorization: `Bearer ${token}`})
    fetch(`${API_BASE}/v22/profile`, { headers })
      .then(async response => {
        if (!response.ok) throw new Error('Unauthorized')
        const payload = await response.json() as { subscription?: {plan?: string}; user?: { subscription?: {plan?: string} } }
        const plan = String(payload.subscription?.plan || payload.user?.subscription?.plan || 'FREE').toUpperCase()
        if (!active) return
        setMasterTradeAccess(['PRO', 'ELITE'].includes(plan) ? 'granted' : 'locked')
      })
      .catch(() => {
        if (active) setMasterTradeAccess('locked')
      })
    return () => { active = false }
  }, [view])

  const refresh = async () => {
    setLoading(true)
    setMarketError(false)
    try {
      const [marketResult,healthResult] = await Promise.allSettled([
        fetch(`${API_BASE}/markets?limit=100`),
        fetch(`${API_BASE}/health`),
      ])
      if (marketResult.status === 'fulfilled' && marketResult.value.ok) {
        try {
          const payload = await marketResult.value.json() as Market[]
          if (Array.isArray(payload)) setMarkets(payload)
          else setMarketError(true)
        } catch {
          setMarketError(true)
        }
      } else {
        setMarketError(true)
      }
      if (healthResult.status === 'fulfilled' && healthResult.value.ok) {
        try { setHealth(await healthResult.value.json() as Health) } catch {}
      }
    } finally {setLoading(false)}
  }

  const refreshConnectionStatus = async () => {
    try {
      const headers = new Headers(); const token = userSessionToken(); if (token) headers.set('Authorization',`Bearer ${token}`)
      const response = await fetch(`${API_BASE}/exchange-connections/status`,{headers})
      if (response.ok) setConnectionStatus(await response.json() as ConnectionStatus)
    } catch {}
  }

  const saveDemoCredentials = async () => {
    const apiKey = credentials.demoApiKey.trim()
    const secretKey = credentials.demoSecretKey.trim()
    if (!apiKey || !secretKey) {
      setDemoVerification({busy:false,kind:'error',message:'Demo API Key ve Secret Key gerekli.'})
      return
    }
    setDemoVerification({busy:true,kind:'info',message:'Demo anahtarları doğrulanıyor ve güvenli kasaya kaydediliyor…'})
    try {
      const headers = new Headers({'Content-Type':'application/json'}); const token = userSessionToken(); if (token) headers.set('Authorization',`Bearer ${token}`)
      const saveResponse = await fetch(`${API_BASE}/exchange-connections/save`,{
        method:'POST',headers,
        body:JSON.stringify(buildDemoSavePayload(apiKey, secretKey)),
      })
      const savePayload = await saveResponse.json().catch(() => null) as {detail?:unknown}|null
      if (!saveResponse.ok) throw new Error(typeof savePayload?.detail === 'string' ? savePayload.detail : 'Demo credentials securely save edilemedi.')
      setCredentials(current => ({...current,demoApiKey:'',demoSecretKey:''}))
      setDemoVerification({busy:false,kind:'ok',message:'Demo credentials saved securely. Verify connection to activate the Demo channel.'})
      await refreshConnectionStatus()
    } catch (error) {
      setDemoVerification({busy:false,kind:'error',message:error instanceof Error ? error.message : 'Demo bağlantısı doğrulanamadı. Ağ ve vault durumunu kontrol edin.'})
    }
  }

  const verifyDemoConnection = async () => {
    setDemoVerification({busy:true,kind:'info',message:'Kayıtlı Demo bağlantısı doğrulanıyor…'})
    try {
      const headers = new Headers({'Content-Type':'application/json'}); const token = userSessionToken(); if (token) headers.set('Authorization',`Bearer ${token}`)
      const testResponse = await fetch(`${API_BASE}/exchange-connections/test`,{method:'POST',headers,body:JSON.stringify({mode:'TESTNET'})})
      const testPayload = await testResponse.json().catch(() => null) as {detail?:unknown}|null
      if (!testResponse.ok) throw new Error(typeof testPayload?.detail === 'string' ? testPayload.detail : 'Saved Demo connection could not be verified.')
      const activateResponse = await fetch(`${API_BASE}/exchange-connections/activate`,{method:'POST',headers,body:JSON.stringify({mode:'TESTNET',confirmation:'TESTNET BAĞLANTIYI AÇ'})})
      const activatePayload = await activateResponse.json().catch(() => null) as {detail?:unknown}|null
      if (!activateResponse.ok) throw new Error(typeof activatePayload?.detail === 'string' ? activatePayload.detail : 'Demo connection could not be activated.')
      setDemoVerification({busy:false,kind:'ok',message:'DEMO CONNECTED · API connection verified. Trading channel: DEMO.'})
      await refreshConnectionStatus()
      await refresh()
    } catch (error) {
      setDemoVerification({busy:false,kind:'error',message:error instanceof Error ? error.message : 'Saved Demo connection could not be verified.'})
      await refreshConnectionStatus()
    }
  }

  useEffect(() => {
    void refresh()
    void refreshConnectionStatus()
    const timer = window.setInterval(() => void refresh(),60000)
    const openExchangeSettings = () => setView('setup')
    window.addEventListener('protrebot-open-exchange-settings', openExchangeSettings)
    return () => {window.clearInterval(timer);window.removeEventListener('protrebot-open-exchange-settings', openExchangeSettings)}
  },[])

  useEffect(() => {
    if (!notificationsOpen) return
    const closeOnOutsideClick = (event:MouseEvent) => {
      if (notificationRef.current && !notificationRef.current.contains(event.target as Node)) setNotificationsOpen(false)
    }
    const closeOnEscape = (event:KeyboardEvent) => {if (event.key === 'Escape') setNotificationsOpen(false)}
    document.addEventListener('mousedown', closeOnOutsideClick)
    document.addEventListener('keydown', closeOnEscape)
    return () => {document.removeEventListener('mousedown', closeOnOutsideClick);document.removeEventListener('keydown', closeOnEscape)}
  },[notificationsOpen])

  useEffect(() => {
    if (!marketPickerOpen) return
    const closeOnOutsideClick = (event:MouseEvent) => {
      if (marketPickerRef.current && !marketPickerRef.current.contains(event.target as Node)) setMarketPickerOpen(false)
    }
    const closeOnEscape = (event:KeyboardEvent) => {if (event.key === 'Escape') setMarketPickerOpen(false)}
    document.addEventListener('mousedown',closeOnOutsideClick)
    document.addEventListener('keydown',closeOnEscape)
    return () => {document.removeEventListener('mousedown',closeOnOutsideClick);document.removeEventListener('keydown',closeOnEscape)}
  },[marketPickerOpen])

  useEffect(() => {
    let previousY = window.scrollY
    let ticking = false
    const updateScrollState = () => {
      const currentY = window.scrollY
      const delta = currentY - previousY
      if (currentY <= 12) setHeaderHidden(false)
      else if (Math.abs(delta) >= 8) setHeaderHidden(delta > 0)
      setShowBackToTop(currentY >= 450)
      previousY = currentY
      ticking = false
    }
    const onScroll = () => {
      if (!ticking) {ticking=true;window.requestAnimationFrame(updateScrollState)}
    }
    window.addEventListener('scroll',onScroll,{passive:true})
    return () => window.removeEventListener('scroll',onScroll)
  },[])

  return <main className={`v26App ${view === 'master-trade' ? 'masterTradeRoute' : ''}`}>
    <header className={`v26Header ${headerHidden ? 'v26HeaderHidden' : ''}`} data-build-commit={BUILD_COMMIT}>
      <div className="v26Brand"><span>X</span><div><b>PROTREBOT ELITE X</b><small>V27 · CLOUD OPERATIONS / TESTNET-FIRST</small></div></div>
      <div className="v26HeaderSignals">
        <span className="ok"><i/>SUNUCU CANLI</span>
        <span className="ok"><i/>TESTNET ANA MOD</span>
        <span className={health?.cloud_evidence === 'KALICI' ? 'ok' : 'locked'}><Cloud/>{health?.cloud_evidence || 'KANIT BAĞLANIYOR'}</span>
        <span className={health?.live_guard === 'SALT OKUNUR BAĞLI' ? 'ok' : 'locked'}><LockKeyhole/>{health?.live_guard || 'CANLI API BEKLİYOR'}</span>
      </div>
      <div className="v26HeaderActions">
        <button className="v26MasterTradeButton" onClick={() => navigate('master-trade')}><ShieldCheck/><span><b>MASTER TRADE</b></span></button>
        <button className="v26SubscriptionBadge" onClick={() => navigate('billing')}><Sparkles/> PLANS &amp; BILLING</button>
        <button className="v26Refresh" aria-label="Piyasa verisini yenile" title="Piyasa verisini yenile" onClick={refresh} disabled={loading}><RefreshCw className={loading ? 'spin' : ''}/>{loading ? 'YENİLENİYOR' : 'YENİLE'}</button>
        <button className="mobileMenuButton" type="button" aria-label={mobileMenuOpen ? 'Menüyü kapat' : 'Menüyü aç'} aria-expanded={mobileMenuOpen} onClick={() => setMobileMenuOpen(open => !open)}>{mobileMenuOpen ? <X/> : <Menu/>}</button>
        <div className="v26Notifications" ref={notificationRef}>
          <button className="v26NotificationButton" type="button" aria-label="Notifications" aria-expanded={notificationsOpen} onClick={() => setNotificationsOpen(open => !open)}><Bell/></button>
          {notificationsOpen && <section className="v26NotificationPanel" role="dialog" aria-label="Notifications">
            <header><div><small>STATUS CENTER</small><h2>Notifications</h2></div><span>{notifications.length}</span></header>
            {notifications.length ? <div className="v26NotificationList">{notifications.map(item => <article key={item.id} className={item.kind}><i><Bell/></i><div><b>{item.title}</b><p>{item.description}</p><small>Current status</small></div></article>)}</div> : <div className="v26NotificationEmpty"><Bell/><b>No notifications</b><p>You're all caught up.<br/>New system notifications will appear here.</p></div>}
          </section>}
        </div>
      </div>
    </header>

    <nav className="v26Nav">
      <button className={view === 'testnet' ? 'active' : ''} onClick={() => setView('testnet')}><TestTube2/><span><b>TESTNET KOMUTA</b><small>Binance Futures Demo · Ana çalışma alanı</small></span></button>
      <button className={view === 'ops' ? 'active' : ''} onClick={() => setView('ops')}><Cloud/><span><b>OPERASYON & KANIT</b><small>Karar, pozisyon ve kalıcı PostgreSQL kaydı</small></span></button>
      <button className={view === 'live' ? 'active liveTab' : ''} onClick={() => setView('live')}><ShieldCheck/><span><b>CANLI HAZIRLIK</b><small>API yoksa kesin kilitli · Gerçek kanal</small></span></button>
      <button className={view === 'setup' ? 'active' : ''} onClick={() => setView('setup')}><CloudCog/><span><b>YAYIN KAPILARI</b><small>Render secret ve geçiş kontrolü</small></span></button>
    </nav>
    {mobileMenuOpen && <div className="mobileMenuBackdrop" role="presentation" onClick={event => { if (event.target === event.currentTarget) setMobileMenuOpen(false) }}><aside className="mobileMenuDrawer" role="dialog" aria-modal="true" aria-label="Mobil menü"><header><div><small>PROTREBOT ELITE X</small><b>Workspace</b></div><button type="button" aria-label="Menüyü kapat" onClick={() => setMobileMenuOpen(false)}><X/></button></header><button onClick={() => navigate('testnet')}><TestTube2/><span><b>Dashboard</b><small>Demo command center</small></span></button><button onClick={() => navigate('ops')}><Cloud/><span><b>Operasyon</b><small>Evidence and cloud ops</small></span></button><button onClick={() => navigate('live')}><ShieldCheck/><span><b>Canlı</b><small>Fail-closed live gates</small></span></button><button onClick={() => navigate('setup')}><CloudCog/><span><b>Ayarlar</b><small>API connections</small></span></button><button onClick={() => navigate('billing')}><Sparkles/><span><b>Billing</b><small>Subscription workspace</small></span></button></aside></div>}

    <section className="v26ModeBar">
      <div><small>AKTİF ÇALIŞMA ALANI</small><h1>{view === 'testnet' ? 'Binance Futures Demo Merkezi' : view === 'ops' ? 'Bulut Operasyon ve Kanıt Merkezi' : view === 'live' ? 'Gerçek Futures Hazırlık Merkezi' : view === 'pricing' ? 'Plans & Pricing' : view === 'billing' ? 'Billing & Subscription' : 'Sunucu ve Anahtar Kapıları'}</h1><p>{view === 'testnet' ? 'Gerçek Binance motoruna en yakın test ortamı; sanal bakiye, gerçek emir akışı ve borsa yanıtları.' : view === 'ops' ? 'Otonom taramanın son kararı, pozisyonlar ve yeniden başlatmaya dayanıklı PostgreSQL kanıt defteri.' : view === 'live' ? 'Şifreli canlı kasa kaydı ve tüm risk kapıları tamamlanana kadar emir gönderimi fail-closed olarak kilitli.' : view === 'pricing' || view === 'billing' ? 'Choose a subscription level for your trading intelligence workspace.' : 'Anahtar değerleri tarayıcıya veya GitHub’a yazılmaz; yalnızca sunucu tarafındaki şifreli kasa veya güvenli geçiş değişkenlerinde tutulur.'}</p></div>
      <aside><span><CircleDollarSign/>GERÇEK PARA</span><b>{view === 'live' ? 'KİLİTLİ' : '0 USDT'}</b><em>Paper devre dışı</em></aside>
    </section>

    {view === 'testnet' && <>
      <section className="v26MarketBar">
        <div className="v26MarketTitle"><Activity/><span><small>SEÇİLİ TESTNET PAZARI</small><b>{symbol.replace('USDT','/USDT')}</b></span><strong className={analysis?.direction === 'SHORT' ? 'short' : analysis?.direction === 'LONG' ? 'long' : ''}>{analysis?.direction || (analysisProgress < 0 ? 'ANALİZ HATASI' : 'HESAPLANIYOR')} <em>{analysis ? `%${analysis.confidence}` : analysisProgress < 0 ? 'TEKRAR DENEYİN' : analysisProgress > 0 ? `%${analysisProgress}` : 'BAŞLATILIYOR'}</em></strong></div>
        <div className="v26MarketPicker" ref={marketPickerRef}>
          <button type="button" className="v26MarketTrigger" aria-expanded={marketPickerOpen} onClick={() => setMarketPickerOpen(open => !open)}><span><small>MARKET</small><b>{selectedMarket?.display || symbol.replace('USDT','/USDT')}</b></span><em>▼</em></button>
          {marketPickerOpen && <div className="v26MarketPopover" role="dialog" aria-label="Testnet market selector">
            <input autoFocus aria-label="Testnet market search" placeholder="Sembol ara" value={marketQuery} onChange={event => setMarketQuery(event.target.value)} />
            <div className="v26MarketOptions">{markets.filter(market => `${market.display} ${market.symbol}`.toUpperCase().includes(marketQuery.trim().toUpperCase())).map(market => <button type="button" key={market.symbol} className={market.symbol === symbol ? 'active' : ''} onClick={() => {setSymbol(market.symbol);setMarketPickerOpen(false);setMarketQuery('')}}><b>{market.display}</b><span>{format(market.price)}</span><em className={market.change >= 0 ? 'up' : 'down'}>{market.change >= 0 ? '+' : ''}{market.change.toFixed(2)}%</em></button>)}</div>
          </div>}
        </div>
        <div className="v26Intervals">{['1m','5m','15m','1h','4h'].map(item => <button key={item} className={interval === item ? 'active' : ''} onClick={() => setInterval(item)}>{item}</button>)}</div>
        {marketError && <div className="v26MarketError" role="alert"><span>Market verisi yüklenemedi.</span><button type="button" onClick={() => void refresh()} disabled={loading}>{loading ? 'YENİLENİYOR…' : 'TEKRAR DENE'}</button></div>}
      </section>
      <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Testnet merkezi hazırlanıyor…</div>}>
        <BinanceDemo active symbol={symbol} markets={markets} onSymbolChange={setSymbol} analysis={analysis} chart={<TestnetMarketChart symbol={symbol} interval={interval} onAnalysis={setAnalysis} onAnalysisProgress={setAnalysisProgress}/>}/>
      </Suspense>
      <CoinAnalysisCenter interval={interval} onIntervalChange={setInterval} chart={(selectedSymbol,selectedInterval,showLevels,showEma) => <TestnetMarketChart symbol={selectedSymbol} interval={selectedInterval} showLevels={showLevels} showEma={showEma} onAnalysis={() => undefined}/>}/>
    </>}

    {view === 'master-trade' && (
      masterTradeAccess === 'loading' ? <div className="v26Loading"><RefreshCw className="spin"/>Master Trade erişim kontrol ediliyor…</div> :
      masterTradeAccess === 'locked' ? <section className="premiumGatePanel" style={{margin:'1.5rem auto',maxWidth:'900px',padding:'2rem',background:'rgba(15,23,42,0.9)',border:'1px solid rgba(148,163,184,0.26)',borderRadius:'18px',boxShadow:'0 18px 45px rgba(15,23,42,0.35)'}}>
        <div style={{display:'grid',gap:'0.75rem',justifyItems:'flex-start'}}>
          <span style={{display:'inline-flex',alignItems:'center',gap:'0.5rem',padding:'0.35rem 0.8rem',borderRadius:'999px',border:'1px solid rgba(251,191,36,0.4)',background:'rgba(251,191,36,0.08)',color:'#fcd34d',fontWeight:700,fontSize:'0.72rem',letterSpacing:'0.12em'}}>PREMIUM</span>
          <h3 style={{margin:0,fontSize:'2rem',lineHeight:1.1}}>MASTER TRADE</h3>
          <p style={{margin:0,maxWidth:'620px',color:'#cbd5e1',fontSize:'1rem'}}>Professional trading workspace</p>
          <p style={{margin:0,maxWidth:'620px',color:'#cbd5e1'}}>Premium members only.</p>
          <button type="button" onClick={() => navigate('billing')} style={{marginTop:'0.5rem',padding:'0.9rem 1.4rem',border:'1px solid rgba(59,130,246,0.35)',background:'linear-gradient(135deg, rgba(59,130,246,0.14), rgba(14,165,233,0.22))',color:'#eff6ff',borderRadius:'12px',fontWeight:700,letterSpacing:'0.06em',cursor:'pointer'}}>UPGRADE TO PREMIUM</button>
        </div>
      </section> :
      <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Master Trade hazırlanıyor…</div>}>
        <MasterTrade onBack={() => navigate('testnet')} />
      </Suspense>
    )}

    {(view === 'pricing' || view === 'billing') && <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Subscription workspace hazırlanıyor…</div>}><SubscriptionCenter mode={view} onNavigate={navigate}/></Suspense>}

    {view === 'live' && <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Canlı güvenlik merkezi hazırlanıyor…</div>}><LiveTradingPanel active symbol={symbol} analysis={analysis}/></Suspense>}

    {view === 'ops' && <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Bulut operasyon merkezi hazırlanıyor…</div>}><CloudOpsCenter/></Suspense>}

    {view === 'setup' && <section className="connectionCenter">
      <header className="connectionCenterHeader"><div><span>SECURE CONNECTIONS · TESTNET-FIRST</span><h2>API &amp; Connection Center</h2><p>Demo ve Live bağlantılarını mevcut şifreli kasa ve fail-closed güvenlik kapılarıyla yönet.</p></div><div className="connectionHeaderStatus"><span><i className={connectionStatus?.connections?.TESTNET?.configured ? 'ok' : 'pending'}/>DEMO {connectionStatus?.connections?.TESTNET?.configured ? 'CONFIGURED' : 'NOT CONFIGURED'}</span><span><i className={connectionStatus?.connections?.TESTNET?.active ? 'ok' : 'pending'}/>DEMO {connectionStatus?.connections?.TESTNET?.active ? 'CONNECTED' : 'LOCKED'}</span><span><i className="locked"/>LIVE LOCKED</span></div></header>
      <section className="connectionStatusRail"><div><small>DEMO STATUS</small><strong><i className={connectionStatus?.connections?.TESTNET?.configured ? 'ok' : 'pending'}/>{connectionStatus?.connections?.TESTNET?.configured ? 'CONFIGURED' : 'NOT CONFIGURED'}</strong></div><div><small>CONNECTION</small><strong><i className={connectionStatus?.connections?.TESTNET?.active ? 'ok' : 'pending'}/>{connectionStatus?.connections?.TESTNET?.active ? 'CONNECTED' : 'NOT CONNECTED'}</strong></div><div><small>TRADING CHANNEL</small><strong><i className="locked"/>LOCKED</strong></div><button type="button" onClick={() => void refreshConnectionStatus()} aria-label="Refresh connection status"><RefreshCw/></button></section>
      <div className="connectionWorkflow"><span><b>01</b><small>ENTER CREDENTIALS</small></span><span><b>02</b><small>SAVE SECURELY</small></span><span><b>03</b><small>VERIFY CONNECTION</small></span><span><b>04</b><small>RUN DEMO TEST</small></span></div>
      <section className="connectionDemoPanel"><header><div><span>DEMO / TESTNET</span><h3>Binance Futures Demo</h3><p>Demo API anahtarları şifreli sunucu kasasına kaydedilir. Bu kanal gerçek para ve Live emir kanalı değildir.</p></div><strong className="connectionChannelBadge"><TestTube2/> DEMO ONLY</strong></header><div className="connectionFormGrid"><label><span>Demo API Key</span><input type="text" value={credentials.demoApiKey} onChange={event => setCredentials(current => ({...current,demoApiKey:event.target.value}))} autoComplete="off" spellCheck={false} placeholder="Enter Demo API Key"/></label><label><span>Demo Secret Key</span><input type="password" value={credentials.demoSecretKey} onChange={event => setCredentials(current => ({...current,demoSecretKey:event.target.value}))} autoComplete="new-password" spellCheck={false} placeholder="Enter Demo Secret Key"/></label></div><div className="connectionActions"><button type="button" className="connectionPrimary" onClick={() => void saveDemoCredentials()} disabled={demoVerification.busy}><Save/>{demoVerification.busy ? 'SAVING…' : 'SAVE SECURELY'}</button><button type="button" className="connectionSecondary" onClick={() => void verifyDemoConnection()} disabled={demoVerification.busy || !connectionStatus?.connections?.TESTNET?.configured}><ShieldCheck/>{demoVerification.busy ? 'VERIFYING…' : 'VERIFY DEMO CONNECTION'}</button></div>{demoVerification.message && <div className={`connectionFeedback ${demoVerification.kind}`}><i/>{demoVerification.message}</div>}<small className="connectionNote">Secrets are sent only to the existing vault API and are never returned to the browser.</small></section>
      <section className="connectionLivePanel"><header><div><span>REAL BINANCE FUTURES</span><h3><LockKeyhole/> Live Trading Locked</h3><p>Live credentials are managed separately. Live trading remains locked until every existing V25 safety condition is satisfied.</p></div><strong className="connectionLiveBadge"><i className="locked"/>{health?.live_guard || 'LIVE LOCKED'}</strong></header><div className="connectionLiveGrid"><label><span>Live API Key</span><input type="text" value={credentials.liveApiKey} onChange={event => setCredentials(current => ({...current,liveApiKey:event.target.value}))} autoComplete="off" spellCheck={false} placeholder="Configured separately"/></label><label><span>Live Secret Key</span><input type="password" value={credentials.liveSecretKey} onChange={event => setCredentials(current => ({...current,liveSecretKey:event.target.value}))} autoComplete="new-password" placeholder="Never displayed"/></label></div><small>Live connection is not tested, activated, or armed from this page.</small></section>
      <section className="connectionSecurityPanel"><header><div><span>SECURITY &amp; SAFETY</span><h3>Fail-closed by design</h3></div><ShieldCheck/></header><div>{['Secrets are stored server-side','Secrets are never displayed in the UI','Live trading remains locked by default','Demo and Live credentials are separated','Orders require existing safety gates','No automatic live orders on startup'].map(item => <span key={item}><CheckCircle2/>{item}</span>)}</div></section>
    </section>}

    {showBackToTop && <button className="v26BackToTop" type="button" aria-label="Yukarı çık" onClick={() => window.scrollTo({top:0,behavior:'smooth'})}><ArrowUp/></button>}
    <nav className={`terminalMobileNav ${headerHidden ? 'terminalMobileNavHidden' : ''}`} aria-label="Mobil ana navigasyon"><button className={view === 'testnet' ? 'active' : ''} onClick={() => setView('testnet')}><TestTube2/><span>Dashboard</span></button><button className={view === 'ops' ? 'active' : ''} onClick={() => setView('ops')}><Cloud/><span>Operasyon</span></button><button className={view === 'live' ? 'active' : ''} onClick={() => setView('live')}><ShieldCheck/><span>Canlı</span></button><button className={view === 'setup' ? 'active' : ''} onClick={() => setView('setup')}><CloudCog/><span>Ayarlar</span></button></nav>
    <section className="v26TrustStrip" style={{margin:'0 1rem 1rem',padding:'1rem 1.25rem',border:'1px solid rgba(148,163,184,0.18)',borderRadius:'16px',background:'rgba(15,23,42,0.82)',display:'grid',gap:'0.7rem'}}>
      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',gap:'0.75rem',flexWrap:'wrap'}}>
        <div>
          <small style={{display:'block',letterSpacing:'0.12em',fontSize:'0.68rem',color:'#94a3b8'}}>CUSTOMER TRUST</small>
          <strong style={{fontSize:'1rem',color:'#f8fafc'}}>Demo-first operating model · Live orders remain locked until all safety gates pass.</strong>
        </div>
        <div style={{display:'flex',gap:'0.5rem',flexWrap:'wrap'}}>
          <button type="button" onClick={() => { setComplianceTab('risk'); setComplianceOpen(true) }} style={{padding:'0.6rem 0.9rem',borderRadius:'10px',border:'1px solid rgba(251,191,36,0.35)',background:'rgba(251,191,36,0.08)',color:'#fef3c7',fontWeight:700,cursor:'pointer'}}>Risk Disclosure</button>
          <button type="button" onClick={() => { setComplianceTab('privacy'); setComplianceOpen(true) }} style={{padding:'0.6rem 0.9rem',borderRadius:'10px',border:'1px solid rgba(59,130,246,0.35)',background:'rgba(59,130,246,0.08)',color:'#dbeafe',fontWeight:700,cursor:'pointer'}}>Privacy</button>
          <button type="button" onClick={() => { setComplianceTab('terms'); setComplianceOpen(true) }} style={{padding:'0.6rem 0.9rem',borderRadius:'10px',border:'1px solid rgba(52,211,153,0.35)',background:'rgba(52,211,153,0.08)',color:'#d1fae5',fontWeight:700,cursor:'pointer'}}>Terms</button>
          <button type="button" onClick={() => { setComplianceTab('support'); setComplianceOpen(true) }} style={{padding:'0.6rem 0.9rem',borderRadius:'10px',border:'1px solid rgba(168,85,247,0.35)',background:'rgba(168,85,247,0.08)',color:'#f3e8ff',fontWeight:700,cursor:'pointer'}}>Support</button>
        </div>
      </div>
    </section>
    <footer className="v26Footer"><span><RadioTower/>API: <b>{health?.status === 'ok' ? 'BAĞLI' : 'KONTROL EDİLİYOR'}</b></span><span>Veritabanı: <b>{health?.database || '—'}</b></span><span>Kanıt defteri: <b>{health?.cloud_evidence || '—'}</b></span><span>Çalışma modu: <b>TESTNET FIRST</b></span><span>Paper: <b>DEVRE DIŞI</b></span><em>Kâr garantisi yoktur. Testnet sonucu gerçek piyasa sonucunu garanti etmez.</em></footer>

    {complianceOpen && <div className="v26ComplianceBackdrop" role="presentation" onClick={(event) => { if (event.target === event.currentTarget) setComplianceOpen(false) }} style={{position:'fixed',inset:0,background:'rgba(2,6,23,0.76)',display:'grid',placeItems:'center',padding:'1rem',zIndex:1000}}>
      <aside className="v26ComplianceModal" role="dialog" aria-modal="true" aria-label="Trust and compliance" style={{width:'min(760px, 100%)',maxHeight:'80vh',overflowY:'auto',background:'#0f172a',border:'1px solid rgba(148,163,184,0.3)',borderRadius:'20px',padding:'1.25rem',boxShadow:'0 30px 80px rgba(2,6,23,0.6)'}} onClick={event => event.stopPropagation()}>
        <header style={{display:'flex',justifyContent:'space-between',alignItems:'center',gap:'1rem',marginBottom:'1rem'}}>
          <div>
            <small style={{letterSpacing:'0.12em',color:'#94a3b8'}}>TRUST CENTER</small>
            <h3 style={{margin:'0.25rem 0 0',fontSize:'1.5rem'}}>Risk, privacy and support</h3>
          </div>
          <button type="button" onClick={() => setComplianceOpen(false)} style={{padding:'0.5rem 0.8rem',borderRadius:'10px',border:'1px solid rgba(148,163,184,0.3)',background:'transparent',color:'#e2e8f0',cursor:'pointer'}}>Close</button>
        </header>
        <nav style={{display:'flex',gap:'0.5rem',flexWrap:'wrap',marginBottom:'1rem'}}>
          {(['risk','privacy','terms','support'] as const).map(tab => <button key={tab} type="button" onClick={() => setComplianceTab(tab)} style={{padding:'0.55rem 0.8rem',borderRadius:'999px',border: complianceTab === tab ? '1px solid rgba(96,165,250,0.7)' : '1px solid rgba(148,163,184,0.2)',background: complianceTab === tab ? 'rgba(59,130,246,0.12)' : 'transparent',color: complianceTab === tab ? '#dbeafe' : '#cbd5e1',fontWeight:700,textTransform:'capitalize',cursor:'pointer'}}>{tab}</button>)}
        </nav>
        {complianceTab === 'risk' && <div style={{display:'grid',gap:'0.8rem',color:'#e2e8f0',lineHeight:1.6}}>
          <p>ProTreBot is a research and demo-first operating workspace. It is not a guarantee of profit and it does not promise financial returns.</p>
          <p>Market data, signal quality, order logic, and execution status can change rapidly. The platform uses fail-closed security gates by default. Live orders are never activated automatically and only proceed after explicit validation and safety checks.</p>
          <p>Users must understand that market exposure carries risk, including potential loss of capital. This platform is designed for education, simulation, risk review, and controlled testnet workflows unless a separate live trading authorization is explicitly completed.</p>
        </div>}
        {complianceTab === 'privacy' && <div style={{display:'grid',gap:'0.8rem',color:'#e2e8f0',lineHeight:1.6}}>
          <p>We do not store raw exchange secrets in browser storage. API keys and credentials are handled through the secure backend vault or server-side environment when available.</p>
          <p>Diagnostic and operational metadata may be retained for monitoring, integrity, and support purposes. Sensitive values are minimized and access is restricted to authorized operational workflows.</p>
          <p>Users remain responsible for safeguarding their own credentials and for reviewing any legal privacy obligations applicable to their region and use case.</p>
        </div>}
        {complianceTab === 'terms' && <div style={{display:'grid',gap:'0.8rem',color:'#e2e8f0',lineHeight:1.6}}>
          <p>Use of this platform is governed by the applicable service agreement, risk acknowledgment, and product terms provided by the operator. The software is provided as a workflow and analytics environment.</p>
          <p>Testnet or demo features are not a substitute for regulated financial advice or live market execution. Users must confirm that their use case complies with local rules and account restrictions.</p>
          <p>Any live trading activation requires separate authorization, security validation, and explicit user acknowledgment of the associated execution risk.</p>
        </div>}
        {complianceTab === 'support' && <div style={{display:'grid',gap:'0.8rem',color:'#e2e8f0',lineHeight:1.6}}>
          <p>Support channels should be used for access issues, credentials, billing questions, onboarding, and operational troubleshooting.</p>
          <p>Use the in-app support workflow or your authorized operational contact channel. No public placeholder support email is used on the customer-facing surface.</p>
          <p>Response time: operational requests are reviewed as soon as possible; live trading access issues are handled in priority order with safety review first.</p>
        </div>}
      </aside>
    </div>}
  </main>
}
