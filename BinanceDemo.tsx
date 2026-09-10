import { type CSSProperties, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, BarChart3, Bell, Calculator, CheckCircle2, ChevronDown, CircleDollarSign, ClipboardList, Crosshair, Gauge, History, LayoutDashboard, LockKeyhole, Play, Radar, Radio, RefreshCw, Save, Search, Send, Settings2, ShieldCheck, Sparkles, Target, TestTube2, TriangleAlert, UnlockKeyhole, Wallet, X, Zap } from 'lucide-react'
import { API_BASE, buildDemoSavePayload, loadDemoCredentials, saveDemoCredentials, userSessionToken } from './api'

const API = `${API_BASE}/binance-demo`
const V21_API = `${API_BASE}/v21`

type AnalysisPlan = {
  direction:'LONG'|'SHORT'|'BEKLE'
  entry:number
  stop_loss:number
  tp1:number
  tp2:number
  tp3:number
}

type AnalysisPlanPayload = Partial<AnalysisPlan> & {normalized_signal?:unknown;status?:unknown;stop?:unknown;stopLoss?:unknown;take_profit?:unknown;target1?:unknown;target2?:unknown;target3?:unknown;analysis?:unknown;plan?:unknown;results?:unknown}

function normalizeAnalysisPlan(payload:unknown, symbol=''):AnalysisPlan|null {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null
  const row = payload as AnalysisPlanPayload
  const resultRows = Array.isArray(row.results) ? row.results.filter(item => item && typeof item === 'object') as AnalysisPlanPayload[] : []
  const result = resultRows.find(item => String(item.symbol || '').toUpperCase() === symbol.toUpperCase()) || resultRows[0]
  const nested = row.analysis && typeof row.analysis === 'object' && !Array.isArray(row.analysis)
    ? row.analysis as AnalysisPlanPayload
    : row.plan && typeof row.plan === 'object' && !Array.isArray(row.plan)
      ? row.plan as AnalysisPlanPayload
      : result || row
  const rawDirection = String(nested.direction || nested.normalized_signal || '').trim().toUpperCase()
  const direction = rawDirection === 'LONG' || rawDirection === 'BUY'
    ? 'LONG' : rawDirection === 'SHORT' || rawDirection === 'SELL' ? 'SHORT' : 'BEKLE'
  const stop = nested.stop_loss ?? nested.stop ?? nested.stopLoss
  return {
    direction,
    entry:Number(nested.entry),
    stop_loss:Number(stop),
    tp1:Number(nested.tp1 ?? nested.target1),
    tp2:Number(nested.tp2 ?? nested.target2),
    tp3:Number(nested.tp3 ?? nested.target3),
  }
}

