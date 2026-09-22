import { type CSSProperties, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, BarChart3, Bell, Calculator, CircleDollarSign, ClipboardList, Crosshair, Gauge, History, LockKeyhole, Play, Radio, RefreshCw, Save, Send, Settings2, ShieldCheck, Target, TestTube2, TriangleAlert, UnlockKeyhole, Wallet, Zap } from 'lucide-react'
import { API_BASE } from './api'

const API = `${API_BASE}/binance-demo`
const V21_API = `${API_BASE}/v21`

type AnalysisPlan = {
  direction?:string|null
  normalized_signal?:string|null
  side?:string|null
  signal?:string|null
  bias?:string|null
  recommendation?:string|null
  entry:number
  stop_loss:number
  tp1:number
  tp2:number
  tp3:number
}

type DemoStatus = {
  version:string
  mode:string
  configured:boolean
  connected:boolean
  armed:boolean
  armed_until:string|null
  rest_host:string
  websocket_host:string
  real_trading_locked:boolean
  limits:{max_margin_usdt:number;max_leverage:number;max_notional_usdt:number;max_open_positions:number;arm_minutes:number}
  last_checked:string|null
  last_error:string|null
  events:{kind:string;message:string;created_at:string}[]
  reconciliation?:{actual_exchange_open_positions:number;internal_active_plans:number;reconciled_active_positions:number;stale_positions_removed:number}
}

type DemoPosition = {
  symbol:string
  direction:'LONG'|'SHORT'
  position_side?:string|null
  opened_at?:string|null
  quantity:number
  entry_price:number
  mark_price:number
  liquidation_price:number
  unrealized_pnl:number
  leverage:number|null
  margin_type:string|null
  requested_leverage?:number|null
  applied_leverage?:number|null
  leverage_verified?:boolean
  configuration_source?:string
}

type DemoOrder = {
  symbol:string
  order_id:number
  client_order_id:string
  side:string
  type:string
  status:string
  price:number
  quantity:number
  executed_quantity:number
  reduce_only:boolean
}

type DemoRiskPreview = {
  quantity?:string|number
  notional_usdt?:number
  risk_per_trade?:number
  risk_adjusted?:boolean
}

type DemoConfirmation = {
  title:string
  message:string
  expected:string
  checkbox:boolean
  action:() => Promise<unknown>
}

type DemoAlgoOrder = {
  symbol:string
  algo_id:number
  client_algo_id:string
  side:string
  type:string
  status:string
  trigger_price:number
  quantity:number
  close_position:boolean
}

type DemoPlan = {
  id:string
  symbol:string
  direction:'LONG'|'SHORT'
  order_type:string
  entry_price:string
  quantity:string
  margin_usdt:number
  leverage:number
  requested_leverage?:number
  applied_leverage?:number
  margin_type?:string
  leverage_verified?:boolean
  configuration_source?:string
  stop_loss:string
  targets:string[]
  status:string
  created_at:string
  monitoring_targets?:string[]
}

type DemoAccount = DemoStatus & {
  wallet_balance:number
  available_balance:number
  margin_balance:number
  unrealized_pnl:number
  positions:DemoPosition[]
  open_orders:DemoOrder[]
  open_algo_orders:DemoAlgoOrder[]
  hedge_mode:boolean
  plans:DemoPlan[]
  exchange_position_diagnostics?:{symbol:string;position_amount:string;exchange_actual_position:boolean}[]
}

type Direction = 'LONG'|'SHORT'

function normalizeDirection(value:unknown):Direction|null {
  const normalized = String(value ?? '').trim().toUpperCase()
  if (normalized === 'LONG' || normalized === 'BUY') return 'LONG'
  if (normalized === 'SHORT' || normalized === 'SELL') return 'SHORT'
  return null
}

type FormState = {
  direction:'LONG'|'SHORT'
  orderType:'MARKET'|'LIMIT'
  margin:string
  leverage:'1'|'2'
  limitPrice:string
  stop:string
  tp1:string
  tp2:string
  tp3:string
}

type V21Settings = {
  allowed_symbols:string[];allow_long:boolean;allow_short:boolean;max_loss_per_trade:number;max_margin_per_trade:number
  daily_loss_limit:number;daily_loss_limit_pct?:number;daily_trade_limit:number;max_positions:number;min_confidence:number;max_volatility_pct:number
  max_correlation_pct:number;schedule_start_hour:number;schedule_end_hour:number;scan_seconds:number
  breakeven_enabled:boolean;breakeven_trigger_r:number;trailing_enabled:boolean;trailing_trigger_r:number
  trailing_distance_r:number;notifications:boolean;fee_bps_per_side:number;slippage_bps_per_side:number;consecutive_loss_limit?:number;kill_switch?:boolean
}

type V21Journal = {id:string;created_at:string;kind:string;symbol?:string|null;status?:string|null;side?:string|null;price?:number|null;quantity?:number|null;realized_pnl?:number|null;reason?:string|null;message:string;source:string;reduce_only:boolean}
type V21Gate = {name:string;passed:boolean;value:string|number;target:string|number}
type ScannerCandidate = {rank:number;symbol:string;score:number;direction:string;confidence:string;confidence_value:number;trend:string;mtf_trend:string;volume:number;volume_ratio:number;rsi:number;status:string;reasons:string[];entry?:number;stop_loss?:number;tp1?:number;tp2?:number;tp3?:number}
type ScannerState = {scan_status:string;running?:boolean;last_scan_at:string|null;next_scan_at:string|null;scan_interval_seconds:number;coins_scanned:number;selected_count:number;eligible_count?:number;scan_duration_seconds:number;top_candidates:ScannerCandidate[];all_candidates:ScannerCandidate[];last_error:string|null}
type AutomationTrade = {symbol:string;side:string;scanner_rank:number;scanner_score:number;confidence:string;entry_time:string;entry_price:string|number;margin:number;leverage:number;tp:(string|number)[];sl:string|number;trade_reason:string[];status:string}
type V21Backtest = {symbol:string;interval:string;trades:number;wins:number;win_rate:number;net_pnl:number;ending_equity:number;max_drawdown_pct:number;profit_factor:number;no_lookahead:boolean;folds:{name:string;trades:number;net_pnl:number}[];recent_trades:{signal_time:number;entry_time:number;exit_time:number;direction:string;entry:number;exit:number;reason:string;pnl:number;cost_usdt:number;regime:string}[];note:string}
type V21Summary = {
  version:string;mode:string;settings:V21Settings
  auto:{enabled:boolean;busy:boolean;cycles:number;last_scan:string|null;last_decision:string;last_error:string|null;status?:string;pause_reason?:string|null;started_at?:string|null;rejection_gate?:string|null;rejection_reason?:string|null}
  risk?:{daily_loss_pct?:number;last_warning_pct?:number;consecutive_losses?:number;consecutive_loss_limit?:number;kill_switch?:boolean}
  notifications?:{unread?:number}
  scanner:ScannerState
  stream:{status:string;transport:string;last_event:string|null;last_sync:string|null;reconnect_count:number;error_count:number;last_error:string|null}
  daily:{date:string;auto_entries:number;events:number;realized_pnl:number;remaining_loss_budget:number}
  account:{wallet_balance:number|null;available_balance:number|null;unrealized_pnl:number|null;positions:number;auto_positions?:number;reconciled_active_positions?:number;normal_orders:number;algo_orders:number}
  protection:{repairs:number;duplicate_blocks:number};journal:V21Journal[];backtest:V21Backtest|null
  certificate:{version:string;status:string;score:number;passed_gates:number;total_gates:number;gates:V21Gate[];reason:string;generated_at:string};automation_trades:AutomationTrade[]
  last_saved:string|null;real_trading_locked:boolean
}

type V21RiskPreview = {symbol:string;leverage:number;risk_pct:number;notional_usdt:number;margin_usdt:number;estimated_stop_loss_usdt:number;capped:boolean;quantity_preview:string;step_size:string}
type V21Tab = 'trade'|'risk'|'journal'|'auto'|'backtest'|'performance'|'certificate'

const initialForm:FormState = {
  direction:'LONG',orderType:'MARKET',margin:'50',leverage:'2',limitPrice:'',stop:'',tp1:'',tp2:'',tp3:'',
}

const SCAN_INTERVAL_SECONDS = 900
const normalizeV21Settings = (settings:V21Settings):V21Settings => ({...settings,scan_seconds:Number.isFinite(settings.scan_seconds) && settings.scan_seconds >= SCAN_INTERVAL_SECONDS ? settings.scan_seconds : SCAN_INTERVAL_SECONDS})
const fmt = (value?:number|null) => value === undefined || value === null || !Number.isFinite(value) ? '—' : value.toLocaleString('tr-TR',{maximumFractionDigits:value < 10 ? 5 : 2})
const stamp = (value?:string|null) => value ? new Date(value).toLocaleString('tr-TR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—'
const numberValue = (value:string) => Number(value.replace(',','.'))

function EmptyState({label='Veri bekleniyor...',detail,icon,className=''}:{label?:string;detail?:string;icon?:ReactNode;className?:string}) {
  return <span className={`v21EmptyState ${className}`.trim()}><span className="v21EmptyIcon">{icon || <Activity/>}</span><strong>{label}</strong>{detail && <small>{detail}</small>}</span>
}

const fieldNames:Record<string,string> = {
  margin_usdt:'Marjin',leverage:'Kaldıraç',limit_price:'Limit fiyatı',stop_loss:'Stop Loss',
  tp1:'TP1',tp2:'TP2',tp3:'TP3',direction:'Yön',order_type:'Emir türü',symbol:'Parite',
}

function apiErrorMessage(detail:unknown):string {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const messages = detail.map(item => {
      if (!item || typeof item !== 'object') return String(item)
      const row = item as {loc?:unknown[];msg?:unknown}
      const field = Array.isArray(row.loc) ? String(row.loc.at(-1) ?? '') : ''
      if (field === 'margin_usdt') return 'Marjin 5–100 USDT arasında olmalı.'
      if (field === 'leverage') return 'Kaldıraç yalnızca 1x veya 2x olabilir.'
      if (['limit_price','stop_loss','tp1','tp2','tp3'].includes(field)) return `${fieldNames[field]} boş bırakılamaz ve 0’dan büyük olmalı.`
      const message = typeof row.msg === 'string' ? row.msg : 'Geçersiz değer'
      return `${fieldNames[field] || field || 'Alan'}: ${message}`
    }).filter(Boolean)
    if (messages.length) return [...new Set(messages)].join(' · ')
  }
  if (detail && typeof detail === 'object') {
    const message = (detail as {message?:unknown}).message
    if (typeof message === 'string') return message
  }
  return 'Binance Demo isteği doğrulanamadı. Emir alanlarını kontrol edin.'
}

async function apiCall<T>(path:string, options?:RequestInit):Promise<T> {
  const response = await fetch(`${API}${path}`, options)
  const payload:unknown = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(apiErrorMessage((payload as {detail?:unknown})?.detail))
  return payload as T
}

async function v21Call<T>(path:string, options?:RequestInit):Promise<T> {
  const response = await fetch(`${V21_API}${path}`, options)
  const payload:unknown = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(apiErrorMessage((payload as {detail?:unknown})?.detail))
  return payload as T
}

function PositionMap({position,plan}:{position:DemoPosition;plan?:DemoPlan}) {
  const entry = position.entry_price
  const mark = position.mark_price
  const stop = Number(plan?.stop_loss || 0)
  const targets = (plan?.targets || []).map(Number).filter(Number.isFinite)
  const rawLevels = [stop,entry,...targets,mark].filter(value => value > 0)
  const low = Math.min(...rawLevels)
  const high = Math.max(...rawLevels)
  const padding = Math.max((high-low)*.10,entry*.001)
  const min = low-padding
  const max = high+padding
  const left = (value:number) => `${Math.max(1,Math.min(99,(value-min)/Math.max(.000001,max-min)*100))}%`
  const grid = Array.from({length:9},(_,index) => min+(max-min)*(index+1)/10)
  return <div className="demoPositionMap">
    <div className="demoMapRail">{grid.map((level,index) => <i key={index} style={{left:left(level)}}/>)}
      {stop > 0 && <span className="demoMapPin demoStopPin" style={{left:left(stop)}}><b>STOP</b><em>{fmt(stop)}</em></span>}
      <span className="demoMapPin demoEntryPin" style={{left:left(entry)}}><b>GİRİŞ</b><em>{fmt(entry)}</em></span>
      {targets.map((target,index) => <span className="demoMapPin demoTargetPin" key={target} style={{left:left(target)}}><b>TP{index+1}</b><em>{fmt(target)}</em></span>)}
      <span className="demoMapMark" style={{left:left(mark)}}><i/><b>CANLI {fmt(mark)}</b></span>
    </div>
    <footer><span>SEVİYE IZGARASI</span><b>{position.direction} · {position.leverage ? `${position.leverage}x` : 'DOĞRULANIYOR'}</b><em>{plan?.status || 'Plan kaydı aranıyor'}</em></footer>
  </div>
}

