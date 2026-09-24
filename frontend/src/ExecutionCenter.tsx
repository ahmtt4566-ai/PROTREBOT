import { useEffect, useMemo, useRef, useState } from 'react'
import { CandlestickSeries, ColorType, createChart, HistogramSeries } from 'lightweight-charts'
import { Activity, AlertTriangle, Bot, CheckCircle2, CircleDollarSign, KeyRound, LockKeyhole, Play, Power, RefreshCw, Save, Send, ShieldCheck, Square, TestTube2, TrendingUp, WalletCards, XCircle } from 'lucide-react'
import { API_BASE } from './api'

const API = `${API_BASE}/v25`

type Gate = {key:string;label:string;passed:boolean;detail:string}
type Policy = {
  allowed_symbols:string[];interval:string;allow_long:boolean;allow_short:boolean;max_margin_per_trade:number;max_loss_per_trade:number;max_leverage:number;max_positions:number;daily_loss_limit:number;daily_trade_limit:number;min_confidence:number;max_trap_score:number;max_spread_bps:number;max_stop_distance_pct:number;fee_bps_per_side:number;slippage_bps_per_side:number;minimum_net_reward_usdt:number;scan_seconds:number
}
type Position = {symbol:string;direction:string;quantity:number;entry_price:number;mark_price:number;unrealized_pnl:number;leverage?:number;margin_type?:string;liquidation_price?:number}
type Plan = {id:string;symbol:string;direction:string;status:string;entry_price:string|number;stop_loss:string|number;targets:(string|number)[];margin_usdt:number;leverage:number;created_at:string;source?:string;pnl_verified?:boolean}
type Event = {id:string;kind:string;message:string;created_at:string;symbol?:string}
type Candle = {time:number;open:number;high:number;low:number;close:number;volume:number}
type CandleResponse = {symbol:string;interval:string;candles:Candle[];updated_at:string;orders_created:false}
type Status = {
  version:string;host:string;credentials:{configured:boolean;fingerprint:string|null;storage:string};consent:{active:boolean;accepted_at:string|null;expires_at:string|null;fingerprint:string|null;storage?:string};connected:boolean;connection:{last_checked:string|null;last_error:string|null;clock_offset_ms:number|null};stream:{status:string;transport:string;last_event:string|null;last_error:string|null;event_count:number;reconnect_count:number};armed:boolean;armed_until:string|null;auto_session_until:string|null;auto:{enabled:boolean;busy:boolean;cycles:number;last_scan:string|null;last_decision:string;last_error:string|null};policy:Policy;policy_digest:string;policy_acknowledged:boolean;readiness:{ready:boolean;score:number;gates:Gate[];demo_certificate:{status?:string;score?:number;live_allowed?:boolean;live_allowance_status?:string;gates?:{name:string;passed:boolean;value:string|number;target:string|number}[]}};account:{wallet_balance:number|null;available_balance:number|null;unrealized_pnl:number|null;positions:Position[];open_orders:unknown[];open_algo_orders:unknown[];hedge_mode:boolean|null};daily:{entries:number;realized_pnl:number;unverified_closures:number};plans:Plan[];events:Event[];emergency:{active:boolean;triggered_at:string|null;reason:string|null};profit_guaranteed:boolean
}
type LiveVaultConnection = {configured:boolean;active:boolean;fingerprint:string|null;last_test_ok:boolean;last_test_at:string|null;last_error:string|null;storage:string}
type LiveVaultStatus = {vault:{ready:boolean;reason:string|null};connections:{LIVE:LiveVaultConnection}}

type OrderForm = {symbol:string;direction:'LONG'|'SHORT';order_type:'MARKET'|'LIMIT';limit_price:string;margin_usdt:string;leverage:string;stop_loss:string;tp1:string;tp2:string;tp3:string}

