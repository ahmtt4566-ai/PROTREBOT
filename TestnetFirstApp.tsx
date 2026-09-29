import { lazy, Suspense, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { CandlestickSeries, ColorType, createChart, HistogramSeries, LineSeries, type IPriceLine } from 'lightweight-charts'
import { Activity, ArrowLeft, BarChart3, Bell, CheckCircle2, CircleDollarSign, Cloud, CloudCog, KeyRound, LockKeyhole, RadioTower, Radar, RefreshCw, Save, ShieldCheck, Sparkles, TestTube2 } from 'lucide-react'
import { API_BASE, buildDemoSavePayload, userSessionToken } from './api'
import CoinAnalysisCenter from './CoinAnalysisCenter'
import ScannerCenter from './ScannerCenter'

const BinanceDemo = lazy(() => import('./BinanceDemo'))
const LiveTradingPanel = lazy(() => import('./frontend/src/LiveTradingPanel'))
const CommercialHub = lazy(() => import('./CommercialHub'))
const CloudOpsCenter = lazy(() => import('./CloudOpsCenter'))
const SubscriptionCenter = lazy(() => import('./SubscriptionCenter'))
const MasterTrade = lazy(() => import('./MasterTrade'))
const BUILD_COMMIT = import.meta.env.VITE_BUILD_COMMIT
type View = 'dashboard'|'trading'|'risk'|'analyst'|'scanner'|'performance'|'testnet'|'ops'|'live'|'setup'|'pricing'|'billing'|'master-trade'
type Market = {symbol:string;display:string;price:number;change:number;volume:number}
type Candle = {time:number;open:number;high:number;low:number;close:number;volume:number}
type Point = {time:number;value:number}
type Analysis = {
  direction:'LONG'|'SHORT'|'BEKLE';confidence:number;entry:number;stop_loss:number;tp1:number;tp2:number;tp3:number;
  support:number;resistance:number;trend:string;momentum:string;rsi:number;adx:number;volume_ratio:number;explanation:string;
  series:{ema20:Point[];ema50:Point[];ema200:Point[]}
}
type Health = {status:string;version:string;mode:string;testnet:string;live_guard:string;paper:string;database:string;cloud_evidence:string;web_access:string}
type ConnectionStatus = {connections?:Record<'TESTNET'|'LIVE',{configured:boolean;active:boolean;last_test_ok:boolean;api_key_masked?:string;last_error?:string|null;storage?:string;account?:{active_positions?:number}|null}>;vault?:{ready:boolean;reason?:string|null}}
type NotificationItem = {id:string;type:string;severity:'success'|'warning'|'error'|'info';title:string;message:string;timestamp:string|null;read:boolean;target:string}
type NotificationResponse = {items:NotificationItem[];unread:number}
const ANALYSIS_TIMEOUT_MS = 30000

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
    if (!symbol) {
      setStream('HATA')
      onAnalysis(null)
      return
    }
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
    <div className="v26ChartStatus"><span className={stream === 'CANLI' ? 'live' : stream === 'HATA' ? 'error' : ''}><i/>{stream}</span></div>
    <div className="v26Chart" ref={host}/>
  </div>
}