export default function BinanceDemo({active,symbol,analysis,chart}:{active:boolean;symbol:string;analysis:AnalysisPlan|null;chart?:ReactNode}) {
  const [status,setStatus] = useState<DemoStatus|null>(null)
  const [account,setAccount] = useState<DemoAccount|null>(null)
  const [form,setForm] = useState<FormState>(initialForm)
  const [armText,setArmText] = useState('')
  const [busy,setBusy] = useState(false)
  const [message,setMessage] = useState('Önce bağlantıyı test edin; ardından analiz planını doğrulayın.')
  const [messageKind,setMessageKind] = useState<'info'|'ok'|'error'>('info')
  const [orderPreview,setOrderPreview] = useState<DemoRiskPreview|null>(null)
  const [confirmation,setConfirmation] = useState<DemoConfirmation|null>(null)
  const [confirmationText,setConfirmationText] = useState('')
  const [confirmationChecked,setConfirmationChecked] = useState(false)
  const [clock,setClock] = useState(Date.now())
  const [tab,setTab] = useState<V21Tab>('trade')
  const [v21,setV21] = useState<V21Summary|null>(null)
  const [settingsDraft,setSettingsDraft] = useState<V21Settings|null>(null)
  const [v21Busy,setV21Busy] = useState(false)
  const [riskLoss,setRiskLoss] = useState('5')
  const [riskPreview,setRiskPreview] = useState<V21RiskPreview|null>(null)
  const [historyPayload,setHistoryPayload] = useState<{orders:Record<string,unknown>[];algo_orders:Record<string,unknown>[];trades:Record<string,unknown>[]} | null>(null)
  const [autoConfirm,setAutoConfirm] = useState('')
  const [scannerBusy,setScannerBusy] = useState(false)
  const [backtestSymbol,setBacktestSymbol] = useState(symbol)
  const accountRefreshId = useRef(0)
  const v21RequestId = useRef(0)
  const initialScanRequested = useRef(false)
  const lastNotificationId = useRef<string|null>(null)

  const refreshStatus = async () => {
    try {
      const payload = await apiCall<DemoStatus>('/status')
      setStatus(payload)
      if (!payload.configured) setAccount(null)
      return payload
    } catch { setStatus(null); return null }
  }
  const refreshAccount = async (quiet=true) => {
    const requestId = ++accountRefreshId.current
    try {
      const payload = await apiCall<DemoAccount>('/account')
      if (requestId !== accountRefreshId.current) return
      setAccount(payload); setStatus(payload)
    } catch (error) {
      if (requestId !== accountRefreshId.current) return
      setAccount(null)
      if (!quiet) { setMessage(error instanceof Error ? error.message : 'Demo hesap okunamadı.'); setMessageKind('error') }
    }
  }
  const refreshV21 = async (quiet=true) => {
    const requestId = ++v21RequestId.current
    try {
      const payload = await v21Call<V21Summary>('/summary')
      if (requestId !== v21RequestId.current) return null
      setV21(payload)
      setSettingsDraft(current => current || normalizeV21Settings(payload.settings))
      return payload
    } catch (error) {
      if (!quiet) { setMessage(error instanceof Error ? error.message : 'V21 merkezi okunamadı.');setMessageKind('error') }
      return null
    }
  }

  const runScanner = async () => {
    if (scannerBusy) return
    setScannerBusy(true);setMessageKind('info');setMessage('100 USDT perpetual paritesi Demo market verisiyle taranıyor…')
    const requestId = ++v21RequestId.current
    try {
      await v21Call<ScannerState>('/scanner/scan',{method:'POST'})
      const payload = await v21Call<V21Summary>('/summary')
      if (requestId === v21RequestId.current) setV21(payload)
      setMessage('Canlı coin taraması tamamlandı; Top 3 ve tablo güncellendi.');setMessageKind('ok')
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Scanner çalıştırılamadı.');setMessageKind('error') }
    finally { setScannerBusy(false) }
  }

  useEffect(() => {
    if (!active) return
    let mounted = true
    const refresh = async () => {
      const payload = await refreshStatus()
      if (mounted && payload?.configured) await refreshAccount(true)
      if (mounted) {
        const summary = await refreshV21(true)
        if (summary?.scanner && !summary.scanner.last_scan_at && !initialScanRequested.current) {
          initialScanRequested.current = true
          void runScanner()
        }
      }
    }
    refresh()
    const timer = window.setInterval(refresh,3500)
    const ticker = window.setInterval(() => setClock(Date.now()),1000)
    return () => { mounted=false;window.clearInterval(timer);window.clearInterval(ticker) }
  },[active])

  useEffect(() => { setBacktestSymbol(symbol) },[symbol])

  useEffect(() => { setOrderPreview(null) },[form.direction,form.orderType,form.margin,form.leverage,form.limitPrice,form.stop,form.tp1,form.tp2,form.tp3])

  useEffect(() => {
    const newest = v21?.journal?.[0]
    if (!newest) return
    if (lastNotificationId.current === null) { lastNotificationId.current = newest.id;return }
    if (newest.id === lastNotificationId.current) return
    lastNotificationId.current = newest.id
    if (v21?.settings.notifications && 'Notification' in window && Notification.permission === 'granted') {
      new Notification(`ProTreBot · ${newest.kind}`, {body:newest.message,tag:newest.id})
    }
  },[v21?.journal?.[0]?.id])

  const armSeconds = status?.armed_until ? Math.max(0,Math.floor((new Date(status.armed_until).getTime()-clock)/1000)) : 0
  const nextScanMs = v21?.scanner.next_scan_at ? new Date(v21.scanner.next_scan_at).getTime() : null
  const nextScanSeconds = nextScanMs === null ? null : Math.max(0,Math.ceil((nextScanMs - clock) / 1000))
  const nextScanCountdown = nextScanSeconds === null ? '—' : `${Math.floor(nextScanSeconds / 60)} dk ${nextScanSeconds % 60} sn sonra`
  const activePlanBySymbol = useMemo(() => {
    const map = new Map<string,DemoPlan>()
    for (const plan of account?.plans || []) if (!['KAPANDI','İPTAL'].includes(plan.status)) map.set(plan.symbol,plan)
    return map
  },[account?.plans])

  const fillFromAnalysis = async () => {
    setBusy(true);setMessageKind('info');setMessage('Seçili Futures paritesi için güncel analiz alınıyor…')
    try {
      let plan = analysis
      if (!plan || !normalizeDirection(plan.direction)) {
        const response = await fetch(`${API_BASE}/analysis/${encodeURIComponent(symbol)}?interval=15m`)
        const payload = await response.json().catch(() => null) as AnalysisPlan | {detail?:unknown} | null
        if (!response.ok) throw new Error(apiErrorMessage(payload && 'detail' in payload ? payload.detail : payload))
        plan = payload as AnalysisPlan
      }
      const direction = normalizeDirection(plan?.direction) || normalizeDirection(plan?.normalized_signal) || normalizeDirection(plan?.side) || normalizeDirection(plan?.signal) || normalizeDirection(plan?.bias) || normalizeDirection(plan?.recommendation)
      if (!direction) throw new Error('Yön belirlenemedi; mevcut yön korunuyor.')
      const levels = [plan.entry,plan.stop_loss,plan.tp1,plan.tp2,plan.tp3]
      if (levels.some(value => !Number.isFinite(value) || value <= 0)) throw new Error('Analiz planında geçerli giriş, Stop ve TP seviyeleri bulunamadı.')
      const ordered = direction === 'LONG'
        ? plan.stop_loss < plan.entry && plan.entry < plan.tp1 && plan.tp1 < plan.tp2 && plan.tp2 < plan.tp3
        : plan.stop_loss > plan.entry && plan.entry > plan.tp1 && plan.tp1 > plan.tp2 && plan.tp2 > plan.tp3
      if (!ordered) throw new Error(`${direction} analizinde Stop, giriş ve TP seviyeleri yanlış sırada.`)
      setForm(current => ({...current,direction,limitPrice:String(plan.entry),stop:String(plan.stop_loss),tp1:String(plan.tp1),tp2:String(plan.tp2),tp3:String(plan.tp3)}))
    setMessage('Giriş, Stop ve TP1–TP3 güncel analizden dolduruldu. Göndermeden önce mutlaka kontrol edin.');setMessageKind('ok')
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Güncel analiz planı alınamadı.');setMessageKind('error') }
    finally { setBusy(false) }
  }

  const payload = () => {
    const margin = numberValue(form.margin)
    const leverage = Number(form.leverage)
    if (!Number.isFinite(margin) || margin < 5 || margin > 100) throw new Error('Demo marjini 5–100 USDT arasında olmalı.')
    if (![1,2].includes(leverage)) throw new Error('Kaldıraç yalnızca 1x veya 2x olabilir.')
    const levels = {stop_loss:numberValue(form.stop),tp1:numberValue(form.tp1),tp2:numberValue(form.tp2),tp3:numberValue(form.tp3)}
    const missing = Object.entries(levels).filter(([,value]) => !Number.isFinite(value) || value <= 0).map(([key]) => fieldNames[key])
    if (missing.length) throw new Error(`${missing.join(', ')} alanlarını güncel analizden doldurun veya elle geçerli fiyat girin.`)
    const limitPrice = numberValue(form.limitPrice)
    if (form.orderType === 'LIMIT' && (!Number.isFinite(limitPrice) || limitPrice <= 0)) throw new Error('Limit emrinde geçerli bir Limit fiyatı girmelisiniz.')
    return {
      symbol,direction:form.direction,order_type:form.orderType,margin_usdt:margin,leverage,
      limit_price:form.orderType === 'LIMIT' ? limitPrice : null,...levels,
    }
  }

  const changeMargin = (value:string) => {
    if (value === '') { setForm({...form,margin:value});return }
    const parsed = numberValue(value)
    if (Number.isFinite(parsed) && parsed <= 100) setForm({...form,margin:value})
  }

  const normalizeMargin = () => {
    const parsed = numberValue(form.margin)
    const safe = Number.isFinite(parsed) ? Math.min(100,Math.max(5,Math.round(parsed))) : 10
    setForm({...form,margin:String(safe)})
  }

  const runAction = async (action:() => Promise<unknown>,success:string) => {
    setBusy(true);setMessageKind('info');setMessage('İşlem Binance Futures Demo üzerinde doğrulanıyor…')
    try {
      const result = await action() as {risk_adjusted?:boolean;preview?:DemoRiskPreview;plan?:DemoRiskPreview} | null
      const preview = result?.preview || result?.plan
      if (preview) setOrderPreview(preview)
      setMessage(result?.risk_adjusted ? 'Risk limiti nedeniyle pozisyon büyüklüğü otomatik olarak azaltıldı.' : success)
      setMessageKind('ok');await refreshStatus();await refreshAccount(true)
    }
    catch (error) { setMessage(error instanceof Error ? error.message : 'İşlem tamamlanamadı.');setMessageKind('error') }
    finally { setBusy(false) }
  }

  const connect = () => runAction(() => apiCall('/connect',{method:'POST'}),'Bağlantı başarılı: Sanal Futures Demo hesabı okunuyor; gerçek hesap kilitli.')
  const arm = () => runAction(() => apiCall('/arm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation:armText})}),'Demo emir kilidi 10 dakika için açıldı.')
  const disarm = () => runAction(() => apiCall('/disarm',{method:'POST'}),'Yeni Demo giriş emirleri kilitlendi; mevcut korumalar açık kalır.')
  const testOrder = () => runAction(() => apiCall('/order/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())}),'Emir testi geçti; hiçbir emir veya pozisyon oluşturulmadı.')
  const submitOrder = () => {
    setConfirmationText('')
    setConfirmationChecked(false)
    setConfirmation({
      title:'Demo order onayı',
      message:'Bu demo order\'ı gerçekten göndermek istiyor musun?',
      expected:'DEMO',
      checkbox:true,
      action:async () => {
        return runAction(() => apiCall('/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload(),confirmation:'DEMO'})}),'Emir yalnızca Binance Futures Demo hesabına gönderildi; koruma durumu yenileniyor.')
      },
    })
  }
  const cancelOrder = (order:DemoOrder) => runAction(() => apiCall('/order/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:order.symbol,order_id:order.order_id})}),`${order.symbol} Demo emri iptal edildi.`)
  const cancelAlgo = (order:DemoAlgoOrder) => runAction(() => apiCall('/algo/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:order.symbol,algo_id:order.algo_id})}),`${order.symbol} koşullu Demo emri iptal edildi.`)
  const closePosition = (position:DemoPosition) => {
    setConfirmationText('')
    setConfirmation({
      title:`${position.symbol} Demo pozisyonunu kapat`,
      message:'Bu yalnızca Binance Demo/Testnet üzerinde reduce-only kapatma işlemidir.',
      expected:'DEMO KAPAT',
      checkbox:false,
      action:() => runAction(() => apiCall('/position/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:position.symbol,position_side:position.position_side || 'BOTH',confirmation:'DEMO KAPAT'})}),`${position.symbol} için reduce-only Demo kapatma emri gönderildi.`),
    })
  }
  const emergency = () => {
    setConfirmationText('')
    setConfirmation({
      title:'Acil Demo durdurmayı onayla',
      message:'Demo emirleri iptal edilecek ve Demo pozisyonları kapatılacaktır.',
      expected:'DEMO ACİL DURDUR',
      checkbox:false,
      action:() => runAction(() => apiCall('/emergency',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation:'DEMO ACİL DURDUR',close_positions:true})}),'Acil Demo durdurma tamamlandı; giriş kilidi kapandı.'),
    })
  }
  const confirmAction = () => {
    if (!confirmation || (confirmation.checkbox ? !confirmationChecked : confirmationText.trim().toUpperCase() !== confirmation.expected)) return
    const action = confirmation.action
    setConfirmation(null)
    setConfirmationText('')
    setConfirmationChecked(false)
    void action()
  }

  const runV21 = async (action:() => Promise<V21Summary|unknown>,success:string) => {
    setV21Busy(true);setMessageKind('info');setMessage('V21 Demo güvenlik kapıları doğrulanıyor…')
    try {
      const result = await action()
      if (result && typeof result === 'object' && 'settings' in result) {
        const summary = result as V21Summary;setV21(summary);setSettingsDraft(normalizeV21Settings(summary.settings))
      } else await refreshV21(true)
      setMessage(success);setMessageKind('ok')
    } catch (error) { setMessage(error instanceof Error ? error.message : 'V21 işlemi tamamlanamadı.');setMessageKind('error') }
    finally { setV21Busy(false) }
  }

  const saveSettings = () => {
    if (!settingsDraft) return
    const safeSettings = normalizeV21Settings(settingsDraft)
    setSettingsDraft(safeSettings)
    runV21(() => v21Call<V21Summary>('/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({...safeSettings,scan_seconds:SCAN_INTERVAL_SECONDS})}),'Risk, yön ve otomasyon sınırları yerel V21 kasasına kaydedildi.')
  }
  const calculateRisk = () => {
    if (!analysis || analysis.entry <= 0 || analysis.stop_loss <= 0) { setMessage('Önce seçili paritenin analiz planını bekleyin.');setMessageKind('error');return }
    setV21Busy(true)
    v21Call<V21RiskPreview>('/risk/size',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol,entry:analysis.entry,stop:analysis.stop_loss,max_loss_usdt:numberValue(riskLoss),leverage:2})})
      .then(payload => {setRiskPreview(payload);setMessage('Maksimum kayba göre Demo pozisyon boyutu hesaplandı.');setMessageKind('ok')})
      .catch(error => {setMessage(error instanceof Error ? error.message : 'Risk hesabı yapılamadı.');setMessageKind('error')})
      .finally(() => setV21Busy(false))
  }
  const toggleAuto = () => {
    if (v21?.auto.enabled) {
      runV21(() => v21Call<V21Summary>('/auto/stop',{method:'POST'}),'Yeni otomatik Demo girişleri durduruldu; mevcut korumalar açık.')
      return
    }
    if (!status?.configured) {
      setMessage('Demo API bağlantısı yok. Önce demo/testnet credential kaydedin ve doğrulayın.'); setMessageKind('error'); return
    }
    const confirmation = (autoConfirm || 'DEMO OTOMATİK').trim()
    runV21(() => v21Call<V21Summary>('/auto/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation})}),'Kontrollü V21 Demo otomasyonu başlatıldı.')
  }
  const runBacktest = () => runV21(
    async () => { const result = await v21Call<V21Backtest>('/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:backtestSymbol,interval:'15m',limit:1000})});await refreshV21(true);return result },
    `${backtestSymbol} kronolojik backtest tamamlandı; ücret ve kayma düşüldü.`,
  )
  const loadHistory = () => {
    setV21Busy(true)
    v21Call<{orders:Record<string,unknown>[];algo_orders:Record<string,unknown>[];trades:Record<string,unknown>[]}>(`/history/${symbol}`)
      .then(payload => {setHistoryPayload(payload);setMessage(`${symbol} Demo emir ve dolum geçmişi getirildi.`);setMessageKind('ok')})
      .catch(error => {setMessage(error instanceof Error ? error.message : 'Demo geçmişi alınamadı.');setMessageKind('error')})
      .finally(() => setV21Busy(false))
  }
  const runDrill = (kind:'RECONNECT'|'EMERGENCY'|'PROTECTION') => runV21(
    () => v21Call('/drill',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind})}),
    `${kind} tatbikatı tamamlandı; hiçbir gerçek emir gönderilmedi.`,
  )
  const enableNotifications = async () => {
    if (!('Notification' in window)) { setMessage('Bu tarayıcı masaüstü bildirimini desteklemiyor.');setMessageKind('error');return }
    const permission = await Notification.requestPermission()
    setMessage(permission === 'granted' ? 'Masaüstü Demo bildirimleri açıldı.' : 'Bildirim izni verilmedi.');setMessageKind(permission === 'granted' ? 'ok' : 'error')
  }

  const performanceSummary = useMemo(() => {
    const realized = v21?.daily.realized_pnl ?? 0
    const backtest = v21?.backtest
    const equity = backtest?.ending_equity ?? 1000
    const returnPct = backtest ? ((backtest.net_pnl / Math.max(1, equity)) * 100) : 0
    const winRate = backtest?.win_rate ?? 0
    const maxDd = backtest?.max_drawdown_pct ?? 0
    const pf = backtest?.profit_factor ?? 0
    return {
      realized,
      returnPct,
      winRate,
      maxDd,
      pf,
      tradeCount: backtest?.trades ?? 0,
      status: realized >= 0 ? 'Profit' : 'Risk',
    }
  }, [v21])

  const entryValue = Number(form.limitPrice) || Number(analysis?.entry) || 0

  const cockpitSummary = useMemo(() => {
    const latestJournal = v21?.journal?.[0]
    const balance = Number(account?.wallet_balance ?? v21?.account.wallet_balance ?? 0)
    const available = Number(account?.available_balance ?? v21?.account.available_balance ?? 0)
    const unrealized = Number(account?.unrealized_pnl ?? v21?.account.unrealized_pnl ?? 0)
    const dailyPnL = Number(v21?.daily.realized_pnl ?? 0)
    const positions = Number(account?.positions.length ?? v21?.account.positions ?? 0)
    const riskBudget = Number(v21?.daily.remaining_loss_budget)
    const dailyRiskUsage = Number(v21?.settings.daily_loss_limit ?? 0) > 0 && Number.isFinite(riskBudget)
      ? Math.max(0, Number(v21?.settings.daily_loss_limit ?? 0) - riskBudget)
      : NaN

    return [
      { label: 'Total Balance', value: Number.isFinite(balance) && balance > 0 ? `${fmt(balance)} USDT` : <EmptyState className="v21EmptyStateCompact"/> },
      { label: 'Available Balance', value: Number.isFinite(available) && available >= 0 ? `${fmt(available)} USDT` : <EmptyState className="v21EmptyStateCompact"/> },
      { label: 'Daily PnL', value: Number.isFinite(dailyPnL) ? `${dailyPnL >= 0 ? '+' : ''}${fmt(dailyPnL)} USDT` : <EmptyState className="v21EmptyStateCompact"/> },
      { label: 'Total / Unrealized PnL', value: Number.isFinite(unrealized) ? `${unrealized >= 0 ? '+' : ''}${fmt(unrealized)} USDT` : <EmptyState className="v21EmptyStateCompact"/> },
      { label: 'Open Positions', value: Number.isFinite(positions) ? `${positions}` : <EmptyState className="v21EmptyStateCompact"/> },
      { label: 'Daily Risk Usage', value: Number.isFinite(dailyRiskUsage) ? `${fmt(dailyRiskUsage)} USDT` : <EmptyState className="v21EmptyStateCompact"/> },
      { label: 'Bot Status', value: v21?.auto.enabled ? 'ACTIVE' : (v21 ? 'OFFLINE' : <EmptyState className="v21EmptyStateCompact"/>) },
      { label: 'Last Trade', value: latestJournal ? `${latestJournal.kind} · ${stamp(latestJournal.created_at)}` : <EmptyState className="v21EmptyStateCompact"/> },
      { label: 'Active Strategy / Scanner State', value: analysis?.direction ? `${analysis.direction} · ${v21?.scanner.scan_status || 'BEKLENİYOR'}` : (v21?.scanner.scan_status || <EmptyState className="v21EmptyStateCompact"/>) },
    ]
  }, [account, analysis, v21])

  const systemHealthNodes = useMemo(() => {
    const apiState = status?.connected ? 'HEALTHY' : (status ? 'DEGRADED' : 'UNKNOWN')
    const marketDataState = v21?.scanner.last_scan_at ? 'HEALTHY' : (v21?.scanner.last_error ? 'OFFLINE' : 'UNKNOWN')
    const scannerState = v21?.scanner.scan_status ? (v21.scanner.last_error ? 'DEGRADED' : 'HEALTHY') : 'UNKNOWN'
    const strategyState = analysis?.direction ? 'HEALTHY' : (v21?.auto.last_decision ? 'DEGRADED' : 'UNKNOWN')
    const riskState = v21?.settings.daily_loss_limit && Number(v21.daily.remaining_loss_budget) >= 0 ? 'HEALTHY' : 'UNKNOWN'
    const executionState = status?.armed !== undefined ? (status.armed ? 'HEALTHY' : 'DEGRADED') : 'UNKNOWN'
    const botState = v21?.auto.enabled ? 'HEALTHY' : (v21 ? 'OFFLINE' : 'UNKNOWN')

    return [
      { name: 'API', state: apiState, lastUpdate: status?.last_checked || v21?.stream.last_sync || null, detail: status?.last_error || v21?.stream.last_error || 'No API error reported.' },
      { name: 'MARKET DATA', state: marketDataState, lastUpdate: v21?.scanner.last_scan_at || null, detail: v21?.scanner.last_error || 'Live market data availability is current.' },
      { name: 'SCANNER', state: scannerState, lastUpdate: v21?.scanner.next_scan_at || v21?.scanner.last_scan_at || null, detail: v21?.scanner.last_error || v21?.scanner.scan_status || 'Scanner has not yet produced a result.' },
      { name: 'STRATEGY / ANALYSIS', state: strategyState, lastUpdate: analysis ? new Date().toISOString() : null, detail: analysis ? `${analysis.direction || 'UNSPECIFIED'} signal is available.` : 'No live strategy signal present.' },
      { name: 'RISK ENGINE', state: riskState, lastUpdate: v21?.last_saved || v21?.daily.date || null, detail: v21?.daily.remaining_loss_budget !== undefined ? `Remaining risk budget ${fmt(v21.daily.remaining_loss_budget)} USDT.` : 'Risk usage is not available.' },
      { name: 'EXECUTION', state: executionState, lastUpdate: account?.positions?.[0]?.opened_at || status?.last_checked || null, detail: status?.armed ? 'Execution system is armed for demo orders.' : 'Execution arm is not active.' },
      { name: 'BOT / AUTOMATION', state: botState, lastUpdate: v21?.auto.last_scan || null, detail: v21?.auto.last_error || v21?.auto.last_decision || 'No automation flow has produced a decision yet.' },
    ]
  }, [account, analysis, status, v21])

  const smartAlerts = useMemo(() => {
    const alerts: Array<{ severity:'INFO'|'WARNING'|'CRITICAL'; timestamp:string; source:string; message:string }> = []

    if (status?.last_error) {
      alerts.push({ severity: 'CRITICAL', timestamp: status.last_checked || new Date().toISOString(), source: 'API', message: status.last_error })
    }
    if (v21?.stream.last_error) {
      alerts.push({ severity: 'WARNING', timestamp: v21.stream.last_sync || new Date().toISOString(), source: 'STREAM', message: v21.stream.last_error })
    }
    if (v21?.scanner.last_error) {
      alerts.push({ severity: 'WARNING', timestamp: v21.scanner.last_scan_at || new Date().toISOString(), source: 'SCANNER', message: v21.scanner.last_error })
    }
    if (v21 && !v21.auto.enabled) {
      alerts.push({ severity: 'INFO', timestamp: v21.auto.last_scan || new Date().toISOString(), source: 'AUTOMATION', message: 'Automation is currently stopped.' })
    }
    if (v21 && Number(v21.daily.remaining_loss_budget) <= 0) {
      alerts.push({ severity: 'CRITICAL', timestamp: v21.last_saved || new Date().toISOString(), source: 'RISK', message: 'Daily loss budget has been exhausted.' })
    } else if (v21 && Number(v21.daily.remaining_loss_budget) > 0 && Number(v21.daily.remaining_loss_budget) <= Number(v21.settings.daily_loss_limit) * 0.2) {
      alerts.push({ severity: 'WARNING', timestamp: v21.last_saved || new Date().toISOString(), source: 'RISK', message: 'Risk budget is approaching its daily limit.' })
    }
    if (account && account.positions.length >= (v21?.settings.max_positions ?? 0) && (v21?.settings.max_positions ?? 0) > 0) {
      alerts.push({ severity: 'WARNING', timestamp: new Date().toISOString(), source: 'POSITION', message: `Open position count is near the configured limit (${account.positions.length}/${v21?.settings.max_positions ?? 0}).` })
    }
    if (v21?.journal?.length && v21.journal[0]?.realized_pnl !== null && v21.journal[0].realized_pnl !== undefined && v21.journal[0].realized_pnl < 0) {
      alerts.push({ severity: 'INFO', timestamp: v21.journal[0].created_at, source: 'JOURNAL', message: `Last trade closed with ${fmt(v21.journal[0].realized_pnl)} USDT.` })
    }

    return alerts.slice(0, 6)
  }, [account, status, v21])

  const autoStatus = v21?.auto.status || (v21?.auto.enabled ? 'ON' : 'OFF')
  const autoStatusLabel = autoStatus === 'PAUSED'
    ? ({DAILY_LOSS_20:'GÜNLÜK ZARAR LİMİTİ',CONSECUTIVE_LOSSES:'3 ARDIŞIK ZARAR',KILL_SWITCH:'KILL SWITCH'} as Record<string,string>)[v21?.auto.pause_reason || ''] || 'RİSK NEDENİYLE DURAKLATILDI'
    : autoStatus === 'ON' ? (v21?.scanner.running ? 'TARAMA YAPIYOR' : 'AKTİF') : 'DURDURULDU'

  const autoActivityCenter = useMemo(() => {
    const auto = (v21?.auto ?? {}) as Partial<V21Summary['auto']>
    const scanner = (v21?.scanner ?? {}) as Partial<ScannerState>
    const journal = v21?.journal ?? []
    const autoTrades = v21?.automation_trades ?? []
    const topCandidates = scanner.top_candidates ?? []
    const lastAutoTrade = autoTrades[0] ?? null
    const lastOrderJournal = [...journal].find(item => item.kind === 'AUTO_ORDER' || item.source === 'AUTO_SCANNER' || String(item.reason || '').startsWith('AUTO_ORDER')) ?? null
    const lastAutoOrder = lastAutoTrade ? {
      time: lastAutoTrade.entry_time ?? lastOrderJournal?.created_at ?? null,
      symbol: lastAutoTrade.symbol ?? lastOrderJournal?.symbol ?? null,
      side: lastAutoTrade.side ?? lastOrderJournal?.side ?? null,
      result: String(lastAutoTrade.status ?? '').toUpperCase().includes('CLOSED') || String(lastAutoTrade.status ?? '').toUpperCase().includes('KAPANDI') ? 'REJECTED' : auto.last_error ? 'ERROR' : 'SUCCESS',
      error: auto.last_error || lastAutoTrade.trade_reason?.join(' · ') || lastOrderJournal?.message || '—',
    } : {
      time: lastOrderJournal?.created_at ?? null,
      symbol: lastOrderJournal?.symbol ?? null,
      side: lastOrderJournal?.side ?? null,
      result: auto.last_error ? 'ERROR' : 'REJECTED',
      error: auto.last_error || lastOrderJournal?.message || 'Henüz Auto Trade emri açılmadı.',
    }

    const openAutoPositions = (v21?.automation_trades ?? []).filter(trade => {
      const status = String(trade.status ?? '').toUpperCase()
      return Boolean(trade.symbol) && !['KAPANDI', 'CLOSED', 'İPTAL', 'CANCELLED'].includes(status)
    })

    const reasons: string[] = []
    const rejectionGate = String(auto.rejection_gate || '').toUpperCase()
    const rejectionReason = String(auto.rejection_reason || auto.last_decision || '').trim()
    const decision = String(auto.last_decision || '').trim()

    if (Number(scanner.eligible_count ?? 0) === 0 || !topCandidates.length) reasons.push('uygun aday yok')
    if (/CONFIDENCE|LOW|GÜVEN/.test(rejectionGate) || /confidence|güven/i.test(rejectionReason) || /LOW/i.test(decision)) reasons.push('confidence düşük')
    if (rejectionGate.includes('BEKLE') || /BEKLE/i.test(rejectionReason) || /BEKLE/i.test(decision)) reasons.push('BEKLE')
    if (rejectionGate.includes('RISK') || /risk|zarar|LIMIT/i.test(rejectionReason) || /risk/i.test(decision)) reasons.push('risk limiti')
    if (rejectionGate.includes('MAX') || /MAXIMUM|POZİSYON|POSITION/.test(rejectionReason) || /POZİSYON/.test(decision)) reasons.push('maksimum pozisyon sayısı')
    if (rejectionGate.includes('DUPLICATE') || /duplicate|aynı|duble|duplicate symbol/i.test(rejectionReason)) reasons.push('duplicate symbol')
    if (rejectionGate.includes('RISK_LEVELS') || /risk_reward|rr|R\/R|risk\/reward/i.test(rejectionReason)) reasons.push('risk/reward uygun değil')
    if (rejectionGate && !reasons.length) reasons.push(rejectionGate.replace(/_/g, ' ').toLowerCase())
    if (!reasons.length && auto.last_decision && !auto.enabled && !lastAutoTrade) reasons.push('Neden bilgisi backend tarafından raporlanmadı')
    if (!reasons.length && !auto.rejection_reason && !auto.last_error && !lastAutoTrade) reasons.push('Neden bilgisi backend tarafından raporlanmadı')

    const recentEvents = [...journal]
      .filter(item => {
        const kind = String(item.kind || '').toUpperCase()
        const reason = String(item.reason || '').toUpperCase()
        const message = String(item.message || '').toUpperCase()
        const eventList = ['SCAN_STARTED', 'SCAN_COMPLETED', 'CANDIDATES_FOUND', 'CANDIDATE_REJECTED', 'AUTO_ORDER_ATTEMPT', 'AUTO_ORDER_SUCCESS', 'AUTO_ORDER_REJECTED', 'POSITION_OPENED', 'POSITION_CLOSED', 'SL', 'TP1', 'TP2', 'TP3', 'BREAK_EVEN', 'TRAILING', 'ERROR', 'PAUSED', 'AUTO_START', 'AUTO_STOP', 'NOTIFICATION']
        if (eventList.includes(kind)) return true
        if (eventList.some(token => reason.includes(token) || message.includes(token))) return true
        return item.source === 'AUTO_SCANNER' || item.source === 'USER' || item.source === 'NOTIFICATION'
      })
      .slice(0, 20)
      .map(item => ({
        time: item.created_at,
        kind: String(item.kind || item.reason || 'EVENT').toUpperCase(),
        symbol: item.symbol || null,
        message: item.message || item.reason || 'Olay kaydedildi.',
      }))

    return {
      botStatus: auto.enabled ? (auto.status === 'PAUSED' ? 'DURDURULDU' : 'AKTİF') : (auto.last_error ? 'HATA' : 'DURDURULDU'),
      heartbeat: auto.started_at || auto.last_scan || scanner.last_scan_at || auto.last_decision || null,
      lastScan: scanner.last_scan_at || null,
      nextScan: scanner.next_scan_at || null,
      scanStatus: scanner.scan_status || 'BEKLEMEDE',
      coinsScanned: Number(scanner.coins_scanned ?? 0),
      eligibleCount: Number(scanner.eligible_count ?? scanner.selected_count ?? 0),
      topCandidates,
      lastAutoOrder,
      openAutoPositions,
      reasons: reasons.length ? reasons : ['Neden bilgisi backend tarafından raporlanmadı'],
      recentEvents,
    }
  }, [v21])

  const formatHealthState = (state: string) => {
    if (state === 'HEALTHY') return 'healthy'
    if (state === 'DEGRADED') return 'degraded'
    if (state === 'OFFLINE') return 'offline'
    return 'unknown'
  }

  const formatRelativeTime = (value?: string | null) => {
    if (!value) return 'Veri bekleniyor'
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return 'Veri bekleniyor'
    const deltaMs = Date.now() - date.getTime()
    const minutes = Math.max(0, Math.floor(deltaMs / 60000))
    if (minutes < 1) return 'just now'
    if (minutes < 60) return `${minutes}m ago`
    const hours = Math.floor(minutes / 60)
    if (hours < 24) return `${hours}h ago`
    const days = Math.floor(hours / 24)
    return `${days}d ago`
  }
  const stopValue = Number(form.stop) || Number(analysis?.stop_loss) || 0
  const tp1Value = Number(form.tp1) || Number(analysis?.tp1) || 0
  const tp3Value = Number(form.tp3) || Number(analysis?.tp3) || 0
  const riskAmount = Math.abs(entryValue - stopValue)
  const rewardAmount = Math.abs(tp1Value - entryValue)
  const rewardRatio = riskAmount > 0 ? (rewardAmount / riskAmount).toFixed(2) : '—'
  const fallbackPositionSize = numberValue(form.margin) * Number(form.leverage)
  const displayedPositionSize = Number(orderPreview?.notional_usdt ?? fallbackPositionSize)
  const displayedRisk = Number(orderPreview?.risk_per_trade ?? (displayedPositionSize * riskAmount / Math.max(entryValue, 1e-12)))

  return <section className="binanceDemoDeck" aria-label="Binance Futures Demo Köprüsü">
    <section className="demoHero">
      <div className="demoHeroCopy"><span>V21 · DEMO COMPLETE · TEK PAKET</span><h2>Binance Futures Demo Komuta Merkezi</h2><p>İşlem masası, risk kasası, canlı günlük, kontrollü otomasyon, kanıtlı backtest ve Demo sertifikası ayrı sekmelerde.</p><div><b><ShieldCheck/> DEMO ONLY</b><span>{status?.rest_host || 'https://demo-fapi.binance.com'}</span></div></div>
      <div className="demoHeroStatus">
        <span className={status?.configured ? 'demoOk' : 'demoWait'}><LockKeyhole/><small>ANAHTAR</small><b>{status?.configured ? 'KASADA HAZIR' : 'KASA BEKLİYOR'}</b></span>
        <span className={status?.connected ? 'demoOk' : 'demoWait'}><Radio/><small>DEMO API</small><b>{status?.connected ? 'BAĞLI' : 'BAĞLI DEĞİL'}</b></span>
        <span className={status?.armed ? 'demoArmed' : 'demoSafe'}>{status?.armed ? <UnlockKeyhole/> : <LockKeyhole/>}<small>EMİR KİLİDİ</small><b>{status?.armed ? `${Math.floor(armSeconds/60)}:${String(armSeconds%60).padStart(2,'0')}` : 'KAPALI'}</b></span>
      </div>
    </section>

    <nav className="v21Tabs" aria-label="V21 çalışma alanları">
      <button className={tab === 'trade' ? 'active' : ''} onClick={() => setTab('trade')}><Crosshair/><span><b>İŞLEM MASASI</b><small>Emir · Grafik · Pozisyon</small></span></button>
      <button className={tab === 'risk' ? 'active' : ''} onClick={() => setTab('risk')}><Gauge/><span><b>RİSK KASASI</b><small>Limit · Boyut · Stop</small></span></button>
      <button className={tab === 'journal' ? 'active' : ''} onClick={() => setTab('journal')}><ClipboardList/><span><b>CANLI GÜNLÜK</b><small>Dolum · Kapanış · Neden</small></span></button>
      <button className={tab === 'auto' ? 'active' : ''} onClick={() => setTab('auto')}><Zap/><span><b>OTOMASYON</b><small>İzin listesi · Kapılar</small></span></button>
      <button className={tab === 'backtest' ? 'active' : ''} onClick={() => setTab('backtest')}><BarChart3/><span><b>BACKTEST LAB</b><small>Ücret · Kayma · 3 dönem</small></span></button>
      <button className={tab === 'performance' ? 'active' : ''} onClick={() => setTab('performance')}><BarChart3/><span><b>PERFORMANS</b><small>PnL · Win rate · Drawdown</small></span></button>
      <button className={tab === 'certificate' ? 'active' : ''} onClick={() => setTab('certificate')}><ShieldCheck/><span><b>SERTİFİKA</b><small>Sağlık · Tatbikat · Kanıt</small></span></button>
    </nav>

    <section className="v21Pulse">
      <span><i className={v21?.stream.status === 'CANLI' ? 'on' : ''}/><small>AKIŞ</small><b>{v21?.stream.status || 'BEKLENİYOR'}</b></span>
      <span><small>OTOMASYON</small><b>{v21?.auto.enabled ? 'ÇALIŞIYOR' : 'KAPALI'}</b></span>
      <span><small>GÜNLÜK DEMO</small><b>{v21?.daily.auto_entries ?? 0} / {v21?.settings.daily_trade_limit ?? 6}</b></span>
      <span><small>RİSK BÜTÇESİ</small><b>{fmt(v21?.daily.remaining_loss_budget)} USDT</b></span>
      <span><small>DEMO KANIT</small><b>%{v21?.certificate.score ?? 0}</b></span>
      <strong>GERÇEK PARA: 0 USDT · GERÇEK EMİR KANALI YOK</strong>
    </section>

    <section className="v21Card v21AutoBotDashboard" aria-label="Auto Trade Bot">
      <header><div><span>DEMO / TESTNET ONLY</span><h2>AUTO TRADE BOT</h2></div><b className={autoStatus === 'ON' ? 'v21Running' : autoStatus === 'PAUSED' ? 'demoLoss' : 'v21Stopped'}>{autoStatusLabel}</b></header>
      <div className="v21AutoBotGrid">
        <div><small>AUTO TRADE</small><strong>{v21?.auto.enabled ? 'ON' : 'OFF'}</strong><button className={v21?.auto.enabled ? 'stop' : ''} disabled={v21Busy || (!v21?.auto.enabled && !status?.armed)} onClick={toggleAuto}>{v21?.auto.enabled ? 'DURDUR' : 'AÇ'}</button></div>
        <div><small>TARAMA</small><strong>~{v21?.scanner.coins_scanned || 100} COIN</strong><span>{v21?.scanner.last_scan_at ? `Son ${stamp(v21.scanner.last_scan_at)}` : 'Bekleniyor'}</span></div>
        <div><small>SONRAKİ TARAMA</small><strong>{stamp(v21?.scanner.next_scan_at)}</strong><span>15 dakika</span></div>
        <div><small>UYGUN ADAY</small><strong>{v21?.scanner.eligible_count ?? 0}</strong><span>En iyi {v21?.scanner.top_candidates.length ?? 0}/3</span></div>
        <div><small>AÇIK AUTO İŞLEM</small><strong>{v21?.account.auto_positions ?? 0}/3</strong><span>Manual işlemler ayrı korunur</span></div>
        <div><small>GÜNLÜK RİSK</small><strong>%{fmt(v21?.risk?.daily_loss_pct ?? 0)} / %20</strong><span>Son uyarı %{fmt(v21?.risk?.last_warning_pct ?? 0)}</span></div>
        <div><small>ARDIŞIK ZARAR</small><strong>{v21?.risk?.consecutive_losses ?? 0}/3</strong><span>{v21?.auto.pause_reason || 'Koruma açık'}</span></div>
        <div><small>BİLDİRİM</small><strong>{v21?.notifications?.unread ?? 0}</strong><span>Demo event</span></div>
      </div>
      <div className="v21AutoBotCandidates">{v21?.scanner.top_candidates.length ? v21.scanner.top_candidates.slice(0,3).map((candidate,index) => <span key={candidate.symbol}><b>{index + 1}. {candidate.symbol}</b><em>{candidate.direction} · {candidate.score}</em></span>) : <span>Sinyal bekleniyor.</span>}</div>
    </section>

    <section className="v21AutoActivityCenter" aria-label="Auto Trade activity center">
      <header className="v21WorkspaceHead compactHead">
        <div>
          <span>AUTO TRADE DIAGNOSTICS</span>
          <h2>AUTO TRADE AKTİVİTE MERKEZİ</h2>
        </div>
        <b className={autoActivityCenter.botStatus === 'AKTİF' ? 'v21Running' : ['HATA','DURDURULDU'].includes(autoActivityCenter.botStatus) ? 'demoLoss' : 'v21Stopped'}>{autoActivityCenter.botStatus}</b>
      </header>
      <div className="v21AutoActivityGrid">
        <article className="v21AutoActivityCard">
          <header><span>BOT DURUMU</span><h3>Auto Trade durumu</h3></header>
          <div className="v21AutoActivityMetricRow">
            <div><small>DURUM</small><b className={autoActivityCenter.botStatus === 'AKTİF' ? 'demoProfit' : ['HATA','DURDURULDU'].includes(autoActivityCenter.botStatus) ? 'demoLoss' : ''}>{autoActivityCenter.botStatus}</b></div>
            <div><small>SON HEARTBEAT</small><b>{autoActivityCenter.heartbeat ? stamp(autoActivityCenter.heartbeat) : '—'}</b></div>
          </div>
          <p className="v21AutoActivityNote">{v21?.auto.last_decision || 'Yeni otomasyon kararı bekleniyor.'}</p>
        </article>

        <article className="v21AutoActivityCard">
          <header><span>TARAMA DURUMU</span><h3>Scanner state</h3></header>
          <div className="v21AutoActivityMetricRow">
            <div><small>SON TARAMA</small><b>{autoActivityCenter.lastScan ? stamp(autoActivityCenter.lastScan) : '—'}</b></div>
            <div><small>SONRAKİ TARAMA</small><b>{autoActivityCenter.nextScan ? stamp(autoActivityCenter.nextScan) : '—'}</b></div>
          </div>
          <div className="v21AutoActivityMetricRow smallGrid">
            <div><small>DURUM</small><b>{autoActivityCenter.scanStatus === 'TAMAMLANDI' ? 'COMPLETED' : autoActivityCenter.scanStatus === 'TARAMA' ? 'RUNNING' : autoActivityCenter.scanStatus === 'HATA' ? 'ERROR' : 'BEKLEMEDE'}</b></div>
            <div><small>TARANAN COIN</small><b>{autoActivityCenter.coinsScanned}</b></div>
            <div><small>GEÇERLİ ADAY</small><b>{autoActivityCenter.eligibleCount}</b></div>
          </div>
        </article>

        <article className="v21AutoActivityCard">
          <header><span>SON SEÇİM</span><h3>Top 3 candidate</h3></header>
          {autoActivityCenter.topCandidates.length ? <div className="v21AutoActivityCandidates">{autoActivityCenter.topCandidates.slice(0, 3).map(candidate => <article key={candidate.symbol} className="v21AutoActivityCandidate">
            <header><strong>{candidate.symbol}</strong><span className={candidate.direction === 'LONG' ? 'v21AutoLong' : 'v21AutoShort'}>{candidate.direction}</span></header>
            <div className="v21AutoActivityCandidateMeta">
              <span><small>SCORE</small><b>{candidate.score}</b></span>
              <span><small>GÜVEN</small><b>{candidate.confidence}</b></span>
            </div>
            <div className="v21AutoActivityCandidateLevels">
              <span><small>ENTRY</small><b>{fmt(candidate.entry)}</b></span>
              <span className="v21StopMetric"><small>STOP</small><b>{fmt(candidate.stop_loss)}</b></span>
              <span><small>TP1</small><b>{fmt(candidate.tp1)}</b></span>
              <span><small>TP2</small><b>{fmt(candidate.tp2)}</b></span>
              <span><small>TP3</small><b>{fmt(candidate.tp3)}</b></span>
            </div>
          </article>)}</div> : <EmptyState label="Uygun işlem adayı yok" detail="Scanner yeni bir sinyal bekliyor."/>}
        </article>

        <article className="v21AutoActivityCard">
          <header><span>EMİR DURUMU</span><h3>Auto order status</h3></header>
          {autoActivityCenter.lastAutoOrder && autoActivityCenter.lastAutoOrder.time ? <div className="v21AutoActivityMetricRow">
            <div><small>SON AUTO EMİR</small><b>{stamp(autoActivityCenter.lastAutoOrder.time)}</b></div>
            <div><small>SEMBOL</small><b>{autoActivityCenter.lastAutoOrder.symbol || '—'}</b></div>
          </div> : <EmptyState label="Henüz Auto Trade emri yok" detail="İlk onaylı işlem burada görünecek."/>}
          {autoActivityCenter.lastAutoOrder && autoActivityCenter.lastAutoOrder.time ? <div className="v21AutoActivityMetricRow smallGrid">
            <div><small>YÖN</small><b>{autoActivityCenter.lastAutoOrder.side || '—'}</b></div>
            <div><small>SONUÇ</small><b className={autoActivityCenter.lastAutoOrder.result === 'SUCCESS' ? 'demoProfit' : autoActivityCenter.lastAutoOrder.result === 'REJECTED' || autoActivityCenter.lastAutoOrder.result === 'ERROR' ? 'demoLoss' : ''}>{autoActivityCenter.lastAutoOrder.result || <EmptyState className="v21EmptyStateCompact"/>}</b></div>
            <div className="fullRow"><small>HATA / AÇIKLAMA</small><b>{autoActivityCenter.lastAutoOrder.error || '—'}</b></div>
          </div> : null}
        </article>

        <article className="v21AutoActivityCard wideCard">
          <header><span>POZİSYONLAR</span><h3>Open AUTO positions</h3></header>
          <div className="v21AutoActivityMetricRow">
            <div><small>AÇIK AUTO POZİSYON</small><b>{autoActivityCenter.openAutoPositions.length}</b></div>
          </div>
          {autoActivityCenter.openAutoPositions.length ? <ul className="v21AutoActivityPositionList">{autoActivityCenter.openAutoPositions.map(trade => <li key={`${trade.symbol}-${trade.side}`}><span>{trade.symbol}</span><strong>{trade.side}</strong><em className={Number(v21?.account.unrealized_pnl ?? 0) >= 0 ? 'demoProfit' : 'demoLoss'}>{Number(v21?.account.unrealized_pnl ?? 0) >= 0 ? '+' : ''}{fmt(Number(v21?.account.unrealized_pnl ?? 0))} USDT</em></li>)}</ul> : <EmptyState label="Açık AUTO pozisyon yok" detail="Yeni pozisyon açıldığında burada görünecek."/>}
        </article>

        <article className="v21AutoActivityCard wideCard">
          <header><span>ÖNCEKİ SEBEP DİYAGRAMI</span><h3>Why no trade?</h3></header>
          <ul className="v21AutoActivityReasonList">{autoActivityCenter.reasons.map(reason => <li className={/stop|loss|kayıp|reject|error|engell|paused|durdu/i.test(reason) ? 'v21CriticalText' : ''} key={reason}>{reason}</li>)}</ul>
        </article>

        <article className="v21AutoActivityCard fullWidthCard">
          <header><span>SON 20 AUTO TRADE OLAYI</span><h3>Time-ordered activity stream</h3></header>
          {autoActivityCenter.recentEvents.length ? <ul className="v21AutoActivityEventList">{autoActivityCenter.recentEvents.map((event, index) => <li className={/stop|loss|sl|reject|error|paused|durdur|kayıp/i.test(`${event.kind} ${event.message}`) ? 'v21CriticalEvent' : ''} key={`${event.kind}-${event.time ?? index}`}><time>{event.time ? stamp(event.time) : '—'}</time><strong>{event.kind}</strong><span>{event.symbol || 'SYSTEM'}</span><small>{event.message}</small></li>)}</ul> : <EmptyState label="Henüz Auto Trade olayı yok" detail="İlk otomasyon olayı burada görünecek."/>}
        </article>
      </div>
    </section>

    <section className="v21CockpitSummary" aria-label="Paper Trading Cockpit Summary">
      <header className="v21WorkspaceHead compactHead">
        <div>
          <span>TRADING COMMAND CENTER</span>
          <h2>Paper Trading Cockpit</h2>
          <p>Live account intelligence, execution readiness and risk context from the current Demo state.</p>
        </div>
        <div className="v21CommandMeta">
          <b><Radio/> {status?.connected ? 'API CONNECTED' : status ? 'API DEGRADED' : 'API UNKNOWN'}</b>
          <b><ShieldCheck/> DEMO ENVIRONMENT</b>
          <small>{status?.last_checked ? `Updated ${formatRelativeTime(status.last_checked)}` : 'Update unavailable'}</small>
        </div>
      </header>
      <div className="v21SectionKicker"><span>ACCOUNT INTELLIGENCE</span><small>Current account snapshot</small></div>
      <div className="v21CockpitGrid v21AccountGrid">
        {cockpitSummary.map(item => (
          <article key={item.label} className={`v21CockpitCard ${item.label.includes('PnL') ? 'pnlCard' : item.label === 'Bot Status' ? 'statusCard' : ''}`}>
            <small>{item.label}</small>
            <strong>{item.value}</strong>
          </article>
        ))}
      </div>
    </section>

    <section className="v21SystemHealthCenter" aria-label="System Health Center">
      <header className="v21WorkspaceHead compactHead">
        <div>
          <span>CONTROL CENTER</span>
          <h2>System Health</h2>
        </div>
        <b><Activity/> API → MARKET → SCANNER → STRATEGY → RISK → EXECUTION → BOT</b>
      </header>
      <div className="v21HealthFlow">
        {systemHealthNodes.map(node => (
          <article key={node.name} className={`v21HealthNode ${formatHealthState(node.state)}`}>
            <div className="v21HealthNodeHeader">
              <span className="v21HealthDot" />
              <strong>{node.name}</strong>
              <em>{node.state}</em>
            </div>
            <div className="v21HealthMeta">
              <small>STATUS</small>
              <b>{node.state}</b>
            </div>
            <div className="v21HealthMeta">
              <small>LAST UPDATE</small>
              <b>{node.lastUpdate ? formatRelativeTime(node.lastUpdate) : <span className="v21EmptyInline">Veri bekleniyor</span>}</b>
            </div>
            <p>{node.detail}</p>
          </article>
        ))}
      </div>
    </section>

    <section className="v21RiskRadar" aria-label="Risk Radar">
      <header className="v21WorkspaceHead compactHead">
        <div>
          <span>RISK POSTURE</span>
          <h2>Risk Radar</h2>
          <p>Only current position, budget and account values are shown. Missing values remain unavailable.</p>
        </div>
        <b><Gauge/> {v21?.daily.remaining_loss_budget !== undefined ? 'RISK DATA AVAILABLE' : 'RISK DATA UNKNOWN'}</b>
      </header>
      <div className="v21RiskRadarGrid">
        <article><small>OPEN POSITIONS</small><strong>{account ? `${account.positions.length}` : <EmptyState className="v21EmptyStateCompact"/>}</strong><span>{v21?.settings.max_positions ? `Limit ${v21.settings.max_positions}` : 'Limit unavailable'}</span></article>
        <article><small>EXPOSURE</small><strong>{account ? `${fmt(account.positions.reduce((total, position) => total + Math.abs(position.quantity * position.mark_price), 0))} USDT` : <EmptyState className="v21EmptyStateCompact"/>}</strong><span>Mark-price notional</span></article>
        <article><small>DAILY LOSS BUDGET</small><strong>{v21?.daily.remaining_loss_budget !== undefined ? `${fmt(v21.daily.remaining_loss_budget)} USDT` : <EmptyState className="v21EmptyStateCompact"/>}</strong><span>{v21?.daily.date || 'Date unavailable'}</span></article>
        <article><small>POSITION CONCENTRATION</small><strong>{account && account.positions.length && v21?.settings.max_positions ? `${Math.round((account.positions.length / v21.settings.max_positions) * 100)}%` : <EmptyState className="v21EmptyStateCompact"/>}</strong><span>Open positions vs limit</span></article>
      </div>
    </section>

    <section className="v21SmartAlerts" aria-label="Smart Alerts">
      <header className="v21WorkspaceHead compactHead">
        <div>
          <span>REAL EVENTS</span>
          <h2>Smart Alerts</h2>
        </div>
        <b><Bell/> EVENT-DRIVEN ALERTS</b>
      </header>
      <div className="v21AlertList">
        {smartAlerts.length ? smartAlerts.map((alert, index) => (
          <article key={`${alert.source}-${index}`} className={`v21Alert ${alert.severity.toLowerCase()}`}>
            <div className="v21AlertBadge">{alert.severity}</div>
            <div className="v21AlertBody">
              <strong>{alert.source}</strong>
              <span>{alert.message}</span>
            </div>
            <time>{formatRelativeTime(alert.timestamp)}</time>
          </article>
        )) : <EmptyState label="Henüz aktif uyarı yok" detail="Yeni bir olay oluştuğunda burada görünecek."/>}
      </div>
    </section>

    <section className="v21ActivityTimeline" aria-label="Live Activity Timeline">
      <header className="v21WorkspaceHead compactHead">
        <div>
          <span>LIVE ACTIVITY</span>
          <h2>Activity Timeline</h2>
        </div>
        <b><Activity/> BACKEND EVENTS ONLY</b>
      </header>
      <div className="v21ActivityList">
        {status?.events?.length ? status.events.slice(0, 4).map((event, index) => (
          <article key={`system-${event.created_at}-${index}`}><i className="system"/><div><strong>System event</strong><span>{event.message || event.kind}</span></div><time>{formatRelativeTime(event.created_at)}</time></article>
        )) : null}
        {v21?.scanner.last_scan_at ? <article><i className="scanner"/><div><strong>Scanner</strong><span>{v21.scanner.scan_status || 'Scan completed'}</span></div><time>{formatRelativeTime(v21.scanner.last_scan_at)}</time></article> : null}
        {v21?.auto.last_scan ? <article><i className="automation"/><div><strong>Automation</strong><span>{v21.auto.last_decision || (v21.auto.enabled ? 'Automation active' : 'Automation stopped')}</span></div><time>{formatRelativeTime(v21.auto.last_scan)}</time></article> : null}
        {v21?.journal?.slice(0, 3).map((entry, index) => <article key={`journal-${entry.created_at}-${index}`}><i className="journal"/><div><strong>Journal · {entry.kind}</strong><span>{entry.reason || 'Journal event recorded'}</span></div><time>{formatRelativeTime(entry.created_at)}</time></article>)}
        {!status?.events?.length && !v21?.scanner.last_scan_at && !v21?.auto.last_scan && !v21?.journal?.length && <EmptyState label="Canlı aktivite bekleniyor" detail="Son olay zamanı henüz alınmadı."/>}
      </div>
    </section>

    <section className="v21ScannerDashboard">
      <header className="v21ScannerHeader"><div><span>CANLI COIN TARAMA · DEMO MARKET DATA</span><h2>Scanner Durumu ve Fırsat Sıralaması</h2><p>Dinamik USDT perpetual evreni, 15m sinyal ve 1h/4h trend doğrulamasıyla her 10 dakikada yenilenir.</p></div><button disabled={scannerBusy} onClick={runScanner}><RefreshCw className={scannerBusy ? 'spin' : ''}/> {scannerBusy ? 'TARANIYOR' : 'ŞİMDİ TARA'}</button></header>
      <div className="v21ScannerMetrics"><span><small>DURUM</small><b>{v21?.scanner.scan_status || 'BEKLEMEDE'}</b></span><span><small>SON TARAMA</small><b>{stamp(v21?.scanner.last_scan_at)}</b></span><span><small>SONRAKİ TARAMA</small><b>{stamp(v21?.scanner.next_scan_at)}</b><em>{nextScanSeconds === null ? 'bekleniyor' : nextScanCountdown}</em></span><span><small>TARANAN COIN</small><b>{v21?.scanner.coins_scanned ?? 0}</b></span><span><small>SEÇİLEN FIRSAT</small><b>{v21?.scanner.selected_count ?? 0}</b></span><span><small>SÜRE</small><b>{fmt(v21?.scanner.scan_duration_seconds)} sn</b></span></div>
      {v21?.scanner.last_error && <div className="v21ScannerError"><TriangleAlert/> {v21.scanner.last_error}</div>}
      <div className="v21ScannerColumns"><article className="v21ScannerTop"><header><div><span>TOP 3</span><h3>Bugünün En İyi Fırsatları</h3></div><b>{v21?.scanner.top_candidates.length ?? 0}/3</b></header>{v21?.scanner.top_candidates.length ? v21.scanner.top_candidates.map(candidate => <div className="v21Candidate" key={candidate.symbol}><div className="v21CandidateHead"><b>#{candidate.rank} {candidate.symbol.replace('USDT','/USDT')}</b><strong>{candidate.direction}</strong><em>{candidate.score} / 100 · {candidate.confidence}</em></div><small>{candidate.trend} · {candidate.mtf_trend}</small><ul>{candidate.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul></div>) : <EmptyState label="Henüz uygun fırsat yok" detail="Yeni tarama bekleniyor." icon={<Target/>}/>}</article>
        <article className="v21ScannerTable"><header><div><span>TÜM SONUÇLAR</span><h3>Taranan Coinler</h3></div><b>{v21?.scanner.all_candidates.length ?? 0}</b></header><div className="v21TableScroll"><table><thead><tr><th>Rank</th><th>Coin</th><th>Score</th><th>Yön</th><th>Güven</th><th>Trend</th><th>Volume</th><th>Status</th></tr></thead><tbody>{v21?.scanner.all_candidates.length ? v21.scanner.all_candidates.map(candidate => <tr key={candidate.symbol}><td>#{candidate.rank}</td><td><b>{candidate.symbol}</b></td><td>{candidate.score}</td><td>{candidate.direction}</td><td>{candidate.confidence}</td><td>{candidate.mtf_trend}</td><td>{candidate.volume.toFixed(2)}x</td><td><em className={`scannerStatus scannerStatus-${candidate.status}`}>{candidate.status}</em></td></tr>) : <tr><td colSpan={8}>Henüz scanner sonucu yok.</td></tr>}</tbody></table></div></article></div>
      <article className="v21ScannerTrades"><header><div><span>GERÇEK BACKEND TRADE HISTORY · DEMO</span><h3>Otomasyon İşlemleri</h3></div><b>{v21?.automation_trades.length ?? 0}</b></header>{v21?.automation_trades.length ? <div className="v21TradeRows">{v21.automation_trades.map((trade,index) => <div key={`${trade.symbol}-${trade.entry_time}-${index}`}><strong>{trade.symbol} · {trade.side}</strong><span>Scanner #{trade.scanner_rank} · Score {trade.scanner_score} · {trade.confidence}</span><span>Giriş {stamp(trade.entry_time)} · {fmt(Number(trade.entry_price))} · {trade.margin} USDT · {trade.leverage}x</span><span>SL {String(trade.sl)} · TP {trade.tp.map(String).join(' · ')}</span><small>{trade.trade_reason.join(' · ')}</small><em>{trade.status}</em></div>)}</div> : <div className="v21ScannerEmpty"><History/><b>Henüz otomasyon işlemi yok.</b><span>Scanner tek başına emir açmaz; yalnızca açık onaylı Demo otomasyonunda işlem kaydı oluşur.</span></div>}</article>
    </section>

    {!status?.configured && <section className="demoSetupCard">
      <div><LockKeyhole/><span><b>Testnet anahtarı şifreli kasada bekleniyor</b><p>Üst menüdeki <strong>BORSA BAĞLANTILARI</strong> sekmesine girin. TESTNET kartında anahtarı test edin, şifreli kaydedin ve aktifleştirin; başka bir siteye veya siyah pencereye gitmeniz gerekmez.</p></span></div>
      <button onClick={refreshStatus}><RefreshCw/> KASAYI YENİDEN KONTROL ET</button>
    </section>}

    <section className={`demoCommandBar ${tab !== 'trade' ? 'demoTabHidden' : ''}`}>
      <button className="demoConnect" disabled={busy || !status?.configured} onClick={connect}><Radio/> BAĞLANTIYI TEST ET</button>
      <div><ShieldCheck/><span><b>DEMO GÜVENLİK SINIRI</b><small>100 USDT marjin · 2x · 200 USDT sanal pozisyon · 3 pozisyon</small></span></div>
      <button className="demoEmergency" disabled={busy || !status?.configured} onClick={emergency}><TriangleAlert/> ACİL DEMO DURDUR</button>
    </section>

    <div className={`demoMessage demoMessage-${messageKind}`}>{messageKind === 'error' ? <TriangleAlert/> : messageKind === 'ok' ? <ShieldCheck/> : <Activity/>}<span>{message}</span></div>

    <section className={`demoAccountStrip ${tab !== 'trade' ? 'demoTabHidden' : ''}`}>
      <article><Wallet/><span><small>SANAL CÜZDAN</small><b>{fmt(account?.wallet_balance)} USDT</b></span></article>
      <article><CircleDollarSign/><span><small>KULLANILABİLİR</small><b>{fmt(account?.available_balance)} USDT</b></span></article>
      <article><Activity/><span><small>AÇIK PnL</small><b className={(account?.unrealized_pnl || 0) >= 0 ? 'demoProfit' : 'demoLoss'}>{(account?.unrealized_pnl || 0) >= 0 ? '+' : ''}{fmt(account?.unrealized_pnl)} USDT</b></span></article>
      <article><Crosshair/><span><small>POZİSYON</small><b>{account?.reconciliation?.reconciled_active_positions ?? 0} / {status?.limits.max_open_positions ?? 3}</b></span></article>
      <article><Target/><span><small>AÇIK EMİRLER</small><b>{(account?.open_orders.length || 0)+(account?.open_algo_orders.length || 0)}</b></span></article>
      <article className={account?.hedge_mode ? 'demoModeBad' : 'demoModeGood'}><ShieldCheck/><span><small>POZİSYON MODU</small><b>{account ? account.hedge_mode ? 'HEDGE · DEĞİŞTİR' : 'ONE-WAY · UYGUN' : '—'}</b></span></article>
    </section>

    {chart && tab === 'trade' && <section className="demoLiveChart">
      <header><div><span>CANLI MUM GRAFİĞİ · EMA20 / EMA50 / EMA200</span><h3>{symbol.replace('USDT','/USDT')} Analiz ve Emir Seviyeleri</h3></div><div><b className={analysis?.direction === 'SHORT' ? 'demoLoss' : analysis?.direction === 'LONG' ? 'demoProfit' : ''}>{analysis?.direction || 'HESAPLANIYOR'}</b><small>Giriş {fmt(analysis?.entry)} · Stop {fmt(analysis?.stop_loss)} · TP3 {fmt(analysis?.tp3)}</small></div></header>
      <div className="demoChartCanvas">{chart}</div>
    </section>}

    <section className={`demoMainGrid ${tab !== 'trade' ? 'demoTabHidden' : ''}`}>
      <div className="demoTicket">
        <header><div><span>GÜVENLİ EMİR BİLETİ</span><h3>{symbol.replace('USDT','/USDT')}</h3></div><b>DEMO</b></header>
        <div className="demoSidePicker"><button className={form.direction === 'LONG' ? 'activeLong' : ''} onClick={() => setForm({...form,direction:'LONG'})}>LONG</button><button className={form.direction === 'SHORT' ? 'activeShort' : ''} onClick={() => setForm({...form,direction:'SHORT'})}>SHORT</button></div>
        <div className="demoTypePicker"><button className={form.orderType === 'MARKET' ? 'activeType' : ''} onClick={() => setForm({...form,orderType:'MARKET'})}>PİYASA</button><button className={form.orderType === 'LIMIT' ? 'activeType' : ''} onClick={() => setForm({...form,orderType:'LIMIT'})}>LİMİT</button></div>
        <button className="demoAnalysisFill" onClick={fillFromAnalysis}><Activity/> GÜNCEL ANALİZDEN DOLDUR</button>
        <div className="demoFieldGrid">
          <label><span>MARJİN · 5–100 DEMO USDT</span><div><input type="number" min="5" max="100" step="1" value={form.margin} onChange={event => changeMargin(event.target.value)} onBlur={normalizeMargin}/><em>USDT</em></div></label>
          <label><span>KALDIRAÇ</span><select value={form.leverage} onChange={event => setForm({...form,leverage:event.target.value as '1'|'2'})}><option value="1">1x</option><option value="2">2x</option></select></label>
          {form.orderType === 'LIMIT' && <label className="fullField"><span>LİMİT FİYATI</span><input value={form.limitPrice} onChange={event => setForm({...form,limitPrice:event.target.value})}/></label>}
          <label className="stopField"><span>STOP LOSS</span><input value={form.stop} onChange={event => setForm({...form,stop:event.target.value})}/></label>
          <label><span>TP1 · %30</span><input value={form.tp1} onChange={event => setForm({...form,tp1:event.target.value})}/></label>
          <label><span>TP2 · %30</span><input value={form.tp2} onChange={event => setForm({...form,tp2:event.target.value})}/></label>
          <label><span>TP3 · KALANI</span><input value={form.tp3} onChange={event => setForm({...form,tp3:event.target.value})}/></label>
        </div>
        <div className="demoExposure"><span><small>POSITION SIZE</small><b>{fmt(displayedPositionSize)} USDT</b></span><span><small>RISK / TRADE</small><b>{fmt(displayedRisk)} USDT</b></span><span><small>GERÇEK PARA</small><b>0 USDT</b></span></div>
        <button className="demoTest" disabled={busy || !status?.connected} onClick={testOrder}><TestTube2/> EMİR TESTİ · OLUŞTURMAZ</button>
        <button className="demoSubmit" disabled={busy || !status?.connected} onClick={submitOrder}><Send/> BINANCE DEMO EMRİ GÖNDER</button>
        <div className="demoOrderSummary">
          <div className="demoSummaryHeader">
            <span>TRADE SUMMARY</span>
            <strong className={form.direction === 'LONG' ? 'demoLongState' : 'demoShortState'}>{form.direction}</strong>
          </div>
          <div className="demoSummaryGrid">
            <div><small>Entry</small><b>{fmt(entryValue)}</b></div>
            <div><small>Stop</small><b>{fmt(stopValue)}</b></div>
            <div><small>TP1</small><b>{fmt(tp1Value)}</b></div>
            <div><small>TP3</small><b>{fmt(tp3Value)}</b></div>
          </div>
          <div className="demoRewardRow">
            <span><small>Risk</small><b>{fmt(Math.min(numberValue(riskLoss || '5'), riskAmount || numberValue(riskLoss || '5')))} USDT</b></span>
            <span><small>Reward</small><b>{fmt(rewardAmount)} USDT</b></span>
            <span><small>R:R</small><b>{rewardRatio}</b></span>
          </div>
        </div>
        <div className={`demoTicketFeedback demoTicketFeedback-${messageKind}`}>{messageKind === 'error' ? <TriangleAlert/> : messageKind === 'ok' ? <ShieldCheck/> : <Activity/>}<span><b>{messageKind === 'error' ? 'İŞLEM ENGELLENDİ' : messageKind === 'ok' ? 'DOĞRULAMA TAMAM' : 'GÜVENLİK DURUMU'}</b><small>{message}</small></span></div>
        <small className="demoTicketNote">Bu tutar yalnızca sanal Binance Demo bakiyesidir. Gerçek Binance emir kanalı kilitlidir.</small>
      </div>

      <div className="demoPositions">
        <header><div><span>CANLI DEMO POZİSYONLARI</span><h3>Giriş, Stop, TP ve Seviye Haritası</h3></div><b>{account?.reconciliation?.reconciled_active_positions ?? 0} AÇIK</b></header>
        <div className="demoPositionList">{account?.positions.length ? account.positions.map(position => <article key={position.symbol}>
          <header><div><b>{position.symbol.replace('USDT','/USDT')}</b><span className={position.direction === 'LONG' ? 'demoLong' : 'demoShort'}>{position.direction}</span><em className={position.leverage_verified ? 'demoVerified' : 'demoPending'}>{position.leverage_verified ? <ShieldCheck/> : <TriangleAlert/>}{position.leverage ? `${position.leverage}x` : '—'} · {(position.margin_type || 'DOĞRULANIYOR').toUpperCase()}</em></div><strong className={position.unrealized_pnl >= 0 ? 'demoProfit' : 'demoLoss'}>{position.unrealized_pnl >= 0 ? '+' : ''}{fmt(position.unrealized_pnl)} USDT</strong></header>
          <div className="demoPositionMetrics"><span><small>Miktar</small><b>{fmt(position.quantity)}</b></span><span><small>Giriş</small><b>{fmt(position.entry_price)}</b></span><span><small>Canlı</small><b>{fmt(position.mark_price)}</b></span><span><small>Likidasyon</small><b>{fmt(position.liquidation_price)}</b></span><span><small>İstenen kaldıraç</small><b>{position.requested_leverage || activePlanBySymbol.get(position.symbol)?.requested_leverage || activePlanBySymbol.get(position.symbol)?.leverage || '—'}x</b></span><span><small>Uygulanan kaldıraç</small><b>{position.applied_leverage || position.leverage || '—'}x · {(position.margin_type || '—').toUpperCase()}</b></span></div>
          <div className={`demoLeverageAudit ${position.leverage_verified ? 'verified' : 'pending'}`}>{position.leverage_verified ? <ShieldCheck/> : <TriangleAlert/>}<span><small>KALDIRAÇ VE MARJİN DENETİMİ</small><b>{position.leverage_verified ? `Binance doğruladı: ${position.leverage}x ISOLATED` : 'Binance yapılandırması doğrulanıyor; değer uydurulmuyor.'}</b></span></div>
          <PositionMap position={position} plan={activePlanBySymbol.get(position.symbol)}/>
          <footer><span>{activePlanBySymbol.get(position.symbol)?.monitoring_targets?.length ? `${activePlanBySymbol.get(position.symbol)?.monitoring_targets?.join(', ')} izleme hedefi` : 'Koşullu koruma kontrol ediliyor'}</span><button disabled={busy} onClick={() => closePosition(position)}>DEMO POZİSYONU KAPAT</button></footer>
        </article>) : <div className="demoEmpty"><Crosshair/><b>Açık Demo pozisyonu yok</b><span>Bağlantı kurulduğunda Binance Demo hesabındaki pozisyonlar burada canlı görünür.</span></div>}</div>
      </div>
    </section>

    <section className={`demoOrdersGrid ${tab !== 'trade' ? 'demoTabHidden' : ''}`}>
      <div className="demoOrderPanel"><header><div><span>BEKLEYEN GİRİŞLER</span><h3>Normal Demo Emirleri</h3></div><b>{account?.open_orders.length ?? 0}</b></header><div>{account?.open_orders.length ? account.open_orders.map(order => <article key={order.order_id} className={order.side === 'BUY' ? 'demoOrderItem buy' : 'demoOrderItem sell'}><span><b>{order.symbol} · {order.side}</b><small>{order.type} · {order.status}</small></span><em>{fmt(order.price || undefined)} · {fmt(order.quantity)}</em><button disabled={busy} onClick={() => cancelOrder(order)}>İPTAL</button></article>) : <p>Açık normal Demo emri yok.</p>}</div></div>
      <div className="demoOrderPanel"><header><div><span>STOP / TAKE PROFIT</span><h3>Koşullu Koruma Emirleri</h3></div><b>{account?.open_algo_orders.length ?? 0}</b></header><div>{account?.open_algo_orders.length ? account.open_algo_orders.map(order => <article key={order.algo_id} className={order.close_position ? 'demoAlgoOrder close' : 'demoAlgoOrder hedge'}><span><b>{order.symbol} · {order.type}</b><small>{order.status} · {order.close_position ? 'Pozisyonu kapatır' : 'Kısmi azaltır'}</small></span><em>Tetik {fmt(order.trigger_price)}</em><button disabled={busy} onClick={() => cancelAlgo(order)}>İPTAL</button></article>) : <p>Açık koşullu Demo emri yok.</p>}</div></div>
      <div className="demoEventPanel"><header><div><span>DENETİM AKIŞI</span><h3>Son Güvenlik Olayları</h3></div><b>{status?.events.length ?? 0}</b></header><div>{status?.events.slice(0,6).map((event,index) => <article key={`${event.created_at}-${index}`}><i/><span><b>{event.kind}</b><small>{event.message}</small></span><time>{stamp(event.created_at)}</time></article>)}</div></div>
    </section>

    {tab === 'risk' && <section className="v21Workspace">
      <header className="v21WorkspaceHead"><div><span>V21 · KAYIP ÖNCE HESAPLANIR</span><h2>Risk Kasası ve Pozisyon Boyutlandırıcı</h2><p>“Kaç USDT yatırayım?” yerine “Stop olursa en fazla kaç USDT kaybedeyim?” sorusundan başlar.</p></div><b><ShieldCheck/> DEMO HARD CAP · 100 USDT · 2X</b></header>
      <div className="v21RiskLayout">
        <article className="v21Card v21Calculator"><header><Calculator/><div><small>SEÇİLİ PLAN</small><h3>{symbol.replace('USDT','/USDT')} Risk Hesabı</h3></div></header><div className="v21CalcQuote"><span><small>GİRİŞ</small><b>{fmt(analysis?.entry)}</b></span><span><small>STOP</small><b>{fmt(analysis?.stop_loss)}</b></span><label><small>MAKS. KAYIP</small><div><input value={riskLoss} onChange={event => setRiskLoss(event.target.value)}/><em>USDT</em></div></label></div><button disabled={v21Busy || !analysis} onClick={calculateRisk}><Calculator/> GÜVENLİ BOYUTU HESAPLA</button>{riskPreview ? <div className="v21RiskResult"><span><small>MARJİN</small><b>{fmt(riskPreview.margin_usdt)} USDT</b></span><span><small>POZİSYON</small><b>{fmt(riskPreview.notional_usdt)} USDT</b></span><span><small>STOP KAYBI</small><b>{fmt(riskPreview.estimated_stop_loss_usdt)} USDT</b></span><span><small>FİYAT RİSKİ</small><b>%{fmt(riskPreview.risk_pct)}</b></span><p>{riskPreview.capped ? 'Hard cap uygulandı; istenen kayıp bütçesinin tamamı kullanılmadı.' : 'Hesap kullanıcı kayıp limitine göre boyutlandı.'}</p></div> : <div className="v21EmptyMini">Güncel analizden giriş/stop geldikten sonra hesapla.</div>}</article>
        <article className="v21Card v21Settings"><header><Settings2/><div><small>YEREL GÜVENLİK POLİTİKASI</small><h3>Risk ve Koruma Limitleri</h3></div></header>{settingsDraft && <div className="v21SettingsGrid">
          <label><span>İşlem başı maks. kayıp</span><input type="number" min=".5" max="25" value={settingsDraft.max_loss_per_trade} onChange={event => setSettingsDraft({...settingsDraft,max_loss_per_trade:Number(event.target.value)})}/><em>USDT</em></label>
          <label><span>İşlem başı maks. marjin</span><input type="number" min="5" max="100" value={settingsDraft.max_margin_per_trade} onChange={event => setSettingsDraft({...settingsDraft,max_margin_per_trade:Number(event.target.value)})}/><em>USDT</em></label>
          <label><span>Günlük zarar kilidi</span><input type="number" min="5" max="250" value={settingsDraft.daily_loss_limit} onChange={event => setSettingsDraft({...settingsDraft,daily_loss_limit:Number(event.target.value)})}/><em>USDT</em></label>
          <label><span>Günlük işlem limiti</span><input type="number" min="1" max="30" value={settingsDraft.daily_trade_limit} onChange={event => setSettingsDraft({...settingsDraft,daily_trade_limit:Number(event.target.value)})}/><em>adet</em></label>
          <label><span>Aynı anda pozisyon</span><select value={settingsDraft.max_positions} onChange={event => setSettingsDraft({...settingsDraft,max_positions:Number(event.target.value)})}><option value="1">1</option><option value="2">2</option><option value="3">3</option></select></label>
          <label><span>Minimum güven</span><input type="number" min="60" max="95" value={settingsDraft.min_confidence} onChange={event => setSettingsDraft({...settingsDraft,min_confidence:Number(event.target.value)})}/><em>%</em></label>
          <label className="v21Switch"><input type="checkbox" checked={settingsDraft.breakeven_enabled} onChange={event => setSettingsDraft({...settingsDraft,breakeven_enabled:event.target.checked})}/><span><b>Başabaş Stop</b><small>{settingsDraft.breakeven_trigger_r}R sonrası</small></span></label>
          <label className="v21Switch"><input type="checkbox" checked={settingsDraft.trailing_enabled} onChange={event => setSettingsDraft({...settingsDraft,trailing_enabled:event.target.checked})}/><span><b>İz Süren Stop</b><small>{settingsDraft.trailing_trigger_r}R sonrası</small></span></label>
        </div>}<button disabled={v21Busy || !settingsDraft} onClick={saveSettings}><Save/> RİSK POLİTİKASINI KAYDET</button></article>
      </div>
      <div className="v21MetricRow"><span><small>GÜNLÜK GERÇEKLEŞEN</small><b className={(v21?.daily.realized_pnl || 0) >= 0 ? 'demoProfit' : 'demoLoss'}>{fmt(v21?.daily.realized_pnl)} USDT</b></span><span><small>KALAN ZARAR BÜTÇESİ</small><b>{fmt(v21?.daily.remaining_loss_budget)} USDT</b></span><span><small>STOP ONARIMI</small><b>{v21?.protection.repairs ?? 0}</b></span><span><small>YİNELENEN GİRİŞ ENGELİ</small><b>{v21?.protection.duplicate_blocks ?? 0}</b></span></div>
    </section>}

    {tab === 'journal' && <section className="v21Workspace">
      <header className="v21WorkspaceHead"><div><span>ORDER_TRADE_UPDATE · ALGO_UPDATE · REST EŞLEŞTİRME</span><h2>Canlı Demo İşlem Günlüğü</h2><p>Açılış, kısmi dolum, kapanış, Stop/TP değişimi ve engelleme nedeni tek zaman çizgisinde.</p></div><button disabled={v21Busy || !status?.connected} onClick={loadHistory}><History/> {symbol} BORSA GEÇMİŞİNİ GETİR</button></header>
      <div className="v21JournalStats"><span><small>BUGÜN OLAY</small><b>{v21?.daily.events ?? 0}</b></span><span><small>USER STREAM</small><b>{v21?.stream.status || '—'}</b></span><span><small>SON EŞLEŞTİRME</small><b>{stamp(v21?.stream.last_sync)}</b></span><span><small>YENİDEN BAĞLANTI</small><b>{v21?.stream.reconnect_count ?? 0}</b></span></div>
      <div className="v21JournalGrid"><article className="v21Card v21Timeline"><header><ClipboardList/><div><small>KALICI YEREL KAYIT</small><h3>V21 Olay Zaman Çizgisi</h3></div><b>{v21?.journal.length ?? 0}</b></header><div>{v21?.journal.length ? v21.journal.map(item => <section key={item.id}><i className={item.realized_pnl && item.realized_pnl < 0 ? 'bad' : ''}/><div><span><b>{item.kind}</b><em>{item.symbol || 'SİSTEM'} · {item.source}</em></span><p>{item.message}</p>{item.reason && <small>{item.reason}</small>}</div><aside><time>{stamp(item.created_at)}</time>{item.realized_pnl !== null && item.realized_pnl !== undefined && <strong className={item.realized_pnl >= 0 ? 'demoProfit' : 'demoLoss'}>{item.realized_pnl >= 0 ? '+' : ''}{fmt(item.realized_pnl)}</strong>}</aside></section>) : <div className="v21EmptyMini">İlk Demo olayı bekleniyor.</div>}</div></article>
        <article className="v21Card v21ExchangeHistory"><header><History/><div><small>BINANCE FUTURES DEMO</small><h3>{symbol} Emir / Dolum Arşivi</h3></div></header>{historyPayload ? <div><h4>NORMAL EMİRLER · {historyPayload.orders.length}</h4>{historyPayload.orders.slice(-12).reverse().map((row,index) => <p key={`o-${index}`}><b>{String(row.side ?? '—')} · {String(row.type ?? '—')}</b><span>{String(row.status ?? '—')} · {String(row.avgPrice ?? row.price ?? '—')}</span></p>)}<h4>KOŞULLU EMİRLER · {historyPayload.algo_orders.length}</h4>{historyPayload.algo_orders.slice(-8).reverse().map((row,index) => <p key={`a-${index}`}><b>{String(row.orderType ?? row.type ?? 'ALGO')}</b><span>{String(row.algoStatus ?? row.status ?? '—')} · {String(row.triggerPrice ?? '—')}</span></p>)}<h4>DOLUMLAR · {historyPayload.trades.length}</h4>{historyPayload.trades.slice(-8).reverse().map((row,index) => <p key={`t-${index}`}><b>{String(row.side ?? '—')} · {String(row.qty ?? '—')}</b><span>PnL {String(row.realizedPnl ?? '0')} · ücret {String(row.commission ?? '—')}</span></p>)}</div> : <div className="v21EmptyMini">Üstteki düğmeyle seçili paritenin tam Demo geçmişini getir.</div>}</article>
      </div>
    </section>}

    {tab === 'auto' && <section className="v21Workspace">
      <header className="v21WorkspaceHead"><div><span>ÇİFT ONAY · DEMO ARM + DEMO OTOMATİK</span><h2>Kontrollü Demo Otopilot</h2><p>İzin listesi, yön, saat, güven, volatilite, korelasyon, günlük kayıp ve pozisyon kapıları birlikte geçmeden emir göndermez.</p></div><b className={v21?.auto.enabled ? 'v21Running' : 'v21Stopped'}><Zap/> {v21?.auto.enabled ? 'ÇALIŞIYOR' : 'GÜVENLİ KAPALI'}</b></header>
      <div className="v21AutoLayout"><article className="v21Card v21AutoControl"><header><Zap/><div><small>İKİLİ GÜVENLİK KAPISI</small><h3>Demo Emir Kilidi</h3></div></header><div className="v21AutoDecision"><small>DEMO ARM</small><b>{status?.armed ? 'AÇIK' : 'KAPALI'}</b><span>{status?.armed ? `Kalan süre ${Math.floor(armSeconds / 60)} dk ${armSeconds % 60} sn` : 'Yeni Demo girişleri kilitli.'}</span></div>{!status?.armed && <label><span>Başlatmak için yaz</span><input value={armText} onChange={event => setArmText(event.target.value)} placeholder="DEMO"/></label>}<button disabled={busy || !status?.connected || (!status?.armed && armText.trim().toUpperCase() !== 'DEMO')} onClick={status?.armed ? disarm : arm}>{status?.armed ? <TriangleAlert/> : <UnlockKeyhole/>}{status?.armed ? ' DEMO ARM KAPAT' : ' DEMO ARM AÇ'}</button><p>Bu kilit yalnızca Binance Futures Demo giriş emirlerini süreli olarak açar; Live kanalını etkilemez.</p></article><article className="v21Card v21AutoControl"><header><Zap/><div><small>İKİNCİ KULLANICI ONAYI</small><h3>Demo Otomasyon Motoru</h3></div></header><div className="v21AutoDecision"><small>SON KARAR</small><b>{v21?.auto.last_decision || 'Bekleniyor'}</b><span>{v21?.auto.last_scan ? `Son tarama ${stamp(v21.auto.last_scan)} · ${v21.auto.cycles} tur` : 'Henüz tarama yapılmadı.'}</span></div>{!v21?.auto.enabled && <label><span>Başlatmak için yaz</span><input value={autoConfirm} onChange={event => setAutoConfirm(event.target.value)} placeholder="DEMO OTOMATİK"/></label>}<button className={v21?.auto.enabled ? 'stop' : ''} disabled={v21Busy || (!v21?.auto.enabled && !status?.armed)} onClick={toggleAuto}>{v21?.auto.enabled ? <TriangleAlert/> : <Play/>}{v21?.auto.enabled ? ' YENİ GİRİŞLERİ DURDUR' : ' KONTROLLÜ DEMO OTOMASYONU BAŞLAT'}</button><p>Uygulama yeniden açıldığında daima kapalı başlar. Stop/TP koruması motor dursa bile Binance Demo hesabında kalır.</p></article>
        <article className="v21Card v21AutoRules"><header><Settings2/><div><small>OTOMASYON EVRENİ</small><h3>İzinler ve Piyasa Kapıları</h3></div></header>{settingsDraft && <div>
          <label className="wide"><span>İzinli USDT pariteleri</span><input value={settingsDraft.allowed_symbols.join(', ')} onChange={event => setSettingsDraft({...settingsDraft,allowed_symbols:event.target.value.toUpperCase().split(',').map(value => value.trim()).filter(Boolean)})}/></label>
          <label><span>Maks. volatilite</span><input type="number" value={settingsDraft.max_volatility_pct} onChange={event => setSettingsDraft({...settingsDraft,max_volatility_pct:Number(event.target.value)})}/><em>%</em></label>
          <label><span>Maks. BTC korelasyonu</span><input type="number" value={settingsDraft.max_correlation_pct} onChange={event => setSettingsDraft({...settingsDraft,max_correlation_pct:Number(event.target.value)})}/><em>%</em></label>
          <label><span>Başlangıç saati</span><input type="number" min="0" max="23" value={settingsDraft.schedule_start_hour} onChange={event => setSettingsDraft({...settingsDraft,schedule_start_hour:Number(event.target.value)})}/></label>
          <label><span>Bitiş saati</span><input type="number" min="1" max="24" value={settingsDraft.schedule_end_hour} onChange={event => setSettingsDraft({...settingsDraft,schedule_end_hour:Number(event.target.value)})}/></label>
          <label className="v21Switch"><input type="checkbox" checked={settingsDraft.allow_long} onChange={event => setSettingsDraft({...settingsDraft,allow_long:event.target.checked})}/><span><b>LONG izinli</b></span></label>
          <label className="v21Switch"><input type="checkbox" checked={settingsDraft.allow_short} onChange={event => setSettingsDraft({...settingsDraft,allow_short:event.target.checked})}/><span><b>SHORT izinli</b></span></label>
        </div>}<button disabled={v21Busy || !settingsDraft} onClick={saveSettings}><Save/> OTOMASYON KAPILARINI KAYDET</button></article></div>
      <div className="v21GateStrip"><span className={status?.armed ? 'passed' : ''}><b>1</b><em>DEMO ARM</em><small>{status?.armed ? 'GEÇTİ' : 'KAPALI'}</small></span><span className={status?.connected ? 'passed' : ''}><b>2</b><em>DEMO API</em><small>{status?.connected ? 'BAĞLI' : 'BEKLİYOR'}</small></span><span className={(v21?.daily.auto_entries || 0) < (v21?.settings.daily_trade_limit || 0) ? 'passed' : ''}><b>3</b><em>GÜNLÜK LİMİT</em><small>{v21?.daily.auto_entries ?? 0}/{v21?.settings.daily_trade_limit ?? 0}</small></span><span className={(v21?.daily.remaining_loss_budget || 0) > 0 ? 'passed' : ''}><b>4</b><em>ZARAR KASASI</em><small>{fmt(v21?.daily.remaining_loss_budget)} USDT</small></span><span className={(v21?.account.positions || 0) < (v21?.settings.max_positions || 0) ? 'passed' : ''}><b>5</b><em>POZİSYON</em><small>{v21?.account.positions ?? 0}/{v21?.settings.max_positions ?? 0}</small></span><span><b>6</b><em>SİNYAL KAPILARI</em><small>Her taramada</small></span></div>
      <div className="v21AutoFlow">
        <div className="v21AutoFlowHeader"><span>OTOMASYON DURUMU</span><h3>İşlem Açma Akışı</h3></div>
        <div className="v21AutoFlowSteps">
          <article className={status?.armed ? 'passed' : ''}><b>01</b><div><strong>Demo arm</strong><small>{status?.armed ? 'Güvenlik kilidi açık.' : 'Emir kilidi kapalı; işlem bekliyor.'}</small></div></article>
          <article className={status?.connected ? 'passed' : ''}><b>02</b><div><strong>Market sync</strong><small>{status?.connected ? 'User stream canlı.' : 'Bağlantı bekleniyor.'}</small></div></article>
          <article className={(v21?.daily.auto_entries ?? 0) < (v21?.settings.daily_trade_limit ?? 0) ? 'passed' : ''}><b>03</b><div><strong>Risk gate</strong><small>{(v21?.daily.remaining_loss_budget ?? 0) > 0 ? 'Günlük zarar bütçesi açık.' : 'Günlük zarar limiti aktif.'}</small></div></article>
          <article className={v21?.auto.last_decision ? 'passed' : ''}><b>04</b><div><strong>Signal decision</strong><small>{v21?.auto.last_decision || 'Henüz karar alınmadı.'}</small></div></article>
        </div>
      </div>
    </section>}

    {tab === 'backtest' && <section className="v21Workspace">
      <header className="v21WorkspaceHead"><div><span>NO LOOK-AHEAD · NEXT OPEN · STOP FIRST</span><h2>Kanıtlı Backtest Laboratuvarı</h2><p>Sinyal kapanan mumdan, giriş sonraki mum açılışından alınır; ücret ve kayma iki yönlü düşülür.</p></div><div className="v21BacktestRun"><select value={backtestSymbol} onChange={event => setBacktestSymbol(event.target.value)}>{(v21?.settings.allowed_symbols || [symbol]).map(item => <option key={item}>{item}</option>)}</select><button disabled={v21Busy || !status?.configured} onClick={runBacktest}><BarChart3/> 1.000 MUMU TEST ET</button></div></header>
      {v21?.backtest ? <><div className="v21BacktestMetrics"><span><small>NET SONUÇ</small><b className={v21.backtest.net_pnl >= 0 ? 'demoProfit' : 'demoLoss'}>{v21.backtest.net_pnl >= 0 ? '+' : ''}{fmt(v21.backtest.net_pnl)} USDT</b></span><span><small>İŞLEM</small><b>{v21.backtest.trades}</b></span><span><small>BAŞARI</small><b>%{fmt(v21.backtest.win_rate)}</b></span><span><small>MAKS. DÜŞÜŞ</small><b>%{fmt(v21.backtest.max_drawdown_pct)}</b></span><span><small>PROFIT FACTOR</small><b>{fmt(v21.backtest.profit_factor)}</b></span><span><small>GELECEK SIZINTISI</small><b>{v21.backtest.no_lookahead ? 'YOK' : 'KONTROL'}</b></span></div><div className="v21BacktestLayout"><article className="v21Card v21Folds"><header><BarChart3/><div><small>3 DÖNEMLİ ZAMAN TÜNELİ</small><h3>Geliştirme · Doğrulama · Görünmeyen</h3></div></header>{v21.backtest.folds.map((fold,index) => <section key={fold.name}><b>{index+1}</b><span><strong>{fold.name}</strong><small>{fold.trades} işlem</small></span><em className={fold.net_pnl >= 0 ? 'demoProfit' : 'demoLoss'}>{fold.net_pnl >= 0 ? '+' : ''}{fmt(fold.net_pnl)} USDT</em></section>)}</article><article className="v21Card v21TradeResults"><header><History/><div><small>SON İŞLEMLER</small><h3>Maliyet Sonrası Sonuçlar</h3></div></header><div>{v21.backtest.recent_trades.slice(0,16).map((trade,index) => <p key={index}><span><b>{trade.direction} · {trade.reason}</b><small>{trade.regime} · maliyet {fmt(trade.cost_usdt)}</small></span><em className={trade.pnl >= 0 ? 'demoProfit' : 'demoLoss'}>{trade.pnl >= 0 ? '+' : ''}{fmt(trade.pnl)}</em></p>)}</div></article></div><p className="v21Disclaimer">{v21.backtest.note}</p></> : <div className="v21LargeEmpty"><BarChart3/><b>Henüz V21 backtest çalıştırılmadı</b><span>Seçili paritede 1.000 Demo Futures mumunu kronolojik olarak sınamak için üstteki düğmeye bas.</span></div>}
    </section>}

    {tab === 'performance' && <section className="v21Workspace">
      <header className="v21WorkspaceHead"><div><span>PROFESSIONAL PERFORMANCE SUMMARY</span><h2>Performans Merkezi</h2><p>Net PnL, kazanç oranı, risk/ödül ve son kapanış verileri tek panoda summarize edilir.</p></div><b className={performanceSummary.realized >= 0 ? 'v21Running' : 'v21Pending'}><BarChart3/> {performanceSummary.realized >= 0 ? 'POSİTİF TREND' : 'RİSK TEDBİRİ'}</b></header>
      <div className="v21PerformanceHero">
        <article className="v21Card v21PerformancePrimary">
          <div className="v21PerformanceLabel"><small>TOPLAM PnL</small><strong className={performanceSummary.realized >= 0 ? 'demoProfit' : 'demoLoss'}>{performanceSummary.realized >= 0 ? '+' : ''}{fmt(performanceSummary.realized)} USDT</strong></div>
          <div className="v21PerformanceMeta"><span><small>Win rate</small><b>%{fmt(performanceSummary.winRate)}</b></span><span><small>Return</small><b>{performanceSummary.returnPct >= 0 ? '+' : ''}{fmt(performanceSummary.returnPct)}%</b></span></div>
        </article>
        <article className="v21Card v21PerformanceMini"><span><small>Trade count</small><b>{performanceSummary.tradeCount}</b></span><span><small>Profit factor</small><b>{fmt(performanceSummary.pf)}</b></span><span><small>Max drawdown</small><b>%{fmt(performanceSummary.maxDd)}</b></span></article>
      </div>
      <div className="v21PerformanceGrid">
        <article className="v21Card">
          <header><BarChart3/><div><small>ANALİTİK GÖRÜNÜM</small><h3>Öne Çıkan Metrikler</h3></div></header>
          <div className="v21MetricRow v21PerformanceMetrics">
            <span><small>NET PnL</small><b className={performanceSummary.realized >= 0 ? 'demoProfit' : 'demoLoss'}>{performanceSummary.realized >= 0 ? '+' : ''}{fmt(performanceSummary.realized)} USDT</b></span>
            <span><small>WIN RATE</small><b>%{fmt(performanceSummary.winRate)}</b></span>
            <span><small>RETURN</small><b>{performanceSummary.returnPct >= 0 ? '+' : ''}{fmt(performanceSummary.returnPct)}%</b></span>
            <span><small>DRAWDOWN</small><b>%{fmt(performanceSummary.maxDd)}</b></span>
          </div>
        </article>
        <article className="v21Card">
          <header><History/><div><small>ÇALIŞMA DURUMU</small><h3>Son Durum</h3></div></header>
          <div className="v21StatusList">
            <div><strong>Otomasyon</strong><span>{v21?.auto.enabled ? 'Aktif' : 'Bekliyor'}</span></div>
            <div><strong>Risk bütçesi</strong><span>{fmt(v21?.daily.remaining_loss_budget)} USDT</span></div>
            <div><strong>Canlı PnL</strong><span className={Number(v21?.account.unrealized_pnl ?? 0) >= 0 ? 'demoProfit' : 'demoLoss'}>{fmt(v21?.account.unrealized_pnl)} USDT</span></div>
            <div><strong>Backtest</strong><span>{v21?.backtest ? `${fmt(v21.backtest.trades)} trade` : 'Bekleniyor'}</span></div>
          </div>
        </article>
      </div>
    </section>}

    {tab === 'certificate' && <section className="v21Workspace">
      <header className="v21WorkspaceHead"><div><span>V21 DEMO DISCIPLINE CERTIFICATE</span><h2>Sistem Sağlığı ve Demo Sertifikası</h2><p>Sertifika kâr vaadi değildir; yalnızca Demo kanıtı, koruma, tekrar, bağlantı ve düşüş eşiklerini ölçer.</p></div><b className={v21?.certificate.status === 'DEMO SERTİFİKALI' ? 'v21Running' : 'v21Pending'}><ShieldCheck/> {v21?.certificate.status || 'KANIT BEKLİYOR'}</b></header>
      <div className="v21CertificateLayout"><article className="v21Card v21Score"><div className="v21ScoreRing" style={{'--score':`${v21?.certificate.score || 0}%`} as CSSProperties}><span><b>%{v21?.certificate.score ?? 0}</b><small>DEMO KANIT</small></span></div><h3>{v21?.certificate.passed_gates ?? 0} / {v21?.certificate.total_gates ?? 0} kapı geçti</h3><p>{v21?.certificate.reason}</p><button onClick={enableNotifications}><Bell/> MASAÜSTÜ BİLDİRİMLERİNİ AÇ</button></article><article className="v21Card v21CertificateGates"><header><ShieldCheck/><div><small>ZORUNLU KANIT KAPILARI</small><h3>V21 Kontrol Listesi</h3></div></header><div>{v21?.certificate.gates.map(gate => <section className={gate.passed ? 'passed' : ''} key={gate.name}><i>{gate.passed ? '✓' : '!'}</i><span><b>{gate.name}</b><small>Hedef: {gate.target}</small></span><strong>{gate.value}</strong></section>)}</div></article><article className="v21Card v21Health"><header><Activity/><div><small>BAĞLANTI VE KURTARMA</small><h3>Canlı Sistem Sağlığı</h3></div></header><span><small>Demo REST</small><b>{status?.connected ? 'BAĞLI' : 'BEKLİYOR'}</b></span><span><small>Kullanıcı akışı</small><b>{v21?.stream.status || '—'}</b></span><span><small>Aktarım yolu</small><b>{v21?.stream.transport || '—'}</b></span><span><small>Akış hatası</small><b>{v21?.stream.error_count ?? 0}</b></span><span><small>Son yedek</small><b>{stamp(v21?.last_saved)}</b></span><div><button disabled={v21Busy} onClick={() => runDrill('RECONNECT')}>BAĞLANTI TATBİKATI</button><button disabled={v21Busy} onClick={() => runDrill('PROTECTION')}>STOP TATBİKATI</button><button disabled={v21Busy} onClick={() => runDrill('EMERGENCY')}>ACİL DURDURMA TATBİKATI</button></div></article></div>
      <div className="v21SafetyLock"><LockKeyhole/><span><b>GERÇEK PARA VE GERÇEK BINANCE EMİR KANALI FİZİKSEL OLARAK YOK</b><small>Bu paket yalnızca https://demo-fapi.binance.com ve wss://demo-fstream.binance.com adreslerini kullanır.</small></span><strong>DEMO ONLY</strong></div>
    </section>}

    {confirmation && <div className="demoConfirmationBackdrop" role="presentation" onClick={() => { setConfirmation(null); setConfirmationText(''); setConfirmationChecked(false) }}>
      <section className="demoConfirmation" role="dialog" aria-modal="true" aria-labelledby="demo-confirmation-title" onClick={event => event.stopPropagation()}>
        <h2 id="demo-confirmation-title">{confirmation.title}</h2>
        <p>{confirmation.message}</p>
        {confirmation.checkbox ? <label><input type="checkbox" checked={confirmationChecked} onChange={event => setConfirmationChecked(event.target.checked)} /><span>Demo order'ın gönderilmesini onaylıyorum.</span></label> : <label><span>Onay için {confirmation.expected} yazın</span><input autoFocus value={confirmationText} onChange={event => setConfirmationText(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') confirmAction() }} /></label>}
        <div><button type="button" onClick={() => { setConfirmation(null); setConfirmationText(''); setConfirmationChecked(false) }}>İPTAL</button><button type="button" disabled={confirmation.checkbox ? !confirmationChecked : confirmationText.trim().toUpperCase() !== confirmation.expected} onClick={confirmAction}>{confirmation.checkbox ? 'ONAYLA VE GÖNDER' : 'Onayla'}</button></div>
      </section>
    </div>}
    </section>
}