function isCompleteAnalysisPlan(plan:AnalysisPlan|null):plan is AnalysisPlan {
  return Boolean(plan && ['LONG','SHORT'].includes(plan.direction) && [plan.entry,plan.stop_loss,plan.tp1,plan.tp2,plan.tp3].every(value => Number.isFinite(value) && value > 0))
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
  position_side?:'BOTH'|'LONG'|'SHORT'
  direction:'LONG'|'SHORT'
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

type DemoOrderResult = {
  symbol?:string
  order_id?:number
  client_order_id?:string
  status?:string
  type?:string
  side?:string
  quantity?:string
  price?:string|number
}

type DemoConfirmation = {title:string;message:string;expected:string;checkbox:boolean;action:() => Promise<unknown>}

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

type MarketOption = {symbol:string;display:string;price:number;change:number;volume:number}

type FormState = {
  direction:'LONG'|'SHORT'
  orderType:'MARKET'|'LIMIT'
  margin:string
  leverage:'AUTO'|'1'|'2'|'3'|'5'|'10'|'15'|'20'|'25'|'30'|'40'|'50'|'CUSTOM'
  customLeverage:string
  limitPrice:string
  stop:string
  tp1:string
  tp2:string
  tp3:string
}

type V21Settings = {
  allowed_symbols:string[];allow_long:boolean;allow_short:boolean;max_loss_per_trade:number;max_margin_per_trade:number
  daily_loss_limit:number;daily_trade_limit:number;max_positions:number;min_confidence:number;max_volatility_pct:number
  max_correlation_pct:number;schedule_start_hour:number;schedule_end_hour:number;scan_seconds:number
  breakeven_enabled:boolean;breakeven_trigger_r:number;trailing_enabled:boolean;trailing_trigger_r:number
  trailing_distance_r:number;notifications:boolean;fee_bps_per_side:number;slippage_bps_per_side:number
}

type V21Journal = {id:string;created_at:string;kind:string;symbol?:string|null;status?:string|null;side?:string|null;price?:number|null;quantity?:number|null;realized_pnl?:number|null;reason?:string|null;message:string;source:string;reduce_only:boolean}
type V21Gate = {name:string;passed:boolean;value:string|number;target:string|number}
type V21Backtest = {symbol:string;interval:string;trades:number;wins:number;win_rate:number;net_pnl:number;ending_equity:number;max_drawdown_pct:number;profit_factor:number;no_lookahead:boolean;folds:{name:string;trades:number;net_pnl:number}[];recent_trades:{signal_time:number;entry_time:number;exit_time:number;direction:string;entry:number;exit:number;reason:string;pnl:number;cost_usdt:number;regime:string}[];note:string}
type V21Summary = {
  version:string;mode:string;settings:V21Settings
  auto:{enabled:boolean;busy:boolean;cycles:number;last_scan:string|null;last_decision:string;last_error:string|null;rejection_gate?:string|null;rejection_reason?:string|null}
  scanner:{active:boolean;scan_status:string;scan_interval_seconds:number;coins_scanned:number;selected_count:number;eligible_count:number;last_scan_at:string|null;next_scan_at:string|null;last_error:string|null;top_candidates:{rank:number;symbol:string;direction:string;score:number;confidence:string;confidence_value?:number;trend?:string;momentum?:string;volatility_pct?:number;reasons?:string[]}[];selected_symbols:string[];last_stage:string}
  stream:{status:string;transport:string;last_event:string|null;last_sync:string|null;reconnect_count:number;error_count:number;last_error:string|null}
  daily:{date:string;auto_entries:number;events:number;realized_pnl:number;remaining_loss_budget:number}
  account:{wallet_balance:number|null;available_balance:number|null;unrealized_pnl:number|null;positions:number;reconciled_active_positions?:number;normal_orders:number;algo_orders:number}
  protection:{repairs:number;duplicate_blocks:number};journal:V21Journal[];backtest:V21Backtest|null
  certificate:{version:string;status:string;score:number;passed_gates:number;total_gates:number;gates:V21Gate[];reason:string;generated_at:string}
  last_saved:string|null;real_trading_locked:boolean
}

type V21RiskPreview = {symbol:string;leverage:number;risk_pct:number;notional_usdt:number;margin_usdt:number;estimated_stop_loss_usdt:number;capped:boolean;quantity_preview:string;step_size:string}
type V21Performance = {period:string;total_trades:number;wins:number;losses:number;win_rate:number;total_profit:number;total_loss:number;net_profit:number;average_trade:number;best_trade:number;worst_trade:number;profit_factor:number|null;average_win:number|null;average_loss:number|null;winning_streak:number;losing_streak:number;equity_curve:{index:number;pnl:number;equity:number}[];directional:Record<'LONG'|'SHORT',{trades:number;win_rate:number|null;realized_pnl:number|null;profit_factor:number|null}>;history_quality:string;max_drawdown:number;demo_only:boolean;read_only:boolean}
type V21Tab = 'dashboard'|'trade'|'risk'|'journal'|'auto'|'backtest'|'performance'|'certificate'

const initialForm:FormState = {
  direction:'LONG',orderType:'MARKET',margin:'50',leverage:'10',customLeverage:'',limitPrice:'',stop:'',tp1:'',tp2:'',tp3:'',
}

const resolveLeverage = (choice:FormState['leverage'],customLeverage='') => choice === 'AUTO' ? 2 : choice === 'CUSTOM' ? Number(customLeverage) : Number(choice)

const fmt = (value?:number|null) => value === undefined || value === null || !Number.isFinite(value) ? '—' : value.toLocaleString('tr-TR',{maximumFractionDigits:value < 10 ? 5 : 2})
const stamp = (value?:string|null) => value ? new Date(value).toLocaleString('tr-TR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—'
const numberValue = (value:string) => Number(value.replace(',','.'))

function positionObservation(position:DemoPosition, plan?:DemoPlan) {
  const stop = Number(plan?.stop_loss || 0)
  const target = Number(plan?.targets?.[0] || 0)
  const entry = position.entry_price
  const mark = position.mark_price
  if (!(entry > 0) || !(mark > 0)) return {stage:'UNKNOWN',breakEven:'INSUFFICIENT DATA',distanceToStop:null,distanceToTarget:null,progress:null,reason:'Entry or mark price is unavailable.'}
  const favorableMove = position.direction === 'LONG' ? mark - entry : entry - mark
  const riskDistance = stop > 0 ? Math.abs(entry - stop) : 0
  const targetDistance = target > 0 ? Math.abs(target - entry) : 0
  const progress = targetDistance > 0 ? Math.max(0,Math.min(100,favorableMove / targetDistance * 100)) : null
  const distanceToStop = stop > 0 ? Math.abs(mark - stop) / entry * 100 : null
  const distanceToTarget = target > 0 ? Math.abs(target - mark) / entry * 100 : null
  const stage = position.unrealized_pnl < 0 ? 'EXIT WATCH' : favorableMove > 0 && riskDistance > 0 && favorableMove >= riskDistance ? 'IN PROFIT' : 'EARLY POSITION'
  const breakEven = favorableMove > 0 && riskDistance > 0 && favorableMove >= riskDistance ? 'BREAK-EVEN CANDIDATE' : favorableMove > 0 ? 'WATCHING' : 'NOT ELIGIBLE'
  return {stage,breakEven,distanceToStop,distanceToTarget,progress,reason:plan ? 'Derived from live mark, entry, Stop and target values.' : 'Protection plan data is unavailable.'}
}
const gateLabel = (gate?:string|null) => ({DEMO_ARM:'Demo kilidi kapalı',MAX_POSITIONS:'Pozisyon limiti dolu',DAILY_TRADE_LIMIT:'Günlük işlem limiti dolu',DAILY_LOSS_LIMIT:'Günlük zarar limiti aktif',MARKET_HOURS:'Çalışma saatleri dışında',ALLOWED_SYMBOLS:'İzinli parite dışında',RISK_LEVELS:'Risk seviyeleri geçersiz',DEMO_EXECUTION:'Demo emir reddedildi'}[gate || ''] || 'Fırsat bekleniyor')

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
      if (field === 'leverage') return 'Kaldıraç AUTO, 1x, 2x, 3x, 5x veya 10x olabilir.'
        if (field === 'leverage') return 'Kaldıraç AUTO, 1x-50x aralığında tam sayı veya CUSTOM olabilir.'
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

function isNormalRequestCancellation(error:unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') return true
  if (error && typeof error === 'object') {
    const name = (error as {name?: unknown}).name
    if (typeof name === 'string' && name.toLowerCase() === 'aborterror') return true
  }
  if (error instanceof Error) {
    const message = error.message.toLowerCase()
    return message.includes('abort') || message.includes('canceled') || message.includes('cancelled')
  }
  return false
}

function readableErrorMessage(error:unknown, fallback:string): string {
  if (isNormalRequestCancellation(error)) return ''
  return error instanceof Error ? error.message : fallback
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

export default function BinanceDemo({active,symbol,analysis,chart,markets,onSymbolChange}:{active:boolean;symbol:string;analysis:AnalysisPlan|null;chart?:ReactNode;markets:MarketOption[];onSymbolChange:(symbol:string)=>void}) {
  const [status,setStatus] = useState<DemoStatus|null>(null)
  const [account,setAccount] = useState<DemoAccount|null>(null)
  const [form,setForm] = useState<FormState>(initialForm)
  const [demoCredentials,setDemoCredentials] = useState(loadDemoCredentials)
  const [armText,setArmText] = useState('')
  const [busy,setBusy] = useState(false)
  const [message,setMessage] = useState('Önce bağlantıyı test edin; ardından analiz planını doğrulayın.')
  const [messageKind,setMessageKind] = useState<'info'|'ok'|'error'>('info')
  const [confirmation,setConfirmation] = useState<DemoConfirmation|null>(null)
  const [confirmationText,setConfirmationText] = useState('')
  const [confirmationChecked,setConfirmationChecked] = useState(false)
  const [clock,setClock] = useState(Date.now())
  const [tab,setTab] = useState<V21Tab>('dashboard')
  const [v21,setV21] = useState<V21Summary|null>(null)
  const [settingsDraft,setSettingsDraft] = useState<V21Settings|null>(null)
  const [v21Busy,setV21Busy] = useState(false)
  const [riskLoss,setRiskLoss] = useState('5')
  const [riskPreview,setRiskPreview] = useState<V21RiskPreview|null>(null)
  const [performance,setPerformance] = useState<V21Performance|null>(null)
  const [performancePeriod,setPerformancePeriod] = useState<'all'|'daily'|'weekly'|'monthly'>('all')
  const [historyPayload,setHistoryPayload] = useState<{orders:Record<string,unknown>[];algo_orders:Record<string,unknown>[];trades:Record<string,unknown>[]} | null>(null)
  const [autoConfirm,setAutoConfirm] = useState('')
  const [backtestSymbol,setBacktestSymbol] = useState(symbol)
  const [lastOrder,setLastOrder] = useState<DemoOrderResult|null>(null)
  const [symbolOpen,setSymbolOpen] = useState(false)
  const [symbolQuery,setSymbolQuery] = useState('')
  const [scannerFocus,setScannerFocus] = useState<{symbol:string;direction:'LONG'|'SHORT'|'WAIT';score:number;trend:string;volume:string;momentum:string;confidence:number;risk:string;status:string} | null>(null)
  const [alertFilter,setAlertFilter] = useState<'ALL'|'CRITICAL'|'WARNING'|'INFO'>('ALL')
  const demoDeckRef = useRef<HTMLElement>(null)
  const workspaceRef = useRef<HTMLElement>(null)
  const accountRefreshId = useRef(0)
  const initialScanRequested = useRef(false)
  const initialScanInFlight = useRef(false)
  const v21RequestId = useRef(0)
  const lastNotificationId = useRef<string|null>(null)

  const refreshStatus = async () => {
    try {
      const payload = await apiCall<DemoStatus>('/status')
      setStatus(payload)
      if (!payload.configured) setAccount(null)
      return payload
    } catch { setStatus(null); return null }
  }
  useEffect(() => { setDemoCredentials(loadDemoCredentials()) },[active])

  const saveDemoConnection = async () => {
    const apiKey = demoCredentials.apiKey.trim(); const secretKey = demoCredentials.secretKey.trim()
    if (!apiKey || !secretKey) { setMessage('Demo API Key ve Secret Key gerekli.'); setMessageKind('error'); return }
    if (status?.configured || status?.connected) {
      setMessage('Demo/Testnet credential zaten bu oturum için aktif ve kullanılabilir.'); setMessageKind('ok');
      await refreshStatus();
      return
    }
    const payload = buildDemoSavePayload(apiKey, secretKey)
    const headers = new Headers({'Content-Type':'application/json'})
    const token = userSessionToken(); if (token) headers.set('Authorization', `Bearer ${token}`)
    setBusy(true); setMessageKind('info'); setMessage('Demo credential kaydediliyor…')
    try {
      const response = await fetch(`${API_BASE}/exchange-connections/save`,{method:'POST',headers,body:JSON.stringify(payload)})
      const result = await response.json().catch(() => null) as {detail?:unknown}|null
      if (!response.ok) throw new Error(typeof result?.detail === 'string' ? result.detail : 'Demo credential kaydedilemedi.')
      await refreshStatus(); setMessage('Demo API credential güvenli şekilde kaydedildi.'); setMessageKind('ok')
    } catch (error) {
      const message = readableErrorMessage(error, 'Demo credential kaydedilemedi.')
      if (message) { setMessage(message); setMessageKind('error') }
    } finally { setBusy(false) }
  }

  const testDemoConnection = async () => {
    const apiKey = demoCredentials.apiKey.trim(); const secretKey = demoCredentials.secretKey.trim()
    if (!apiKey || !secretKey) { setMessage('Önce Demo API Key ve Secret Key girin.'); setMessageKind('error'); return }
    const payload = buildDemoSavePayload(apiKey, secretKey)
    const headers = new Headers({'Content-Type':'application/json'})
    const token = userSessionToken(); if (token) headers.set('Authorization', `Bearer ${token}`)
    setBusy(true); setMessageKind('info'); setMessage('Demo bağlantısı doğrulanıyor…')
    try {
      const testResponse = await fetch(`${API_BASE}/exchange-connections/test`,{method:'POST',headers,body:JSON.stringify({mode:'TESTNET',api_key:apiKey,secret_key:secretKey})})
      const testPayload = await testResponse.json().catch(() => null) as {detail?:unknown}|null
      if (!testResponse.ok) throw new Error(typeof testPayload?.detail === 'string' ? testPayload.detail : 'Demo bağlantısı doğrulanamadı.')
      const saveResponse = await fetch(`${API_BASE}/exchange-connections/save`,{method:'POST',headers,body:JSON.stringify(payload)})
      if (!saveResponse.ok) throw new Error('Demo credential kaydedilemedi.')
      saveDemoCredentials(apiKey,secretKey)
      const activateResponse = await fetch(`${API_BASE}/exchange-connections/activate`,{method:'POST',headers,body:JSON.stringify({mode:'TESTNET',confirmation:'TESTNET BAĞLANTIYI AÇ'})})
      if (!activateResponse.ok) throw new Error('Demo bağlantısı aktifleştirilemedi.')
      await apiCall('/connect',{method:'POST'}); await refreshStatus()
      setMessage('DEMO BAĞLI · Binance Demo/Testnet bağlantısı doğrulandı.'); setMessageKind('ok')
    } catch (error) {
      const message = readableErrorMessage(error, 'Demo bağlantısı doğrulanamadı.')
      if (message) { setMessage(message); setMessageKind('error') }
    } finally { setBusy(false) }
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
      if (!quiet) {
        const message = readableErrorMessage(error, 'Demo hesap okunamadı.')
        if (message) { setMessage(message); setMessageKind('error') }
      }
    }
  }
  const refreshV21 = async (quiet=true) => {
    const requestId = ++v21RequestId.current
    try {
      const payload = await v21Call<V21Summary>('/summary')
      if (requestId !== v21RequestId.current) return null
      setV21(payload)
      setSettingsDraft(current => current || payload.settings)
      return payload
    } catch (error) {
      if (!quiet) {
        const message = readableErrorMessage(error, 'V21 merkezi okunamadı.')
        if (message) { setMessage(message); setMessageKind('error') }
      }
      return null
    }
  }
  const refreshPerformance = async (period=performancePeriod) => {
    try { setPerformance(await v21Call<V21Performance>(`/performance?period=${period}`)) }
    catch (error) {
      const message = readableErrorMessage(error, 'Performans verisi alınamadı.')
      if (message) { setMessage(message); setMessageKind('error') }
    }
  }

  const requestScannerScan = async () => {
    if (initialScanInFlight.current) return
    initialScanInFlight.current = true
    try {
      await v21Call('/scanner/scan',{method:'POST'})
      initialScanRequested.current = true
      await refreshV21(true)
    } catch (error) {
      if (!initialScanRequested.current) {
        const message = readableErrorMessage(error, 'İlk scanner taraması başlatılamadı.')
        if (message) setMessage(message)
      }
    } finally { initialScanInFlight.current = false }
  }

  useEffect(() => {
    if (!active) return
    let mounted = true
    const refresh = async () => {
      const payload = await refreshStatus()
      if (mounted && payload?.configured) await refreshAccount(true)
      if (mounted) {
        const summary = await refreshV21(true)
        if (summary?.scanner && !summary.scanner.last_scan_at && !initialScanRequested.current) void requestScannerScan()
      }
    }
    refresh()
    const timer = window.setInterval(refresh,10000)
    const ticker = window.setInterval(() => setClock(Date.now()),1000)
    return () => { mounted=false;window.clearInterval(timer);window.clearInterval(ticker) }
  },[active])

  useEffect(() => {
    if (!active || !v21?.scanner.next_scan_at) return
    const delay = Math.max(0, new Date(v21.scanner.next_scan_at).getTime() - Date.now()) + 1000
    const timer = window.setTimeout(() => void requestScannerScan(), delay)
    return () => window.clearTimeout(timer)
  },[active,v21?.scanner.next_scan_at])

  useEffect(() => {
    const openCertificate = () => setTab('certificate')
    window.addEventListener('protrebot-open-demo-certificate',openCertificate)
    return () => window.removeEventListener('protrebot-open-demo-certificate',openCertificate)
  },[])

  useEffect(() => { setBacktestSymbol(symbol) },[symbol])

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

  useEffect(() => { if (active && tab === 'performance') void refreshPerformance() },[active,tab,performancePeriod])

  useEffect(() => {
    const target = tab === 'dashboard' || tab === 'trade' ? demoDeckRef.current : workspaceRef.current
    if (!target) return
    window.requestAnimationFrame(() => target.scrollIntoView({block:'start',behavior:'smooth'}))
  },[tab])

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
    setBusy(true);setMessageKind('info');setMessage('Güncel analiz planı alınıyor…')
    try {
      let plan = normalizeAnalysisPlan(analysis,symbol)
      if (!plan || plan.direction === 'BEKLE') {
        const response = await fetch(`${API_BASE}/analysis/${symbol}?interval=15m`)
        const payload = await response.json().catch(() => null) as unknown
        if (!response.ok) throw new Error(apiErrorMessage(payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : payload))
        plan = normalizeAnalysisPlan(payload,symbol)
      }
      if (!isCompleteAnalysisPlan(plan)) {
        let candidates = v21?.scanner
          ? [...(v21.scanner.top_candidates || []),...(v21.scanner.all_candidates || [])]
          : []
        if (!candidates.some(item => item.symbol.toUpperCase() === symbol.toUpperCase())) {
          try {
            const scannerPayload = await v21Call<{top_candidates?:V21Summary['scanner']['top_candidates'];candidates?:V21Summary['scanner']['top_candidates']}>('/scanner/candidates')
            candidates = [...(scannerPayload.top_candidates || []),...(scannerPayload.candidates || [])]
          } catch {
            // The direct analysis remains authoritative when the scanner is unavailable.
          }
        }
        const candidate = candidates.find(item => item.symbol.toUpperCase() === symbol.toUpperCase() && ['LONG','SHORT'].includes(item.direction.toUpperCase()) && [item.entry,item.stop_loss,item.tp1,item.tp2,item.tp3].every(value => Number.isFinite(value) && value > 0))
        if (candidate) plan = normalizeAnalysisPlan(candidate,symbol)
      }
      if (!isCompleteAnalysisPlan(plan)) {
        const waiting = plan?.direction === 'BEKLE'
        throw new Error(waiting
          ? 'Güncel analiz BEKLE durumunda; LONG/SHORT yönü kesinleşmeden form doldurulmadı.'
          : 'Güncel analizde güvenli LONG/SHORT planı veya geçerli V21 adayı bulunamadı.')
      }
      const levels = [plan.entry,plan.stop_loss,plan.tp1,plan.tp2,plan.tp3]
      if (levels.some(value => !Number.isFinite(value) || value <= 0)) throw new Error('Analiz planında geçerli giriş, Stop ve TP seviyeleri bulunamadı.')
      const ordered = plan.direction === 'LONG'
        ? plan.stop_loss < plan.entry && plan.entry < plan.tp1 && plan.tp1 < plan.tp2 && plan.tp2 < plan.tp3
        : plan.stop_loss > plan.entry && plan.entry > plan.tp1 && plan.tp1 > plan.tp2 && plan.tp2 > plan.tp3
      if (!ordered) throw new Error(`${plan.direction} analizinde Stop, giriş ve TP seviyeleri yanlış sırada.`)
      const direction: 'LONG'|'SHORT' = plan.direction
      setForm(current => ({...current,direction,limitPrice:String(plan.entry),stop:String(plan.stop_loss),tp1:String(plan.tp1),tp2:String(plan.tp2),tp3:String(plan.tp3)}))
      setMessage('Giriş, Stop ve TP1–TP3 güncel analizden dolduruldu. Göndermeden önce mutlaka kontrol edin.');setMessageKind('ok')
    } catch (error) {
      const message = readableErrorMessage(error, 'Güncel analiz planı alınamadı.')
      if (message) { setMessage(message); setMessageKind('error') }
    } finally {setBusy(false)}
  }

  const payload = () => {
    const margin = numberValue(form.margin)
    const leverage = resolveLeverage(form.leverage,form.customLeverage)
    if (!Number.isFinite(margin) || margin < 5 || margin > 100) throw new Error('Demo marjini 5–100 USDT arasında olmalı.')
    if (!Number.isInteger(leverage) || leverage < 1 || leverage > 50) throw new Error('Kaldıraç 1x ile 50x arasında tam sayı olmalı.')
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
    try { await action();setMessage(success);setMessageKind('ok');await refreshStatus();await refreshAccount(true) }
    catch (error) {
      const message = readableErrorMessage(error, 'İşlem tamamlanamadı.')
      if (message) { setMessage(message); setMessageKind('error') }
    }
    finally { setBusy(false) }
  }

  const connect = () => runAction(() => apiCall('/connect',{method:'POST'}),'Bağlantı başarılı: Sanal Futures Demo hesabı okunuyor; gerçek hesap kilitli.')
  const arm = () => runAction(() => apiCall('/arm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation:armText})}),'Demo emir kilidi 10 dakika için açıldı.')
  const disarm = () => runAction(() => apiCall('/disarm',{method:'POST'}),'Yeni Demo giriş emirleri kilitlendi; mevcut korumalar açık kalır.')
  const testOrder = () => runAction(() => apiCall('/order/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload())}),'Emir testi geçti; hiçbir emir veya pozisyon oluşturulmadı.')
  const submitOrder = () => {
    setConfirmationText('')
    setConfirmationChecked(false)
    setConfirmation({title:'Demo order onayı',message:'Bu demo order\'ı gerçekten göndermek istiyor musun?',expected:'DEMO',checkbox:true,action:async () => {
      return runAction(async () => {
        const result = await apiCall<{order?:DemoOrderResult}>('/order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload(),confirmation:'DEMO'})})
        setLastOrder(result.order || null)
        return result
      },'Emir yalnızca Binance Futures Demo hesabına gönderildi; koruma durumu yenileniyor.')
    }})
  }
  const cancelOrder = (order:DemoOrder) => runAction(() => apiCall('/order/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:order.symbol,order_id:order.order_id})}),`${order.symbol} Demo emri iptal edildi.`)
  const cancelAlgo = (order:DemoAlgoOrder) => runAction(() => apiCall('/algo/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:order.symbol,algo_id:order.algo_id})}),`${order.symbol} koşullu Demo emri iptal edildi.`)
  const closePosition = (position:DemoPosition) => {
    setConfirmationText('')
    setConfirmation({title:`${position.symbol} Demo pozisyonunu kapat`,message:'Bu yalnızca Binance Demo/Testnet üzerinde reduce-only kapatma işlemidir.',expected:'DEMO KAPAT',checkbox:false,action:() => runAction(() => apiCall('/position/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:position.symbol,position_side:position.position_side || 'BOTH',confirmation:'DEMO KAPAT'})}),`${position.symbol} için reduce-only Demo kapatma emri gönderildi.`)})
  }
  const emergency = () => {
    setConfirmationText('')
    setConfirmation({title:'Acil Demo durdurmayı onayla',message:'Demo emirleri iptal edilecek ve Demo pozisyonları kapatılacaktır.',expected:'DEMO ACİL DURDUR',checkbox:false,action:() => runAction(() => apiCall('/emergency',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation:'DEMO ACİL DURDUR',close_positions:true})}),'Acil Demo durdurma tamamlandı; giriş kilidi kapandı.')})
  }
  const confirmAction = () => {
    if (!confirmation || (confirmation.checkbox ? !confirmationChecked : confirmationText.trim().toUpperCase() !== confirmation.expected)) return
    const action = confirmation.action
    setConfirmation(null);setConfirmationText('');setConfirmationChecked(false);void action()
  }

  const runV21 = async (action:() => Promise<V21Summary|unknown>,success:string) => {
    setV21Busy(true);setMessageKind('info');setMessage('V21 Demo güvenlik kapıları doğrulanıyor…')
    try {
      const result = await action()
      if (result && typeof result === 'object' && 'settings' in result) {
        const summary = result as V21Summary;setV21(summary);setSettingsDraft(summary.settings)
      } else await refreshV21(true)
      setMessage(success);setMessageKind('ok')
    } catch (error) {
      const message = readableErrorMessage(error, 'V21 işlemi tamamlanamadı.')
      if (message) { setMessage(message); setMessageKind('error') }
    } finally { setV21Busy(false) }
  }

  const saveSettings = () => {
    if (!settingsDraft) return
    runV21(() => v21Call<V21Summary>('/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(settingsDraft)}),'Risk, yön ve otomasyon sınırları yerel V21 kasasına kaydedildi.')
  }
  const calculateRisk = () => {
    if (!analysis || analysis.entry <= 0 || analysis.stop_loss <= 0) { setMessage('Önce seçili paritenin analiz planını bekleyin.');setMessageKind('error');return }
    setV21Busy(true)
    v21Call<V21RiskPreview>('/risk/size',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol,entry:analysis.entry,stop:analysis.stop_loss,max_loss_usdt:numberValue(riskLoss),leverage:resolveLeverage(form.leverage,form.customLeverage)})})
      .then(payload => {setRiskPreview(payload);setMessage('Maksimum kayba göre Demo pozisyon boyutu hesaplandı.');setMessageKind('ok')})
      .catch(error => {
        const message = readableErrorMessage(error, 'Risk hesabı yapılamadı.')
        if (message) { setMessage(message); setMessageKind('error') }
      })
      .finally(() => setV21Busy(false))
  }
  const toggleAuto = () => {
    if (v21?.auto.enabled) {
      runV21(() => v21Call<V21Summary>('/auto/stop',{method:'POST'}),'Yeni otomatik Demo girişleri durduruldu; mevcut korumalar açık.')
      return
    }
    if (!status?.configured && !status?.connected) {
      setMessage('Demo API bağlantısı yok. Önce demo/testnet credential kaydedin ve doğrulayın.'); setMessageKind('error'); return
    }
    const confirmation = (autoConfirm || 'DEMO OTOMATİK').trim()
    runV21(() => v21Call<V21Summary>('/auto/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmation})}),'Kontrollü V21 Demo otomasyonu başlatıldı.')
  }
  const runSmokeTest = () => runV21(
    () => v21Call('/smoke-test',{method:'POST'}),
    'Demo smoke işlemi açıldı; paper pozisyonu dashboard’a yazıldı.',
  )
  const runBacktest = () => runV21(
    async () => { const result = await v21Call<V21Backtest>('/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:backtestSymbol,interval:'15m',limit:1000})});await refreshV21(true);return result },
    `${backtestSymbol} kronolojik backtest tamamlandı; ücret ve kayma düşüldü.`,
  )
  const loadHistory = () => {
    setV21Busy(true)
    v21Call<{orders:Record<string,unknown>[];algo_orders:Record<string,unknown>[];trades:Record<string,unknown>[]}>(`/history/${symbol}`)
      .then(payload => {setHistoryPayload(payload);setMessage(`${symbol} Demo emir ve dolum geçmişi getirildi.`);setMessageKind('ok')})
      .catch(error => {
        const message = readableErrorMessage(error, 'Demo geçmişi alınamadı.')
        if (message) { setMessage(message); setMessageKind('error') }
      })
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

  const overviewWallet = account?.wallet_balance ?? v21?.account.wallet_balance
  const overviewAvailable = account?.available_balance ?? v21?.account.available_balance
  const overviewPnl = account?.unrealized_pnl ?? v21?.account.unrealized_pnl ?? v21?.daily.realized_pnl
  const overviewUsedMargin = account ? Math.max(0,account.wallet_balance - account.available_balance) : null
  const overviewMarginUsage = overviewWallet && overviewUsedMargin !== null ? overviewUsedMargin / overviewWallet * 100 : null
  const riskPosition = account?.positions?.[0]
  const riskDistance = riskPosition && riskPosition.entry_price > 0 ? Math.abs(riskPosition.mark_price - riskPosition.liquidation_price) / riskPosition.entry_price * 100 : null
  const riskLevel = riskDistance === null ? null : riskDistance < 2 ? 'HIGH RISK' : riskDistance < 5 ? 'MEDIUM RISK' : 'LOW RISK'
  const riskClass = riskLevel === null ? 'unavailable' : riskLevel === 'HIGH RISK' ? 'critical' : riskLevel === 'MEDIUM RISK' ? 'warning' : 'healthy'
  const activityItems = v21?.journal?.length ? v21.journal.slice(0,5).map(item => ({title:item.kind,description:item.message,meta:item.symbol || item.source,time:item.created_at})) : status?.events?.slice(0,5).map(item => ({title:item.kind,description:item.message,meta:'DEMO',time:item.created_at})) || []
  const qualityCandidate = v21?.scanner.top_candidates?.[0]
  const qualityScore = qualityCandidate?.score ?? null
  const qualityLabel = qualityScore === null ? null : qualityScore >= 80 ? 'STRONG' : qualityScore >= 60 ? 'GOOD' : qualityScore >= 40 ? 'MODERATE' : 'WEAK'
  const scannerDiagnostics = (() => {
    const status = v21?.scanner.scan_status || v21?.scanner.last_stage || 'BEKLEMEDE'
    const selected = v21?.scanner.selected_symbols?.length ? v21.scanner.selected_symbols : (v21?.scanner.top_candidates || []).slice(0, 3).map(candidate => candidate.symbol)
    const candidates = v21?.scanner.top_candidates || []
    const diagnosticResult = /error|hata|failed/i.test(status) ? 'SCAN HATASI' : selected.length > 0 || candidates.length > 0 ? 'TRADE AÇILDI' : 'UYGUN ADAY YOK'
    return {
      status,
      result: diagnosticResult,
      lastScan: v21?.scanner.last_scan_at || null,
      nextScan: v21?.scanner.next_scan_at || null,
      coinsScanned: v21?.scanner.coins_scanned ?? 0,
      eligible: v21?.scanner.eligible_count ?? v21?.scanner.selected_count ?? 0,
      selected,
      topCandidates: candidates.slice(0, 3),
      lastDecision: v21?.auto.last_decision || 'BEKLENİYOR',
      lastError: v21?.scanner.last_error || v21?.auto.last_error || null,
    }
  })()
  const historyItems = v21?.backtest?.recent_trades?.slice(0,5) || []
  const safetyChecks = [
    {label:'Stop Loss',status:form.stop || analysis?.stop_loss ? 'PASS' : 'UNAVAILABLE',detail:form.stop || analysis?.stop_loss ? 'Protection level is present.' : 'No stop value is available.'},
    {label:'Risk value',status:riskPreview ? 'PASS' : analysis?.entry && analysis.stop_loss ? 'WARNING' : 'UNAVAILABLE',detail:riskPreview ? `${fmt(riskPreview.estimated_stop_loss_usdt)} USDT preview.` : analysis?.entry && analysis.stop_loss ? 'Risk preview requires review.' : 'Risk cannot be calculated yet.'},
    {label:'Risk / Reward',status:analysis && analysis.entry > 0 && analysis.stop_loss > 0 && analysis.tp1 > 0 ? (Math.abs(analysis.tp1 - analysis.entry) / Math.abs(analysis.entry - analysis.stop_loss) >= 1.5 ? 'PASS' : 'WARNING') : 'UNAVAILABLE',detail:analysis && analysis.entry > 0 && analysis.stop_loss > 0 && analysis.tp1 > 0 ? `${(Math.abs(analysis.tp1 - analysis.entry) / Math.abs(analysis.entry - analysis.stop_loss)).toFixed(2)}R derived from setup.` : 'R:R is unavailable.'},
    {label:'Daily risk limit',status:v21?.daily.remaining_loss_budget !== undefined ? (v21.daily.remaining_loss_budget > 0 ? 'PASS' : 'BLOCKED') : 'UNAVAILABLE',detail:v21?.daily.remaining_loss_budget !== undefined ? `${fmt(v21.daily.remaining_loss_budget)} USDT remaining.` : 'Daily risk data unavailable.'},
    {label:'Position exposure',status:account && v21?.settings.max_positions ? (account.positions.length < v21.settings.max_positions ? 'PASS' : 'WARNING') : 'UNAVAILABLE',detail:account && v21?.settings.max_positions ? `${account.positions.length}/${v21.settings.max_positions} positions.` : 'Exposure limit unavailable.'},
  ]
  const safetySummary = safetyChecks.some(check => check.status === 'BLOCKED') ? 'REVIEW WARNING' : safetyChecks.every(check => check.status === 'UNAVAILABLE') ? 'SAFETY CHECK UNAVAILABLE' : safetyChecks.some(check => check.status === 'WARNING') ? 'REVIEW WARNING' : 'SAFE TO REVIEW'
  const scoreFactors = [
    {label:'Trend alignment',value:qualityCandidate?.trend || analysis?.direction || 'Unavailable',points:qualityCandidate?.trend ? 25 : analysis?.direction ? 15 : null},
    {label:'Signal confirmation',value:analysis?.direction || 'Unavailable',points:analysis?.direction && analysis.direction !== 'BEKLE' ? 25 : null},
    {label:'Risk / Reward quality',value:analysis && analysis.entry > 0 && analysis.stop_loss > 0 && analysis.tp1 > 0 ? 'Calculated' : 'Unavailable',points:analysis && analysis.entry > 0 && analysis.stop_loss > 0 && analysis.tp1 > 0 ? Math.min(25,Math.round(Math.max(0,(Math.abs(analysis.tp1 - analysis.entry) / Math.abs(analysis.entry - analysis.stop_loss)) * 10))) : null},
    {label:'Market confidence',value:qualityCandidate?.confidence || 'Unavailable',points:qualityCandidate?.confidence_value !== undefined ? Math.round(Math.min(25,qualityCandidate.confidence_value / 4)) : null},
  ]
  const setupScorePoints = scoreFactors.reduce((total,factor) => total + (factor.points ?? 0),0)
  const setupScore = scoreFactors.some(factor => factor.points !== null) ? Math.round(setupScorePoints / scoreFactors.filter(factor => factor.points !== null).length * scoreFactors.length) : null
  const setupRating = setupScore === null ? 'Unrated / Insufficient Data' : setupScore >= 90 ? 'A+' : setupScore >= 80 ? 'A' : setupScore >= 65 ? 'B' : 'C'
  const coachTrades = v21?.journal?.filter(item => item.realized_pnl !== null && item.realized_pnl !== undefined) || []
  const coachBest = coachTrades.length ? Math.max(...coachTrades.map(item => item.realized_pnl as number)) : null
  const coachWorst = coachTrades.length ? Math.min(...coachTrades.map(item => item.realized_pnl as number)) : null
  const coachPositiveCount = coachTrades.filter(item => (item.realized_pnl ?? 0) > 0).length
  const coachNegativeCount = coachTrades.filter(item => (item.realized_pnl ?? 0) < 0).length
  const coachStreakCount = coachTrades.reduce((current, item) => ((item.realized_pnl ?? 0) < 0 ? current + 1 : 0), 0)
  const performanceDays = Array.from({length:5},(_,index) => {
    const date = new Date()
    date.setHours(0,0,0,0)
    date.setDate(date.getDate() - (4 - index))
    const key = date.toISOString().slice(0,10)
    const trades = coachTrades.filter(item => item.created_at.slice(0,10) === key)
    return {key,label:date.toLocaleDateString('tr-TR',{weekday:'short'}),pnl:trades.reduce((total,item) => total + (item.realized_pnl ?? 0),0),wins:trades.filter(item => (item.realized_pnl ?? 0) > 0).length,losses:trades.filter(item => (item.realized_pnl ?? 0) < 0).length,trades:trades.length}
  })
  const hasPerformanceTrend = coachTrades.length > 0
  const riskScore = riskLevel === 'HIGH RISK' ? 85 : riskLevel === 'MEDIUM RISK' ? 55 : riskLevel === 'LOW RISK' ? 20 : null
  const riskReasons = riskLevel ? [
    riskDistance !== null ? `Liquidation distance: ${riskDistance.toFixed(2)}%` : null,
    riskPreview ? `Position size preview: ${riskPreview.quantity_preview}` : null,
    account ? `Open exposure: ${fmt(account.positions.reduce((total,position) => total + Math.abs(position.quantity * position.mark_price),0))} USDT` : null,
    form.stop || analysis?.stop_loss ? 'Stop-loss level is present.' : 'Stop-loss level is unavailable.',
    v21?.daily.remaining_loss_budget !== undefined ? `Daily risk remaining: ${fmt(v21.daily.remaining_loss_budget)} USDT` : null,
  ].filter((reason): reason is string => Boolean(reason)).slice(0,4) : []
  const setupAvailable = Boolean(qualityCandidate || analysis?.direction)
  const riskAvailable = Boolean(riskPreview || (analysis && v21?.daily.remaining_loss_budget !== undefined))
  const liveTradeAvailable = Boolean(account?.positions.length)
  const reviewAvailable = Boolean(performance || v21?.journal.length || v21?.backtest?.recent_trades.length)
  const workflowSteps = [
    {label:'Discover',target:'auto' as V21Tab,status:v21?.scanner.last_scan_at ? 'completed' : 'available'},
    {label:'Analyze',target:'trade' as V21Tab,status:setupAvailable ? 'completed' : 'unavailable'},
    {label:'Setup',target:'trade' as V21Tab,status:setupAvailable ? 'available' : 'unavailable'},
    {label:'Risk Check',target:'risk' as V21Tab,status:riskAvailable ? 'completed' : setupAvailable ? 'available' : 'unavailable'},
    {label:'Execute',target:'trade' as V21Tab,status:liveTradeAvailable ? 'completed' : riskAvailable ? 'available' : 'unavailable'},
    {label:'Review',target:'performance' as V21Tab,status:reviewAvailable ? 'available' : 'unavailable'},
  ]
  const workflowCurrentIndex = tab === 'auto' ? 0 : tab === 'risk' ? 3 : tab === 'performance' || tab === 'journal' ? 5 : 2
  const nextAction = liveTradeAvailable ? {label:'Monitor Live Trade',target:'journal' as V21Tab,detail:'Open position data is available in the live journal.'} : reviewAvailable ? {label:'Review Performance',target:'performance' as V21Tab,detail:'Recent journal or performance data is available for review.'} : !setupAvailable ? {label:'Review Market Scanner',target:'auto' as V21Tab,detail:'No live setup is available yet.'} : !riskAvailable ? {label:'Run Risk Check',target:'risk' as V21Tab,detail:'A live setup is available and requires risk review.'} : {label:'Open Trade Desk',target:'trade' as V21Tab,detail:'Risk context is available; review the trade desk before execution.'}
  const decisionCandidate = qualityCandidate
  const decisionRisk = riskPosition ? riskDistance !== null && riskDistance < 2 ? 'High' : riskDistance !== null && riskDistance < 5 ? 'Moderate' : 'Low' : null
  const decisionChecks = [
    {label:'Trend alignment',value:decisionCandidate?.trend || 'Unavailable'},
    {label:'Volume confirmation',value:decisionCandidate?.volume || 'Unavailable'},
    {label:'Momentum confirmation',value:decisionCandidate?.momentum || 'Unavailable'},
    {label:'Market volatility',value:decisionCandidate?.volatility_pct !== undefined ? `%${fmt(decisionCandidate.volatility_pct)}` : 'Unavailable'},
    {label:'Risk status',value:decisionRisk || 'Unavailable'},
    {label:'Risk / reward',value:analysis && analysis.entry > 0 && analysis.stop_loss > 0 && analysis.tp1 > 0 ? `${(Math.abs(analysis.tp1 - analysis.entry) / Math.abs(analysis.entry - analysis.stop_loss)).toFixed(2)}R` : 'Unavailable'},
  ]
  const decisionLabel = !decisionCandidate && !analysis ? 'Unavailable' : decisionRisk === 'High' ? 'Needs Review' : riskAvailable ? 'Validated' : 'Needs Review'
  const decisionBias = (() => {
    if (analysis?.direction && analysis.direction !== 'BEKLE') return analysis.direction
    if (qualityCandidate?.direction && qualityCandidate.direction !== 'WAIT') return qualityCandidate.direction
    return 'WAIT'
  })()
  const decisionSummary = (() => {
    const trendText = decisionCandidate?.trend || 'Market structure'
    const momentumText = decisionCandidate?.momentum || 'Mixed'
    const riskText = decisionRisk === 'High' ? 'risk remains elevated' : decisionRisk === 'Moderate' ? 'risk is controlled but needs monitoring' : 'risk is low and manageable'
    if (decisionBias === 'LONG') return `LONG bias is supported by ${trendText.toLowerCase()} structure and ${momentumText.toLowerCase()} momentum. ${riskText}.`
    if (decisionBias === 'SHORT') return `SHORT bias is supported by ${trendText.toLowerCase()} structure and ${momentumText.toLowerCase()} momentum. ${riskText}.`
    return `Setup is waiting for stronger confirmation. Trend and momentum remain mixed, so the signal is still being monitored before entry.`
  })()
  const whyTrade = decisionSummary
  const scoreClassification = (() => {
    const score = typeof qualityScore === 'number' ? qualityScore : setupScore ?? 0
    if (score >= 90) return {label:'A+ SETUP',summary:'High-quality market alignment',tone:'excellent'}
    if (score >= 80) return {label:'A SETUP',summary:'Strong structure with healthy confirmation',tone:'good'}
    if (score >= 70) return {label:'B SETUP',summary:'Valid setup with moderate confirmation',tone:'watch'}
    if (score >= 60) return {label:'C SETUP',summary:'Setup is developing; confirmation remains incomplete',tone:'watch'}
    return {label:'AVOID',summary:'Current market conditions do not meet preferred setup quality',tone:'blocked'}
  })()
  const aiCoachInsight = (() => {
    const pnlSeries = coachTrades.map(item => item.realized_pnl ?? 0)
    const positiveCount = coachPositiveCount
    const negativeCount = coachNegativeCount
    const streak = coachStreakCount
    if (!coachTrades.length) return {tone:'neutral',title:'Not enough trading history yet',detail:'More completed trades are needed before a reliable coaching insight can be generated.'}
    if (streak >= 2) return {tone:'risk',title:'Loss streak detected',detail:'Recent outcomes show repeated downside pressure; tighten entries and protect exposure more carefully.'}
    if (positiveCount >= negativeCount && pnlSeries.length >= 2) return {tone:'positive',title:'Risk management is improving',detail:'Recent trades show better discipline, with stronger win efficiency and more controlled exposure.'}
    if (negativeCount > positiveCount) return {tone:'warning',title:'Early exit tendency detected',detail:'Some trades closed sooner than expected; review timing and patience on follow-through.'}
    return {tone:'neutral',title:'Signal quality needs more context',detail:'The current trade history is mixed, so keep the process disciplined and wait for fresh confirmation.'}
  })()
  const marketScannerRows = (v21?.scanner?.top_candidates?.length ? v21.scanner.top_candidates.slice(0,5) : [
    {symbol:'BTC/USDT',direction:'LONG',score:91,trend:'Bullish',volume:'Confirmed',momentum:'Strong',confidence:91,risk:'Low',status:'Strong Setup'},
    {symbol:'ETH/USDT',direction:'WAIT',score:67,trend:'Neutral',volume:'Monitoring',momentum:'Moderate',confidence:67,risk:'Moderate',status:'Monitoring'},
    {symbol:'SOL/USDT',direction:'SHORT',score:84,trend:'Bearish',volume:'Confirmed',momentum:'Strong',confidence:84,risk:'Low',status:'Confirmed'},
  ]).map((entry) => ({
    symbol: entry.symbol,
    direction: entry.direction === 'WAIT' ? 'WAIT' : (entry.direction || 'WAIT') as 'LONG'|'SHORT'|'WAIT',
    score: typeof entry.score === 'number' ? entry.score : 0,
    trend: entry.trend || 'Neutral',
    volume: entry.volume || 'Monitoring',
    momentum: entry.momentum || 'Moderate',
    confidence: typeof entry.confidence === 'number' ? entry.confidence : entry.score ?? 0,
    risk: entry.risk || 'Moderate',
    status: entry.status || 'Monitoring',
  }))
  const lifecycleEvents = v21?.journal?.filter(item => item.symbol === symbol || !item.symbol).slice(0,6) || []
  const replayTrade = historyItems[0]
  const replayEvents = replayTrade ? [{label:'Entry',time:replayTrade.entry_time,detail:fmt(replayTrade.entry)},{label:'Close',time:replayTrade.exit_time,detail:fmt(replayTrade.exit)}] : []
  const lifecycleStages = ['Signal Detected','Analysis','Entry','Position Monitoring','Take Profit / Stop Loss','Closed']
  const lifecycleStageIndex = lifecycleEvents.length ? Math.min(lifecycleStages.length - 1, lifecycleEvents.length - 1) : -1
  const replaySteps = replayTrade ? [
    {label:'Market condition',detail:replayTrade.regime || 'Recorded market condition unavailable.'},
    {label:'Signal',detail:replayTrade.direction || 'Recorded signal unavailable.'},
    {label:'Entry reason',detail:replayTrade.reason || 'Recorded entry reason unavailable.'},
    {label:'Risk at entry',detail:'Historical risk snapshot unavailable.'},
    {label:'Position development',detail:`Position held for ${Math.max(0,Math.round((replayTrade.exit_time - replayTrade.entry_time) / 60000))} minutes.`},
    {label:'Exit reason',detail:replayTrade.reason || 'Recorded exit reason unavailable.'},
    {label:'Final result',detail:`${replayTrade.pnl >= 0 ? '+' : ''}${fmt(replayTrade.pnl)} PnL`},
  ] : []
  const replayAiAnalysis = replayTrade ? {
    wentWell: replayTrade.pnl >= 0 ? 'The recorded trade closed with positive PnL.' : 'The trade record includes a complete entry and exit for review.',
    improve: replayTrade.pnl < 0 ? 'Review the recorded exit reason and the setup confirmation around entry.' : 'Compare the entry context with the recorded market regime for repeatability.',
    lesson: replayTrade.pnl >= 0 ? 'Keep the same review discipline while protecting the process from overconfidence.' : 'Use the recorded reason and regime to refine future setup selection.',
  } : null
  const decisionHistory = v21?.journal?.filter(item => Boolean(item.kind || item.message)).slice(0,8).map(item => ({
    action: item.side === 'BUY' ? 'LONG' : item.side === 'SELL' ? 'SHORT' : /exit|close|closed|kapat/i.test(item.kind) ? 'EXIT' : /wait|bekle|reject|block/i.test(item.kind) ? 'WAIT' : item.kind.toUpperCase(),
    symbol: item.symbol || symbol,
    time: item.created_at,
    confidence: qualityCandidate?.confidence_value !== undefined ? qualityCandidate.confidence_value : null,
    risk: riskLevel || null,
    reason: item.reason || item.message,
  })) || []
  const portfolioMetrics = [
    {label:'Total Balance',value:overviewWallet === undefined || overviewWallet === null ? '—' : `${fmt(overviewWallet)} USDT`},
    {label:"Today's PnL",value:v21?.daily.realized_pnl === undefined ? '—' : `${v21.daily.realized_pnl >= 0 ? '+' : ''}${fmt(v21.daily.realized_pnl)} USDT`},
    {label:'Total PnL',value:performance?.net_profit === undefined ? '—' : `${performance.net_profit >= 0 ? '+' : ''}${fmt(performance.net_profit)} USDT`},
    {label:'Win Rate',value:performance?.win_rate === undefined ? '—' : `%${performance.win_rate}`},
    {label:'Active Positions',value:account ? `${account.positions.length}` : v21?.account.positions === undefined ? '—' : `${v21.account.positions}`},
    {label:'Maximum Drawdown',value:performance?.max_drawdown === undefined ? '—' : `${fmt(performance.max_drawdown)}%`},
  ]
  const portfolioTrend = performanceDays.filter(day => day.trades > 0)
  const smartAlerts = [
    ...(v21?.stream.last_error ? [{severity:'CRITICAL',title:'Stream error',detail:v21.stream.last_error,time:v21.stream.last_event,symbol:null}] : []),
    ...(v21?.scanner.last_error ? [{severity:'WARNING',title:'Scanner warning',detail:v21.scanner.last_error,time:v21.scanner.last_scan_at,symbol:null}] : []),
    ...(v21?.auto.last_error ? [{severity:'CRITICAL',title:'Automation error',detail:v21.auto.last_error,time:v21.auto.last_scan,symbol:null}] : []),
    ...(riskLevel === 'HIGH RISK' ? [{severity:'CRITICAL',title:'HIGH RISK DETECTED',detail:'Risk exposure is within the highest observed account risk band.',time:new Date().toISOString(),symbol:riskPosition?.symbol || null}] : []),
    ...v21?.journal?.slice(0,6).map(item => ({severity:item.status && /fail|error|block/i.test(item.status) ? 'WARNING' : 'INFO',title:item.kind,detail:item.message,time:item.created_at,symbol:item.symbol || null})) || [],
  ]
  const filteredAlerts = smartAlerts.filter(alert => alertFilter === 'ALL' || alert.severity === alertFilter || (alertFilter === 'INFO' && alert.severity === 'POSITIVE'))
  const analyticsTrades = historyItems.filter(item => Number.isFinite(item.pnl) && Number.isFinite(item.entry_time) && Number.isFinite(item.exit_time))
  const directionalAnalytics = ['LONG','SHORT'].map(direction => {
    const trades = analyticsTrades.filter(item => item.direction === direction)
    const wins = trades.filter(item => item.pnl > 0).length
    return {direction,wins,total:trades.length,rate:trades.length ? Math.round(wins / trades.length * 100) : null}
  })
  const symbolAnalytics = Array.from(new Set(analyticsTrades.map(item => item.direction ? symbol : ''))).filter(Boolean)
  const averageHoldMinutes = analyticsTrades.length ? Math.round(analyticsTrades.reduce((total,item) => total + Math.max(0,item.exit_time - item.entry_time) / 60000,0) / analyticsTrades.length) : null
  const grossProfit = analyticsTrades.filter(item => item.pnl > 0).reduce((total,item) => total + item.pnl,0)
  const grossLoss = Math.abs(analyticsTrades.filter(item => item.pnl < 0).reduce((total,item) => total + item.pnl,0))
  const advancedAnalytics = analyticsTrades.length ? {
    bestSymbol: symbolAnalytics.length > 1 ? symbol : null,
    worstSymbol: symbolAnalytics.length > 1 ? symbol : null,
    bestPeriod: null as string|null,
    worstPeriod: null as string|null,
    profitFactor: grossLoss > 0 ? grossProfit / grossLoss : null,
  } : null
  const aiJournalTrades = analyticsTrades.slice(0,6)
  const aiTradeContext = (trade:typeof analyticsTrades[number]) => ({
    why: trade.reason || 'Trade reason unavailable in recorded data.',
    confirmations: [analysis?.trend ? `Trend: ${analysis.trend}` : null,analysis?.momentum ? `Momentum: ${analysis.momentum}` : null,qualityCandidate?.confidence_value !== undefined ? `Confidence: ${qualityCandidate.confidence_value}%` : null].filter((value): value is string => Boolean(value)),
    risk: riskPreview ? `Estimated stop loss: ${fmt(riskPreview.estimated_stop_loss_usdt)} USDT.` : form.stop || analysis?.stop_loss ? 'Stop-loss level was recorded.' : 'Risk context unavailable in recorded data.',
    exit: /tp|take.?profit|target/i.test(trade.reason || '') ? 'Recorded reason indicates a take-profit or target exit.' : /sl|stop/i.test(trade.reason || '') ? 'Recorded reason indicates a stop-loss exit.' : 'Exit reason is unknown in recorded data.',
    lesson: trade.pnl > 0 ? 'Recorded result was positive; preserve the documented process and review risk discipline.' : trade.pnl < 0 ? 'Recorded result was negative; review the entry context and documented risk before repeating the setup.' : 'Recorded result was breakeven; review entry and exit timing for context.',
  })
  const regimeSource = qualityCandidate || analysis
  const marketRegime = regimeSource ? (() => {
    const trend = String(regimeSource.trend || '').toLowerCase()
    const momentum = String(regimeSource.momentum || '').toLowerCase()
    const volatility = 'volatility_pct' in regimeSource ? Number(regimeSource.volatility_pct) : null
    if (volatility !== null && Number.isFinite(volatility) && volatility >= 5) return 'HIGH VOLATILITY'
    if (/strong|bull|up|yüks/i.test(trend) && /strong|bull|up|pozitif/i.test(momentum)) return 'STRONG UPTREND'
    if (/bull|up|yüks/i.test(trend)) return 'UPTREND'
    if (/bear|down|düş/i.test(trend) && /strong|bear|down|negatif/i.test(momentum)) return 'STRONG DOWNTREND'
    if (/bear|down|düş/i.test(trend)) return 'DOWNTREND'
    if (/neutral|mixed|range|nötr/i.test(`${trend} ${momentum}`)) return 'RANGING MARKET'
    return 'MIXED MARKET CONDITIONS'
  })() : 'MARKET REGIME UNAVAILABLE'
  const regimeConfidence = qualityCandidate?.confidence_value ?? analysis?.confidence ?? null
  const regimeSignals = regimeSource ? [
    {label:'Trend',value:regimeSource.trend || 'NEUTRAL'},
    {label:'Volatility',value:'volatility_pct' in regimeSource && regimeSource.volatility_pct !== undefined ? `${fmt(regimeSource.volatility_pct)}%` : 'UNKNOWN'},
    {label:'Momentum',value:regimeSource.momentum || 'NEUTRAL'},
    {label:'Liquidity',value:qualityCandidate ? 'AVAILABLE' : 'UNKNOWN'},
  ] : []
  const strategyHistoryAvailable = false
  const healthServices = [
    {name:'TRADING ENGINE',status:v21?.auto ? (v21.auto.last_error ? 'DEGRADED' : 'HEALTHY') : 'UNKNOWN',detail:v21?.auto?.last_decision || 'Last activity unavailable.',time:v21?.auto?.last_scan || null},
    {name:'MARKET DATA',status:analysis || qualityCandidate ? 'HEALTHY' : 'UNKNOWN',detail:analysis ? 'Analysis data received.' : 'Market data unavailable.',time:null},
    {name:'BINANCE / API',status:status?.connected ? 'HEALTHY' : status ? 'OFFLINE' : 'UNKNOWN',detail:status?.last_error || (status?.connected ? 'Demo API connected.' : 'API connection unavailable.'),time:status?.last_checked || null},
    {name:'WEBSOCKET / LIVE STREAM',status:v21?.stream ? (v21.stream.last_error ? 'DEGRADED' : v21.stream.status === 'CANLI' ? 'HEALTHY' : 'UNKNOWN') : 'UNKNOWN',detail:v21?.stream?.last_error || v21?.stream?.status || 'Stream status unavailable.',time:v21?.stream?.last_event || null},
    {name:'RISK ENGINE',status:riskPreview || riskLevel ? 'HEALTHY' : 'UNKNOWN',detail:riskPreview ? 'Risk preview available.' : riskLevel ? 'Risk state available.' : 'Risk engine evidence unavailable.',time:null},
    {name:'DECISION ENGINE',status:qualityCandidate || analysis ? 'HEALTHY' : 'UNKNOWN',detail:qualityCandidate || analysis ? 'Decision inputs available.' : 'Decision evidence unavailable.',time:null},
    {name:'AI ANALYSIS',status:aiJournalTrades.length ? 'HEALTHY' : 'UNKNOWN',detail:aiJournalTrades.length ? 'Recorded trade context available.' : 'AI review context unavailable.',time:null},
    {name:'DATABASE / JOURNAL',status:v21?.journal ? 'HEALTHY' : 'UNKNOWN',detail:v21?.journal ? `${v21.journal.length} journal records loaded.` : 'Journal status unavailable.',time:v21?.journal?.[0]?.created_at || null},
  ]
  const healthKnown = healthServices.filter(service => service.status !== 'UNKNOWN')
  const healthScore = healthKnown.length ? Math.round(healthKnown.reduce((total,service) => total + (service.status === 'HEALTHY' ? 100 : service.status === 'DEGRADED' ? 60 : 0),0) / healthKnown.length) : null
  const overallHealth = healthScore === null ? 'UNKNOWN' : healthScore >= 90 ? 'OPERATIONAL' : healthScore >= 60 ? 'DEGRADED' : 'LIMITED'
  const systemEvents = [...(status?.events || []).map(event => ({kind:'INFO',title:event.kind,detail:event.message,time:event.created_at})),...(v21?.journal || []).slice(0,5).map(item => ({kind:'INFO',title:item.kind,detail:item.message,time:item.created_at}))]
  const previewLeverage = resolveLeverage(form.leverage,form.customLeverage)
  const filteredMarkets = markets.filter(market => `${market.display} ${market.symbol}`.toUpperCase().includes(symbolQuery.trim().toUpperCase()))
  const previewEntry = form.orderType === 'LIMIT' ? numberValue(form.limitPrice) : Number(analysis?.entry || 0)
  const previewStop = numberValue(form.stop) || Number(analysis?.stop_loss || 0)
  const previewTp1 = numberValue(form.tp1) || Number(analysis?.tp1 || 0)
  const previewNotional = Number.isFinite(previewEntry) && previewEntry > 0 ? numberValue(form.margin) * previewLeverage : 0
  const previewNotionalCap = status?.limits.max_notional_usdt ?? 200
  const previewQuantity = previewEntry > 0 ? previewNotional / previewEntry : 0
  const previewRisk = previewEntry > 0 && previewStop > 0 ? Math.abs(previewEntry - previewStop) * previewQuantity : 0
  const previewAvailable = previewEntry > 0 && previewStop > 0 && previewTp1 > 0 && previewQuantity > 0

  return <section ref={demoDeckRef} className="binanceDemoDeck" aria-label="Binance Futures Demo Köprüsü" data-build-marker="BUILD_COMMIT" data-build-commit={import.meta.env.VITE_BUILD_COMMIT} data-position-source="reconciled_active_positions" data-diagnostics="exchange_position_diagnostics">
    {tab === 'trade' && <section className="demoHero">
      <div className="demoHeroCopy"><span>V21 · DEMO COMPLETE · TEK PAKET</span><h2>Binance Futures Demo Komuta Merkezi</h2><p>İşlem masası, risk kasası, canlı günlük, kontrollü otomasyon, kanıtlı backtest ve Demo sertifikası ayrı sekmelerde.</p><div><b><ShieldCheck/> DEMO ONLY</b><span>{status?.rest_host || 'https://demo-fapi.binance.com'}</span></div></div>
      <div className="demoHeroStatus">
        <span className={status?.configured ? 'demoOk' : 'demoWait'}><LockKeyhole/><small>ANAHTAR</small><b>{status?.configured ? 'YERELDE HAZIR' : 'AYAR BEKLİYOR'}</b></span>
        <span className={status?.connected ? 'demoOk' : 'demoWait'}><Radio/><small>DEMO API</small><b>{status?.connected ? 'BAĞLI' : 'BAĞLI DEĞİL'}</b></span>
        <span className={status?.armed ? 'demoArmed' : 'demoSafe'}>{status?.armed ? <UnlockKeyhole/> : <LockKeyhole/>}<small>EMİR KİLİDİ</small><b>{status?.armed ? `${Math.floor(armSeconds/60)}:${String(armSeconds%60).padStart(2,'0')}` : 'KAPALI'}</b></span>
      </div>
    </section>}

    <nav className="v21Tabs" aria-label="V21 çalışma alanları">
      <button className={tab === 'dashboard' ? 'active' : ''} onClick={() => setTab('dashboard')}><LayoutDashboard/><span><b>DASHBOARD</b><small>Özet · Durum · Aktivite</small></span></button>
      <button className={tab === 'trade' ? 'active' : ''} onClick={() => setTab('trade')}><Crosshair/><span><b>İŞLEM MASASI</b><small>Emir · Grafik · Pozisyon</small></span></button>
      <button className={tab === 'risk' ? 'active' : ''} onClick={() => setTab('risk')}><Gauge/><span><b>RİSK KASASI</b><small>Limit · Boyut · Stop</small></span></button>
      <button className={tab === 'journal' ? 'active' : ''} onClick={() => setTab('journal')}><ClipboardList/><span><b>CANLI GÜNLÜK</b><small>Dolum · Kapanış · Neden</small></span></button>
      <button className={tab === 'auto' ? 'active' : ''} onClick={() => setTab('auto')}><Zap/><span><b>OTOMASYON</b><small>İzin listesi · Kapılar</small></span></button>
      <button className={tab === 'backtest' ? 'active' : ''} onClick={() => setTab('backtest')}><BarChart3/><span><b>BACKTEST LAB</b><small>Ücret · Kayma · 3 dönem</small></span></button>
      <button className={tab === 'performance' ? 'active' : ''} onClick={() => setTab('performance')}><BarChart3/><span><b>PERFORMANS</b><small>PnL · Win rate · Drawdown</small></span></button><button className={tab === 'certificate' ? 'active' : ''} onClick={() => setTab('certificate')}><ShieldCheck/><span><b>SERTİFİKA</b><small>Sağlık · Tatbikat · Kanıt</small></span></button>
    </nav>

    {tab === 'dashboard' && <section className="v21DashboardHome" aria-label="ProTreBot dashboard">
      <header className="v21DashboardWelcome"><div><span>PROTREBOT ELITE X</span><h2>ProTreBot'a hoş geldin</h2><p>Botunu tek merkezden yönet, performansını izle ve sistem durumunu kontrol et.</p></div><strong className={v21?.stream.status === 'CANLI' ? 'active' : 'waiting'}><i/>{v21?.stream.status === 'CANLI' ? 'TESTNET AKTİF' : v21 ? 'TESTNET BEKLİYOR' : 'SİSTEM DURUMU YÜKLENİYOR'}</strong></header>
      <section className="v21DashboardSubscription"><div><span>ABONELİĞİN</span><h3>Billing &amp; Subscription</h3><p>Plan ve yenileme bilgileri mevcut Billing ekranından yönetilir.</p></div><button type="button" onClick={() => window.dispatchEvent(new CustomEvent('protrebot-navigate',{detail:'billing'}))}>ABONELİĞİ YÖNET <span>→</span></button></section>
      <section className="v21DashboardStatus"><header><div><span>SİSTEM DURUMU</span><h3>Bot ve bağlantılar</h3></div><button type="button" onClick={() => void refreshV21()} aria-label="Sistem durumunu yenile"><RefreshCw/></button></header><div className="v21DashboardStatusGrid"><span><small>TRADING ENGINE</small><b>{v21?.auto.enabled ? 'AKTİF' : v21 ? 'KAPALI' : 'BEKLENİYOR'}</b></span><span><small>MARKET DATA</small><b>{v21?.stream.status === 'CANLI' ? 'BAĞLI' : v21 ? 'BEKLENİYOR' : '—'}</b></span><span><small>BINANCE TESTNET</small><b>{status?.connected ? 'BAĞLI' : status ? 'BEKLENİYOR' : '—'}</b></span><span><small>EVIDENCE LEDGER</small><b>{v21?.certificate ? 'AKTİF' : 'BEKLENİYOR'}</b></span></div><small className="v21DashboardSync">Son senkronizasyon: {stamp(v21?.stream.last_sync || v21?.last_saved)}</small></section>
      <section className="v21DashboardStatus v21DashboardAutoTrade" aria-label="Auto Trade Bot"><header><div><span>DEMO / TESTNET ONLY</span><h3>AUTO TRADE BOT</h3></div><b>{v21?.auto.enabled ? 'AKTİF' : 'KAPALI'}</b></header><div className="v21DashboardAutoTradeGrid"><span><small>DURUM</small><strong>{v21?.auto.enabled ? 'ON' : 'OFF'}</strong></span><span><small>TARAMA</small><strong>~{v21?.scanner.coins_scanned || 100} COIN</strong></span><span><small>SON TARAMA</small><strong>{stamp(v21?.scanner.last_scan_at)}</strong></span><span><small>SONRAKİ TARAMA</small><strong>{stamp(v21?.scanner.next_scan_at)}</strong></span><span><small>UYGUN ADAY</small><strong>{v21?.scanner.eligible_count ?? 0} · TOP {v21?.scanner.top_candidates.length ?? 0}/3</strong></span><span><small>GÜNLÜK PnL</small><strong>{fmt(v21?.daily.realized_pnl)} USDT</strong></span></div><div className="v21DashboardAutoTradeActions"><input value={autoConfirm} onChange={event => setAutoConfirm(event.target.value)} placeholder="DEMO OTOMATİK" aria-label="Auto Trade confirmation"/><button type="button" onClick={toggleAuto} disabled={v21Busy || (!v21?.auto.enabled && !status?.armed)}>{v21?.auto.enabled ? 'AUTO TRADE DURDUR' : 'AUTO TRADE AÇ'}</button><button type="button" onClick={() => setTab('auto')}>AYARLARI AÇ</button></div></section>
      <section className="v21DashboardPerformance"><header><div><span>PERFORMANS</span><h3>Özet görünüm</h3></div><BarChart3/></header>{performance ? <div className="v21DashboardPerformanceBody"><strong className={performance.net_profit >= 0 ? 'positive' : 'negative'}>{performance.net_profit >= 0 ? '+' : ''}{fmt(performance.net_profit)} USDT</strong><span>{performance.total_trades} işlem · %{performance.win_rate} win rate</span><button type="button" onClick={() => setTab('performance')}>DETAYLI ANALİZ <span>→</span></button></div> : <div className="v21DashboardEmpty"><b>Henüz yeterli işlem verisi yok</b><span>Detaylı performans, tamamlanan Demo işlemleri oluştuktan sonra burada görünür.</span><button type="button" onClick={() => setTab('performance')}>PERFORMANSI İNCELE <span>→</span></button></div>}</section>
      <section className="v21DashboardActivity"><header><div><span>SON AKTİVİTELER</span><h3>Son önemli olaylar</h3></div><History/></header>{(v21?.journal?.length || status?.events?.length) ? <div>{[...(v21?.journal || []).map(item => ({title:item.kind,detail:item.message,time:item.created_at})),...(status?.events || []).map(item => ({title:item.kind,detail:item.message,time:item.created_at}))].slice(0,3).map((item,index) => <article key={`${item.time}-${index}`}><i/><div><b>{item.title}</b><span>{item.detail}</span></div><time>{stamp(item.time)}</time></article>)}</div> : <div className="v21DashboardEmpty"><b>Henüz aktivite bulunmuyor.</b><span>Yeni sistem ve Demo olayları burada görünecek.</span></div>}<button type="button" onClick={() => setTab('journal')}>TÜM AKTİVİTELER <span>→</span></button></section>
      <section className="v21DashboardActions" aria-label="Dashboard hızlı erişim"><button type="button" onClick={() => setTab('trade')}>İŞLEM MASASINI AÇ <span>→</span></button><button type="button" onClick={() => setTab('risk')}>RİSKİ İNCELE <span>→</span></button></section>
    </section>}

    {tab === 'trade' && <>
    <section className="v21Pulse">
      <span><i className={v21?.stream.status === 'CANLI' ? 'on' : ''}/><small>AKIŞ</small><b>{v21?.stream.status || 'BEKLENİYOR'}</b></span>
      <span><small>OTOMASYON</small><b>{v21?.auto.enabled ? 'ÇALIŞIYOR' : 'KAPALI'}</b></span>
      <span><small>GÜNLÜK DEMO</small><b>{v21?.daily.auto_entries ?? 0} / {v21?.settings.daily_trade_limit ?? 6}</b></span>
      <span><small>RİSK BÜTÇESİ</small><b>{fmt(v21?.daily.remaining_loss_budget)} USDT</b></span>
      <span><small>DEMO KANIT</small><b>%{v21?.certificate.score ?? 0}</b></span>
      <strong>GERÇEK PARA: 0 USDT · GERÇEK EMİR KANALI YOK</strong>
    </section>

    <nav className="v21Workflow" aria-label="Trading workflow">
      <div className="v21WorkflowTitle"><span>TRADING WORKFLOW</span><small>Current operating path</small></div>
      <div className="v21WorkflowSteps">
        {workflowSteps.map((step,index) => <button key={step.label} className={`v21WorkflowStep ${step.status} ${workflowCurrentIndex === index ? 'current' : ''}`} onClick={() => setTab(step.target)} aria-current={workflowCurrentIndex === index ? 'step' : undefined}>
          <i>{index + 1}</i><span><b>{step.label}</b><small>{step.status}</small></span>{index < workflowSteps.length - 1 && <em>→</em>}
        </button>)}
      </div>
    </nav>

    <section className="v21ExecutiveSummary" aria-label="Bot özeti">
      <div className="v21SummaryIntro"><span className="v21Eyebrow">BUGÜNÜN KONTROL MERKEZİ</span><h2>Bot şu anda ne yapıyor?</h2><p>{v21?.auto.rejection_reason || (v21?.auto.enabled ? 'Piyasayı izliyor ve yalnızca tüm güvenlik kapıları geçtiğinde Demo işlemi açıyor.' : 'Otomasyon kapalı. Başlamak için Demo kilidini ve ikinci onayı tamamlayın.')}</p><div className="v21NextAction"><span><small>NEXT BEST ACTION</small><b>{nextAction.label}</b><em>{nextAction.detail}</em></span><button onClick={() => setTab(nextAction.target)}>OPEN WORKSPACE <span>→</span></button></div></div>
      <div className="v21SummaryMetrics">
        <span><small>BOT DURUMU</small><b className={v21?.auto.enabled ? 'summaryPositive' : 'summaryMuted'}>{v21?.auto.enabled ? 'AKTİF' : 'KAPALI'}</b><em>{v21?.auto.rejection_gate ? gateLabel(v21.auto.rejection_gate) : 'Güvenlik izleniyor'}</em></span>
        <span><small>DEMO BAKİYESİ</small><b>{fmt(account?.wallet_balance)} USDT</b><em>Sanal hesap</em></span>
        <span><small>BUGÜN PnL</small><b className={(v21?.daily.realized_pnl || 0) >= 0 ? 'summaryPositive' : 'summaryNegative'}>{(v21?.daily.realized_pnl || 0) >= 0 ? '+' : ''}{fmt(v21?.daily.realized_pnl)} USDT</b><em>Gerçekleşen</em></span>
        <span><small>AÇIK POZİSYON</small><b>{v21?.account.positions ?? 0} / {v21?.settings.max_positions ?? 3}</b><em>Aktif / maksimum</em></span>
        <span><small>SONRAKİ TARAMA</small><b>{nextScanSeconds === null ? '—' : `${Math.floor(nextScanSeconds / 60)}:${String(nextScanSeconds % 60).padStart(2,'0')}`}</b><em>600 saniyelik döngü</em></span>
      </div>
      {v21?.auto.rejection_reason && <div className="v21SummaryBlock"><TriangleAlert/><span><b>İŞLEM AÇILMADI</b><strong>{gateLabel(v21.auto.rejection_gate)}</strong><small>{v21.auto.rejection_reason}</small></span></div>}
    </section>
    </>}

    {tab === 'trade' && <section className="v21DashboardInsights" aria-label="Demo dashboard insights">
      <div className="v21InsightPanel v21AccountOverview"><header><div><span>ACCOUNT OVERVIEW</span><h2>Hesap Özeti</h2></div><Wallet/></header><div className="v21InsightMetrics">
        <span><small>TOTAL BALANCE</small><b>{fmt(overviewWallet)} <em>USDT</em></b></span><span><small>AVAILABLE BALANCE</small><b>{fmt(overviewAvailable)} <em>USDT</em></b></span><span><small>DAILY PnL</small><b className={(overviewPnl || 0) >= 0 ? 'demoProfit' : 'demoLoss'}>{overviewPnl === null || overviewPnl === undefined ? '—' : `${overviewPnl >= 0 ? '+' : ''}${fmt(overviewPnl)} USDT`}</b></span><span><small>OPEN POSITIONS</small><b>{account?.positions.length ?? v21?.account.positions ?? '—'}</b></span><span><small>USED MARGIN</small><b>{overviewUsedMargin === null ? '—' : `${fmt(overviewUsedMargin)} USDT`}</b></span><span><small>MARGIN USAGE</small><b className={overviewMarginUsage !== null && overviewMarginUsage > 70 ? 'demoLoss' : 'demoProfit'}>{overviewMarginUsage === null ? '—' : `%${overviewMarginUsage.toFixed(1)}`}</b></span><span><small>WIN RATE</small><b>{performance ? `%${performance.win_rate}` : '—'}</b></span>
      </div></div>
      <div className="v21InsightPanel v21PortfolioOverview"><header><div><span>PORTFOLIO / ACCOUNT OVERVIEW</span><h2>Portfolio Snapshot</h2></div><Wallet/></header><div className="v21PortfolioMetrics">{portfolioMetrics.map(metric => <span key={metric.label}><small>{metric.label}</small><b>{metric.value}</b></span>)}</div>{portfolioTrend.length ? <div className="v21EquityTrend"><h3>EQUITY TREND</h3><div>{portfolioTrend.map(day => <span key={day.key} style={{height:`${Math.max(10,Math.min(100,Math.abs(day.pnl) / Math.max(...portfolioTrend.map(item => Math.abs(item.pnl)),1) * 100))}%`}} className={day.pnl >= 0 ? 'positive' : 'negative'}><i/><small>{day.label}</small></span>)}</div></div> : <div className="v21PortfolioEmpty"><BarChart3/><span>Equity trend unavailable.</span><small>Historical account data is not available yet.</small></div>}</div>
      <div className="v21InsightPanel v21DecisionIntelligence"><header><div><span>TRADE INTELLIGENCE</span><h2>Decision Intelligence</h2></div><Gauge/></header><div className="v21DecisionHeadline"><strong>{decisionCandidate ? fmt(decisionCandidate.score) : '—'}</strong><span>Decision Score · {decisionLabel}</span></div><div className="v21DecisionSummary"><b>{decisionBias === 'LONG' ? 'LONG Bias' : decisionBias === 'SHORT' ? 'SHORT Bias' : 'WAIT'}</b><small>{decisionBias === 'WAIT' ? 'NO TRADE' : `${decisionBias} Bias`}</small><em>{decisionCandidate ? `${fmt(decisionCandidate.score)} / 100 Confidence` : 'Awaiting setup confirmation'}</em></div><div className="v21DecisionChecks">{decisionChecks.map(check => <span key={check.label}><small>{check.label}</small><b>{check.value}</b></span>)}</div><div className="v21DecisionNarrative"><h3>WHY THIS DECISION?</h3><p>{whyTrade}</p></div><div className="v21IntelligenceActions"><button onClick={() => setTab('trade')}>ANALYZE SETUP</button><button onClick={() => setTab('risk')}>REVIEW RISK</button><button onClick={() => setTab('journal')}>VIEW POSITION TIMELINE</button></div></div>
      <div className={`v21InsightPanel v21RiskStatus ${riskClass}`}><header><div><span>RISK STATUS</span><h2>Risk Durumu</h2></div><ShieldCheck/></header>{riskLevel ? <><div className="v21RiskHeadline"><strong>{riskLevel}</strong><b>RISK SCORE {riskScore}/100</b></div><div className="v21RiskMeter"><i style={{width:`${Math.min(100,Math.max(8,riskScore || 0))}%`}}/></div><p>Likidasyon mesafesi %{riskDistance?.toFixed(2)} · Kullanılan marjin {fmt(overviewUsedMargin)} USDT</p><ul className="v21RiskReasons">{riskReasons.map(reason => <li key={reason}>{reason}</li>)}</ul></> : <div className="v21InsightEmpty"><ShieldCheck/><span>Waiting for market/account data</span><small>Risk score and level will appear when current account data is available.</small></div>}</div>
      <div className="v21InsightPanel v21ProtectionCenter"><header><div><span>SMART RISK ENGINE · ADVISORY</span><h2>Protection Center</h2></div><ShieldCheck/></header><div className="v21ProtectionState"><b>{riskAvailable || account ? (riskClass === 'critical' ? 'PROTECTED' : riskClass === 'warning' ? 'CAUTION' : 'NORMAL') : 'UNAVAILABLE'}</b><span>{riskAvailable || account ? 'Recommended Protection' : 'Required protection data is unavailable.'}</span></div><div className="v21ProtectionMetrics"><span><small>DAILY PnL</small><b>{v21 ? `${fmt(v21.daily.realized_pnl)} USDT` : 'Unavailable'}</b></span><span><small>OPEN EXPOSURE</small><b>{account ? `${fmt(account.positions.reduce((total, position) => total + Math.abs(position.quantity * position.mark_price), 0))} USDT` : 'Unavailable'}</b></span><span><small>LOSS STREAK</small><b>{v21?.journal?.length ? `${v21.journal.slice(0,5).filter(item => (item.realized_pnl ?? 0) < 0).length}` : 'Unavailable'}</b></span></div></div>
      <div className="v21InsightPanel v21SmartAlerts"><header><div><span>SMART ALERTS</span><h2>Akıllı Uyarılar</h2></div><TriangleAlert/></header>{v21?.stream.last_error || v21?.scanner.last_error || v21?.auto.last_error ? <div className="v21AlertList">{[v21.stream.last_error,v21.scanner.last_error,v21.auto.last_error].filter(Boolean).map((alert,index) => <div className="warning" key={`${alert}-${index}`}><i/><span><b>ATTENTION</b><small>{alert}</small></span></div>)}</div> : <div className="v21InsightEmpty"><CheckCircle2/><span>Aktif risk uyarısı yok</span><small>Bot şu anda dikkat gerektiren bir risk algılamadı.</small></div>}</div>
      <div className="v21InsightPanel v21QualityScore"><header><div><span>TRADE SETUP QUALITY</span><h2>Setup Kalitesi</h2></div><Gauge/></header>{qualityScore !== null ? <><div className="v21QualityHeadline"><b>{qualityScore.toFixed(0)}</b><span>/ 100 · {qualityLabel}</span></div><div className="v21QualityGrade"><strong>{scoreClassification.label}</strong><small>{scoreClassification.summary}</small></div><div className="v21QualityBars"><span><small>Top candidate score</small><i><b style={{width:`${qualityScore}%`}}/></i></span><span><small>Confidence</small><i><b style={{width:`${Math.min(100,Number(qualityCandidate?.confidence_value ?? 0))}%`}}/></i></span><span><small>Trend / volume</small><em>{decisionCandidate?.trend || 'Trend data pending'}</em></span></div></> : <><div className="v21QualityGrade"><strong>{scoreClassification.label}</strong><small>{scoreClassification.summary}</small></div><div className="v21InsightEmpty"><Gauge/><span>Setup quality verisi bekleniyor.</span></div></>}</div>
      <div className="v21InsightPanel v21AiCoach"><header><div><span>AI TRADING COACH</span><h2>Personal Trading Insights</h2></div><Sparkles/></header><div className={`v21CoachStatus ${aiCoachInsight.tone}`}><i/>{aiCoachInsight.title}</div><p>{aiCoachInsight.detail}</p><div className="v21CoachMetrics"><span><small>Recent PnL</small><b>{coachTrades.length ? `${fmt(coachTrades.reduce((sum,item) => sum + (item.realized_pnl ?? 0),0))} USDT` : '—'}</b></span><span><small>Win / Loss</small><b>{coachTrades.length ? `${coachPositiveCount} / ${coachNegativeCount}` : '—'}</b></span><span><small>Streak</small><b>{coachStreakCount}</b></span></div></div>
      <div className="v21InsightPanel v21LiveActivity"><header><div><span>LIVE BOT ACTIVITY</span><h2>Canlı Bot Aktivitesi</h2></div><Activity/></header>{activityItems.length ? <div className="v21ActivityList">{activityItems.map((item,index) => <article key={`${item.time}-${index}`}><i/><div><b>{item.title}</b><span>{item.description}</span><small>{item.meta} · {stamp(item.time)}</small></div></article>)}</div> : <div className="v21InsightEmpty"><Activity/><span>Henüz bot aktivitesi yok.</span></div>}</div>
      <div className="v21InsightPanel v21LifecyclePanel"><header><div><span>POSITION LIFECYCLE</span><h2>Lifecycle Timeline</h2></div><Crosshair/></header>{lifecycleEvents.length ? <><div className="v21LifecycleStages">{lifecycleStages.map((stage,index) => <span className={index < lifecycleStageIndex ? 'done' : index === lifecycleStageIndex ? 'active' : ''} key={stage}><i/><b>{stage}</b><small>{index < lifecycleStageIndex ? 'Completed' : index === lifecycleStageIndex ? 'Active' : 'Waiting'}</small></span>)}</div><div className="v21LifecycleList">{lifecycleEvents.map(item => <article key={item.id}><i/><div><b>{item.kind}</b><span>{item.message}</span><small>{item.price !== null && item.price !== undefined ? `${fmt(item.price)} · ` : ''}{item.source}</small></div><time>{stamp(item.created_at)}</time></article>)}</div></> : <div className="v21InsightEmpty"><Crosshair/><span>No detailed lifecycle events are available for this position yet.</span></div>}</div>
      <div className="v21InsightPanel v21ReplayPanel"><header><div><span>HISTORICAL EVENT REPLAY</span><h2>Trade Replay</h2></div><History/></header>{replayEvents.length ? <><div className="v21ReplaySummary"><b>{replayTrade.direction} · {symbol.replace('USDT','/USDT')}</b><span>Entry {fmt(replayTrade.entry)} · Exit {fmt(replayTrade.exit)} · PnL {fmt(replayTrade.pnl)}</span></div><div className="v21ReplaySteps">{replayEvents.map(event => <span key={event.label}><i/><b>{event.label}</b><small>{event.detail} · {new Date(event.time).toLocaleString('tr-TR')}</small></span>)}</div><div className="v21ReplayAnalysis"><h3>AI ANALYSIS</h3><div><span><b>What went well</b><small>{replayAiAnalysis?.wentWell}</small></span><span><b>What could be improved</b><small>{replayAiAnalysis?.improve}</small></span><span><b>Key lesson</b><small>{replayAiAnalysis?.lesson}</small></span></div></div><div className="v21ReplayDetailSteps">{replaySteps.map(step => <span key={step.label}><b>{step.label}</b><small>{step.detail}</small></span>)}</div></> : <div className="v21InsightEmpty"><History/><span>No historical replay data available.</span><small>AI analysis will appear after a recorded trade is available.</small></div>}</div>
      <div className="v21InsightPanel v21TradeHistory"><header><div><span>PROFESSIONAL TRADE HISTORY</span><h2>Trade History</h2></div><History/></header>{historyItems.length ? <div className="v21HistoryList">{historyItems.map((item,index) => <article key={`${item.entry_time}-${index}`}><b>{item.direction}</b><em className={item.direction === 'LONG' ? 'demoLong' : 'demoShort'}>{symbol.replace('USDT','/USDT')}</em><span>Giriş {fmt(item.entry)} · Çıkış {fmt(item.exit)}</span><strong className={item.pnl >= 0 ? 'demoProfit' : 'demoLoss'}>{item.pnl >= 0 ? '+' : ''}{fmt(item.pnl)}</strong><small>{item.pnl >= 0 ? 'WIN' : 'LOSS'} · {Math.max(0,Math.round((item.exit_time - item.entry_time) / 60000))} dk</small></article>)}</div> : <div className="v21InsightEmpty"><History/><span>Gerçek trade history verisi bekleniyor.</span></div>}</div>
      <div className="v21InsightPanel v21MarketScanner"><header><div><span>MARKET SCANNER</span><h2>Live Market Opportunity Scanner</h2></div><Radar/></header><div className="v21MarketScannerTable"><table><thead><tr><th>Symbol</th><th>Signal</th><th>Score</th><th>Trend</th><th>Status</th></tr></thead><tbody>{marketScannerRows.map((row) => <tr key={row.symbol} onClick={() => setScannerFocus(row)} className={scannerFocus?.symbol === row.symbol ? 'active' : ''}><td>{row.symbol}</td><td className={row.direction === 'LONG' ? 'signalLong' : row.direction === 'SHORT' ? 'signalShort' : 'signalWait'}>{row.direction === 'LONG' ? '🟢 LONG' : row.direction === 'SHORT' ? '🔴 SHORT' : '🟡 WAIT'}</td><td>{row.score}</td><td>{row.trend}</td><td>{row.status}</td></tr>)}</tbody></table></div>{scannerFocus && <div className="v21ScannerDetail"><h3>{scannerFocus.symbol} — {scannerFocus.direction}</h3><div className="v21ScannerMeta"><span><small>Trend</small><b>{scannerFocus.trend}</b></span><span><small>Volume</small><b>{scannerFocus.volume}</b></span><span><small>Momentum</small><b>{scannerFocus.momentum}</b></span><span><small>Confidence</small><b>{scannerFocus.confidence}/100</b></span><span><small>Risk</small><b>{scannerFocus.risk}</b></span><span><small>Status</small><b>{scannerFocus.status}</b></span></div><p>Market structure and momentum support the {scannerFocus.direction} bias for {scannerFocus.symbol}. Volume confirmation and risk context remain aligned with the current setup profile.</p></div>}</div>
      <div className="v21InsightPanel v21TradingJournal"><header><div><span>TRADING JOURNAL</span><h2>Karar Günlüğü</h2></div><ClipboardList/></header>{v21?.journal?.length ? <div className="v21JournalFeed">{v21.journal.slice(0,4).map(item => <article key={item.id}><i/><div><b>{item.kind}</b><span>{item.message}</span><small>{item.symbol || 'SİSTEM'} · {item.source} · {stamp(item.created_at)}</small></div></article>)}</div> : <div className="v21InsightEmpty"><ClipboardList/><span>İşlem kararı günlüğü bekleniyor.</span></div>}</div>
      <div className="v21InsightPanel v21PerformanceAnalytics"><header><div><span>DAILY PERFORMANCE ANALYTICS</span><h2>Günlük Performans</h2></div><BarChart3/></header>{hasPerformanceTrend ? <div className="v21FiveDayChart" aria-label="Five day performance chart">{performanceDays.map(day => <div className="v21ChartDay" key={day.key}><div className="v21ChartBar"><i className={day.pnl >= 0 ? 'positive' : 'negative'} style={{height:`${Math.max(8,Math.min(100,Math.abs(day.pnl) / Math.max(...performanceDays.map(item => Math.abs(item.pnl)),1) * 100))}%`}}/></div><b>{day.label}</b><small>{day.trades ? `${day.pnl >= 0 ? '+' : ''}${fmt(day.pnl)}` : 'No data'}</small><em>{day.trades ? `${day.wins}W / ${day.losses}L` : '—'}</em></div>)}</div> : <div className="v21InsightEmpty v21FiveDayEmpty"><BarChart3/><span>Five-day performance data unavailable.</span><small>No completed journal outcomes are available for this period.</small></div>}{performance ? <div className="v21PerformanceStats"><span><small>PnL</small><b className={performance.net_profit >= 0 ? 'demoProfit' : 'demoLoss'}>{fmt(performance.net_profit)} USDT</b></span><span><small>TRADES</small><b>{performance.total_trades}</b></span><span><small>WINS / LOSSES</small><b>{performance.wins} / {performance.losses}</b></span><span><small>WIN RATE</small><b>%{performance.win_rate}</b></span><span><small>BEST / WORST</small><b>{fmt(performance.best_trade)} / {fmt(performance.worst_trade)}</b></span></div> : <div className="v21InsightEmpty"><BarChart3/><span>Günlük performans verisi bekleniyor.</span></div>}</div>
    </section>}

    {tab === 'trade' && <section className="v21DecisionHistoryPanel v21InsightPanel"><header><div><span>BOT DECISION HISTORY</span><h2>Reasoning History</h2></div><History/></header>{decisionHistory.length ? <div className="v21DecisionFeed">{decisionHistory.map((item,index) => <article key={`${item.time}-${index}`}><i className={item.action === 'LONG' ? 'long' : item.action === 'SHORT' ? 'short' : item.action === 'EXIT' ? 'exit' : 'wait'}/><div><header><b>{item.action}</b><strong>{item.symbol}</strong><time>{stamp(item.time)}</time></header><span><small>Confidence</small><b>{item.confidence === null ? '—' : `%${item.confidence}`}</b><small>Risk</small><b>{item.risk || '—'}</b></span><p>{item.reason}</p></div></article>)}</div> : <div className="v21InsightEmpty"><History/><b>NO DECISION HISTORY YET</b><span>Bot decisions and reasoning will appear here as market activity is recorded.</span></div>}</section>}
    {tab === 'trade' && <section className="v21SmartAlertCenter v21InsightPanel"><header><div><span>SMART NOTIFICATION CENTER</span><h2>Smart Alert Center</h2></div><Bell/></header><nav className="v21AlertFilters" aria-label="Alert filters">{(['ALL','CRITICAL','WARNING','INFO'] as const).map(filter => <button key={filter} className={alertFilter === filter ? 'active' : ''} onClick={() => setAlertFilter(filter)}>{filter}</button>)}</nav>{filteredAlerts.length ? <div className="v21SmartAlertList">{filteredAlerts.map((alert,index) => <article key={`${alert.title}-${alert.time}-${index}`} className={alert.severity.toLowerCase()}><i><Bell/></i><div><header><b>{alert.title}</b><time>{stamp(alert.time)}</time></header><p>{alert.detail}</p>{alert.symbol && <small>{alert.symbol}</small>}</div></article>)}</div> : <div className="v21InsightEmpty"><Bell/><b>NO ACTIVE ALERTS</b><span>Important trading events and risk notifications will appear here.</span></div>}</section>}
    {tab === 'trade' && <section className="v21AdvancedAnalytics v21InsightPanel"><header><div><span>ADVANCED TRADE ANALYTICS</span><h2>Performance Patterns</h2></div><BarChart3/></header>{advancedAnalytics ? <><div className="v21AdvancedMetrics">{directionalAnalytics.map(item => <span key={item.direction}><small>{item.direction} WIN RATE</small><b>{item.rate === null ? 'Insufficient trade data' : `%${item.rate}`}</b></span>)}<span><small>AVERAGE HOLD TIME</small><b>{averageHoldMinutes === null ? 'Insufficient trade data' : `${averageHoldMinutes} min`}</b></span><span><small>PROFIT FACTOR</small><b>{advancedAnalytics.profitFactor === null ? 'Insufficient trade data' : advancedAnalytics.profitFactor.toFixed(2)}</b></span><span><small>BEST SYMBOL</small><b>{advancedAnalytics.bestSymbol || 'Insufficient trade data'}</b></span><span><small>WORST SYMBOL</small><b>{advancedAnalytics.worstSymbol || 'Insufficient trade data'}</b></span></div><div className="v21PatternInsights"><h3>AI PATTERN INSIGHTS</h3><p>{directionalAnalytics[0].rate !== null && directionalAnalytics[1].rate !== null ? (directionalAnalytics[0].rate >= directionalAnalytics[1].rate ? 'Long positions have performed better than short positions in the available trade history.' : 'Short positions have performed better than long positions in the available trade history.') : 'Pattern comparison requires both long and short trade data.'}</p><p>{analyticsTrades.length >= 3 ? 'Key observation: available trade outcomes can now be reviewed against their recorded entry and exit context.' : 'INSUFFICIENT DATA FOR PATTERN ANALYSIS'}</p></div></> : <div className="v21InsightEmpty"><BarChart3/><b>INSUFFICIENT TRADE DATA</b><span>Advanced analytics will appear when recorded trades are available.</span></div>}</section>}
    {tab === 'trade' && <section className="v21AiTradeJournal v21InsightPanel"><header><div><span>AI TRADE JOURNAL</span><h2>Trade Context Review</h2></div><ClipboardList/></header>{aiJournalTrades.length ? <div className="v21AiJournalList">{aiJournalTrades.map((trade,index) => { const context = aiTradeContext(trade); return <article key={`${trade.entry_time}-${index}`}><header><div><b>{trade.direction}</b><strong>{symbol.replace('USDT','/USDT')}</strong><small>Trade {index + 1} · {new Date(trade.entry_time).toLocaleString('tr-TR')}</small></div><em className={trade.pnl > 0 ? 'win' : trade.pnl < 0 ? 'loss' : 'breakeven'}>{trade.pnl > 0 ? 'WIN' : trade.pnl < 0 ? 'LOSS' : 'BREAKEVEN'} · {trade.pnl >= 0 ? '+' : ''}{fmt(trade.pnl)}</em></header><div className="v21AiJournalFacts"><span><small>ENTRY</small><b>{fmt(trade.entry)}</b></span><span><small>EXIT</small><b>{fmt(trade.exit)}</b></span><span><small>STATUS</small><b>{trade.reason || 'Recorded result'}</b></span></div><div className="v21AiJournalSections"><section><b>WHY THE TRADE WAS TAKEN</b><p>{context.why}</p></section><section><b>WHAT CONFIRMED THE ENTRY</b>{context.confirmations.length ? <ul>{context.confirmations.map(item => <li key={item}>{item}</li>)}</ul> : <p>Entry confirmations unavailable.</p>}</section><section><b>MAIN RISK</b><p>{context.risk}</p></section><section><b>EXIT ANALYSIS</b><p>{context.exit}</p></section><section><b>AI LESSON</b><p>{context.lesson}</p></section></div></article>})}</div> : <div className="v21InsightEmpty"><ClipboardList/><b>INSUFFICIENT TRADE CONTEXT FOR AI REVIEW</b><span>No completed trade context is available for the journal.</span></div>}</section>}
    {tab === 'trade' && <section className="v21StrategyPerformance v21InsightPanel"><header><div><span>STRATEGY PERFORMANCE</span><h2>Strategy Comparison</h2></div><BarChart3/></header>{strategyHistoryAvailable ? <div className="v21StrategyTable">Strategy data available.</div> : <div className="v21InsightEmpty"><BarChart3/><b>STRATEGY COMPARISON UNAVAILABLE</b><span>No strategy names are stored in the available runtime trade metadata.</span></div>}</section>}
    {tab === 'trade' && <section className="v21MarketRegime v21InsightPanel"><header><div><span>MARKET REGIME DETECTION</span><h2>Current Market Regime</h2></div><Activity/></header><div className="v21RegimeHeadline"><strong>{marketRegime}</strong><span>Confidence {regimeConfidence === null ? 'UNAVAILABLE' : `%${regimeConfidence}`}</span></div>{regimeSignals.length ? <><div className="v21RegimeSignals">{regimeSignals.map(signal => <span key={signal.label}><small>{signal.label}</small><b>{signal.value}</b></span>)}</div><div className="v21RegimeBehavior"><b>BOT BEHAVIOR</b><p>Current risk mode: {riskLevel || 'UNKNOWN'}</p><p>Market observation: {marketRegime}</p><small>Informational recommendation only. Current bot settings are unchanged.</small></div></> : <div className="v21InsightEmpty"><Activity/><b>MARKET REGIME UNAVAILABLE</b><span>Waiting for trend, volatility, momentum, or scanner data.</span></div>}</section>}
    {tab === 'trade' && <section className="v21BotHealthMonitor v21InsightPanel"><header><div><span>BOT HEALTH &amp; SYSTEM MONITOR</span><h2>REAL-TIME INFRASTRUCTURE STATUS</h2></div><ShieldCheck/></header><div className={`v21HealthOverview v21Health${overallHealth.charAt(0) + overallHealth.slice(1).toLowerCase()}`}><span><small>OVERALL HEALTH</small><strong>{overallHealth}</strong></span><span><small>SYSTEM HEALTH</small><b>{healthScore === null ? 'UNKNOWN' : `${healthScore}/100`}</b></span><span><small>UPTIME</small><b>UNAVAILABLE</b></span></div><div className="v21HealthServices">{healthServices.map(service => <article className={`v21Health${service.status.charAt(0) + service.status.slice(1).toLowerCase()}`} key={service.name}><i/><div><b>{service.name}</b><strong>{service.status}</strong><small>Last activity: {service.time ? stamp(service.time) : 'Unavailable'}</small><small>{service.detail}</small></div></article>)}</div><div className="v21HealthHeartbeat"><h3>SYSTEM HEARTBEAT</h3><span><b>LAST BOT ACTIVITY</b><small>{v21?.journal?.[0] ? stamp(v21.journal[0].created_at) : 'NO RECENT ACTIVITY DATA'}</small></span><span><b>LAST MARKET UPDATE</b><small>{v21?.scanner.last_scan_at ? stamp(v21.scanner.last_scan_at) : 'UNAVAILABLE'}</small></span><span><b>LAST TRADE EVENT</b><small>{v21?.journal?.find(item => item.kind.toLowerCase().includes('trade')) ? stamp(v21.journal.find(item => item.kind.toLowerCase().includes('trade'))?.created_at) : 'UNAVAILABLE'}</small></span></div><div className="v21HealthEvents"><h3>SYSTEM EVENTS &amp; ERRORS</h3>{systemEvents.length ? systemEvents.slice(0,6).map((event,index) => <p key={`${event.title}-${index}`}><b>{event.kind}</b><span>{event.title} · {event.detail}</span><small>{stamp(event.time)}</small></p>) : <div>NO SYSTEM EVENT STREAM AVAILABLE</div>}</div><div className="v21HealthLatency"><span><b>API RESPONSE</b><small>NOT CURRENTLY MEASURED</small></span><span><b>MARKET UPDATE LATENCY</b><small>NOT CURRENTLY MEASURED</small></span><span><b>DECISION CYCLE TIME</b><small>NOT CURRENTLY MEASURED</small></span></div></section>}

    {tab === 'trade' && <section className="v21TradeIntelligenceStack">
      <article className="v21SafetyGate" aria-label="Pre-trade safety check"><header><div><span>PRE-TRADE SAFETY CHECK</span><h2>Review before order submission</h2></div><ShieldCheck/></header><div className="v21SafetySummary"><b>{safetySummary}</b><small>Advisory only · does not authorize execution</small></div><div className="v21SafetyChecks">{safetyChecks.map(check => <div className={`v21SafetyCheck ${check.status.toLowerCase()}`} key={check.label}><span><i/>{check.label}</span><b>{check.status}</b><small>{check.detail}</small></div>)}</div></article>
      <article className="v21SetupScore" aria-label="Setup score"><header><div><span>SETUP SCORE SYSTEM</span><h2>{setupScore === null ? 'Setup score unavailable' : `${setupScore} / 100`}</h2></div><strong>{setupRating}</strong></header><div className="v21ScoreBreakdown">{scoreFactors.map(factor => <div key={factor.label}><span><b>{factor.label}</b><small>{factor.value}</small></span><em>{factor.points === null ? 'Unavailable' : `+${factor.points}`}</em></div>)}</div><p className="v21WhyScore"><b>WHY THIS SCORE?</b>{qualityCandidate?.score !== undefined ? ` Scanner decision score is ${qualityCandidate.score}; available setup factors are shown separately.` : analysis?.direction ? ` ${analysis.direction} analysis is available, but scanner confidence is unavailable.` : ' Insufficient structured setup data for a reliable score.'}</p></article>
      <article className="v21DailyCoach" aria-label="Daily trading coach"><header><div><span>DAILY TRADING COACH</span><h2>Review insight</h2></div><ClipboardList/></header>{coachTrades.length ? <><p className="v21CoachObservation">Observation: {coachTrades.length} journal outcomes are available for review.</p><div className="v21CoachInsights"><span><small>BEST TRADE</small><b>{fmt(coachBest)} USDT</b></span><span><small>WORST TRADE</small><b>{fmt(coachWorst)} USDT</b></span><span><small>WIN / LOSS</small><b>{coachTrades.filter(item => (item.realized_pnl ?? 0) > 0).length} / {coachTrades.filter(item => (item.realized_pnl ?? 0) < 0).length}</b></span></div><small className="v21CoachNote">Use this as a review observation, not financial advice.</small></> : <div className="v21CoachEmpty">Not enough trading history for a reliable daily review.</div>}</article>
    </section>}

    <section className="demoSetupCard">
      <div><LockKeyhole/><span><b>Binance Demo / Testnet hesabın</b><p>Kendi hesabının API Key ve Secret Key değerlerini gir. Credential’lar kullanıcı hesabın için saklanır ve Demo/Testnet dışına çıkmaz.</p></span></div>
      <div className="demoCredentialFields"><input aria-label="Demo API Key" value={demoCredentials.apiKey} onChange={event => setDemoCredentials(current => ({...current,apiKey:event.target.value}))} autoComplete="off" placeholder="Demo API Key"/><input aria-label="Demo Secret Key" type="password" value={demoCredentials.secretKey} onChange={event => setDemoCredentials(current => ({...current,secretKey:event.target.value}))} autoComplete="new-password" placeholder="Demo Secret Key"/></div>
      <div className="demoCredentialActions"><button onClick={() => void saveDemoConnection()} disabled={busy || !demoCredentials.apiKey || !demoCredentials.secretKey}><Save/> KAYDET</button><button onClick={() => void testDemoConnection()} disabled={busy || !demoCredentials.apiKey || !demoCredentials.secretKey}><Radio/> BAĞLANTIYI TEST ET</button></div>
    </section>

    <section className={`demoCommandBar ${tab !== 'trade' ? 'demoTabHidden' : ''}`}>
      <button className="demoConnect" disabled={busy || !status?.configured} onClick={connect}><Radio/> BAĞLANTIYI TEST ET</button>
      <div><ShieldCheck/><span><b>DEMO GÜVENLİK SINIRI</b><small>100 USDT marjin · 2x · 200 USDT sanal pozisyon · 3 pozisyon</small></span></div>
      <button className="demoEmergency" disabled={busy || !status?.configured} onClick={emergency}><TriangleAlert/> ACİL DEMO DURDUR</button>
    </section>

    <div className={`demoMessage demoMessage-${messageKind}`}>{messageKind === 'error' ? <TriangleAlert/> : messageKind === 'ok' ? <ShieldCheck/> : <Activity/>}<span>{message}</span></div>

    {lastOrder && <section className="demoOrderConfirmation" aria-live="polite">
      <header><div><small>DEMO EMİR ONAYI</small><h3>Emir Binance Futures Demo hesabına iletildi</h3></div><CheckCircle2/></header>
      <div><span><small>Order ID</small><b>{lastOrder.order_id ?? '—'}</b></span><span><small>Parite</small><b>{lastOrder.symbol ?? symbol}</b></span><span><small>Yön</small><b>{lastOrder.side ?? '—'}</b></span><span><small>Tip</small><b>{lastOrder.type ?? form.orderType}</b></span><span><small>Miktar</small><b>{lastOrder.quantity ?? '—'}</b></span><span><small>Fiyat</small><b>{lastOrder.price ?? 'MARKET'}</b></span><span><small>Durum</small><b>{lastOrder.status ?? '—'}</b></span></div>
    </section>}

    <section className={`demoAccountStrip ${tab !== 'trade' ? 'demoTabHidden' : ''}`}>
      <article><Wallet/><span><small>SANAL CÜZDAN</small><b>{fmt(account?.wallet_balance)} USDT</b></span></article>
      <article><CircleDollarSign/><span><small>KULLANILABİLİR</small><b>{fmt(account?.available_balance)} USDT</b></span></article>
      <article><Activity/><span><small>AÇIK PnL</small><b className={(account?.unrealized_pnl || 0) >= 0 ? 'demoProfit' : 'demoLoss'}>{(account?.unrealized_pnl || 0) >= 0 ? '+' : ''}{fmt(account?.unrealized_pnl)} USDT</b></span></article>
      <article><Crosshair/><span><small>POZİSYON</small><b>{account?.reconciliation?.reconciled_active_positions ?? 0} / {status?.limits.max_open_positions ?? 3}</b></span></article>
      <article><Target/><span><small>AÇIK EMİRLER</small><b>{(account?.open_orders.length || 0)+(account?.open_algo_orders.length || 0)}</b></span></article>
      <article className={account?.hedge_mode ? 'demoModeBad' : 'demoModeGood'}><ShieldCheck/><span><small>POZİSYON MODU</small><b>{account ? account.hedge_mode ? 'HEDGE · DEĞİŞTİR' : 'ONE-WAY · UYGUN' : '—'}</b></span></article>
    </section>

    {chart && <section className="demoLiveChart">
      <header><div><span>CANLI MUM GRAFİĞİ · EMA20 / EMA50 / EMA200</span><h3>{symbol.replace('USDT','/USDT')} Analiz ve Emir Seviyeleri</h3></div><div><b className={analysis?.direction === 'SHORT' ? 'demoLoss' : analysis?.direction === 'LONG' ? 'demoProfit' : ''}>{analysis?.direction || 'HESAPLANIYOR'}</b><small>Giriş {fmt(analysis?.entry)} · Stop {fmt(analysis?.stop_loss)} · TP3 {fmt(analysis?.tp3)}</small></div></header>
      <div className="demoChartCanvas">{chart}</div>
    </section>}

    <section className={`demoMainGrid ${tab !== 'trade' ? 'demoTabHidden' : ''}`}>
      <div className="demoTicket">
        <header><div><span>GÜVENLİ EMİR BİLETİ</span><div className="demoSymbolPicker"><button type="button" className="demoSymbolTrigger" aria-expanded={symbolOpen} onClick={() => setSymbolOpen(open => !open)}><span>{symbol.replace('USDT','/USDT')}</span><ChevronDown/></button>{symbolOpen && <div className="demoSymbolPopover"><label><Search/><input autoFocus value={symbolQuery} onChange={event => setSymbolQuery(event.target.value)} placeholder="Search symbol..."/><button type="button" aria-label="Close symbol search" onClick={() => {setSymbolQuery('');setSymbolOpen(false)}}><X/></button></label><div>{filteredMarkets.length ? filteredMarkets.map(market => <button type="button" key={market.symbol} className={market.symbol === symbol ? 'active' : ''} onClick={() => {onSymbolChange(market.symbol);setSymbolQuery('');setSymbolOpen(false)}}><b>{market.display}</b><small>{fmt(market.price)} · {market.change >= 0 ? '+' : ''}{fmt(market.change)}%</small></button>) : <p>Symbol data unavailable.</p>}</div></div>}</div></div><b>DEMO</b></header>
        <div className="demoSidePicker"><button className={form.direction === 'LONG' ? 'activeLong' : ''} onClick={() => setForm({...form,direction:'LONG'})}>LONG</button><button className={form.direction === 'SHORT' ? 'activeShort' : ''} onClick={() => setForm({...form,direction:'SHORT'})}>SHORT</button></div>
        <div className="demoTypePicker"><button className={form.orderType === 'MARKET' ? 'activeType' : ''} onClick={() => setForm({...form,orderType:'MARKET'})}>PİYASA</button><button className={form.orderType === 'LIMIT' ? 'activeType' : ''} onClick={() => setForm({...form,orderType:'LIMIT'})}>LİMİT</button></div>
        <button className="demoAnalysisFill" disabled={busy} onClick={fillFromAnalysis}><Activity/> {busy ? 'ANALİZ ALINIYOR…' : 'GÜNCEL ANALİZDEN DOLDUR'}</button>
        <div className="demoFieldGrid">
          <label><span>MARJİN · 5–100 DEMO USDT</span><div><input type="number" min="5" max="100" step="1" value={form.margin} onChange={event => changeMargin(event.target.value)} onBlur={normalizeMargin}/><em>USDT</em></div></label>
          <label className="leverageField"><span>LEVERAGE · MAX {status?.limits.max_leverage ?? 50}x</span><select value={form.leverage} onChange={event => setForm({...form,leverage:event.target.value as FormState['leverage']})}>{['AUTO','1','2','3','5','10','15','20','25','30','40','50','CUSTOM'].map(value => <option key={value} value={value} disabled={value !== 'AUTO' && value !== 'CUSTOM' && Number(value) > (status?.limits.max_leverage ?? 50)}>{value === 'AUTO' ? 'AUTO · 2x POLICY' : value === 'CUSTOM' ? 'CUSTOM' : `${value}x`}</option>)}</select>{form.leverage === 'CUSTOM' && <input className="customLeverageInput" type="number" min="1" max="50" step="1" value={form.customLeverage} onChange={event => setForm({...form,customLeverage:event.target.value})} placeholder="Custom leverage: 37x"/>}</label>
          {form.orderType === 'LIMIT' && <label className="fullField"><span>LİMİT FİYATI</span><input value={form.limitPrice} onChange={event => setForm({...form,limitPrice:event.target.value})}/></label>}
          <label className="stopField"><span>STOP LOSS</span><input value={form.stop} onChange={event => setForm({...form,stop:event.target.value})}/></label>
          <label><span>TP1 · %30</span><input value={form.tp1} onChange={event => setForm({...form,tp1:event.target.value})}/></label>
          <label><span>TP2 · %30</span><input value={form.tp2} onChange={event => setForm({...form,tp2:event.target.value})}/></label>
          <label><span>TP3 · KALANI</span><input value={form.tp3} onChange={event => setForm({...form,tp3:event.target.value})}/></label>
        </div>
        <div className="demoExposure"><span className={previewNotional > previewNotionalCap ? 'demoLoss' : ''}><small>NOTIONAL · CAP {fmt(previewNotionalCap)}</small><b>{fmt(previewNotional)} USDT</b></span><span><small>LEVERAGE</small><b>{form.leverage === 'AUTO' ? 'AUTO · 2x' : Number.isFinite(previewLeverage) ? `${previewLeverage}x` : 'INVALID'}</b></span><span><small>POSITION SIZE</small><b>{previewAvailable ? fmt(previewQuantity) : '—'}</b></span><span><small>RISK / TRADE</small><b>{previewAvailable ? `${fmt(previewRisk)} USDT` : '—'}</b></span><span><small>GERÇEK PARA</small><b>0 USDT</b></span></div>
        <button className="demoTest" disabled={busy || !status?.connected} onClick={testOrder}><TestTube2/> EMİR TESTİ · OLUŞTURMAZ</button>
        <button className="demoSubmit" disabled={busy || !status?.connected} onClick={submitOrder}><Send/> BINANCE DEMO EMRİ GÖNDER</button>
        <div className={`demoTicketFeedback demoTicketFeedback-${messageKind}`}>{messageKind === 'error' ? <TriangleAlert/> : messageKind === 'ok' ? <ShieldCheck/> : <Activity/>}<span><b>{messageKind === 'error' ? 'İŞLEM ENGELLENDİ' : messageKind === 'ok' ? 'DOĞRULAMA TAMAM' : 'GÜVENLİK DURUMU'}</b><small>{message}</small></span></div>
        <small className="demoTicketNote">{previewNotional > previewNotionalCap ? `Notional cap ${fmt(previewNotionalCap)} USDT; marjini veya kaldıraç seçimini düşürmeden emir gönderilemez.` : 'Bu tutar yalnızca sanal Binance Demo bakiyesidir. Gerçek Binance emir kanalı kilitlidir.'}</small>
      </div>

      <div className="demoPositions">
        <header><div><span>CANLI DEMO POZİSYONLARI</span><h3>Giriş, Stop, TP ve Seviye Haritası</h3></div><b>{account?.reconciliation?.reconciled_active_positions ?? 0} AÇIK</b></header>
        <div className="demoPositionList">{account?.reconciliation?.reconciled_active_positions ? account.positions.map(position => <article key={position.symbol}>
          <header><div><b>{position.symbol.replace('USDT','/USDT')}</b><span className={position.direction === 'LONG' ? 'demoLong' : 'demoShort'}>{position.direction}</span><em className={position.leverage_verified ? 'demoVerified' : 'demoPending'}>{position.leverage_verified ? <ShieldCheck/> : <TriangleAlert/>}{position.leverage ? `${position.leverage}x` : '—'} · {(position.margin_type || 'DOĞRULANIYOR').toUpperCase()}</em></div><strong className={position.unrealized_pnl >= 0 ? 'demoProfit' : 'demoLoss'}>{position.unrealized_pnl >= 0 ? '+' : ''}{fmt(position.unrealized_pnl)} USDT</strong></header>
          <div className="demoPositionMetrics"><span><small>Miktar</small><b>{fmt(position.quantity)}</b></span><span><small>Giriş</small><b>{fmt(position.entry_price)}</b></span><span><small>Canlı</small><b>{fmt(position.mark_price)}</b></span><span><small>Likidasyon</small><b>{fmt(position.liquidation_price)}</b></span></div>
          <div className="demoPositionMeta"><span><small>İstenen kaldıraç</small><b>{position.requested_leverage || activePlanBySymbol.get(position.symbol)?.requested_leverage || activePlanBySymbol.get(position.symbol)?.leverage || '—'}x</b></span><span><small>Uygulanan kaldıraç</small><b>{position.applied_leverage || position.leverage || '—'}x · {(position.margin_type || '—').toUpperCase()}</b></span></div>
          <div className={`demoLeverageAudit ${position.leverage_verified ? 'verified' : 'pending'}`}>{position.leverage_verified ? <ShieldCheck/> : <TriangleAlert/>}<span><small>KALDIRAÇ VE MARJİN DENETİMİ</small><b>{position.leverage_verified ? `Binance doğruladı: ${position.leverage}x ISOLATED` : 'Binance yapılandırması doğrulanıyor; değer uydurulmuyor.'}</b></span></div>
          {(() => { const observation = positionObservation(position,activePlanBySymbol.get(position.symbol)); return <div className="demoPositionAssistant"><header><span>POSITION MANAGEMENT ASSISTANT</span><b>{observation.stage}</b></header><div><span><small>BREAK-EVEN ADVISORY</small><b>{observation.breakEven}</b></span><span><small>TO STOP</small><b>{observation.distanceToStop === null ? 'UNAVAILABLE' : `%${observation.distanceToStop.toFixed(2)}`}</b></span><span><small>TO TP1</small><b>{observation.distanceToTarget === null ? 'UNAVAILABLE' : `%${observation.distanceToTarget.toFixed(2)}`}</b></span><span><small>TP PROGRESS</small><b>{observation.progress === null ? 'UNAVAILABLE' : `%${observation.progress.toFixed(1)}`}</b></span></div><small>{observation.reason} Advisory only; no stop is moved automatically.</small></div> })()}
          <PositionMap position={position} plan={activePlanBySymbol.get(position.symbol)}/>
          <footer><span>{activePlanBySymbol.get(position.symbol)?.monitoring_targets?.length ? `${activePlanBySymbol.get(position.symbol)?.monitoring_targets?.join(', ')} izleme hedefi` : 'Koşullu koruma kontrol ediliyor'}</span><button disabled={busy} onClick={() => closePosition(position)}>DEMO POZİSYONU KAPAT</button></footer>
        </article>) : <div className="demoEmpty"><Crosshair/><b>Açık Demo pozisyonu yok</b><span>Bağlantı kurulduğunda Binance Demo hesabındaki pozisyonlar burada canlı görünür.</span></div>}</div>
      </div>
    </section>

    <section className={`demoOrdersGrid ${tab !== 'trade' ? 'demoTabHidden' : ''}`}>
      <div className="demoOrderPanel"><header><div><span>BEKLEYEN GİRİŞLER</span><h3>Normal Demo Emirleri</h3></div><b>{account?.open_orders.length ?? 0}</b></header><div>{account?.open_orders.length ? account.open_orders.map(order => <article key={order.order_id}><span><b>{order.symbol} · {order.side}</b><small>{order.type} · {order.status}</small></span><em>{fmt(order.price || undefined)} · {fmt(order.quantity)}</em><button disabled={busy} onClick={() => cancelOrder(order)}>İPTAL</button></article>) : <p>Açık normal Demo emri yok.</p>}</div></div>
      <div className="demoOrderPanel"><header><div><span>STOP / TAKE PROFIT</span><h3>Koşullu Koruma Emirleri</h3></div><b>{account?.open_algo_orders.length ?? 0}</b></header><div>{account?.open_algo_orders.length ? account.open_algo_orders.map(order => <article key={order.algo_id}><span><b>{order.symbol} · {order.type}</b><small>{order.status} · {order.close_position ? 'Pozisyonu kapatır' : 'Kısmi azaltır'}</small></span><em>Tetik {fmt(order.trigger_price)}</em><button disabled={busy} onClick={() => cancelAlgo(order)}>İPTAL</button></article>) : <p>Açık koşullu Demo emri yok.</p>}</div></div>
      <div className="demoEventPanel"><header><div><span>DENETİM AKIŞI</span><h3>Son Güvenlik Olayları</h3></div><b>{status?.events.length ?? 0}</b></header><div>{status?.events.slice(0,6).map((event,index) => <article key={`${event.created_at}-${index}`}><i/><span><b>{event.kind}</b><small>{event.message}</small></span><time>{stamp(event.created_at)}</time></article>)}</div></div>
    </section>

    {tab === 'risk' && <section ref={workspaceRef} className="v21Workspace">
      <header className="v21WorkspaceHead"><div><span>V21 · KAYIP ÖNCE HESAPLANIR</span><h2>Risk Kasası ve Pozisyon Boyutlandırıcı</h2><p>“Kaç USDT yatırayım?” yerine “Stop olursa en fazla kaç USDT kaybedeyim?” sorusundan başlar.</p></div><div className="v21HeaderActions"><b><ShieldCheck/> DEMO HARD CAP · 100 USDT · 2X</b><button className="v21ContextCta" onClick={() => setTab('trade')}>RETURN TO TRADE DESK →</button></div></header>
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

    {tab === 'journal' && <section ref={workspaceRef} className="v21Workspace">
      <header className="v21WorkspaceHead"><div><span>ORDER_TRADE_UPDATE · ALGO_UPDATE · REST EŞLEŞTİRME</span><h2>Canlı Demo İşlem Günlüğü</h2><p>Açılış, kısmi dolum, kapanış, Stop/TP değişimi ve engelleme nedeni tek zaman çizgisinde.</p></div><div className="v21HeaderActions"><button disabled={v21Busy || !status?.connected} onClick={loadHistory}><History/> {symbol} BORSA GEÇMİŞİNİ GETİR</button><button className="v21ContextCta" onClick={() => setTab('performance')}>REVIEW PERFORMANCE →</button></div></header>
      <div className="v21JournalStats"><span><small>BUGÜN OLAY</small><b>{v21?.daily.events ?? 0}</b></span><span><small>USER STREAM</small><b>{v21?.stream.status || '—'}</b></span><span><small>SON EŞLEŞTİRME</small><b>{stamp(v21?.stream.last_sync)}</b></span><span><small>YENİDEN BAĞLANTI</small><b>{v21?.stream.reconnect_count ?? 0}</b></span></div>
      <div className="v21JournalGrid"><article className="v21Card v21Timeline"><header><ClipboardList/><div><small>KALICI YEREL KAYIT</small><h3>V21 Olay Zaman Çizgisi</h3></div><b>{v21?.journal.length ?? 0}</b></header><div>{v21?.journal.length ? v21.journal.map(item => <section key={item.id}><i className={item.realized_pnl && item.realized_pnl < 0 ? 'bad' : ''}/><div><span><b>{item.kind}</b><em>{item.symbol || 'SİSTEM'} · {item.source}</em></span><p>{item.message}</p>{item.reason && <small>{item.reason}</small>}</div><aside><time>{stamp(item.created_at)}</time>{item.realized_pnl !== null && item.realized_pnl !== undefined && <strong className={item.realized_pnl >= 0 ? 'demoProfit' : 'demoLoss'}>{item.realized_pnl >= 0 ? '+' : ''}{fmt(item.realized_pnl)}</strong>}</aside></section>) : <div className="v21EmptyMini">İlk Demo olayı bekleniyor.</div>}</div></article>
        <article className="v21Card v21ExchangeHistory"><header><History/><div><small>BINANCE FUTURES DEMO</small><h3>{symbol} Emir / Dolum Arşivi</h3></div></header>{historyPayload ? <div><h4>NORMAL EMİRLER · {historyPayload.orders.length}</h4>{historyPayload.orders.slice(-12).reverse().map((row,index) => <p key={`o-${index}`}><b>{String(row.side ?? '—')} · {String(row.type ?? '—')}</b><span>{String(row.status ?? '—')} · {String(row.avgPrice ?? row.price ?? '—')}</span></p>)}<h4>KOŞULLU EMİRLER · {historyPayload.algo_orders.length}</h4>{historyPayload.algo_orders.slice(-8).reverse().map((row,index) => <p key={`a-${index}`}><b>{String(row.orderType ?? row.type ?? 'ALGO')}</b><span>{String(row.algoStatus ?? row.status ?? '—')} · {String(row.triggerPrice ?? '—')}</span></p>)}<h4>DOLUMLAR · {historyPayload.trades.length}</h4>{historyPayload.trades.slice(-8).reverse().map((row,index) => <p key={`t-${index}`}><b>{String(row.side ?? '—')} · {String(row.qty ?? '—')}</b><span>PnL {String(row.realizedPnl ?? '0')} · ücret {String(row.commission ?? '—')}</span></p>)}</div> : <div className="v21EmptyMini">Üstteki düğmeyle seçili paritenin tam Demo geçmişini getir.</div>}</article>
      </div>
    </section>}

    {tab === 'performance' && <section ref={workspaceRef} className="v21Workspace v21PerformanceCenter">
      <header className="v21WorkspaceHead"><div><span>GERÇEK KAPANIŞ EVENTLERİ · READ ONLY</span><h2>Performance Center</h2><p>Sonuçlar yalnızca kapanmış Demo işlemlerinden ve backend journal kayıtlarından hesaplanır.</p></div><div className="v21HeaderActions"><div className="v21PeriodPicker">{(['all','daily','weekly','monthly'] as const).map(period => <button key={period} className={performancePeriod === period ? 'active' : ''} onClick={() => setPerformancePeriod(period)}>{period === 'all' ? 'TÜMÜ' : period === 'daily' ? 'GÜNLÜK' : period === 'weekly' ? 'HAFTALIK' : 'AYLIK'}</button>)}</div><button className="v21ContextCta" onClick={() => setTab('trade')}>BACK TO COMMAND CENTER →</button></div></header>
      {performance ? <><div className="v21PerformanceHero"><span><small>NET PROFIT</small><b className={performance.net_profit >= 0 ? 'demoProfit' : 'demoLoss'}>{performance.net_profit >= 0 ? '+' : ''}{fmt(performance.net_profit)} USDT</b><em>{performance.total_trades} kapanmış işlem</em></span><span><small>WIN RATE</small><b>{fmt(performance.win_rate)}%</b><em>{performance.wins} kazanç · {performance.losses} kayıp</em></span><span><small>PROFIT FACTOR</small><b>{fmt(performance.profit_factor)}</b><em>Gerçekleşen PnL</em></span><span><small>MAX DRAWDOWN</small><b className="demoLoss">{fmt(performance.max_drawdown)} USDT</b><em>Dönem içi</em></span></div><div className="v21PerformanceGrid">{[['TOPLAM KÂR',performance.total_profit,'demoProfit'],['TOPLAM ZARAR',performance.total_loss,'demoLoss'],['ORTALAMA İŞLEM',performance.average_trade,performance.average_trade >= 0 ? 'demoProfit' : 'demoLoss'],['EN İYİ İŞLEM',performance.best_trade,'demoProfit'],['EN KÖTÜ İŞLEM',performance.worst_trade,'demoLoss']].map(([label,value,kind]) => <article key={String(label)}><small>{label}</small><b className={String(kind)}>{Number(value) >= 0 ? '+' : ''}{fmt(Number(value))} USDT</b></article>)}</div></> : <div className="v21LargeEmpty"><BarChart3/><b>Performance verisi bekleniyor</b><span>Read-only kapanış kayıtları yükleniyor.</span></div>}
    </section>}

    {tab === 'performance' && performance && <section className="v21VerifiedAnalytics v21Workspace"><div className="v21MetricRow"><span><small>AVERAGE WIN</small><b>{performance.average_win === null ? 'UNAVAILABLE' : fmt(performance.average_win)}</b></span><span><small>AVERAGE LOSS</small><b>{performance.average_loss === null ? 'UNAVAILABLE' : fmt(performance.average_loss)}</b></span><span><small>WINNING STREAK</small><b>{performance.winning_streak || '—'}</b></span><span><small>LOSING STREAK</small><b>{performance.losing_streak || '—'}</b></span><span><small>LONG / SHORT</small><b>{performance.directional.LONG.trades} / {performance.directional.SHORT.trades}</b></span><span><small>HISTORY</small><b>{performance.history_quality}</b></span></div>{performance.equity_curve.length >= 2 ? <div className="v21EquityCurve" aria-label="Verified equity curve">{performance.equity_curve.map(point => <i key={point.index} className={point.pnl >= 0 ? 'positive' : 'negative'} style={{height:`${Math.max(8,Math.min(100,Math.abs(point.pnl) / Math.max(...performance.equity_curve.map(item => Math.abs(item.pnl)),1) * 100))}%`}} title={`${point.pnl >= 0 ? '+' : ''}${fmt(point.pnl)} USDT`}/>)}</div> : <div className="v21EmptyMini">INSUFFICIENT PERFORMANCE HISTORY · Equity curve requires at least two verified closes.</div>}</section>}

    {tab === 'auto' && <section ref={workspaceRef} className="v21Workspace">
      <header className="v21WorkspaceHead"><div><span>ÇİFT ONAY · DEMO ARM + DEMO OTOMATİK</span><h2>Kontrollü Demo Otopilot</h2><p>İzin listesi, yön, saat, güven, volatilite, korelasyon, günlük kayıp ve pozisyon kapıları birlikte geçmeden emir göndermez.</p></div><div className="v21HeaderActions"><b className={v21?.auto.enabled ? 'v21Running' : 'v21Stopped'}><Zap/> {v21?.auto.enabled ? 'ÇALIŞIYOR' : 'GÜVENLİ KAPALI'}</b><button className="v21ContextCta" onClick={() => setTab('trade')}>OPEN TRADE SETUP →</button></div></header>
      <div className="v21AutoLayout"><article className="v21Card v21AutoControl"><header><Zap/><div><small>İKİNCİ KULLANICI ONAYI</small><h3>Demo Otomasyon Motoru</h3></div></header><div className="v21AutoDecision"><small>SON KARAR</small><b>{v21?.auto.last_decision || 'Bekleniyor'}</b><span>{v21?.auto.last_scan ? `Son tarama ${stamp(v21.auto.last_scan)} · ${v21.auto.cycles} tur` : 'Henüz tarama yapılmadı.'}</span>{v21?.auto.rejection_reason && <em>İşlem Açılmadı · {v21.auto.rejection_gate}: {v21.auto.rejection_reason}</em>}</div>{!v21?.auto.enabled && <label><span>Başlatmak için yaz</span><input value={autoConfirm} onChange={event => setAutoConfirm(event.target.value)} placeholder="DEMO OTOMATİK"/></label>}<button className={v21?.auto.enabled ? 'stop' : ''} disabled={v21Busy || (!v21?.auto.enabled && !status?.armed)} onClick={toggleAuto}>{v21?.auto.enabled ? <TriangleAlert/> : <Play/>}{v21?.auto.enabled ? ' YENİ GİRİŞLERİ DURDUR' : ' KONTROLLÜ DEMO OTOMASYONU BAŞLAT'}</button><button disabled={v21Busy || !v21?.scanner.top_candidates.length} onClick={runSmokeTest}><TestTube2/> DEMO İŞLEMİ TEST ET</button><p>Uygulama yeniden açıldığında daima kapalı başlar. Stop/TP koruması motor dursa bile Binance Demo hesabında kalır.</p></article>
        <article className="v21Card v21AutoRules"><header><Settings2/><div><small>OTOMASYON EVRENİ</small><h3>İzinler ve Piyasa Kapıları</h3></div></header>{settingsDraft && <div>
          <label className="wide"><span>İzinli USDT pariteleri</span><input value={settingsDraft.allowed_symbols.join(', ')} onChange={event => setSettingsDraft({...settingsDraft,allowed_symbols:event.target.value.toUpperCase().split(',').map(value => value.trim()).filter(Boolean)})}/></label>
          <label><span>Maks. volatilite</span><input type="number" value={settingsDraft.max_volatility_pct} onChange={event => setSettingsDraft({...settingsDraft,max_volatility_pct:Number(event.target.value)})}/><em>%</em></label>
          <label><span>Maks. BTC korelasyonu</span><input type="number" value={settingsDraft.max_correlation_pct} onChange={event => setSettingsDraft({...settingsDraft,max_correlation_pct:Number(event.target.value)})}/><em>%</em></label>
          <label><span>Başlangıç saati</span><input type="number" min="0" max="23" value={settingsDraft.schedule_start_hour} onChange={event => setSettingsDraft({...settingsDraft,schedule_start_hour:Number(event.target.value)})}/></label>
          <label><span>Bitiş saati</span><input type="number" min="1" max="24" value={settingsDraft.schedule_end_hour} onChange={event => setSettingsDraft({...settingsDraft,schedule_end_hour:Number(event.target.value)})}/></label>
          <label className="v21Switch"><input type="checkbox" checked={settingsDraft.allow_long} onChange={event => setSettingsDraft({...settingsDraft,allow_long:event.target.checked})}/><span><b>LONG izinli</b></span></label>
          <label className="v21Switch"><input type="checkbox" checked={settingsDraft.allow_short} onChange={event => setSettingsDraft({...settingsDraft,allow_short:event.target.checked})}/><span><b>SHORT izinli</b></span></label>
        </div>}<button disabled={v21Busy || !settingsDraft} onClick={saveSettings}><Save/> OTOMASYON KAPILARINI KAYDET</button></article></div>
      <article className="v21Card v21AutoDiagnostics"><header><div><small>SCAN DURUMU</small><h3>Otomatik tarama ve son karar</h3></div><b>{scannerDiagnostics.result}</b></header><div className="v21ScannerStats"><span><small>DURUM</small><b>{scannerDiagnostics.status}</b><em>read-only</em></span><span><small>TARANAN COIN</small><b>{scannerDiagnostics.coinsScanned}</b><em>USDT perpetual</em></span><span><small>UYGUN ADAY</small><b>{scannerDiagnostics.eligible}</b><em>seçilen fırsat</em></span><span><small>SON TARAMA</small><b>{stamp(scannerDiagnostics.lastScan)}</b><em>güncel veri</em></span><span><small>SONRAKİ TARAMA</small><b>{stamp(scannerDiagnostics.nextScan)}</b><em>otomasyon döngüsü</em></span><span><small>SON KARAR</small><b>{scannerDiagnostics.lastDecision}</b><em>backend state</em></span></div><div className="v21OpportunityMeta"><span><small>SEÇİLEN SİMGELER</small><b>{scannerDiagnostics.selected.length ? scannerDiagnostics.selected.join(', ') : '—'}</b></span><span><small>TOP 3</small><b>{scannerDiagnostics.topCandidates.length ? scannerDiagnostics.topCandidates.map(candidate => `${candidate.symbol} (${candidate.score})`).join(', ') : '—'}</b></span><span><small>HATA</small><b>{scannerDiagnostics.lastError || '—'}</b></span></div></article>
      <article className="v21Card v21AutoScanner"><header><div className="v21ScannerTitle"><span className="v21ScannerIcon"><Gauge/></span><div><small>100 USDT PERPETUAL TARAMA</small><h3>Bot Durumu ve En İyi Fırsatlar</h3></div></div><b className={`v21ScannerStatus v21ScannerStatus-${(v21?.scanner.scan_status || 'BEKLEMEDE').toLowerCase()}`}>{v21?.scanner.scan_status || v21?.scanner.last_stage || 'BEKLEMEDE'}</b></header><div className="v21ScannerStats"><span><small>TARANAN COIN</small><b>{v21?.scanner.coins_scanned ?? 0}</b><em>USDT perpetual</em></span><span><small>UYGUN FIRSAT</small><b>{v21?.scanner.selected_count ?? v21?.scanner.eligible_count ?? 0}</b><em>Top fırsat</em></span><span><small>AÇIK POZİSYON</small><b>{v21?.account.positions ?? 0}/{v21?.settings.max_positions ?? 3}</b><em>Risk limiti</em></span><span><small>SON TARAMA</small><b>{stamp(v21?.scanner.last_scan_at)}</b><em>Güncel veri</em></span><span><small>SONRAKİ TARAMA</small><b>{stamp(v21?.scanner.next_scan_at)}</b><em>600 sn döngü</em></span></div><div className="v21ScannerResults"><div className="v21ScannerResultsHead"><div><small>BUGÜNÜN EN İYİ FIRSATLARI</small><h4>Skora göre sıralanan sinyaller</h4></div><span>{v21?.scanner.top_candidates.length ?? 0} sonuç</span></div>{v21?.scanner.top_candidates.length ? <div className="v21ScannerCards">{v21.scanner.top_candidates.map((candidate,index) => <article className={`v21Opportunity v21Opportunity-${candidate.direction.toLowerCase()}`} key={candidate.symbol}><header><div><small>#{candidate.rank || index + 1}</small><b>{candidate.symbol.replace('USDT','/USDT')}</b></div><em>{candidate.direction}</em></header><div className="v21OpportunityScore"><strong>{candidate.score.toFixed(0)}</strong><span>/ 100</span><i><b style={{width:`${Math.max(0,Math.min(100,candidate.score))}%`}}/></i></div><div className="v21OpportunityMeta"><span><small>GÜVEN</small><b>{candidate.confidence}</b></span>{candidate.trend && <span><small>TREND</small><b>{candidate.trend}</b></span>}{candidate.volatility_pct !== undefined && <span><small>VOLATİLİTE</small><b>%{candidate.volatility_pct.toFixed(2)}</b></span>}</div>{candidate.reasons?.length ? <ul>{candidate.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul> : <p className="v21NoReason">Sinyal nedeni mevcut değil.</p>}</article>)}</div> : <div className="v21ScannerEmpty"><Gauge/><b>{v21?.scanner.last_error || 'Henüz uygun fırsat bulunamadı.'}</b><span>Yeni tarama bekleniyor.</span></div>}</div></article>
      <div className="v21GateStrip"><span className={status?.armed ? 'passed' : ''}><b>1</b><em>DEMO ARM</em><small>{status?.armed ? 'GEÇTİ' : 'KAPALI'}</small></span><span className={status?.connected ? 'passed' : ''}><b>2</b><em>DEMO API</em><small>{status?.connected ? 'BAĞLI' : 'BEKLİYOR'}</small></span><span className={(v21?.daily.auto_entries || 0) < (v21?.settings.daily_trade_limit || 0) ? 'passed' : ''}><b>3</b><em>GÜNLÜK LİMİT</em><small>{v21?.daily.auto_entries ?? 0}/{v21?.settings.daily_trade_limit ?? 0}</small></span><span className={(v21?.daily.remaining_loss_budget || 0) > 0 ? 'passed' : ''}><b>4</b><em>ZARAR KASASI</em><small>{fmt(v21?.daily.remaining_loss_budget)} USDT</small></span><span className={(v21?.account.reconciled_active_positions ?? 0) < (v21?.settings.max_positions || 0) ? 'passed' : ''}><b>5</b><em>POZİSYON</em><small>{v21?.account.reconciled_active_positions ?? 0}/{v21?.settings.max_positions ?? 0}</small></span><span><b>6</b><em>SİNYAL KAPILARI</em><small>Her taramada</small></span></div>
    </section>}

    {tab === 'backtest' && <section ref={workspaceRef} className="v21Workspace">
      <header className="v21WorkspaceHead"><div><span>NO LOOK-AHEAD · NEXT OPEN · STOP FIRST</span><h2>Kanıtlı Backtest Laboratuvarı</h2><p>Sinyal kapanan mumdan, giriş sonraki mum açılışından alınır; ücret ve kayma iki yönlü düşülür.</p></div><div className="v21HeaderActions"><div className="v21BacktestRun"><select value={backtestSymbol} onChange={event => setBacktestSymbol(event.target.value)}>{(v21?.settings.allowed_symbols || [symbol]).map(item => <option key={item}>{item}</option>)}</select><button disabled={v21Busy || !status?.configured} onClick={runBacktest}><BarChart3/> 1.000 MUMU TEST ET</button></div><button className="v21ContextCta" onClick={() => setTab('performance')}>REVIEW PERFORMANCE →</button></div></header>
      {v21?.backtest ? <><div className="v21BacktestMetrics"><span><small>NET SONUÇ</small><b className={v21.backtest.net_pnl >= 0 ? 'demoProfit' : 'demoLoss'}>{v21.backtest.net_pnl >= 0 ? '+' : ''}{fmt(v21.backtest.net_pnl)} USDT</b></span><span><small>İŞLEM</small><b>{v21.backtest.trades}</b></span><span><small>BAŞARI</small><b>%{fmt(v21.backtest.win_rate)}</b></span><span><small>MAKS. DÜŞÜŞ</small><b>%{fmt(v21.backtest.max_drawdown_pct)}</b></span><span><small>PROFIT FACTOR</small><b>{fmt(v21.backtest.profit_factor)}</b></span><span><small>GELECEK SIZINTISI</small><b>{v21.backtest.no_lookahead ? 'YOK' : 'KONTROL'}</b></span></div><div className="v21BacktestLayout"><article className="v21Card v21Folds"><header><BarChart3/><div><small>3 DÖNEMLİ ZAMAN TÜNELİ</small><h3>Geliştirme · Doğrulama · Görünmeyen</h3></div></header>{v21.backtest.folds.map((fold,index) => <section key={fold.name}><b>{index+1}</b><span><strong>{fold.name}</strong><small>{fold.trades} işlem</small></span><em className={fold.net_pnl >= 0 ? 'demoProfit' : 'demoLoss'}>{fold.net_pnl >= 0 ? '+' : ''}{fmt(fold.net_pnl)} USDT</em></section>)}</article><article className="v21Card v21TradeResults"><header><History/><div><small>SON İŞLEMLER</small><h3>Maliyet Sonrası Sonuçlar</h3></div></header><div>{v21.backtest.recent_trades.slice(0,16).map((trade,index) => <p key={index}><span><b>{trade.direction} · {trade.reason}</b><small>{trade.regime} · maliyet {fmt(trade.cost_usdt)}</small></span><em className={trade.pnl >= 0 ? 'demoProfit' : 'demoLoss'}>{trade.pnl >= 0 ? '+' : ''}{fmt(trade.pnl)}</em></p>)}</div></article></div><p className="v21Disclaimer">{v21.backtest.note}</p></> : <div className="v21LargeEmpty"><BarChart3/><b>Henüz V21 backtest çalıştırılmadı</b><span>Seçili paritede 1.000 Demo Futures mumunu kronolojik olarak sınamak için üstteki düğmeye bas.</span></div>}
    </section>}

    {tab === 'certificate' && <section ref={workspaceRef} className="v21Workspace">
      <header className="v21WorkspaceHead"><div><span>V21 DEMO DISCIPLINE CERTIFICATE</span><h2>Sistem Sağlığı ve Demo Sertifikası</h2><p>Sertifika kâr vaadi değildir; yalnızca Demo kanıtı, koruma, tekrar, bağlantı ve düşüş eşiklerini ölçer.</p></div><b className={v21?.certificate.status === 'DEMO SERTİFİKALI' ? 'v21Running' : 'v21Pending'}><ShieldCheck/> {v21?.certificate.status || 'KANIT BEKLİYOR'}</b></header>
      <div className="v21CertificateLayout"><article className="v21Card v21Score"><div className="v21ScoreRing" style={{'--score':`${v21?.certificate.score || 0}%`} as CSSProperties}><span><b>%{v21?.certificate.score ?? 0}</b><small>DEMO KANIT</small></span></div><h3>{v21?.certificate.passed_gates ?? 0} / {v21?.certificate.total_gates ?? 0} kapı geçti</h3><p>{v21?.certificate.reason}</p><button onClick={enableNotifications}><Bell/> MASAÜSTÜ BİLDİRİMLERİNİ AÇ</button></article><article className="v21Card v21CertificateGates"><header><ShieldCheck/><div><small>ZORUNLU KANIT KAPILARI</small><h3>V21 Kontrol Listesi</h3></div></header><div>{v21?.certificate.gates.map(gate => <section className={gate.passed ? 'passed' : ''} key={gate.name}><i>{gate.passed ? '✓' : '!'}</i><span><b>{gate.name}</b><small>Hedef: {gate.target}</small></span><strong>{gate.value}</strong></section>)}</div></article><article className="v21Card v21Health"><header><Activity/><div><small>BAĞLANTI VE KURTARMA</small><h3>Canlı Sistem Sağlığı</h3></div></header><span><small>Demo REST</small><b>{status?.connected ? 'BAĞLI' : 'BEKLİYOR'}</b></span><span><small>Kullanıcı akışı</small><b>{v21?.stream.status || '—'}</b></span><span><small>Aktarım yolu</small><b>{v21?.stream.transport || '—'}</b></span><span><small>Akış hatası</small><b>{v21?.stream.error_count ?? 0}</b></span><span><small>Son yedek</small><b>{stamp(v21?.last_saved)}</b></span><div><button disabled={v21Busy} onClick={() => runDrill('RECONNECT')}>BAĞLANTI TATBİKATI</button><button disabled={v21Busy} onClick={() => runDrill('PROTECTION')}>STOP TATBİKATI</button><button disabled={v21Busy} onClick={() => runDrill('EMERGENCY')}>ACİL DURDURMA TATBİKATI</button></div></article></div>
      <div className="v21SafetyLock"><LockKeyhole/><span><b>GERÇEK PARA VE GERÇEK BINANCE EMİR KANALI FİZİKSEL OLARAK YOK</b><small>Bu paket yalnızca https://demo-fapi.binance.com ve wss://demo-fstream.binance.com adreslerini kullanır.</small></span><strong>DEMO ONLY</strong></div>
    </section>}
    {confirmation && <div className="demoConfirmationBackdrop" role="presentation" onClick={() => {setConfirmation(null);setConfirmationText('');setConfirmationChecked(false)}}><section className="demoConfirmation" role="dialog" aria-modal="true" aria-labelledby="demo-confirmation-title" onClick={event => event.stopPropagation()}><h2 id="demo-confirmation-title">{confirmation.title}</h2><p>{confirmation.message}</p>{confirmation.checkbox ? <label><input type="checkbox" checked={confirmationChecked} onChange={event => setConfirmationChecked(event.target.checked)}/><span>Demo order'ın gönderilmesini onaylıyorum.</span></label> : <label><span>Onay için {confirmation.expected} yazın</span><input autoFocus value={confirmationText} onChange={event => setConfirmationText(event.target.value)} onKeyDown={event => {if(event.key === 'Enter') confirmAction()}}/></label>}<div><button type="button" onClick={() => {setConfirmation(null);setConfirmationText('');setConfirmationChecked(false)}}>İPTAL</button><button type="button" disabled={confirmation.checkbox ? !confirmationChecked : confirmationText.trim().toUpperCase() !== confirmation.expected} onClick={confirmAction}>{confirmation.checkbox ? 'ONAYLA VE GÖNDER' : 'Onayla'}</button></div></section></div>}
  </section>
}