const money = (value:number|null|undefined) => value == null ? '—' : value.toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:4})
const date = (value:string|null|undefined) => value ? new Date(value).toLocaleString('tr-TR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—'

function errorText(value:unknown):string {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(item => typeof item === 'object' && item && 'msg' in item ? String(item.msg) : String(item)).join(' · ')
  return 'İşlem tamamlanamadı; güvenlik kapılarını ve bağlantıyı kontrol edin.'
}

function LivePositionChart({token,symbol,interval,plan}:{token?:string;symbol:string;interval:string;plan:Plan|null}) {
  const ref = useRef<HTMLDivElement>(null)
  const [message,setMessage] = useState('Canlı mumlar hazırlanıyor…')
  const [updated,setUpdated] = useState<string|null>(null)
  const planKey = plan ? [plan.id,plan.entry_price,plan.stop_loss,...plan.targets].join('|') : 'none'

  useEffect(() => {
    if (!ref.current || !symbol) return
    let active = true
    const chart = createChart(ref.current,{
      autoSize:true,
      layout:{background:{type:ColorType.Solid,color:'#fffef7'},textColor:'#53664e'},
      grid:{vertLines:{color:'#edf1e4'},horzLines:{color:'#edf1e4'}},
      rightPriceScale:{borderColor:'#dce5ce'},
      timeScale:{borderColor:'#dce5ce',timeVisible:true,secondsVisible:false},
      crosshair:{vertLine:{color:'#8eac73'},horzLine:{color:'#8eac73'}},
    })
    const candleSeries = chart.addSeries(CandlestickSeries,{upColor:'#0dac5b',downColor:'#ef5646',wickUpColor:'#0dac5b',wickDownColor:'#ef5646',borderVisible:false})
    const volumeSeries = chart.addSeries(HistogramSeries,{priceFormat:{type:'volume'},priceScaleId:''})
    volumeSeries.priceScale().applyOptions({scaleMargins:{top:.82,bottom:0}})

    if (plan) {
      const entry = Number(plan.entry_price)
      const stop = Number(plan.stop_loss)
      const targets = plan.targets.map(Number).filter(Number.isFinite)
      if (Number.isFinite(entry)) candleSeries.createPriceLine({price:entry,color:'#087d46',lineWidth:3,lineStyle:0,axisLabelVisible:true,title:`${plan.direction} GİRİŞ`})
      if (Number.isFinite(stop)) candleSeries.createPriceLine({price:stop,color:'#ef4f3f',lineWidth:3,lineStyle:0,axisLabelVisible:true,title:'STOP'})
      targets.forEach((price,index) => candleSeries.createPriceLine({price,color:'#18a95a',lineWidth:2,lineStyle:2,axisLabelVisible:true,title:`TP${index + 1}`}))
      const bounds = [entry,stop,...targets].filter(Number.isFinite)
      if (bounds.length >= 2) {
        const low = Math.min(...bounds)
        const high = Math.max(...bounds)
        const step = (high - low) / 8
        for (let index=1;index<8;index+=1) {
          const price = low + step * index
          if ([entry,stop,...targets].some(value => Math.abs(value - price) < Math.max(step * .08,1e-8))) continue
          candleSeries.createPriceLine({price,color:'rgba(213,171,16,.48)',lineWidth:1,lineStyle:2,axisLabelVisible:false,title:`K${index}`})
        }
      }
    }

    const load = async () => {
      try {
        const headers = new Headers()
        if (token) headers.set('Authorization',`Bearer ${token}`)
        const response = await fetch(`${API}/market/candles?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=360`,{headers})
        const payload = await response.json().catch(() => null) as CandleResponse|{detail?:unknown}|null
        if (!response.ok) throw new Error(errorText(payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : payload))
        if (!active || !payload || !('candles' in payload)) return
        candleSeries.setData(payload.candles.map(item => ({time:item.time as never,open:item.open,high:item.high,low:item.low,close:item.close})))
        volumeSeries.setData(payload.candles.map(item => ({time:item.time as never,value:item.volume,color:item.close >= item.open ? 'rgba(24,169,90,.34)' : 'rgba(239,86,70,.30)'})))
        chart.timeScale().fitContent()
        setUpdated(payload.updated_at)
        setMessage(`${payload.symbol} · ${payload.interval} canlı Futures mumları`)
      } catch (error) {
        if (active) setMessage(error instanceof Error ? error.message : 'Canlı grafik yüklenemedi.')
      }
    }
    void load()
    const timer = window.setInterval(() => void load(),10000)
    return () => {active=false;window.clearInterval(timer);chart.remove()}
  },[token,symbol,interval,planKey])

  return <section className="executionCard liveChartCard">
    <div className="executionHead"><div><small>POZİSYON + BORSADAKİ KORUMA HARİTASI</small><h3>{symbol} Canlı Seviye Grafiği</h3></div><TrendingUp/></div>
    <div className="liveChartMeta"><span className={plan ? 'active' : ''}>{plan ? `${plan.direction} · ${plan.status}` : 'Açık tracked plan seçilmedi'}</span><span>Güncelleme {date(updated)}</span><em>⚠ Sarı kademeler görsel rehberdir; borsa grid emri değildir.</em></div>
    <div className="liveExecutionChart" ref={ref}/>
    <p>{message}</p>
  </section>
}

export default function ExecutionCenter({token=''}:{token?:string}) {
  const [status,setStatus] = useState<Status|null>(null)
  const [policy,setPolicy] = useState<Policy|null>(null)
  const [busy,setBusy] = useState('')
  const [notice,setNotice] = useState('V25 Live Guard güvenlik durumu yükleniyor…')
  const [noticeKind,setNoticeKind] = useState<'ok'|'warn'|'error'>('warn')
  const [test,setTest] = useState<OrderForm>({symbol:'BTCUSDT',direction:'LONG',order_type:'MARKET',limit_price:'',margin_usdt:'10',leverage:'1',stop_loss:'',tp1:'',tp2:'',tp3:''})
  const [chartSymbol,setChartSymbol] = useState('BTCUSDT')
  const [chartPlanId,setChartPlanId] = useState<string|null>(null)
  const [liveVault,setLiveVault] = useState<LiveVaultStatus|null>(null)
  const [liveCredentials,setLiveCredentials] = useState({apiKey:'',secretKey:''})
  const [liveVaultBusy,setLiveVaultBusy] = useState('')

  const call = async <T,>(path:string,options:RequestInit={}):Promise<T> => {
    const headers = new Headers(options.headers)
    if (token) headers.set('Authorization',`Bearer ${token}`)
    if (options.body) headers.set('Content-Type','application/json')
    const response = await fetch(`${API}${path}`,{...options,headers})
    let payload:unknown = null
    try { payload = await response.json() } catch { payload = null }
    if (!response.ok) {
      const detail = payload && typeof payload === 'object' && 'detail' in payload ? (payload as {detail:unknown}).detail : payload
      throw new Error(errorText(detail))
    }
    return payload as T
  }

  const refresh = async (quiet=false) => {
    try {
      const next = await call<Status>('/status')
      setStatus(next)
      setPolicy(current => current ?? next.policy)
      if (!quiet) {setNotice('Canlı kasa, hesap ve yayın kapıları yenilendi.');setNoticeKind('ok')}
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'V25 Live Guard API bağlantısı kurulamadı.');setNoticeKind('error')
    }
  }

  const refreshLiveVault = async () => {
    try {
      const response = await fetch(`${API_BASE}/exchange-connections/status`)
      if (response.ok) setLiveVault(await response.json() as LiveVaultStatus)
    } catch { /* Keep the Live Guard fail-closed when the vault is unavailable. */ }
  }

  useEffect(() => {
    void refresh(true)
    void refreshLiveVault()
    const timer = window.setInterval(() => void refresh(true),5000)
    const vaultTimer = window.setInterval(() => void refreshLiveVault(),5000)
    return () => {window.clearInterval(timer);window.clearInterval(vaultTimer)}
  },[token])

  const run = async (key:string,path:string,body?:unknown,success='İşlem tamamlandı.') => {
    setBusy(key)
    try {
      const next = await call<Status>(path,{method:'POST',body:body === undefined ? undefined : JSON.stringify(body)})
      setStatus(next);setPolicy(next.policy);setNotice(success);setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'İşlem tamamlanamadı.');setNoticeKind('error')}
    finally {setBusy('')}
  }

  const connect = () => run('connect','/connect/read-only',undefined,'Canlı hesap salt-okunur bağlandı; emir gönderilmedi.')
  const grantConsent = () => {
    const phrase = window.prompt('24 saatlik canlı risk izni için aynen yazın: CANLI İŞLEM RİSKİNİ 24 SAAT KABUL EDİYORUM')
    if (!phrase) return
    void run('consent','/consent',{confirmation:phrase},'24 saatlik canlı izin verildi. Sunucu yeniden başlarsa otomatik iptal olur.')
  }
  const acknowledge = () => {
    const phrase = window.prompt('Mevcut limitleri onaylamak için aynen yazın: RİSK LİMİTLERİNİ ONAYLIYORUM')
    if (!phrase) return
    void run('ack','/policy/acknowledge',{confirmation:phrase},'Risk limitleri onaylandı. Limit değişirse onay sıfırlanır.')
  }
  const arm = () => {
    const phrase = window.prompt('24 saatlik canlı emir kilidini açmak için aynen yazın: CANLI EMİR RİSKİNİ KABUL EDİYORUM')
    if (!phrase) return
    void run('arm','/arm',{confirmation:phrase},'Canlı yeni giriş kilidi 24 saat için açıldı.')
  }
  const startAuto = () => {
    const phrase = window.prompt('Bir saatlik canlı otomasyonu başlatmak için aynen yazın: CANLI OTOMATİK')
    if (!phrase) return
    void run('auto','/auto/start',{confirmation:phrase},'Bir saatlik kontrollü canlı tarama başladı; her giriş bütün risk kapılarından geçer.')
  }
  const emergency = () => {
    const phrase = window.prompt('Yalnızca ProTreBot emirlerini iptal edip tracked pozisyonları kapatmak için aynen yazın: CANLI ACİL DURDUR')
    if (!phrase) return
    void run('emergency','/emergency',{confirmation:phrase,close_tracked_positions:true},'Canlı acil durdurma komutu işlendi.')
  }

  const savePolicy = async () => {
    if (!policy) return
    setBusy('policy')
    try {
      const next = await call<Status>('/policy',{method:'PUT',body:JSON.stringify(policy)})
      setStatus(next);setPolicy(next.policy);setNotice('Limitler kaydedildi; güvenlik gereği risk onayı ve canlı kilit sıfırlandı.');setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Limitler kaydedilemedi.');setNoticeKind('error')}
    finally {setBusy('')}
  }

  const orderPayload = (intentPrefix:string) => ({
    symbol:test.symbol,
    direction:test.direction,
    order_type:test.order_type,
    limit_price:test.order_type === 'LIMIT' ? Number(test.limit_price) : null,
    margin_usdt:Number(test.margin_usdt),
    leverage:Number(test.leverage),
    stop_loss:Number(test.stop_loss),
    tp1:Number(test.tp1),
    tp2:Number(test.tp2),
    tp3:Number(test.tp3),
    intent_id:`${intentPrefix}-${Date.now()}`,
  })

  const validOrderForm = () => {
    if (!test.stop_loss || !test.tp1 || !test.tp2 || !test.tp3 || (test.order_type === 'LIMIT' && !test.limit_price)) {
      setNotice('Emir için giriş türü, Stop ve TP1–TP3 alanlarını eksiksiz doldurun.');setNoticeKind('warn');return false
    }
    return true
  }

  const orderTest = async () => {
    if (!validOrderForm()) return
    setBusy('test')
    try {
      await call('/order/test',{method:'POST',body:JSON.stringify(orderPayload('ui-test'))})
      setNotice('Binance canlı /order/test başarılı: imza ve emir şeması doğrulandı, gerçek emir oluşmadı.');setNoticeKind('ok');await refresh(true)
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Emir testi başarısız.');setNoticeKind('error')}
    finally {setBusy('')}
  }

  const verifyLiveCredentials = async () => {
    if (!liveCredentials.apiKey.trim() || !liveCredentials.secretKey.trim()) {setNotice('Live API Key ve API Secret birlikte girilmelidir.');setNoticeKind('warn');return}
    setLiveVaultBusy('verify')
    try {
      const response = await fetch(`${API_BASE}/exchange-connections/test`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'LIVE',['api' + '_key']:liveCredentials.apiKey,['secret' + '_key']:liveCredentials.secretKey})})
      const payload = await response.json().catch(() => null) as {detail?:unknown}|null
      if (!response.ok) throw new Error(errorText(payload?.detail))
      setNotice('Live bağlantı ve imza doğrulandı; hiçbir emir oluşturulmadı.');setNoticeKind('ok');await refreshLiveVault()
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Live bağlantı doğrulanamadı.');setNoticeKind('error')}
    finally {setLiveVaultBusy('')}
  }

  const saveLiveCredentials = async () => {
    if (!liveCredentials.apiKey.trim() || !liveCredentials.secretKey.trim()) {setNotice('Live API Key ve API Secret birlikte girilmelidir.');setNoticeKind('warn');return}
    setLiveVaultBusy('save')
    try {
      const response = await fetch(`${API_BASE}/exchange-connections/save`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'LIVE',['api' + '_key']:liveCredentials.apiKey,['secret' + '_key']:liveCredentials.secretKey,confirmation:'CANLI KASAYA KAYDET'})})
      const payload = await response.json().catch(() => null) as {detail?:unknown;message?:string}|null
      if (!response.ok) throw new Error(errorText(payload?.detail))
      setLiveCredentials({apiKey:'',secretKey:''});setNotice(payload?.message || 'Live credentials şifreli kasaya kaydedildi ve doğrulandı.');setNoticeKind('ok');await refreshLiveVault();await refresh(true)
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Live credentials kaydedilemedi.');setNoticeKind('error')}
    finally {setLiveVaultBusy('')}
  }

  const submitLiveOrder = async () => {
    if (!status?.credentials.configured) {
      setNotice('Live trading için API credentials gerekli.')
      setNoticeKind('error')
      return
    }
    if (!validOrderForm()) return
    const payload = orderPayload('ui-live')
    const details = [
      `Parite: ${payload.symbol}`,
      `Yön: ${payload.direction}`,
      `Emir tipi: ${payload.order_type}`,
      `Marjin: ${money(Number(payload.margin_usdt))} USDT`,
      `Kaldıraç: ${payload.leverage}x`,
      `Stop: ${money(Number(payload.stop_loss))} USDT`,
      `TP1: ${money(Number(payload.tp1))} USDT`,
      `TP2: ${money(Number(payload.tp2))} USDT`,
      `TP3: ${money(Number(payload.tp3))} USDT`,
      `Limit fiyatı: ${payload.order_type === 'LIMIT' ? money(Number(payload.limit_price)) : 'MARKET'}`,
    ].join('\n')
    const phrase = window.prompt(`Bu işlem GERÇEK PARA kullanabilir.\n\nCanlı emir detayları:\n${details}\n\nGöndermek için aynen yazın: CANLI EMİR GÖNDER`)
    if (!phrase || phrase.trim() !== 'CANLI EMİR GÖNDER') return
    setBusy('live-order')
    try {
      const result = await call<{ok:boolean;plan:Plan}>('/order',{method:'POST',body:JSON.stringify({...payload,confirmation:phrase.trim()})})
      setChartSymbol(result.plan.symbol);setChartPlanId(result.plan.id)
      setNotice(`${result.plan.symbol} ${result.plan.direction} canlı emir kabul edildi; Stop/TP koruması denetleniyor.`);setNoticeKind('ok')
      await refresh(true)
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Canlı emir gönderilmedi.');setNoticeKind('error')}
    finally {setBusy('')}
  }

  const closePlan = (plan:Plan) => {
    const phrase = window.prompt(`${plan.symbol} tracked pozisyonunu reduce-only kapatmak için aynen yazın: CANLI POZİSYONU KAPAT`)
    if (!phrase) return
    void run(`close-${plan.id}`,'/position/close',{plan_id:plan.id,confirmation:phrase},`${plan.symbol} kapanış emri gönderildi.`)
  }

  const pendingGate = useMemo(() => status?.readiness.gates.find(item => !item.passed),[status])
  const activePlans = useMemo(() => status?.plans.filter(item => !['KAPANDI','İPTAL'].includes(item.status)) ?? [],[status])
  const chartPlan = useMemo(() => activePlans.find(item => item.id === chartPlanId) || activePlans.find(item => item.symbol === chartSymbol) || null,[activePlans,chartPlanId,chartSymbol])
  if (!status || !policy) return <div className="executionLoading"><RefreshCw className="spin"/><b>V25 LIVE GUARD BAĞLANIYOR</b><span>{notice}</span></div>

  return <div className="executionV25">
    <header className="executionHero">
      <div><span><ShieldCheck/></span><div><small>V28 · IN-APP VAULT / FAIL-CLOSED LIVE</small><h2>Canlı Kasa & Otonom Emir Merkezi</h2><p>Uygulama içi şifreli API kasası → salt-okunur canlı hesap → süreli gerçek emir zinciri.</p></div></div>
      <aside className={status.readiness.ready ? 'ready' : 'locked'}><b>{status.readiness.ready ? 'CANLI HAZIR' : 'CANLI KONTROLLER'}</b><strong>%{status.readiness.score}</strong><span>{pendingGate ? `${pendingGate.label} kontrolü bekleniyor` : 'Bütün yayın kapıları geçti'}</span></aside>
    </header>

    <div className="executionRiskBanner"><AlertTriangle/><div><b>KÂR VE “SORUNSUZ ÇALIŞMA” GARANTİSİ YOKTUR</b><span>Vadeli işlemler tüm sermayeyi kaybettirebilir. V25 yalnızca teknik ve operasyonel hataları azaltan korumalar uygular; piyasa riskini yok etmez.</span></div></div>
    <div className={`executionNotice ${noticeKind}`}>{notice}</div>

    <section className="executionCard liveCredentialsCard" aria-label="Live API credentials">
      <div className="executionHead"><div><small>V28 · ŞİFRELİ LIVE KASA</small><h3>Live API Credentials</h3><p>Secret değerleri yalnızca mevcut PostgreSQL + Fernet kasasına gider; arayüze geri döndürülmez.</p></div><KeyRound/></div>
      <div className="executionFields"><label>Live API Key<input type="password" autoComplete="new-password" value={liveCredentials.apiKey} onChange={event => setLiveCredentials(current => ({...current,apiKey:event.target.value}))} placeholder={liveVault?.connections.LIVE.configured ? 'Kayıtlı anahtar mevcut' : 'Live API Key'}/></label><label>Live API Secret<input type="password" autoComplete="new-password" value={liveCredentials.secretKey} onChange={event => setLiveCredentials(current => ({...current,secretKey:event.target.value}))} placeholder={liveVault?.connections.LIVE.configured ? 'Kayıtlı secret görüntülenmez' : 'Live API Secret'}/></label></div>
      <div className="orderButtons"><button onClick={() => void verifyLiveCredentials()} disabled={!!liveVaultBusy || !liveVault?.vault.ready}><Activity/>{liveVaultBusy === 'verify' ? 'DOĞRULANIYOR…' : 'VERIFY LIVE CONNECTION'}</button><button className="savePolicy" onClick={() => void saveLiveCredentials()} disabled={!!liveVaultBusy || !liveVault?.vault.ready}><Save/>{liveVaultBusy === 'save' ? 'KAYDEDİLİYOR…' : 'SAVE SECURELY'}</button></div>
      <p>{liveVault?.connections.LIVE.configured ? `Kasa: ${liveVault.connections.LIVE.storage} · İz: ${liveVault.connections.LIVE.fingerprint || '—'} · ${liveVault.connections.LIVE.last_test_ok ? 'Bağlantı doğrulandı' : 'Doğrulama bekleniyor'}` : liveVault?.vault.reason || 'Live kasa durumu bekleniyor.'}</p>
    </section>

    <section className="executionPulse" aria-label="Sistem durumu">
      <article className={status.credentials.configured ? 'ok' : 'wait'}><KeyRound/><small>ŞİFRELİ API KASASI</small><b>{status.credentials.configured ? 'UYGULAMADA AKTİF' : 'API BEKLİYOR'}</b><span>{status.credentials.fingerprint ? `İz ${status.credentials.fingerprint}` : 'Borsa Bağlantıları sekmesinden ekleyin · BINANCE-CANLI-AYARLA.bat'}</span></article>
      <article className={status.connected ? 'ok' : 'wait'}><Activity/><small>SALT OKUNUR API</small><b>{status.connected ? 'BAĞLI' : 'BAĞLI DEĞİL'}</b><span>{status.connection.last_error || `Saat farkı ${status.connection.clock_offset_ms ?? '—'} ms`}</span></article>
      <article className={status.stream.status === 'CANLI' ? 'ok' : 'wait'}><RefreshCw/><small>EMİR / POZİSYON AKIŞI</small><b>{status.stream.status}</b><span>{status.stream.last_error || `${status.stream.event_count} olay · ${status.stream.transport}`}</span></article>
      <article className={status.consent.active ? 'ok' : 'wait'}><LockKeyhole/><small>CANLI RİSK İZNİ</small><b>{status.consent.active ? '24 SAATLİK AKTİF' : 'KAPALI'}</b><span>{status.consent.active ? `${date(status.consent.expires_at)} · ${status.consent.storage || ''}` : 'API’den sonra elle onaylanır'}</span></article>
      <article className={status.armed ? 'hot' : 'wait'}><Power/><small>EMİR KİLİDİ</small><b>{status.armed ? '24 SAAT AÇIK' : 'KİLİTLİ'}</b><span>{status.armed_until ? date(status.armed_until) : 'Her açılışta sıfırlanır'}</span></article>
      <article className={status.auto.enabled ? 'hot' : 'wait'}><Bot/><small>V25 LIVE AUTOMATION</small><b>{status.auto.enabled ? 'ACTIVE' : 'INACTIVE'}</b><span>{status.auto.enabled ? `Session until ${date(status.auto_session_until)} · ` : 'Session inactive · '}{status.auto.cycles} tur · {status.auto.last_decision}</span></article>
    </section>

    <section className="executionActionArea">
      <div className="actionStep"><span>01</span><div><small>TEST VE DOĞRULAMA</small><b>Bağlantıyı önce kontrol edin</b></div><button onClick={connect} disabled={!!busy}><Activity/>{busy === 'connect' ? 'BAĞLANIYOR…' : 'SALT OKUNUR TEST'}</button></div>
      <div className="actionStep"><span>02</span><div><small>GÜVENLİK VE İZİNLER</small><b>Risk kapılarını tamamlayın</b></div><div className="actionStepButtons"><button onClick={acknowledge} disabled={!!busy || status.policy_acknowledged}><ShieldCheck/>{status.policy_acknowledged ? 'LİMİTLER ONAYLI' : 'LİMİTLERİ ONAYLA'}</button><button onClick={grantConsent} disabled={!!busy || !status.credentials.configured || status.consent.active}><LockKeyhole/>{status.consent.active ? '24 SAAT İZİNLİ' : '24 SAAT İZİN VER'}</button><button className="arm" onClick={arm} disabled={!!busy || !status.readiness.ready || status.armed}><LockKeyhole/>{status.armed ? '24 SAAT KİLİT AÇIK' : 'CANLI KİLİDİ AÇ'}</button></div></div>
      <div className="actionStep actionStepPrimary"><span>03</span><div><small>OTOMASYON</small><b>{status.auto.enabled ? 'Tarama oturumu aktif' : 'Hazır olduğunuzda başlatın'}</b></div>{status.auto.enabled ? <button onClick={() => run('stop','/auto/stop',undefined,'Yeni otomatik girişler durduruldu; korumalar açık.')} disabled={!!busy}><Square/>OTOMASYONU DURDUR</button> : <button className="auto" onClick={startAuto} disabled={!!busy || !status.armed}><Play/>OTOMASYONU BAŞLAT</button>}</div>
      <div className="actionDanger"><small>TEHLİKE ALANI</small><button className="danger" onClick={emergency} disabled={!!busy || !status.credentials.configured}><AlertTriangle/>ACİL DURDUR</button></div>
    </section>

    <div className="executionColumns">
      <section className="executionCard policyCard">
        <div className="executionHead"><div><small>HARD CAP + KULLANICI LİMİTİ</small><h3>Canlı Risk Politikası</h3><p>Botun hangi piyasaları izleyeceğini ve hangi koşullarda işlem açabileceğini belirleyin.</p></div><ShieldCheck/></div>
        <div className="policyGroups">
          <div className="policyGroup"><div className="policyGroupHead"><Activity/><div><b>Pazar ve zaman</b><span>İzlenecek piyasa ve yön tercihleri</span></div></div><div className="executionFields"><label>İzinli pariteler<small>Scanner’ın izleyeceği semboller</small><input value={policy.allowed_symbols.join(', ')} onChange={e => setPolicy({...policy,allowed_symbols:e.target.value.split(',').map(v => v.trim().toUpperCase()).filter(Boolean)})}/></label><label>Zaman dilimi<small>Analiz mumlarının aralığı</small><select value={policy.interval} onChange={e => setPolicy({...policy,interval:e.target.value})}><option>1m</option><option>5m</option><option>15m</option><option>1h</option><option>4h</option></select></label></div><div className="directionChecks"><label className={policy.allow_long ? 'active' : ''}><input type="checkbox" checked={policy.allow_long} onChange={e => setPolicy({...policy,allow_long:e.target.checked})}/> <span>LONG</span><small>{policy.allow_long ? 'AKTİF' : 'PASİF'}</small></label><label className={policy.allow_short ? 'active' : ''}><input type="checkbox" checked={policy.allow_short} onChange={e => setPolicy({...policy,allow_short:e.target.checked})}/> <span>SHORT</span><small>{policy.allow_short ? 'AKTİF' : 'PASİF'}</small></label></div></div>
          <div className="policyGroup"><div className="policyGroupHead"><WalletCards/><div><b>Pozisyon riski</b><span>Tek işlem ve portföy kapasitesi</span></div></div><div className="executionFields"><label>Maks. kaldıraç<small>Bir işlem için izin verilen maksimum kaldıraç</small><input type="number" min="1" max="50" value={policy.max_leverage} onChange={e => setPolicy({...policy,max_leverage:Number(e.target.value)})}/></label><label>Maks. pozisyon<small>Aynı anda açık tutulabilecek işlem sayısı</small><input type="number" min="1" max="5" value={policy.max_positions} onChange={e => setPolicy({...policy,max_positions:Number(e.target.value)})}/><span>işlem</span></label><label>Maks. marjin / işlem<small>Her yeni işlem için kullanılabilecek üst sınır</small><input type="number" min="5" max="100" value={policy.max_margin_per_trade} onChange={e => setPolicy({...policy,max_margin_per_trade:Number(e.target.value)})}/><span>USDT</span></label></div></div>
          <div className="policyGroup"><div className="policyGroupHead"><LockKeyhole/><div><b>Günlük koruma</b><span>Kayıp ve işlem sayısı sınırları</span></div></div><div className="executionFields"><label>Günlük kayıp kilidi<small>Günlük zarar bu seviyeye ulaşınca durur</small><input type="number" min="5" max="100" value={policy.daily_loss_limit} onChange={e => setPolicy({...policy,daily_loss_limit:Number(e.target.value)})}/><span>USDT</span></label><label>Günlük işlem limiti<small>Bir günde açılabilecek maksimum işlem</small><input type="number" min="1" max="12" value={policy.daily_trade_limit} onChange={e => setPolicy({...policy,daily_trade_limit:Number(e.target.value)})}/><span>işlem</span></label><label>Maks. kayıp / işlem<small>Tek işlemde kabul edilen maksimum zarar</small><input type="number" min="0.5" max="25" step="0.5" value={policy.max_loss_per_trade} onChange={e => setPolicy({...policy,max_loss_per_trade:Number(e.target.value)})}/><span>USDT</span></label></div></div>
          <div className="policyGroup"><div className="policyGroupHead"><TrendingUp/><div><b>İşlem kalitesi</b><span>Sinyalin geçmesi gereken piyasa filtreleri</span></div></div><div className="executionFields"><label>Min. karar güveni<small>İşlem açmadan önce gereken sinyal güveni</small><input type="number" min="70" max="95" value={policy.min_confidence} onChange={e => setPolicy({...policy,min_confidence:Number(e.target.value)})}/><span>%</span></label><label>Maks. tuzak skoru<small>Riskli piyasa davranışı için üst sınır</small><input type="number" min="10" max="60" value={policy.max_trap_score} onChange={e => setPolicy({...policy,max_trap_score:Number(e.target.value)})}/><span>%</span></label><label>Maks. spread<small>İzin verilen alış-satış farkı</small><input type="number" min="0.5" max="25" step="0.5" value={policy.max_spread_bps} onChange={e => setPolicy({...policy,max_spread_bps:Number(e.target.value)})}/><span>bp</span></label><label>Maks. Stop mesafesi<small>Giriş ile Stop arasındaki maksimum mesafe</small><input type="number" min="0.25" max="5" step="0.25" value={policy.max_stop_distance_pct} onChange={e => setPolicy({...policy,max_stop_distance_pct:Number(e.target.value)})}/><span>%</span></label></div></div>
        </div>
        <div className="policyFoot"><span>Politika izi: {status.policy_digest}</span><button className="savePolicy" onClick={savePolicy} disabled={!!busy}><Save/>{busy === 'policy' ? 'KAYDEDİLİYOR…' : 'LİMİTLERİ KAYDET'}</button></div>
      </section>

      <section className="executionCard gateCard">
        <div className="executionHead"><div><small>OTOMATİK VE ZORUNLU</small><h3>Hazırlık Kontrol Merkezi</h3><p>Canlı otomasyon, aşağıdaki güvenlik kontrolleri tamamlanmadan başlatılamaz.</p></div><strong>%{status.readiness.score}</strong></div>
        <div className={`readinessSummary ${status.readiness.ready ? 'ready' : 'pending'}`}><div><span className="statusDot"/><div><small>CANLI İŞLEM HAZIRLIĞI</small><b>{status.readiness.ready ? 'HAZIR' : 'HAZIR DEĞİL'}</b></div></div><span>{status.readiness.gates.filter(gate => gate.passed).length} / {status.readiness.gates.length} tamamlandı</span></div>
        <div className="readinessProgress"><span style={{width:`${Math.min(100,status.readiness.score)}%`}}/></div>
        <div className="liveGates">{status.readiness.gates.map(gate => <article key={gate.key} className={gate.passed ? 'passed' : 'pending'}>{gate.passed ? <CheckCircle2/> : <XCircle/>}<div><b>{gate.label}</b><span>{gate.detail}</span></div><em>{gate.passed ? 'TAMAM' : 'DÜZELT'}</em></article>)}</div>
        <div className="demoCertificate"><TestTube2/><div><small>BINANCE DEMO KANITI</small><b>{status.readiness.demo_certificate.live_allowed ? status.readiness.demo_certificate.live_allowance_status : status.readiness.demo_certificate.status || 'KANIT BEKLİYOR'}</b><span>Demo evidence score: %{status.readiness.demo_certificate.score ?? 0}. LIVE waiver, Demo evidence sonuçlarını değiştirmez.</span></div><button onClick={connect} disabled={!!busy}><RefreshCw/> KONTROL ET</button></div>
      </section>
    </div>

    <div className="executionColumns lower">
      <section className="executionCard testCard">
        <div className="executionHead"><div><small>ÖNCE TEST · SONRA ÇİFT ONAYLI CANLI</small><h3>MARKET / LIMIT Emir Bileti</h3></div><Send/></div>
        <div className="executionFields testFields">
          <label>Parite<input value={test.symbol} onChange={e => setTest({...test,symbol:e.target.value.toUpperCase()})}/></label>
          <label>Yön<select value={test.direction} onChange={e => setTest({...test,direction:e.target.value as 'LONG'|'SHORT'})}><option>LONG</option><option>SHORT</option></select></label>
          <label>Emir tipi<select value={test.order_type} onChange={e => setTest({...test,order_type:e.target.value as 'MARKET'|'LIMIT'})}><option>MARKET</option><option>LIMIT</option></select></label>
          <label>Limit fiyatı<input type="number" disabled={test.order_type !== 'LIMIT'} placeholder={test.order_type === 'LIMIT' ? 'Zorunlu' : 'Market için kullanılmaz'} value={test.limit_price} onChange={e => setTest({...test,limit_price:e.target.value})}/></label>
          <label>Marjin<input type="number" value={test.margin_usdt} onChange={e => setTest({...test,margin_usdt:e.target.value})}/></label>
          <label>Kaldıraç<select value={test.leverage} onChange={e => setTest({...test,leverage:e.target.value})}><option>1</option><option>2</option><option>3</option></select></label>
          <label>Stop<input type="number" value={test.stop_loss} onChange={e => setTest({...test,stop_loss:e.target.value})}/></label>
          <label>TP1<input type="number" value={test.tp1} onChange={e => setTest({...test,tp1:e.target.value})}/></label>
          <label>TP2<input type="number" value={test.tp2} onChange={e => setTest({...test,tp2:e.target.value})}/></label>
          <label>TP3<input type="number" value={test.tp3} onChange={e => setTest({...test,tp3:e.target.value})}/></label>
        </div>
        <div className="orderButtons"><button className="testOrder" onClick={orderTest} disabled={!!busy || !status.connected}><TestTube2/>{busy === 'test' ? 'DOĞRULANIYOR…' : '1 · GERÇEK EMİR OLUŞTURMADAN TEST ET'}</button><button className="liveOrder" onClick={submitLiveOrder} disabled={!!busy || !status.connected}><Send/>{busy === 'live-order' ? 'GÖNDERİLİYOR…' : '2 · ÇİFT ONAYLA CANLI EMİR GÖNDER'}</button></div>
        <p>Test düğmesi Binance <b>/fapi/v1/order/test</b> çağrısı yapar ve emir oluşturmaz. Canlı düğme ancak tüm yayın kapıları geçip 24 saatlik kilit açıldığında etkinleşir.</p>
      </section>

      <section className="executionCard accountCard">
        <div className="executionHead"><div><small>SALT OKUNUR CANLI HESAP</small><h3>Bakiye & Pozisyonlar</h3></div><WalletCards/></div>
        <div className="accountMetrics"><span><small>Cüzdan</small><b>{money(status.account.wallet_balance)} USDT</b></span><span><small>Kullanılabilir</small><b>{money(status.account.available_balance)} USDT</b></span><span><small>Açık PnL</small><b className={(status.account.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{money(status.account.unrealized_pnl)} USDT</b></span><span><small>Pozisyon</small><b>{status.account.positions.length} / {policy.max_positions}</b></span><span><small>Mod</small><b>{status.account.hedge_mode === false ? 'ONE-WAY' : status.account.hedge_mode === true ? 'HEDGE · UYGUN DEĞİL' : '—'}</b></span></div>
        <div className="positionRows">{status.account.positions.length ? status.account.positions.map(position => {
          const plan = status.plans.find(item => item.symbol === position.symbol && !['KAPANDI','İPTAL','ACİL DURDURULDU'].includes(item.status))
          return <article key={position.symbol} className={chartSymbol === position.symbol ? 'selected' : ''}><CircleDollarSign/><div><b>{position.symbol} · {position.direction}</b><span>Giriş {money(position.entry_price)} · Mark {money(position.mark_price)} · {position.leverage ?? '—'}x {position.margin_type || '—'}</span></div><strong className={position.unrealized_pnl >= 0 ? 'positive' : 'negative'}>{position.unrealized_pnl >= 0 ? '+' : ''}{money(position.unrealized_pnl)} USDT</strong><div className="positionActions"><button className="chartButton" onClick={() => {setChartSymbol(position.symbol);setChartPlanId(plan?.id || null)}}>GRAFİK + SEVİYELER</button>{plan ? <button onClick={() => closePlan(plan)} disabled={!!busy}>REDUCE-ONLY KAPAT</button> : <em>ProTreBot dışı · dokunulmaz</em>}</div></article>
        }) : <div className="emptyLive"><WalletCards/><b>Açık canlı pozisyon yok</b><span>V25 yalnızca kendi client ID’siyle açtığı pozisyonları yönetir.</span></div>}</div>
      </section>
    </div>

    <LivePositionChart token={token} symbol={chartPlan?.symbol || chartSymbol || test.symbol} interval={policy.interval} plan={chartPlan}/>

    <section className="executionCard eventCard">
      <div className="executionHead"><div><small>ANAHTARSIZ DENETİM KAYDI</small><h3>Canlı Karar ve Emir Günlüğü</h3></div><RefreshCw/></div>
      <div className="executionEvents">{status.events.length ? status.events.slice(0,20).map(event => <article key={event.id}><i/><div><b>{event.kind}{event.symbol ? ` · ${event.symbol}` : ''}</b><span>{event.message}</span></div><time>{date(event.created_at)}</time></article>) : <div className="emptyLive"><Activity/><b>Henüz canlı olay yok</b><span>Önce salt-okunur bağlantıyı doğrulayın.</span></div>}</div>
    </section>
  </div>
}