export default function TestnetFirstApp() {
  const initialView = ():View => window.location.pathname === '/pricing' ? 'pricing' : window.location.pathname === '/billing' ? 'billing' : window.location.pathname === '/master-trade' ? 'master-trade' : 'dashboard'
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
  const [connectionOffline,setConnectionOffline] = useState(false)
  const [credentials,setCredentials] = useState({demoApiKey:'',demoSecretKey:'',liveApiKey:'',liveSecretKey:''})
  const [demoSaveState,setDemoSaveState] = useState<'idle'|'saving'|'saved'|'error'>('idle')
  const [demoVerifyState,setDemoVerifyState] = useState<'idle'|'verifying'|'verified'|'error'>('idle')
  const [demoVerification,setDemoVerification] = useState({kind:'info',message:''})
  const [connectionStatus,setConnectionStatus] = useState<ConnectionStatus|null>(null)
  const [notifications,setNotifications] = useState<NotificationItem[]>([])
  const [notificationsOpen,setNotificationsOpen] = useState(false)
  const [headerHidden,setHeaderHidden] = useState(false)
  const [complianceOpen,setComplianceOpen] = useState(false)
  const [complianceTab,setComplianceTab] = useState<'risk'|'privacy'|'terms'|'support'>('risk')
  const notificationRef = useRef<HTMLDivElement>(null)
  const marketPickerRef = useRef<HTMLDivElement>(null)
  const unreadNotifications = notifications.filter(item => !item.read).length
  const selectedMarket = markets.find(market => market.symbol === symbol)
  const pulseSymbols = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT']
  const pulseMarkets = pulseSymbols.map(pulseSymbol => markets.find(market => market.symbol.replace('/','').toUpperCase() === pulseSymbol))

  const navigate = (target:View) => {
    if (target === 'testnet' as View) target = 'dashboard'
    setView(target)
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
        const payload = await response.json() as { access?: {canAccessMasterTrade?: boolean} }
        if (!active) return
        setMasterTradeAccess(payload.access?.canAccessMasterTrade === true ? 'granted' : 'locked')
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
      const marketUnavailable = marketResult.status !== 'fulfilled' || !marketResult.value.ok
      const healthUnavailable = healthResult.status !== 'fulfilled' || !healthResult.value.ok
      setConnectionOffline(marketUnavailable && healthUnavailable)
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

  const refreshNotifications = async () => {
    try {
      const headers = new Headers(); const token = userSessionToken(); if (token) headers.set('Authorization',`Bearer ${token}`)
      const response = await fetch(`${API_BASE}/v21/notifications?limit=100`,{headers})
      if (!response.ok) return
      const payload = await response.json() as NotificationResponse
      if (Array.isArray(payload.items)) setNotifications(payload.items)
    } catch {}
  }

  const saveDemoCredentials = async () => {
    const apiKey = credentials.demoApiKey.trim()
    const secretKey = credentials.demoSecretKey.trim()
    if (!apiKey || !secretKey) {
      setDemoSaveState('error')
      setDemoVerification({kind:'error',message:'Demo API Key ve Secret Key gerekli.'})
      return
    }
    setDemoSaveState('saving')
    setDemoVerification({kind:'info',message:'Kaydediliyor… Demo anahtarları güvenli kasaya kaydediliyor.'})
    try {
      const headers = new Headers({'Content-Type':'application/json'}); const token = userSessionToken(); if (token) headers.set('Authorization',`Bearer ${token}`)
      const saveResponse = await fetch(`${API_BASE}/exchange-connections/save`,{
        method:'POST',headers,
        body:JSON.stringify(buildDemoSavePayload(apiKey, secretKey)),
      })
      await saveResponse.json().catch(() => null)
      if (!saveResponse.ok) throw new Error('Demo bağlantı bilgileri kaydedilemedi.')
      setCredentials(current => ({...current,demoApiKey:'',demoSecretKey:''}))
      setDemoSaveState('saved')
      setDemoVerification({kind:'ok',message:'Bağlantı bilgileri kaydedildi · Doğrulamaya hazır.'})
      await refreshConnectionStatus()
    } catch {
      setDemoSaveState('error')
      setDemoVerification({kind:'error',message:'Bağlantı bilgileri kaydedilemedi. Sunucu bağlantısını kontrol edin.'})
    }
  }

  const verifyDemoConnection = async () => {
    setDemoVerifyState('verifying')
    setDemoVerification({kind:'info',message:'Doğrulanıyor… Kayıtlı Demo bağlantı bilgileri kullanılıyor.'})
    try {
      const headers = new Headers({'Content-Type':'application/json'}); const token = userSessionToken(); if (token) headers.set('Authorization',`Bearer ${token}`)
      const testResponse = await fetch(`${API_BASE}/exchange-connections/test`,{method:'POST',headers,body:JSON.stringify({mode:'TESTNET'})})
      await testResponse.json().catch(() => null)
      if (!testResponse.ok) throw new Error('Demo bağlantısı doğrulanamadı.')
      const activateResponse = await fetch(`${API_BASE}/exchange-connections/activate`,{method:'POST',headers,body:JSON.stringify({mode:'TESTNET',confirmation:'TESTNET BAĞLANTIYI AÇ'})})
      await activateResponse.json().catch(() => null)
      if (!activateResponse.ok) throw new Error('Demo bağlantısı etkinleştirilemedi.')
      setDemoVerifyState('verified')
      setDemoVerification({kind:'ok',message:'DEMO BAĞLANDI · Bağlantı doğrulandı. İşlem kanalı: DEMO.'})
      await refreshConnectionStatus()
      await refresh()
    } catch {
      setDemoVerifyState('error')
      setDemoVerification({kind:'error',message:'Bağlantı doğrulanamadı. Sunucu bağlantısını kontrol edin.'})
      await refreshConnectionStatus()
    }
  }

  useEffect(() => {
    void refresh()
    void refreshConnectionStatus()
    void refreshNotifications()
    const timer = window.setInterval(() => void refresh(),60000)
    const notificationTimer = window.setInterval(() => void refreshNotifications(),10000)
    const openExchangeSettings = () => setView('setup')
    window.addEventListener('protrebot-open-exchange-settings', openExchangeSettings)
    return () => {window.clearInterval(timer);window.clearInterval(notificationTimer);window.removeEventListener('protrebot-open-exchange-settings',openExchangeSettings)}
  },[])

  const notificationTarget = (target:string):{view:View;selector:string} => ({
    'system-health': {view:'dashboard',selector:'#dashboard-title'},
    'execution-status': {view:'trading',selector:'.v26MarketBar'},
    'demo-trading': {view:'trading',selector:'.binanceDemoDeck'},
    'risk-management': {view:'risk',selector:'.binanceDemoDeck'},
    'trade-history': {view:'performance',selector:'.v21PerformanceCenter'},
    subscription: {view:'billing',selector:'.subscriptionCenter'},
    settings: {view:'setup',selector:'.connectionCenter'},
  }[target] || {view:'trading',selector:'.v26MarketBar'})

  const openNotification = async (item:NotificationItem) => {
    setNotifications(current => current.map(entry => entry.id === item.id ? {...entry,read:true} : entry))
    setNotificationsOpen(false)
    try { const headers = new Headers(); const token = userSessionToken(); if (token) headers.set('Authorization',`Bearer ${token}`); await fetch(`${API_BASE}/v21/notifications/${encodeURIComponent(item.id)}/read`,{method:'POST',headers}) } catch {}
    const destination = notificationTarget(item.target)
    navigate(destination.view)
    window.setTimeout(() => {
      const target = document.querySelector(destination.selector)
      if (!target) return
      target.scrollIntoView({behavior:'smooth',block:'start'})
      target.classList.add('notificationTargetHighlight')
      window.setTimeout(() => target.classList.remove('notificationTargetHighlight'),1600)
    },120)
  }

  const markAllNotificationsRead = async () => {
    setNotifications(current => current.map(item => ({...item,read:true})))
    try { const headers = new Headers(); const token = userSessionToken(); if (token) headers.set('Authorization',`Bearer ${token}`); await fetch(`${API_BASE}/v21/notifications/read-all`,{method:'POST',headers}) } catch {}
  }

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
    if (view !== 'setup') setComplianceOpen(false)
    if (view !== 'trading') setMarketPickerOpen(false)
  },[view])

  useEffect(() => {
    let previousY = window.scrollY
    let ticking = false
    const updateScrollState = () => {
      const currentY = window.scrollY
      const delta = currentY - previousY
      if (currentY <= 12) setHeaderHidden(false)
      else if (Math.abs(delta) >= 8) setHeaderHidden(delta > 0)
      previousY = currentY
      ticking = false
    }
    const onScroll = () => {
      if (!ticking) {ticking=true;window.requestAnimationFrame(updateScrollState)}
    }
    window.addEventListener('scroll',onScroll,{passive:true})
    return () => window.removeEventListener('scroll',onScroll)
  },[])

  return <main className={`v26App ${view === 'dashboard' ? 'homeRoute' : 'workspaceRoute'} ${view === 'master-trade' ? 'masterTradeRoute' : ''}${connectionOffline ? ' backendOffline' : ''}`}>
    {connectionOffline && <section className="v26OfflineNotice" role="status" aria-live="polite"><span><i/><b>Sunucu bağlantısı bekleniyor</b><small>Veriler güncellenemiyor. Bağlantı kurulduğunda otomatik olarak yeniden denenecek.</small></span><button type="button" onClick={() => void refresh()} disabled={loading}>{loading ? 'KONTROL EDİLİYOR…' : 'YENİDEN DENE'}</button></section>}
    {view === 'dashboard' ? <header className={`v26Header v26HomeHeader ${headerHidden ? 'v26HeaderHidden' : ''}`} data-build-commit={BUILD_COMMIT}>
      <div className="v26Brand"><span>X</span><div><b>PROTREBOT ELITE X</b><small>TESTNET ÖNCELİKLİ İŞLEM PLATFORMU</small></div></div>
      <div className="v26HomeHeaderStatus" aria-label="Sistem durumu"><i className={connectionOffline ? 'pending' : health?.status === 'ok' ? 'ok' : 'pending'}/>{connectionOffline ? 'BAĞLANTI BEKLENİYOR' : 'ÇEVRİMİÇİ'}</div>
      <div className="v26HeaderActions">
        <button className="v26Refresh" aria-label="Piyasa verisini yenile" title="Piyasa verisini yenile" onClick={refresh} disabled={loading}><RefreshCw className={loading ? 'spin' : ''}/></button>
        <div className="v26Notifications" ref={notificationRef}>
          <button className={`v26NotificationButton${unreadNotifications ? ' hasUnread' : ''}`} type="button" aria-label={`Bildirimler${unreadNotifications ? `, ${unreadNotifications} okunmamış` : ''}`} aria-expanded={notificationsOpen} onClick={() => setNotificationsOpen(open => !open)}><Bell/>{unreadNotifications > 0 && <span className="v26NotificationBadge">{unreadNotifications > 99 ? '99+' : unreadNotifications}</span>}</button>
          {notificationsOpen && <section className="v26NotificationPanel" role="dialog" aria-label="Bildirimler">
            <header><div><small>DURUM MERKEZİ</small><h2>Bildirimler</h2></div><div><span>{unreadNotifications}</span>{unreadNotifications > 0 && <button type="button" onClick={() => void markAllNotificationsRead()}>TÜMÜ OKUNDU</button>}</div></header>
            {notifications.length ? <div className="v26NotificationList">{notifications.map(item => <button type="button" key={item.id} className={`v26NotificationItem ${item.severity}${item.read ? ' isRead' : ''}`} onClick={() => void openNotification(item)}><i><Bell/></i><span><b>{item.title}</b><p>{item.message}</p><small>{item.timestamp ? new Date(item.timestamp).toLocaleString('tr-TR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—'}</small></span></button>)}</div> : <div className="v26NotificationEmpty"><Bell/><b>Bildirim yok</b><p>Gerçek bir sistem olayı oluştuğunda burada görünecek.</p></div>}
          </section>}
        </div>
      </div>
    </header> : <button type="button" className="workspaceBack" onClick={() => setView('dashboard')}><ArrowLeft/> <span>Home</span></button>}

    {view !== 'dashboard' && <section className="v26ModeBar">
      <div><small>WORKSPACE</small><h1>{view === 'trading' ? 'İşlem Masası' : view === 'risk' ? 'Risk Kasası' : view === 'analyst' ? 'Analyst' : view === 'scanner' ? 'Scanner' : view === 'performance' ? 'Performance' : view === 'ops' ? 'Bulut Operasyon ve Kanıt Merkezi' : view === 'live' ? 'Gerçek Futures Hazırlık Merkezi' : view === 'pricing' ? 'Plans & Pricing' : view === 'billing' ? 'Billing & Subscription' : view === 'master-trade' ? 'Master Trade' : 'Sunucu ve Anahtar Kapıları'}</h1><p>{view === 'trading' ? 'Market, sinyal, grafik ve testnet işlem yönetimi.' : view === 'risk' ? 'Risk limiti, pozisyon boyutu ve koruma ayarları.' : view === 'analyst' ? 'Scanner snapshot üzerinden market intelligence ve sinyal analizi.' : view === 'scanner' ? 'Piyasadaki uygun adayları ve sinyalleri tara.' : view === 'performance' ? 'İşlem sonuçlarını, PnL ve risk ölçümlerini incele.' : view === 'ops' ? 'Otonom taramanın son kararı, pozisyonlar ve yeniden başlatmaya dayanıklı PostgreSQL kanıt defteri.' : view === 'live' ? 'Şifreli canlı kasa kaydı ve tüm risk kapıları tamamlanana kadar emir gönderimi fail-closed olarak kilitli.' : view === 'pricing' || view === 'billing' ? 'Choose a subscription level for your trading intelligence workspace.' : 'Anahtar değerleri tarayıcıya veya GitHub’a yazılmaz; yalnızca sunucu tarafındaki şifreli kasa veya güvenli geçiş değişkenlerinde tutulur.'}</p></div>
    </section>}

    {view === 'dashboard' && <section className="v26Dashboard" aria-labelledby="dashboard-title">
      <header className="v26DashboardHero"><div><small>PROTREBOT ELITE X</small><h1 id="dashboard-title">İşlem Terminali</h1><p>Devam etmek için bir çalışma alanı seçin.</p></div></header>
      <section className="v26DashboardWorkspaces" aria-labelledby="workspace-title">
        <div><h2 id="workspace-title">NE YAPMAK İSTİYORSUNUZ?</h2><p>Bir çalışma alanı seçin</p></div>
        <div className="v26DashboardChoices">
          <button type="button" onClick={() => setView('trading')}><Activity/><span><b>İŞLEM</b><small>İşlem açın ve yönetin</small></span><em>→</em></button>
          <button type="button" onClick={() => navigate('master-trade')}><ShieldCheck/><span><b>MASTER TRADE</b><small>Profesyonel işlem alanı</small></span><em>→</em></button>
          <button type="button" onClick={() => setView('analyst')}><BarChart3/><span><b>ANALİST</b><small>Piyasa zekâsı ve sinyaller</small></span><em>→</em></button>
          <button type="button" onClick={() => setView('scanner')}><Radar/><span><b>TARAMA</b><small>Piyasa fırsatlarını tarayın</small></span><em>→</em></button>
          <button type="button" onClick={() => setView('performance')}><BarChart3/><span><b>PERFORMANS</b><small>Kâr, kazanma oranı ve düşüş</small></span><em>→</em></button>
          <button type="button" onClick={() => setView('live')}><RadioTower/><span><b>CANLI</b><small>Canlı işlem hazırlığı</small></span><em>→</em></button>
          <button type="button" onClick={() => setView('ops')}><Cloud/><span><b>OPERASYON</b><small>Sistem operasyonları</small></span><em>→</em></button>
          <button type="button" onClick={() => setView('setup')}><CloudCog/><span><b>AYARLAR</b><small>Hesap ve güvenlik</small></span><em>→</em></button>
          <button type="button" onClick={() => navigate('billing')}><Sparkles/><span><b>ABONELİK</b><small>Planlar ve abonelik</small></span><em>→</em></button>
          <button type="button" onClick={() => setView('risk')}><ShieldCheck/><span><b>RİSK</b><small>Pozisyon ve risk kontrolü</small></span><em>→</em></button>
        </div>
      </section>
      <section className="v26DashboardPulse" aria-labelledby="market-pulse-title">
          <header><div><h2 id="market-pulse-title">Piyasa Nabzı</h2><p>{markets.length ? 'Canlı piyasa özeti' : 'Piyasa verisi bekleniyor'}</p></div><small>{markets.length ? 'CANLI VERİ' : 'VERİ BEKLENİYOR'}</small></header>
        <div className="v26DashboardPulseItems">
          {pulseSymbols.map((pulseSymbol,index) => {
            const market = pulseMarkets[index]
            const hasPrice = typeof market?.price === 'number' && Number.isFinite(market.price)
            const hasChange = typeof market?.change === 'number' && Number.isFinite(market.change)
            return <button type="button" key={pulseSymbol} onClick={() => {setSymbol(pulseSymbol);setView('trading')}}><span><b>{pulseSymbol.replace('USDT','/USDT')}</b><small>{hasPrice ? `$${format(market.price)}` : '$—'}</small></span><em className={hasChange ? market.change >= 0 ? 'up' : 'down' : ''}>{hasChange ? `${market.change >= 0 ? '+' : ''}${format(market.change)}%` : '—%'}</em></button>
          })}
        </div>
      </section>
    </section>}

    {view === 'trading' && <>
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
        {marketError && <div className="v26MarketError" role="alert"><span>Market verisi yüklenemedi.</span><button className="action-button" type="button" aria-label="Market verisini yeniden dene" title="Market verisini yeniden dene" onClick={() => void refresh()} disabled={loading}>{loading ? <RefreshCw className="spin"/> : 'TEKRAR DENE'}</button></div>}
      </section>
      <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Testnet merkezi hazırlanıyor…</div>}>
        <BinanceDemo active symbol={symbol} markets={markets} onSymbolChange={setSymbol} analysis={analysis} workspace="trade" chart={<TestnetMarketChart symbol={symbol} interval={interval} onAnalysis={setAnalysis} onAnalysisProgress={setAnalysisProgress}/>}/>
      </Suspense>
    </>}

    {view === 'risk' && <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Risk Kasası hazırlanıyor…</div>}><BinanceDemo active symbol={symbol} markets={markets} onSymbolChange={setSymbol} analysis={analysis} workspace="risk"/></Suspense>}

    {view === 'scanner' && <ScannerCenter
      active markets={markets} symbol={symbol} onSymbolChange={setSymbol} analysis={analysis}
      interval={interval} onIntervalChange={setInterval}
      chart={<TestnetMarketChart symbol={symbol} interval={interval} onAnalysis={setAnalysis} showLevels={false}/>} />}

    {view === 'performance' && <Suspense fallback={<div className="v26Loading"><RefreshCw className="spin"/>Performance hazırlanıyor…</div>}><BinanceDemo active symbol={symbol} markets={markets} onSymbolChange={setSymbol} analysis={analysis} initialTab="performance" workspace="trade"/></Suspense>}

    {view === 'analyst' && <CoinAnalysisCenter interval={interval} onIntervalChange={setInterval} chart={(selectedSymbol,selectedInterval,showLevels,showEma) => <TestnetMarketChart symbol={selectedSymbol} interval={selectedInterval} showLevels={showLevels} showEma={showEma} onAnalysis={() => undefined}/>}/>}

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
      <section className="connectionDemoPanel"><header><div><span>DEMO / TESTNET</span><h3>Binance Futures Demo</h3><p>Demo API anahtarları şifreli sunucu kasasına kaydedilir. Bu kanal gerçek para ve Live emir kanalı değildir.</p></div><strong className="connectionChannelBadge"><TestTube2/> DEMO ONLY</strong></header><div className="connectionFormGrid"><label><span>Demo API Key</span><input type="text" value={credentials.demoApiKey} onChange={event => setCredentials(current => ({...current,demoApiKey:event.target.value}))} autoComplete="off" spellCheck={false} placeholder="Enter Demo API Key"/></label><label><span>Demo Secret Key</span><input type="password" value={credentials.demoSecretKey} onChange={event => setCredentials(current => ({...current,demoSecretKey:event.target.value}))} autoComplete="new-password" spellCheck={false} placeholder="Enter Demo Secret Key"/></label></div>{connectionStatus?.connections?.TESTNET?.configured && <div className="connectionCredentialState"><span><small>SAVED API KEY</small><b>{connectionStatus.connections.TESTNET.api_key_masked || 'MASKED KEY'}</b></span><strong><i className={connectionStatus.connections.TESTNET.active ? 'ok' : 'pending'}/>{connectionStatus.connections.TESTNET.active ? 'SAVED / READY' : 'SAVED / VERIFY REQUIRED'}</strong></div>}<div className="connectionActions"><button type="button" className="connectionPrimary" onClick={() => void saveDemoCredentials()} disabled={demoSaveState === 'saving' || demoVerifyState === 'verifying' || !credentials.demoApiKey.trim() || !credentials.demoSecretKey.trim()}><Save/>{demoSaveState === 'saving' ? 'SAVING…' : 'SAVE SECURELY'}</button><button type="button" className="connectionSecondary" onClick={() => void verifyDemoConnection()} disabled={demoSaveState === 'saving' || demoVerifyState === 'verifying' || !connectionStatus?.connections?.TESTNET?.configured}><ShieldCheck/>{demoVerifyState === 'verifying' ? 'VERIFYING…' : 'VERIFY DEMO CONNECTION'}</button></div>{demoVerification.message && <div className={`connectionFeedback ${demoVerification.kind}`}><i/>{demoVerification.message}</div>}<small className="connectionNote">Secrets are sent only to the existing vault API and are never returned to the browser.</small></section>
      <section className="connectionLivePanel"><header><div><span>REAL BINANCE FUTURES</span><h3><LockKeyhole/> Live Trading Locked</h3><p>Live credentials are managed separately. Live trading remains locked until every existing V25 safety condition is satisfied.</p></div><strong className="connectionLiveBadge"><i className="locked"/>{health?.live_guard || 'LIVE LOCKED'}</strong></header><div className="connectionLiveGrid"><label><span>Live API Key</span><input type="text" value={credentials.liveApiKey} onChange={event => setCredentials(current => ({...current,liveApiKey:event.target.value}))} autoComplete="off" spellCheck={false} placeholder="Configured separately"/></label><label><span>Live Secret Key</span><input type="password" value={credentials.liveSecretKey} onChange={event => setCredentials(current => ({...current,liveSecretKey:event.target.value}))} autoComplete="new-password" placeholder="Never displayed"/></label></div><small>Live connection is not tested, activated, or armed from this page.</small></section>
      <section className="connectionSecurityPanel"><header><div><span>SECURITY &amp; SAFETY</span><h3>Fail-closed by design</h3></div><ShieldCheck/></header><div>{['Secrets are stored server-side','Secrets are never displayed in the UI','Live trading remains locked by default','Demo and Live credentials are separated','Orders require existing safety gates','No automatic live orders on startup'].map(item => <span key={item}><CheckCircle2/>{item}</span>)}</div></section>
    </section>}

    {view === 'dashboard' && <nav className={`terminalMobileNav ${headerHidden ? 'terminalMobileNavHidden' : ''}`} aria-label="Mobil ana navigasyon"><button className="active" onClick={() => setView('dashboard')}><TestTube2/><span>Home</span></button></nav>}
    {view === 'setup' && <section className="v26TrustStrip" style={{margin:'0 1rem 1rem',padding:'1rem 1.25rem',border:'1px solid rgba(148,163,184,0.18)',borderRadius:'16px',background:'rgba(15,23,42,0.82)',display:'grid',gap:'0.7rem'}}>
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
    </section>}
    {view === 'dashboard' && <footer className="v26Footer"><span><i className={health?.status === 'ok' ? 'ok' : health ? 'error' : 'pending'}/>{health?.status === 'ok' ? 'API CONNECTED' : health ? 'API DISCONNECTED' : 'API CHECKING'}</span><span><i className={health?.status === 'ok' ? 'ok' : health ? 'error' : 'pending'}/>{health?.status === 'ok' ? 'SYSTEM ONLINE' : health ? 'SYSTEM OFFLINE' : 'SYSTEM CHECKING'}</span><span>TESTNET FIRST</span></footer>}
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
