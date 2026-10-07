// @ts-nocheck
import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertTriangle, CheckCircle2, CircleDollarSign, Eye, EyeOff, KeyRound, LockKeyhole, Power, RefreshCw, Send, ShieldAlert, ShieldCheck, TriangleAlert, UnlockKeyhole, Wallet } from 'lucide-react'
import { API_BASE } from './api'
import { fetchWithTimeout } from '../../master-trade-request'
import { liveCopy } from '../../ui-copy'
import { MasterTradeLiveLayout } from '../../MasterTradeLayout'
import {useKaisErrorReaction} from '../../useKaisPageReactions'
import {useTradingPreferences} from '../../useTradingPreferences'
import {preferredRiskMargin} from '../../account-risk-sizing'

type LivePolicy = Record<string, unknown>
type LiveStatus = {
  credentials?: {configured?: boolean; fingerprint?: string | null; storage?: string}
  connected?: boolean
  connection?: {last_checked?: string | null; last_error?: string | null; clock_offset_ms?: number | null}
  stream?: {status?: string; last_error?: string | null}
  armed?: boolean
  armed_until?: string | null
  real_trading_locked?: boolean
  live_auto_trade?: boolean
  auto?: {enabled?: boolean; status?: string; last_decision?: string; last_error?: string | null; last_scan?: string | null; last_skip_reason?: string | null; last_cycle_stage?: string | null; session_until?: string | null}
  scanner?: {last_scan_at?: string | null; scanned_symbol_count?: number; candidate_symbols?: string[]; candidate_count?: number; selected_symbols?: string[]; selected_symbols_count?: number; last_skip_reason?: string | null; last_cycle_stage?: string | null}
  policy?: LivePolicy
  policy_acknowledged?: boolean
  consent?: {active?: boolean; expires_at?: string | null; fingerprint?: string | null; reason?: string; scope_match?: boolean}
  authorization?: {valid?: boolean; expires_at?: string | null; reason?: string; scope_match?: boolean}
  readiness?: {ready?: boolean; gates?: Array<{key?: string; label?: string; passed?: boolean; detail?: string}>}
  account?: {wallet_balance?: number | null; available_balance?: number | null; margin_balance?: number | null; unrealized_pnl?: number | null; positions?: Array<Record<string, unknown>>; open_orders?: Array<Record<string, unknown>>}
  daily?: {realized_pnl?: number; remaining_loss_budget?: number; entries?: number}
  plans?: Array<Record<string, unknown> & {targets?: unknown[]; stop_loss?: unknown; protection_state?: string; protection_status?: string}>
  events?: Array<{kind?: string; message?: string; created_at?: string; symbol?: string; failures?: string[]}>
  emergency?: {active?: boolean; reason?: string | null}
  recovery_ready?: boolean
  recovery_error?: string | null
  execution_state?: string
  reconciliation_required?: boolean
  reconciliation_diagnostic?: {exception_type?: string; exception_message?: string; reconciliation_stage?: string; source?: string; function?: string; line?: number; timestamp?: string | null}
}

type ConnectionStatus = {
  vault?: {ready?: boolean}
  connections?: {LIVE?: {configured?: boolean; active?: boolean; last_test_ok?: boolean; fingerprint?: string | null; last_error?: string | null; account?: {wallet_balance?: number}}}
  safety?: {secrets_returned_to_browser?: boolean; connection_test_creates_orders?: boolean; live_orders_require_v25_gates?: boolean}
}

type ConnectionTestResult = {testedAt?: string; fingerprint?: string | null}
type ExternalHistory = {read_only?: boolean; label?: string; external_trades?: Array<Record<string, unknown>>; external_income?: Array<Record<string, unknown>>; errors?: Array<Record<string, unknown>>}

type OrderDraft = {symbol: string; direction: 'LONG' | 'SHORT'; order_type: 'MARKET' | 'LIMIT'; margin_usdt: string; leverage: string; limit_price: string; stop_loss: string; tp1: string; tp2: string; tp3: string}

export type SharedLiveStatus = LiveStatus
export type SharedConnectionStatus = ConnectionStatus
type Props = {active: boolean; symbol: string; analysis?: {direction?: string | null; confidence?: number; entry?: number; stop_loss?: number; tp1?: number; tp2?: number; tp3?: number} | null; masterTrade?: boolean; masterTradeTab?: 'analiz' | 'canli' | 'pozisyonlar' | 'baglanti'; sharedStatus?: LiveStatus | null; sharedConnections?: ConnectionStatus | null; onRefreshStatus?: (force?: boolean) => Promise<void>}

const V25 = `${API_BASE}/v25`
const CONNECTIONS = `${API_BASE}/exchange-connections`
const initialOrder = (symbol: string): OrderDraft => ({symbol, direction: 'LONG', order_type: 'MARKET', margin_usdt: '10', leverage: '1', limit_price: '', stop_loss: '', tp1: '', tp2: '', tp3: ''})
const text = (value: unknown) => value === null || value === undefined || value === '' ? '—' : String(value)
const signalPrice = (value: number | null | undefined) => value === null || value === undefined || !Number.isFinite(value) ? '—' : value.toLocaleString('tr-TR', {minimumFractionDigits: 2, maximumFractionDigits: 2})
const money = (value: unknown) => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  const decimals = Math.abs(value) < 10 ? 4 : 2
  return `${value.toLocaleString('tr-TR', {maximumFractionDigits: decimals, minimumFractionDigits: decimals})} USDT`
}
const date = (value: unknown) => value ? new Date(String(value)).toLocaleString('tr-TR', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—'
const activityTime = (value: unknown) => value ? new Date(String(value)).toLocaleTimeString('tr-TR', {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : '--:--:--'

export function liveBlockerMessage(status: LiveStatus | null, liveArmed: boolean): string {
  if (status === null) return 'LIVE status is not available.'
  if (status.emergency?.active) return status.reconciliation_diagnostic?.exception_message && status.emergency.reason === 'LIVE_EXCEPTION'
    ? `${status.emergency.reason}: ${status.reconciliation_diagnostic.exception_message}`
    : status.emergency.reason || 'Emergency stop is active.'
  if (status.reconciliation_required || status.recovery_error || status.execution_state === 'UNKNOWN') return status.recovery_error || (status.reconciliation_required
    ? 'Reconciliation is required before LIVE execution can continue.' : 'LIVE recovery is required before execution can continue.')
  if (!status.connected) return 'LIVE account connection is required.'
  if (status.authorization?.valid !== true) {
    const reason = status.authorization?.reason || status.consent?.reason || 'EXPIRED'
    const messages: Record<string, string> = {
      EXPIRED: '24 saatlik izin sona erdi.',
      TRADING_ACCOUNT_CHANGED: 'Doğrulanmış trading account değişti.',
      CREDENTIAL_CHANGED: 'API credentials değişti.',
      POLICY_CHANGED: 'Risk policy değişti; policy acknowledgement ve authorization yenilenmeli.',
      USER_CHANGED: 'Kullanıcı değişti.',
      RECOVERY_REQUIRED: 'Recovery tamamlanmadan authorization kullanılamaz.',
      RECONCILIATION_REQUIRED: 'Reconciliation tamamlanmadan authorization kullanılamaz.',
      EMERGENCY_BLOCKED: 'Emergency block aktif.',
    }
    return Object.hasOwn(messages, reason) ? messages[reason] : '24 saatlik LIVE authorization gerekli.'
  }
  if (status.readiness?.ready !== true) return 'Complete the required LIVE readiness checks before arming.'
  return liveArmed ? 'No LIVE blocker.' : 'Canlı işlem onayı bekleniyor.'
}

function errorMessage(payload: unknown, fallback: string): string {
  if (typeof payload === 'string' && payload.trim()) return payload
  if (payload && typeof payload === 'object') {
    const row = payload as {detail?: unknown; message?: unknown}
    if (typeof row.detail === 'string' && row.detail.trim()) return row.detail
    if (row.detail && typeof row.detail === 'object') {
      const detail = row.detail as {message?: unknown; msg?: unknown; reason?: unknown}
      const message = detail.message ?? detail.msg ?? detail.reason
      if (typeof message === 'string' && message.trim()) return message
    }
    if (Array.isArray(row.detail)) {
      const messages = row.detail.map(item => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object' && typeof (item as {msg?: unknown}).msg === 'string') return (item as {msg: string}).msg
        return ''
      }).filter(Boolean)
      if (messages.length) return messages.join(' · ')
    }
    if (typeof row.message === 'string') return row.message
  }
  return fallback
}

export default function LiveTradingPanel({active, symbol, analysis, masterTrade, masterTradeTab, sharedStatus, sharedConnections, onRefreshStatus}: Props) {
  const tradingDefaults = useTradingPreferences()
  const defaultsApplied = useRef(false)
  const [sizingNotice, setSizingNotice] = useState('')
  useEffect(() => {
    if (!active || (masterTrade && masterTradeTab !== 'canli') || defaultsApplied.current || !tradingDefaults.preferences) return
    defaultsApplied.current = true
    if (tradingDefaults.untouched() && tradingDefaults.preferences.trading_mode === 'AUTO') {
      const section = document.getElementById('master-trade-auto-trade')
      section?.scrollIntoView({block: 'center'})
      section?.focus({preventScroll: true})
    }
  }, [active, masterTrade, masterTradeTab, tradingDefaults.preferences, tradingDefaults.untouched])
  const [localStatus, setLocalStatus] = useState<LiveStatus | null>(null)
  const [localConnections, setLocalConnections] = useState<ConnectionStatus | null>(null)
  const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null)
  const [credentials, setCredentials] = useState({apiKey: '', secretKey: '', accepted: false})
  const [order, setOrder] = useState<OrderDraft>(initialOrder(symbol))
  const [policyDraft, setPolicyDraft] = useState<LivePolicy | null>(null)
  const [confirm, setConfirm] = useState<{title: string;message: string;expected?: string;simple?: boolean;action: () => Promise<void>} | null>(null)
  const [confirmText, setConfirmText] = useState('')
  const [busy, setBusy] = useState('')
  const [armPendingSync, setArmPendingSync] = useState(false)
  const [rateLimitSeconds, setRateLimitSeconds] = useState<number | null>(null)
  const [showSecret, setShowSecret] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [externalHistory, setExternalHistory] = useState<ExternalHistory | null>(null)
  const [reviewResult, setReviewResult] = useState<{message: string; notional?: number; estimated_stop_loss_usdt?: number} | null>(null)
  const [credentialModalOpen, setCredentialModalOpen] = useState(false)
  const [refreshPending, setRefreshPending] = useState(false)
  const [adoptionCandidate, setAdoptionCandidate] = useState<{
    symbol: string
    candidatePlan: any
    confirmToken: string
  } | null>(null)
  const [notice, setNotice] = useState<{kind: 'info' | 'ok' | 'error'; text: string}>({kind: 'info', text: 'LIVE başlatılmadı. Gerçek emir kilidi varsayılan olarak kapalıdır.'})
  useKaisErrorReaction(notice)
  const refreshInFlight = useRef(false)
  const actionInFlight = useRef(false)
  const confirmationOpen = useRef(false)
  confirmationOpen.current = confirm !== null
  const status = sharedStatus !== undefined ? sharedStatus : localStatus
  const connections = sharedConnections !== undefined ? sharedConnections : localConnections

  useEffect(() => {
    if (rateLimitSeconds === null || rateLimitSeconds <= 0) return
    const timer = window.setInterval(() => setRateLimitSeconds(current => current === null ? null : Math.max(0, current - 1)), 1000)
    return () => window.clearInterval(timer)
  }, [rateLimitSeconds])

  const isConnectionAction = (path: string) => ['/test', '/save', '/activate'].includes(path)
  const rateLimitMessage = (seconds: number | null) => seconds && seconds > 0
    ? `Binance bağlantısı geçici olarak hız sınırına ulaştı. Tekrar denemeden önce ${seconds} saniye bekleyin.`
    : 'Binance bağlantısı geçici olarak hız sınırına ulaştı.'

  const call = async <T,>(base: string, path: string, options: RequestInit = {}): Promise<T> => {
    if (rateLimitSeconds !== null && rateLimitSeconds > 0 && base === CONNECTIONS && isConnectionAction(path)) throw new Error(rateLimitMessage(rateLimitSeconds))
    const headers = new Headers(options.headers)
    if (options.body) headers.set('Content-Type', 'application/json')
    const response = await fetchWithTimeout(`${base}${path}`, {...options, headers})
    const payload:unknown = await response.json()
    if (response.status === 429) {
      const retryAfter = Number(response.headers.get('Retry-After'))
      const seconds = Number.isFinite(retryAfter) && retryAfter > 0 ? Math.ceil(retryAfter) : 0
      setRateLimitSeconds(seconds)
      throw new Error(rateLimitMessage(seconds || null))
    }
    if (!response.ok) {
      const statusLabel = response.status === 423
        ? 'LIVE KİLİTLİ / GATE BLOCKED'
        : response.status === 502
          ? 'BELİRSİZ YÜRÜTME / MANUEL İNCELEME GEREKLİ'
          : response.status === 503
            ? 'BAKIM MODU / YENİ GİRİŞLER KAPALI'
            : `LIVE HTTP ${response.status}`
      throw new Error(`${statusLabel}: ${errorMessage(payload, 'LIVE isteği reddedildi.')}`)
    }
    return payload as T
  }

  const refresh = async (quiet = true) => {
    if (!onRefreshStatus && (refreshInFlight.current || confirmationOpen.current)) return
    setRefreshPending(true)
    if (onRefreshStatus) {
      try {
        await onRefreshStatus(!quiet)
        return true
      } catch (error) {
        if (!quiet) setNotice({kind: 'error', text: error instanceof Error ? error.message : 'LIVE durumu okunamadı.'})
        return false
      }
      finally { setRefreshPending(false) }
    }
    refreshInFlight.current = true
    try {
      const [nextStatus, nextConnections] = await Promise.all([
        call<LiveStatus>(V25, '/status'),
        call<ConnectionStatus>(CONNECTIONS, '/status'),
      ])
      if (confirmationOpen.current) return
      setLocalStatus(nextStatus)
      setLocalConnections(nextConnections)
      setPolicyDraft(current => current || nextStatus.policy || {})
      return true
    } catch (error) {
      setLocalStatus(null)
      setLocalConnections(null)
      if (!quiet) setNotice({kind: 'error', text: error instanceof Error ? error.message : 'LIVE durumu okunamadı.'})
      return false
    } finally {
      refreshInFlight.current = false
      setRefreshPending(false)
    }
  }

  useEffect(() => {
    if (!active) return
    setOrder(initialOrder(symbol))
    setConfirm(null)
    setConfirmText('')
    void refresh()
    const timer = window.setInterval(() => void refresh(), 10000)
    return () => { if (timer !== undefined) window.clearInterval(timer) }
  }, [active, symbol, Boolean(onRefreshStatus)])

  const run = async (key: string, action: () => Promise<void>, success: string) => {
    if (actionInFlight.current) return
    actionInFlight.current = true
    setBusy(key)
    try {
      await action()
      if (!await refresh()) throw new Error('İşlem sonrasında LIVE durumu doğrulanamadı; tekrar göndermeden önce durumu kontrol edin.')
      setNotice({kind: 'ok', text: success})
    }
    catch (error) {
      await refresh()
      setNotice({kind: 'error', text: error instanceof Error ? error.message : 'LIVE işlemi tamamlanamadı.'})
    }
    finally { actionInFlight.current = false; setBusy('') }
  }

  const liveConnection = connections?.connections?.LIVE
  const configured = Boolean(liveConnection?.configured || status?.credentials?.configured)
  const connected = Boolean(status?.connected)
  const marketDataConnected = status?.stream?.status === 'CANLI'
  const emergency = Boolean(status?.emergency?.active)
  const recoveryRequired = Boolean(emergency || status?.reconciliation_required || status?.recovery_error || status?.execution_state === 'UNKNOWN')
  const protectionGate = status?.readiness?.gates?.find(gate => /protection|koruma/i.test(`${gate.key} ${gate.label}`))
  const recoveryState = recoveryRequired ? 'REQUIRED' : status?.recovery_ready === true ? 'READY' : 'UNKNOWN'
  const readinessReady = status?.readiness?.ready === true
  const authorizationValid = status?.authorization?.valid === true
  const authorizationReason = status?.authorization?.reason || status?.consent?.reason || 'EXPIRED'
  const authorizationReasonText = authorizationReason === 'EXPIRED'
    ? '24 saatlik izin sona erdi.'
    : authorizationReason === 'TRADING_ACCOUNT_CHANGED'
      ? 'Doğrulanmış trading account değişti.'
      : authorizationReason === 'CREDENTIAL_CHANGED'
        ? 'API credentials değişti.'
        : authorizationReason === 'POLICY_CHANGED'
          ? 'Risk policy değişti; policy acknowledgement ve authorization yenilenmeli.'
          : authorizationReason === 'USER_CHANGED'
            ? 'Kullanıcı değişti.'
            : authorizationReason === 'RECOVERY_REQUIRED'
              ? 'Recovery tamamlanmadan authorization kullanılamaz.'
              : authorizationReason === 'RECONCILIATION_REQUIRED'
                ? 'Reconciliation tamamlanmadan authorization kullanılamaz.'
                : authorizationReason === 'EMERGENCY_BLOCKED'
                  ? 'Emergency block aktif.'
                  : '24 saatlik LIVE authorization gerekli.'
  const riskGate = status?.readiness?.gates?.find(gate => /risk|policy|limit|safety|guven/i.test(`${gate.key} ${gate.label}`)) || {passed: false}
  const activePlans = status?.plans?.filter(plan => !['CLOSED', 'CANCELLED', 'CLOSED_CONFIRMED'].includes(String(plan.status || '').toUpperCase())).length ?? 0
  const noActivePositionOrPlan = (status?.account?.positions || []).length === 0 && activePlans === 0
  const protectionState = recoveryRequired ? 'BLOCKED' : noActivePositionOrPlan ? 'READY' : activePlans === 0 ? 'EXTERNAL' : protectionGate ? protectionGate.passed ? 'READY' : 'REQUIRED' : 'UNKNOWN'
  const protectionReady = protectionState === 'READY'
  const exposure = (status?.account?.positions || []).reduce((sum, item) => sum + Math.abs(Number(item.notional || item.positionAmt || 0)), 0)
  const executionLocked = status === null || status.real_trading_locked !== false
  const gateState = (patterns: RegExp[]) => {
    if (!status) return 'UNKNOWN'
    const gate = status.readiness?.gates?.find(item => patterns.some(pattern => pattern.test(`${item.key} ${item.label}`)))
    return gate ? gate.passed ? 'READY' : 'BLOCKED' : 'UNKNOWN'
  }
  const connectionReady = status !== null && connections !== null && connected
  const connectionState = status === null || connections === null ? 'UNKNOWN' : connected ? 'CONNECTED' : 'NOT CONNECTED'
  const liveArmed = Boolean((status?.armed || armPendingSync) && !executionLocked)
  const armState = status === null ? 'UNKNOWN' : liveArmed ? 'READY' : 'LOCKED'
  const riskState = riskGate ? riskGate.passed ? 'READY' : 'BLOCKED' : gateState([/risk/i, /policy/i, /limit/i, /safety/i, /guven/i])
  const exposureGate = gateState([/exposure/i, /notional/i])
  const exposureState = exposureGate !== 'UNKNOWN' ? exposureGate : status === null ? 'UNKNOWN' : exposure === 0 ? 'READY' : 'BLOCKED'
  const activePlanState = status === null ? 'UNKNOWN' : activePlans ? 'CONFLICT' : 'READY'
  const emergencyState = status === null ? 'UNKNOWN' : emergency ? 'ACTIVE' : 'CLEAR'
  const policy = policyDraft || status?.policy || {}
  const selectedAutoTimeframe = String(policy.interval ?? status?.policy?.interval ?? '15m')
  const savedAutoTimeframe = String(status?.policy?.interval ?? '15m')
  const autoTimeframeSupported = selectedAutoTimeframe === '15m' && savedAutoTimeframe === '15m'
  const autoTimeframeMessage = "LIVE Auto Trade yalnızca 15m zaman diliminde çalışır. Başlatmak için timeframe'i 15m seçip politikayı kaydedin."
  const timeframeWarning = !autoTimeframeSupported ? <div className="liveNotice error" role="alert" data-testid="live-auto-timeframe-warning"><TriangleAlert aria-hidden="true"/><span>{autoTimeframeMessage} Timeframe: {selectedAutoTimeframe !== '15m' ? selectedAutoTimeframe : savedAutoTimeframe}.</span></div> : null
  const autoReady = Boolean(autoTimeframeSupported && status && connections && authorizationValid && connectionReady && armState === 'READY' && !executionLocked && readinessReady && riskState === 'READY' && exposureState === 'READY' && activePlanState === 'READY' && protectionState === 'READY' && recoveryState === 'READY' && emergencyState === 'CLEAR')
  const manualOrderReady = Boolean(status && connected && authorizationValid && liveArmed && readinessReady && !recoveryRequired && !emergency)
  const setPolicy = (key: string, value: unknown) => setPolicyDraft(current => ({...(current || {}), [key]: value}))
  const numericPolicy = (key: string, fallback: number) => Number(policy[key] ?? fallback)
  const updatePolicy = () => run('policy', async () => {
    await call<LiveStatus>(V25, '/policy', {method: 'PUT', body: JSON.stringify(policy)})
  }, 'LIVE risk ve strateji politikası kaydedildi; policy acknowledgement ayrıca gereklidir.')

  const testConnection = () => run('test', async () => {
    const body = credentials.apiKey && credentials.secretKey ? {mode: 'LIVE', api_key: credentials.apiKey, secret_key: credentials.secretKey} : {mode: 'LIVE'}
    const result = await call<{fingerprint?: string | null; account?: {tested_at?: string}}>(CONNECTIONS, '/test', {method: 'POST', body: JSON.stringify(body)})
    setTestResult({testedAt: result.account?.tested_at, fingerprint: result.fingerprint})
  }, 'LIVE API erişimi doğrulandı; test bağlantısı emir oluşturmadı.')

  const saveCredentials = () => run('save', async () => {
    if (!credentials.apiKey || !credentials.secretKey) throw new Error('LIVE API Key ve Secret Key birlikte girilmelidir.')
    if (!credentials.accepted) throw new Error('Secret yalnızca şifreli sunucu kasasında tutulur onayını verin.')
    await call(CONNECTIONS, '/save', {method: 'POST', body: JSON.stringify({mode: 'LIVE', api_key: credentials.apiKey, secret_key: credentials.secretKey, confirmation: 'CANLI KASAYA KAYDET'})})
    setCredentials({apiKey: '', secretKey: '', accepted: false})
  }, 'LIVE credential şifreli kasaya kaydedildi; secret tarayıcıda tutulmuyor.')

  const connectReadOnly = () => run('connect', async () => {
    await call(CONNECTIONS, '/activate', {method: 'POST', body: JSON.stringify({mode: 'LIVE', confirmation: 'CANLI SALT OKUNUR BAĞLANTIYI AÇ'})})
  }, 'LIVE hesap salt-okunur bağlandı; gerçek emir kilidi kapalı kaldı.')

  const connectAndSave = () => run('connect-save', async () => {
    if (!credentials.apiKey || !credentials.secretKey) throw new Error('LIVE API Key ve Secret Key birlikte girilmelidir.')
    if (!credentials.accepted) throw new Error('Secret yalnızca şifreli sunucu kasasında tutulur onayını verin.')
    await call(CONNECTIONS, '/save', {method: 'POST', body: JSON.stringify({mode: 'LIVE', api_key: credentials.apiKey, secret_key: credentials.secretKey, confirmation: 'CANLI KASAYA KAYDET'})})
    await refresh()
    await call(CONNECTIONS, '/activate', {method: 'POST', body: JSON.stringify({mode: 'LIVE', confirmation: 'CANLI SALT OKUNUR BAĞLANTIYI AÇ'})})
    await refresh()
    setCredentials({apiKey: '', secretKey: '', accepted: false})
  }, 'Binance Connected; hesap salt-okunur bağlandı ve gerçek emir kilidi korunuyor.')

  const armLive = () => {
    if (!authorizationValid) {
      setNotice({kind: 'error', text: authorizationReasonText})
      return
    }
    if (!readinessReady) {
      setNotice({kind: 'error', text: 'LIVE ARM için tüm backend readiness gate’leri PASS olmalıdır.'})
      return
    }
    setConfirm({title: 'LIVE ARM onayı', message: 'REAL MONEY — LIVE ACCOUNT. Backend readiness gate’leri geçmeden kilit açılmaz. LIVE ARM AUTO-TRADE başlatmaz.', expected: 'ONAYLIYORUM', action: async () => {
      await call(V25, '/arm', {method: 'POST', body: JSON.stringify({confirmation: 'CANLI EMİR RİSKİNİ KABUL EDİYORUM'})})
      setArmPendingSync(true)
    }})
    setConfirmText('')
  }

  const grantConsent = () => {
    setConfirm({title: '24 saatlik LIVE risk izni', message: 'Bu izin yalnızca mevcut LIVE hesap fingerprint’i için 24 saat geçerlidir. Backend readiness tamamlanmadan LIVE kilidi açılmaz.', expected: 'CANLI İŞLEM RİSKİNİ 24 SAAT KABUL EDİYORUM', action: async () => {
      await call(V25, '/consent', {method: 'POST', body: JSON.stringify({confirmation: 'CANLI İŞLEM RİSKİNİ 24 SAAT KABUL EDİYORUM'})})
    }})
    setConfirmText('')
  }

  const revokeConsent = () => {
    setConfirm({title: 'LIVE consent kaldırma', message: '24 saatlik LIVE risk izni, ARM penceresi ve Auto Trade yetkisi kaldırılacak. Binance pozisyonlarına veya emirlerine dokunulmaz.', expected: '24 SAATLİK CONSENTİ KALDIR', action: async () => {
      await call(V25, '/consent/revoke', {method: 'POST', body: JSON.stringify({confirmation: '24 SAATLİK CONSENTİ KALDIR'})})
      setArmPendingSync(false)
    }})
    setConfirmText('')
  }

  const acknowledgePolicy = () => {
    setConfirm({title: 'LIVE risk limitleri onayı', message: 'Mevcut backend risk limitlerini ve policy digest’ini açıkça onaylayın. Limitler değişirse bu acknowledgement geçersiz olur.', expected: 'RİSK LİMİTLERİNİ ONAYLIYORUM', action: async () => {
      await call(V25, '/policy/acknowledge', {method: 'POST', body: JSON.stringify({confirmation: 'RİSK LİMİTLERİNİ ONAYLIYORUM'})})
    }})
    setConfirmText('')
  }

  const stopAutoTrade = () => run('auto-stop', () => call(V25, '/auto/stop', {method: 'POST'}).then(() => undefined), 'LIVE AUTO-TRADE kapatıldı; mevcut protection yönetimi backend’de devam eder.')
  const disarmLive = () => run('disarm', () => call(V25, '/disarm', {method: 'POST'}).then(() => { setArmPendingSync(false) }), 'LIVE disarmed; AUTO-TRADE kapalı.')
  const checkRecovery = () => run('recovery', async () => {
    await call(V25, '/recovery/check', {method: 'POST', body: JSON.stringify({confirmation: 'LIVE RECOVERY CHECK'})})
  }, 'LIVE hesabı uzlaştırıldı; yeni emir kilidi korunuyor ve yeniden arm edilebilir.')
  const autoToggle = () => {
    if (status?.live_auto_trade || status?.auto?.enabled) return stopAutoTrade()
    if (!autoTimeframeSupported) {
      setNotice({kind: 'error', text: autoTimeframeMessage})
      return
    }
    setConfirm({title: 'LIVE AUTO-TRADE onayı', message: 'REAL MONEY WILL BE USED. Otomatik işlemler yalnız mevcut V25 live safety chain üzerinden ilerler.', expected: 'CANLI OTOMATİK', action: async () => { await call(V25, '/auto/start', {method: 'POST', body: JSON.stringify({confirmation: 'CANLI OTOMATİK'})}) }})
    setConfirmText('')
  }

  const emergencyStop = () => {
    setConfirm({title: 'EMERGENCY STOP', message: 'Yeni LIVE submissions bloklanacak, AUTO-TRADE kapanacak ve recovery gerekecek. Mevcut protection yönetimi merkezi backend kurallarına bırakılır.', expected: 'CANLI ACİL DURDUR', action: async () => { await call(V25, '/emergency', {method: 'POST', body: JSON.stringify({confirmation: 'CANLI ACİL DURDUR', close_tracked_positions: true})}) }})
    setConfirmText('')
  }

  const clearEmergency = () => {
    setConfirm({title: 'Emergency Stop kaldırma', message: 'Bu işlem yalnızca backend recovery temizse emergency kilidini kaldırır. Gerçek emir kilidi açık kalır ve yeniden ARM gerekir.', expected: 'ACİL DURDURMAYI KALDIRIYORUM', action: async () => {
      await call(V25, '/emergency/clear', {method: 'POST', body: JSON.stringify({confirmation: 'ACİL DURDURMAYI KALDIRIYORUM'})})
    }})
    setConfirmText('')
  }

  const previewAdoption = async (adoptionSymbol: string) => {
    setBusy('adoption-preview')
    try {
      const result = await call<{
        would_create_plan?: boolean
        candidate_plan?: any
        confirm_token?: string | null
      }>(V25, '/adopt-external-position/preview', {
        method: 'POST',
        body: JSON.stringify({symbol: adoptionSymbol}),
      })
      if (result.would_create_plan !== true || !result.candidate_plan || !result.confirm_token) {
        setAdoptionCandidate(null)
        setNotice({kind: 'info', text: `${adoptionSymbol} için read-only plan oluşturulamadı.`})
        return
      }
      const candidate = {
        symbol: adoptionSymbol,
        candidatePlan: result.candidate_plan,
        confirmToken: result.confirm_token,
      }
      setAdoptionCandidate(candidate)
      setConfirm({
        title: 'Adopt & Manage',
        message: `${adoptionSymbol} pozisyonu V25 read-only plan olarak sahiplenilecek. Mevcut emirler değiştirilmeyecek, yalnızca izlenecek.`,
        simple: true,
        action: async () => {
          const executeResult = await call<{ok?: boolean; code?: string}>(V25, '/adopt-external-position', {
            method: 'POST',
            body: JSON.stringify({symbol: candidate.symbol, confirm_token: candidate.confirmToken}),
          })
          if (executeResult.ok === false && executeResult.code === 'ADOPTION_PERSISTENCE_UNCERTAIN') {
            throw new Error('Pozisyon sahiplenilmiş olabilir ancak kayıt belirsiz; reconciliation gerekiyor.')
          }
          setAdoptionCandidate(null)
          if (onRefreshStatus) await onRefreshStatus(true)
        },
      })
      setConfirmText('')
    } catch (error) {
      setAdoptionCandidate(null)
      setNotice({kind: 'error', text: error instanceof Error ? error.message : 'Pozisyon sahiplenme önizlemesi alınamadı.'})
    } finally {
      setBusy('')
    }
  }

  const fillAnalysis = () => {
    if (!analysis || !Number.isFinite(Number(analysis.entry))) {
      setNotice({kind: 'error', text: liveCopy.noSelectedCoin})
      return
    }
    setOrder(current => ({...current, direction: analysis.direction === 'SHORT' ? 'SHORT' : 'LONG', limit_price: text(analysis.entry).replace('—', ''), stop_loss: text(analysis.stop_loss).replace('—', ''), tp1: text(analysis.tp1).replace('—', ''), tp2: text(analysis.tp2).replace('—', ''), tp3: text(analysis.tp3).replace('—', '')}))
    setNotice({kind: 'ok', text: 'Seçili analiz yönü ve seviyeleri forma dolduruldu.'})
  }
  const reviewOrder = (nextOrder = order) => {
    const values = [nextOrder.margin_usdt, nextOrder.leverage, nextOrder.stop_loss, nextOrder.tp1, nextOrder.tp2, nextOrder.tp3]
    if (!nextOrder.symbol.endsWith('USDT') || values.some(value => !Number.isFinite(Number(value)) || Number(value) <= 0)) {
      setNotice({kind: 'error', text: 'Sembol, miktar, kaldıraç, Stop Loss ve tüm TP seviyeleri pozitif olmalıdır.'}); return
    }
    const leverage = Number(nextOrder.leverage)
    if (!Number.isInteger(leverage) || leverage < 1 || leverage > 50) {
      setNotice({kind: 'error', text: 'Kaldıraç 1x ile 50x arasında tam sayı olmalıdır.'}); return
    }
    if (nextOrder.order_type === 'LIMIT' && (!Number.isFinite(Number(nextOrder.limit_price)) || Number(nextOrder.limit_price) <= 0)) {
      setNotice({kind: 'error', text: 'Limit emir için pozitif bir limit fiyatı girin.'}); return
    }
    const referencePrice = nextOrder.order_type === 'LIMIT' ? Number(nextOrder.limit_price) : Number(analysis?.entry)
    const stop = Number(nextOrder.stop_loss)
    const targets = [Number(nextOrder.tp1), Number(nextOrder.tp2), Number(nextOrder.tp3)]
    if (!Number.isFinite(referencePrice) || referencePrice <= 0) {
      setNotice({kind: 'error', text: 'Stop Loss yönünü doğrulamak için giriş fiyatını analizden doldurun veya Limit seçin.'}); return
    }
    const validDirection = nextOrder.direction === 'LONG' ? stop < referencePrice && targets[0] > referencePrice : stop > referencePrice && targets[0] < referencePrice
    const orderedTargets = nextOrder.direction === 'LONG' ? targets[0] < targets[1] && targets[1] < targets[2] : targets[0] > targets[1] && targets[1] > targets[2]
    if (!validDirection) {
      setNotice({kind: 'error', text: 'Stop Loss, seçilen yöne göre giriş fiyatının doğru tarafında olmalıdır.'}); return
    }
    if (!orderedTargets) {
      setNotice({kind: 'error', text: 'TP seviyeleri seçilen yöne göre sıralı olmalıdır.'}); return
    }
    const availableBalance = Number(status?.account?.available_balance)
    if (Number.isFinite(availableBalance) && availableBalance > 0 && Number(nextOrder.margin_usdt) > availableBalance) {
      setNotice({kind: 'error', text: `Seçilen marjin ${Number(nextOrder.margin_usdt).toFixed(2)} USDT; kullanılabilir bakiye yalnızca ${availableBalance.toFixed(2)} USDT.`}); return
    }
    const payload = {...nextOrder, margin_usdt: Number(nextOrder.margin_usdt), leverage, limit_price: nextOrder.order_type === 'LIMIT' ? Number(nextOrder.limit_price) : null, stop_loss: Number(nextOrder.stop_loss), tp1: Number(nextOrder.tp1), tp2: Number(nextOrder.tp2), tp3: Number(nextOrder.tp3), intent_id: `ui-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`}
    void run('review', async () => {
      const result = await call<{message?: string; notional_usdt?: number; estimated_stop_loss_usdt?: number}>(V25, '/order/test', {method: 'POST', body: JSON.stringify(payload)})
      setReviewResult({message: result.message || 'Dry-run tamamlandı; gerçek emir gönderilmedi.', notional: result.notional_usdt, estimated_stop_loss_usdt: result.estimated_stop_loss_usdt})
    }, 'Dry-run tamamlandı; gerçek emir gönderilmedi.')
  }

  const confirmAction = () => {
    if (actionInFlight.current || !confirm || (!confirm.simple && confirmText.trim().toUpperCase() !== confirm.expected)) return
    const action = confirm.action
    setConfirm(null); setConfirmText('')
    void run('confirmed', action, 'LIVE backend safety zinciri işlemi tamamlandı.')
  }

  const stateRows = [
    ['LIVE LOCK', executionLocked ? 'LOCKED' : 'READY', !executionLocked],
    ['BINANCE CONNECTION', connected ? 'CONNECTED' : 'DISCONNECTED', connected],
    ['ACCOUNT STATUS', configured ? 'CONFIGURED' : 'MISSING', configured],
    ['RISK GATE', riskGate ? riskGate.passed ? 'PASS' : 'BLOCKED' : 'UNKNOWN', Boolean(riskGate?.passed)],
    ['PROTECTION', protectionState, protectionReady],
    ['AUTO TRADE', status?.live_auto_trade ? 'ON' : 'OFF', Boolean(status?.live_auto_trade)],
  ] as const

  const positions = status?.account?.positions || []
  const openOrders = status?.account?.open_orders || []
  const liveExposure = positions.reduce((sum, position) => sum + Math.abs(Number(position.quantity || 0) * Number(position.mark_price || 0)), 0)
  const positionLeverage = (position: Record<string, unknown>) => position.leverage === null || position.leverage === undefined ? '—' : `${text(position.leverage)}x`
  const terminalPlanStatuses = ['CLOSED', 'CANCELLED', 'KAPANDI', 'İPTAL']
  const findActivePlanForPosition = (position: Record<string, unknown>) => status?.plans?.find(plan => String(plan.symbol || '').toUpperCase() === String(position.symbol || '').toUpperCase() && !terminalPlanStatuses.includes(String(plan.status || '').toUpperCase()))
  const recentOrder = status?.events?.find(event => ['LIVE_ENTRY', 'LIVE_ENTRY_RECOVERED', 'LIVE_ORDER_UPDATE'].includes(String(event.kind)))
  const protectionLabelForPlan = (plan: Record<string, unknown> | undefined) => {
    const protectionStatus = String(plan?.protection_status || plan?.protection_state || 'UNKNOWN').toUpperCase()
    return protectionStatus === 'MATCHED' ? 'PROTECTED' : protectionStatus === 'MISSING' ? 'NOT PROTECTED' : 'OWNERSHIP UNKNOWN'
  }
  const protectionPriority = ['BLOCKED', 'OWNERSHIP UNKNOWN', 'NOT PROTECTED', 'EXTERNAL / NOT MANAGED', 'PROTECTED']
  const positionProtectionStates = positions.map(position => {
    const activePlanForPosition = findActivePlanForPosition(position)
    return activePlanForPosition ? protectionLabelForPlan(activePlanForPosition) : 'EXTERNAL / NOT MANAGED'
  })
  const currentProtection = recoveryRequired
    ? 'BLOCKED'
    : positions.length === 0
      ? protectionState
      : positionProtectionStates.reduce((worst, current) => protectionPriority.indexOf(current) < protectionPriority.indexOf(worst) ? current : worst, 'PROTECTED')
  const blocker = liveBlockerMessage(status, liveArmed)
  const autoStatusLabel = status === null ? 'UNKNOWN' : status.live_auto_trade ? executionLocked ? 'SCANNING ONLY' : 'RUNNING' : 'OFF'
  const liveState = status === null ? 'UNKNOWN' : emergency || recoveryRequired ? 'BLOCKED' : status.live_auto_trade ? executionLocked ? 'SCANNING ONLY' : 'RUNNING' : liveArmed ? 'ARMED' : readinessReady ? 'READY' : 'LOCKED'
  const activeConfirm = confirm
  const statusValue = (value: string) => value === 'READY' || value === 'CLEAR' || value === 'CONNECTED' || value === 'ON' || value === 'PROTECTED' ? 'READY' : value === 'UNKNOWN' ? 'UNKNOWN' : 'BLOCKED'
  const statusItems = [
    [liveCopy.status.liveTrading, status === null ? 'UNKNOWN' : statusValue(liveState === 'READY' || liveState === 'ARMED' ? 'READY' : liveState)],
    [liveCopy.status.autoTrade, status === null ? 'UNKNOWN' : statusValue(status.live_auto_trade ? 'ON' : 'BLOCKED')],
    [liveCopy.status.marketData, status === null ? 'UNKNOWN' : statusValue(marketDataConnected ? 'CONNECTED' : 'DISCONNECTED')],
    [liveCopy.status.liveAccount, status === null ? 'UNKNOWN' : statusValue(connected ? 'CONNECTED' : 'DISCONNECTED')],
    [liveCopy.status.risk, status === null ? 'UNKNOWN' : statusValue(riskState)],
    [liveCopy.status.exposure, status === null ? 'UNKNOWN' : statusValue(exposureState)],
    [liveCopy.status.protection, status === null ? 'UNKNOWN' : statusValue(currentProtection)],
    [liveCopy.status.recovery, status === null ? 'UNKNOWN' : statusValue(recoveryState)],
    [liveCopy.status.emergency, status === null ? 'UNKNOWN' : statusValue(emergencyState)],
  ] as const
  const closeCredentialModal = () => {
    setCredentialModalOpen(false)
    setShowSecret(false)
    setCredentials({apiKey: '', secretKey: '', accepted: false})
  }
  const confirmationModal = activeConfirm && typeof document !== 'undefined' ? createPortal(
    <div className="liveConfirmBackdrop"><section className="liveConfirm" role="dialog" aria-modal="true"><h2>{activeConfirm.title}</h2><p>{activeConfirm.message}</p>{!activeConfirm.simple && <label>TYPE TO CONFIRM<input autoFocus value={confirmText} onChange={event => setConfirmText(event.target.value)} onKeyDown={event => {if (event.key === 'Enter') confirmAction()}} placeholder={activeConfirm.expected}/></label>}<div><button type="button" onClick={() => {setConfirm(null);setConfirmText('')}}>{activeConfirm.simple ? 'HAYIR' : 'CANCEL'}</button><button type="button" disabled={!activeConfirm.simple && confirmText.trim().toUpperCase() !== activeConfirm.expected} onClick={confirmAction}>{activeConfirm.simple ? 'EVET' : 'CONFIRM'}</button></div></section></div>,
    document.body,
  ) : null
  const credentialModal = credentialModalOpen && typeof document !== 'undefined' ? createPortal(
    <div className="liveCredentialBackdrop" role="presentation" onClick={event => { if (event.target === event.currentTarget) closeCredentialModal() }}>
      <section className="liveCredentialModal" role="dialog" aria-modal="true" aria-labelledby="live-credentials-title">
        <header><div><small>{liveCopy.configureCredentials}</small><h2 id="live-credentials-title">{liveCopy.credentialsTitle}</h2></div><button type="button" onClick={closeCredentialModal} aria-label={liveCopy.close}>×</button></header>
        <p>{liveCopy.credentialsHint} Gerçek emir gönderilmez.</p>
        <label>{liveCopy.apiKey}<input data-private="true" type="password" autoComplete="new-password" value={credentials.apiKey} onChange={event => setCredentials({...credentials, apiKey: event.target.value})}/></label>
        <label>{liveCopy.secretKey}<input data-private="true" type={showSecret ? 'text' : 'password'} autoComplete="new-password" value={credentials.secretKey} onChange={event => setCredentials({...credentials, secretKey: event.target.value})}/></label>
        <label className="liveCredentialCheck"><input type="checkbox" checked={credentials.accepted} onChange={event => setCredentials({...credentials, accepted: event.target.checked})}/><span>Secret yalnızca şifreli sunucu kasasında tutulur.</span></label>
        <div className="liveCredentialActions"><button type="button" onClick={testConnection} disabled={Boolean(busy)}>{busy === 'test' ? 'TEST EDİLİYOR...' : liveCopy.testConnection}</button><button type="button" className="primary" onClick={connectAndSave} disabled={Boolean(busy)}>{busy === 'connect-save' ? 'BAĞLANIYOR...' : liveCopy.saveAndConnect}</button></div>
        <small className="liveCredentialNote">Test ve kaydetme bağlantı doğrulaması yapar; bu işlemler emir oluşturmaz.</small>
      </section>
    </div>,
    document.body,
  ) : null

  const safetyStatus = (value: string) => value === 'READY' || value === 'CLEAR' || value === 'PASS' ? 'ready' : value === 'UNKNOWN' ? 'unknown' : 'blocked'
  const configuredSymbols = Array.isArray(policy.allowed_symbols) ? policy.allowed_symbols as string[] : []
  const approvedSymbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT']
  const toggleSymbol = (value: string) => {
    const next = configuredSymbols.includes(value) ? configuredSymbols.filter(item => item !== value) : [...configuredSymbols, value]
    setPolicy('allowed_symbols', next)
  }
  const activity = (status?.events || []).slice(-8).reverse()
  const externalDate = (value: unknown) => {
    const timestamp = Number(value)
    return Number.isFinite(timestamp) && timestamp > 0 ? new Date(timestamp).toLocaleString('tr-TR', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : date(value)
  }
  useEffect(() => {
    if (!active || !status) return
    void call<ExternalHistory>(V25, '/history?limit=1000').then(setExternalHistory).catch(() => setExternalHistory(null))
  }, [active, status?.events?.length, status?.plans?.length])
  const ownershipUncertain = status?.events?.find(event => String(event.kind) === 'OWNERSHIP_UNCERTAIN')
  const latestSignal = activity.find(event => /SIGNAL|ENTRY|ORDER|SCAN|CANDIDATE/i.test(String(event.kind || '')))
  const signalStatus = status?.live_auto_trade ? autoStatusLabel : latestSignal ? text(latestSignal.kind).replaceAll('_', ' ') : 'WAITING'
  const scannerStatus = status?.scanner?.last_cycle_stage || (status?.scanner?.last_scan_at ? 'WAITING FOR NEXT SCAN' : 'WAITING')
  const candidateCount = status?.scanner?.selected_symbols_count ?? status?.scanner?.candidate_count ?? 0
  const automationReason = status?.auto?.last_error || status?.auto?.last_skip_reason || status?.scanner?.last_skip_reason || status?.auto?.last_decision || blocker
  const safetyRows = [
    ['Account', configured && connected ? 'PASS' : 'BLOCKED', configured && connected ? 'LIVE account verified' : 'Connect and verify LIVE API'],
    ['Risk', riskState === 'READY' ? 'PASS' : riskState, riskGate?.detail || 'Risk policy is backend-controlled'],
    ['Exposure', exposureState === 'READY' ? 'PASS' : exposureState, activePlanState === 'CONFLICT' ? 'Active plan conflict' : `Current exposure ${money(liveExposure)}`],
    ['Protection', protectionState === 'READY' ? 'PASS' : protectionState, protectionGate?.detail || 'Protection readiness is backend-controlled'],
    ['LIVE lock', manualOrderReady ? 'MANUAL READY' : 'BLOCKED', manualOrderReady ? 'Manual LIVE orders require the active ARM window and final EVET confirmation.' : blocker],
    ['Execution', status?.execution_state === 'UNKNOWN' || status?.reconciliation_required ? 'BLOCKED' : readinessReady ? 'PASS' : 'WARNING', status?.reconciliation_required ? 'Reconciliation required' : blocker],
  ] as const

  const renderManualOrderCard = () => (
    <div className="liveUxGrid liveUxOrderGrid">
      <section className="liveUxCard liveUxManual" data-locked={masterTrade && executionLocked || undefined}>
        <header><Send/><div><small>{liveCopy.manualOrder}</small><h3>Dry-run öncesi emir hazırlığı</h3></div><b>{executionLocked ? 'KİLİTLİ' : 'V25 ZİNCİRİ'}</b></header>
        <div className="liveUxForm">
          <label className="masterTradeOrderField">{liveCopy.symbol}<input value={order.symbol} onChange={event => setOrder({...order, symbol: event.target.value.toUpperCase()})}/></label>
          <div className="liveChoice masterTradeOrderField"><span>{liveCopy.side}</span><button type="button" aria-pressed={order.direction === 'LONG'} className={order.direction === 'LONG' ? 'selectedLong' : ''} onClick={() => setOrder({...order, direction: 'LONG'})}>{liveCopy.long}</button><button type="button" aria-pressed={order.direction === 'SHORT'} className={order.direction === 'SHORT' ? 'selectedShort' : ''} onClick={() => setOrder({...order, direction: 'SHORT'})}>{liveCopy.short}</button></div>
          <label className="masterTradeOrderField">{liveCopy.orderType}<select value={order.order_type} onChange={event => setOrder({...order, order_type: event.target.value as OrderDraft['order_type']})}><option>{liveCopy.market}</option><option>{liveCopy.limit}</option></select></label>
          <label className="masterTradeOrderField">{liveCopy.margin}<input type="number" min="5" value={order.margin_usdt} onChange={event => setOrder({...order, margin_usdt: event.target.value})}/></label>
          {order.order_type === 'LIMIT' && <label className="masterTradeOrderField">{liveCopy.limitPrice}<input type="number" value={order.limit_price} onChange={event => setOrder({...order, limit_price: event.target.value})}/></label>}
          <label>{liveCopy.stopLoss}<input type="number" value={order.stop_loss} onChange={event => setOrder({...order, stop_loss: event.target.value})}/></label>
          <label>{liveCopy.tp1}<input type="number" value={order.tp1} onChange={event => setOrder({...order, tp1: event.target.value})}/></label>
          <label>{liveCopy.tp2}<input type="number" value={order.tp2} onChange={event => setOrder({...order, tp2: event.target.value})}/></label>
          <label>{liveCopy.tp3}<input type="number" value={order.tp3} onChange={event => setOrder({...order, tp3: event.target.value})}/></label>
        </div>
        {tradingDefaults.preferences?.risk_per_trade && <div className="liveUxFormActions"><span>Kişisel risk tercihi: %{tradingDefaults.preferences.risk_per_trade}</span><button type="button" disabled={Boolean(busy) || !manualOrderReady} onClick={() => {
          const result = preferredRiskMargin({wallet: Number(status?.account?.wallet_balance), available: Number(status?.account?.available_balance), entry: order.order_type === 'LIMIT' ? Number(order.limit_price) : Number(analysis?.entry), stop: Number(order.stop_loss), leverage: Number(order.leverage), percent: tradingDefaults.preferences.risk_per_trade})
          setSizingNotice(result.reason)
          if (result.margin !== null) setOrder(current => ({...current, margin_usdt: String(result.margin)}))
        }}>Tercih edilen riskle marjı hesapla</button></div>}
        {(sizingNotice || tradingDefaults.error) && <p role="status">{sizingNotice || `İşlem tercihleri alınamadı: ${tradingDefaults.error}`}</p>}
        <div className="liveUxFormActions"><button type="button" onClick={fillAnalysis}><CircleDollarSign/> {liveCopy.fillAnalysis}</button><button type="button" className="primary" onClick={reviewOrder} disabled={Boolean(busy)}><Send/> {liveCopy.reviewOrder}</button></div>
        {reviewResult && <p className="liveUxReviewResult" role="status">{reviewResult.message} Gerçek emir gönderilmedi.</p>}
      </section>
      <section className="liveUxCard liveUxSummary">
        <header><ShieldCheck/><div><small>{liveCopy.orderSummary}</small><h3>{order.symbol || 'SEMBOL YOK'}</h3></div><b>LIVE · {executionLocked ? 'KİLİTLİ' : status?.armed ? 'ARMED' : 'ARM YOK'}</b></header>
        <div className="liveUxSummaryLead"><strong>{order.direction === 'LONG' ? liveCopy.long : liveCopy.short}</strong><span>{order.order_type}</span></div>
        <div className="liveUxMetrics">
          <span><small>{liveCopy.margin}</small><b>{text(order.margin_usdt)} USDT</b></span>
          <span><small>{liveCopy.stopLoss}</small><b>{text(order.stop_loss)}</b></span>
          <span><small>{liveCopy.tp1}</small><b>{text(order.tp1)}</b></span>
          <span><small>{liveCopy.tp2}</small><b>{text(order.tp2)}</b></span>
          <span><small>{liveCopy.tp3}</small><b>{text(order.tp3)}</b></span>
          <span><small>RİSK</small><b>{riskState}</b></span>
          <span><small>MARUZİYET</small><b>{exposureState}</b></span>
          <span><small>KORUMA</small><b>{protectionState}</b></span>
        </div>
      </section>
    </div>
  )

  if (!active) return null
  if (masterTrade) return <MasterTradeLiveLayout tab={masterTradeTab} step={status?.live_auto_trade ? 3 : !configured || !connected ? 0 : !readinessReady ? 1 : !liveArmed ? 2 : 3} notice={notice.kind !== 'info' ? <div className={`liveNotice masterTradeLiveNotice ${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}>{notice.kind === 'error' ? <TriangleAlert/> : <CheckCircle2/>}<span>{notice.text}</span></div> : undefined}><section id="master-trade-live-terminal" className={`masterTradeLiveUx ${advancedOpen ? 'advanced-open' : 'operational'}`} aria-label="LIVE Auto Trade workspace">
    <header className={`masterTradeLiveHeader ${liveState.toLowerCase()}`}>
      <div className="masterTradeLiveTitle"><span className="masterTradeLiveKicker">LIVE OPERATIONS / REAL MONEY</span><h2>LIVE AUTO TRADE</h2><p>Binance Futures Mainnet · V25 backend control</p></div>
      <div className="masterTradeLiveHeadline"><span className="liveStateDot" /><strong>{liveState}</strong><small>{status?.live_auto_trade ? executionLocked ? 'SCANNER ACTIVE · LIVE ORDERS LOCKED' : 'AUTO TRADE IS RUNNING' : 'AUTO TRADE IS OFF'}</small></div>
      <div className="masterTradeLiveChips"><span>Market Data <b>{marketDataConnected ? 'CONNECTED' : status === null ? 'UNKNOWN' : 'DISCONNECTED'}</b></span><span>Live Account <b>{connected ? 'CONNECTED' : 'DISCONNECTED'}</b></span><span>Risk <b>{riskState}</b></span><span>Exposure <b>{exposureState}</b></span><span>Protection <b>{protectionState}</b></span></div>
    </header>

    <div className="masterTradeLiveAssistant">
      <div className="masterTradeLiveAssistantCopy"><Power aria-hidden="true"/><div><span className="masterTradeLiveKicker">NEXT BEST ACTION</span><strong>{status?.live_auto_trade ? 'Auto Trade is actively monitoring approved markets.' : liveState === 'BLOCKED' ? 'LIVE is blocked until the backend recovery state is clear.' : !configured || !connected ? 'Connect and verify your LIVE API to continue.' : !readinessReady ? 'Complete the required safety checks before enabling Auto Trade.' : liveArmed ? 'LIVE is armed. Start supervised Auto Trade to continue.' : 'Everything is ready. Arm LIVE Auto Trade to continue.'}</strong><small>{status?.live_auto_trade ? 'STOP remains available at any time.' : blocker}</small></div></div>
      {timeframeWarning}
      {status?.reconciliation_diagnostic && <div className="masterTradeLiveDiagnostic" role="alert"><span className="masterTradeLiveKicker">RECONCILIATION ERROR</span><small>Stage: {text(status.reconciliation_diagnostic.reconciliation_stage)}</small><small>Error: {text(status.reconciliation_diagnostic.exception_message)}</small><small>Source: {text(status.reconciliation_diagnostic.source)}:{text(status.reconciliation_diagnostic.line)}</small></div>}
      <div className="masterTradeLiveAssistantActions"><button type="button" className="masterTradeLivePrimary" onClick={status?.live_auto_trade ? stopAutoTrade : autoToggle} disabled={Boolean(busy) || (!status?.live_auto_trade && !autoReady)}>{!status?.live_auto_trade && !autoReady ? <LockKeyhole aria-hidden="true"/> : <Power aria-hidden="true"/>}{status?.live_auto_trade ? ' STOP AUTO TRADE' : ' START LIVE AUTO TRADE'}</button><button type="button" className="masterTradeLiveTextButton" onClick={() => setAdvancedOpen(true)}>VIEW REQUIREMENTS</button></div>
    </div>

    <div className="masterTradeLiveFlow" aria-label="LIVE Auto Trade progression"><span className={readinessReady ? 'complete' : 'current'}>1 <b>{readinessReady ? 'READY' : 'LOCKED'}</b></span><i /><span className={!liveArmed && readinessReady ? 'current' : liveArmed ? 'complete' : ''}>2 <b>ARM REQUIRED</b></span><i /><span className={liveArmed && !status?.live_auto_trade ? 'current' : status?.live_auto_trade ? 'complete' : ''}>3 <b>ARMED</b></span><i /><span className={status?.live_auto_trade ? 'current running' : ''}>4 <b>RUNNING</b></span></div>

    <section className="masterTradeLiveSection masterTradeLiveConfirmations" aria-label="LIVE Safety Confirmations">
      <header><div><span className="masterTradeLiveKicker">LIVE SAFETY CONFIRMATIONS</span><h3>Complete the release prerequisites</h3></div><strong className={readinessReady ? 'ready' : 'blocked'}>{readinessReady ? 'READY' : 'REQUIRED'}</strong></header>
      <div className="masterTradeLiveGrid masterTradeLiveConfirmationGrid">
        <article><div><span className="masterTradeLiveKicker">24 HOUR CONSENT</span><h4>{authorizationValid ? 'Risk consent active' : authorizationReasonText}</h4><small>{authorizationValid ? `Valid until ${date(status?.authorization?.expires_at || status?.consent?.expires_at)}` : authorizationReasonText}</small></div><strong className={authorizationValid ? 'ready' : 'blocked'}>{authorizationValid ? 'PASS' : 'PENDING'}</strong><button type="button" className="masterTradeLiveSecondary" onClick={grantConsent} disabled={Boolean(busy) || authorizationValid}>{authorizationValid ? '24 HOUR CONSENT ACTIVE' : '24 SAAT İZİN VER'}</button>{authorizationValid && <button type="button" className="masterTradeLiveSecondary masterTradeLiveRevokeButton" onClick={revokeConsent} disabled={Boolean(busy)}>REVOKE CONSENT</button>}</article>
        <article><div><span className="masterTradeLiveKicker">POLICY ACKNOWLEDGEMENT</span><h4>{status?.policy_acknowledged ? 'Risk policy acknowledged' : 'Risk limitleri için politika onayı gerekli.'}</h4><small>{status?.policy_acknowledged ? 'Current policy is covered by the active 24-hour consent.' : 'Give 24-hour LIVE consent to acknowledge the current policy.'}</small></div><strong className={status?.policy_acknowledged ? 'ready' : 'blocked'}>{status?.policy_acknowledged ? 'PASS' : 'PENDING'}</strong><label className="masterTradeLivePolicyCheck"><input type="checkbox" aria-label="Acknowledge current risk policy" checked={status?.policy_acknowledged === true} onChange={event => { if (event.target.checked) acknowledgePolicy() }} disabled={Boolean(busy) || status?.policy_acknowledged === true}/><span>{status?.policy_acknowledged ? 'POLICY ACKNOWLEDGED' : 'I acknowledge the current risk limits'}</span></label><button type="button" className="masterTradeLiveSecondary" onClick={acknowledgePolicy} disabled={Boolean(busy) || status?.policy_acknowledged === true}>{status?.policy_acknowledged ? 'POLICY ACKNOWLEDGED' : 'LİMİTLERİ ONAYLA'}</button></article>
      </div>
      <p className="masterTradeLiveOperationalNote">Consent ve policy acknowledgement arka planda kontrol edilir. Manuel LIVE ORDER için ayrıca ARM LIVE gerekmez; ARM LIVE yalnızca Auto Trade içindir.</p>
    </section>

    <section className="masterTradeLiveAccountHud" aria-label="LIVE Account Overview">
      <header><div><span className="masterTradeLiveKicker">LIVE ACCOUNT OVERVIEW</span><h3>Binance Futures account</h3></div><strong className={connected ? 'ready' : 'blocked'}><i />{connected ? 'LIVE CONNECTED' : 'LIVE DISCONNECTED'}</strong></header>
      <div className="masterTradeLiveAccountMetrics">
        <span><small>WALLET BALANCE</small><b>{money(status?.account?.wallet_balance)}</b><em>USDT</em></span>
        <span><small>AVAILABLE BALANCE</small><b>{money(status?.account?.available_balance)}</b><em>USDT</em></span>
        <span><small>MARGIN USED</small><b>--</b><em>Unavailable from V25 snapshot</em></span>
        <span><small>UNREALIZED PNL</small><b className={Number(status?.account?.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{money(status?.account?.unrealized_pnl)}</b><em>Open positions</em></span>
        <span><small>REALIZED PNL</small><b>--</b><em>Unavailable from V25 snapshot</em></span>
        <span><small>MARGIN RATIO</small><b>--</b><em>Unavailable from V25 snapshot</em></span>
        <span><small>TOTAL EXPOSURE</small><b>{money(liveExposure)}</b><em>Position notional</em></span>
        <span><small>OPEN POSITIONS</small><b>{positions.length}</b><em>Live snapshot</em></span>
        <span><small>OPEN ORDERS</small><b>{openOrders.length}</b><em>Live snapshot</em></span>
        <span><small>ACTIVE LEVERAGE</small><b>{positions.length ? positions.map(positionLeverage).join(' / ') : '--'}</b><em>Per open position</em></span>
      </div>
    </section>

    {renderManualOrderCard()}

    <section className="masterTradeLiveSection masterTradeLiveOperations" aria-label="LIVE Auto Trade operations"><header><div><span className="masterTradeLiveKicker">AUTO TRADE OPERATIONS</span><h3>Scanner and decision state</h3></div><strong className={status?.live_auto_trade ? 'ready' : 'blocked'}>{status?.live_auto_trade ? 'ACTIVE' : 'OFF'}</strong></header><div className="masterTradeLiveControlMeta"><span><small>SCANNER</small><b>{scannerStatus}</b></span><span><small>SCANNED</small><b>{status?.scanner?.scanned_symbol_count ?? 0}</b></span><span><small>CANDIDATES</small><b>{candidateCount}</b></span><span><small>LAST SCAN</small><b>{date(status?.scanner?.last_scan_at || status?.auto?.last_scan)}</b></span></div><p className="masterTradeLiveOperationalNote"><span className="masterTradeLiveWarningChip">{automationReason}</span></p></section>

    <div className="masterTradeLiveGrid masterTradeLivePrimaryGrid">
      <section className="masterTradeLiveSection masterTradeLiveControl"><header><div><span className="masterTradeLiveKicker">AUTO TRADE CONTROL</span><h3>{status?.live_auto_trade ? 'AUTO TRADE' : 'AUTO TRADE OFF'}</h3></div><strong className={status?.live_auto_trade ? 'ready' : 'blocked'}>{status?.live_auto_trade ? 'RUNNING' : 'OFF'}</strong></header><div className="masterTradeLiveControlBody"><div className="masterTradeLiveControlMain"><div><b>{status?.live_auto_trade ? 'Monitoring approved markets' : 'Ready when backend gates pass'}</b><small>{status?.auto?.last_decision || 'No automatic entries are enabled by default.'}</small></div><button type="button" className={status?.live_auto_trade ? 'dangerAction' : 'primaryAction'} onClick={status?.live_auto_trade ? stopAutoTrade : autoToggle} disabled={Boolean(busy) || (!status?.live_auto_trade && !autoReady)}>{status?.live_auto_trade ? 'STOP AUTO TRADE' : 'START LIVE AUTO TRADE'}</button></div></div><div className="masterTradeLiveControlMeta"><span>LIVE ARM <b>{armState}</b></span><span>LIVE LOCK <b>{executionLocked ? 'LOCKED' : 'READY'}</b></span><span>BLOCKER <b>{status?.live_auto_trade ? 'NONE' : blocker}</b></span></div><button type="button" className="masterTradeLiveSecondary" onClick={liveArmed ? () => run('disarm', () => call(V25, '/disarm', {method: 'POST'}).then(() => undefined), 'LIVE disarmed; AUTO-TRADE kapalı.') : armLive} disabled={Boolean(busy) || (!liveArmed && (!configured || !connected || emergency || recoveryRequired || !readinessReady))}>{liveArmed ? 'DISARM LIVE' : 'LIVE ARM'}</button></section>

      <aside className="masterTradeLiveActivityRail" aria-label="Live Auto Trade activity"><div className="masterTradeLiveActivityRailHeader"><span className="masterTradeLiveKicker">LIVE ACTIVITY</span><b>{activity.length ? `${activity.length} EVENTS` : 'WAITING'}</b></div><div className="masterTradeLiveActivityRailList">{activity.length ? activity.slice(0, 6).map((event, index) => <div className="masterTradeLiveActivityRailItem" key={`${event.created_at}-${event.kind}-${index}`}><time>{activityTime(event.created_at)}</time><span>{text(event.message || event.kind).replaceAll('_', ' ')}</span></div>) : <p>No recent events.</p>}</div></aside>

      <section className="masterTradeLiveSection masterTradeLiveSetup">
        <header><div><span className="masterTradeLiveKicker">AUTO TRADE SETUP</span><h3>Core strategy settings</h3></div><button type="button" className="masterTradeLiveTextButton" onClick={() => setAdvancedOpen(value => !value)}>{advancedOpen ? 'HIDE ADVANCED' : 'ADVANCED SETTINGS'} <span>{advancedOpen ? '⌃' : '⌄'}</span></button></header>
        <div className="masterTradeLiveSetupGrid">
          <label>Strategy<strong>V25 SUPERVISED</strong></label>
          <label>Timeframe<select value={selectedAutoTimeframe} onChange={event => setPolicy('interval', event.target.value)}><option>1m</option><option>5m</option><option>15m</option><option>1h</option><option>4h</option></select></label>
          <label>Risk / trade<div className="masterTradeLiveUnitField"><input type="number" min="0.5" max="25" step="0.1" value={numericPolicy('max_loss_per_trade', 1)} onChange={event => setPolicy('max_loss_per_trade', Number(event.target.value))}/><span>USDT</span></div></label>
          <label>Max positions<input type="number" min="1" max="5" value={numericPolicy('max_positions', 2)} onChange={event => setPolicy('max_positions', Number(event.target.value))}/></label>
          <label>Max exposure<div className="masterTradeLiveUnitField"><input type="number" min="25" max="350" value={numericPolicy('max_total_exposure_usdt', 350)} onChange={event => setPolicy('max_total_exposure_usdt', Number(event.target.value))}/><span>USDT</span></div></label>
        </div>
        <div className="masterTradeLiveMarkets"><span>MARKETS</span><div className="masterTradeLiveMarketsScroll">{approvedSymbols.map(item => <button type="button" key={item} aria-pressed={configuredSymbols.includes(item)} className={configuredSymbols.includes(item) ? 'selected' : ''} onClick={() => toggleSymbol(item)}>{item.replace('USDT', '')}</button>)}</div><small>{configuredSymbols.length ? `${configuredSymbols.length} approved` : 'Backend default universe'}</small></div>
        {advancedOpen && <div className="masterTradeLiveAdvanced">
          <label>Minimum confidence<div className="masterTradeLiveUnitField"><input type="number" min="70" max="95" value={numericPolicy('min_confidence', 85)} onChange={event => setPolicy('min_confidence', Number(event.target.value))}/><span>%</span></div></label>
          <label>Max leverage<input type="number" min="1" max="50" value={numericPolicy('max_leverage', 2)} onChange={event => setPolicy('max_leverage', Number(event.target.value))}/></label>
          <label>Liquidation buffer<div className="masterTradeLiveUnitField"><input type="number" min="0.05" max="5" step="0.05" value={numericPolicy('liquidation_buffer_pct', 0.5)} onChange={event => setPolicy('liquidation_buffer_pct', Number(event.target.value))}/><span>% entry</span></div></label>
          <label>Daily loss limit<div className="masterTradeLiveUnitField"><input type="number" min="5" max="100" value={numericPolicy('daily_loss_limit', 20)} onChange={event => setPolicy('daily_loss_limit', Number(event.target.value))}/><span>USDT</span></div></label>
          <label>Consecutive losses<input type="number" min="1" max="10" value={numericPolicy('consecutive_loss_limit', 3)} onChange={event => setPolicy('consecutive_loss_limit', Number(event.target.value))}/></label>
          <label>Cooldown / scan seconds<input type="number" min="30" max="300" value={numericPolicy('scan_seconds', 60)} onChange={event => setPolicy('scan_seconds', Number(event.target.value))}/></label>
          <div className="masterTradeLiveSide"><span>POSITION SIDE</span><button type="button" className={policy.allow_long !== false ? 'selected' : ''} onClick={() => setPolicy('allow_long', policy.allow_long === false)}>LONG</button><button type="button" className={policy.allow_short !== false ? 'selected' : ''} onClick={() => setPolicy('allow_short', policy.allow_short === false)}>SHORT</button></div>
        </div>}
        <div className="masterTradeLiveSetupFooter"><small>Settings are validated by the backend before execution. Effective trap limit: {Math.min(35, numericPolicy('max_trap_score', 35))} (min of Original 35 and policy).</small><button type="button" className="masterTradeLivePrimary" onClick={updatePolicy} disabled={Boolean(busy)}>SAVE SETUP</button></div>
      </section>
    </div>

    <div className="masterTradeLiveGrid masterTradeLiveDataGrid"><section className="masterTradeLiveSection masterTradeLiveAccountTable"><header><div><span className="masterTradeLiveKicker">LIVE POSITIONS</span><h3>Open positions</h3></div><span className="masterTradeLiveCount">{positions.length}</span></header>{positions.length ? <div className="masterTradeLiveTableWrap"><table><thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Mark</th><th>Leverage</th><th>Unrealized PnL</th><th>Liquidation</th><th>Margin</th><th>Management</th></tr></thead><tbody>{positions.map((position, index) => { const activePlanForPosition = status?.plans?.find(plan => String(plan.symbol || '').toUpperCase() === String(position.symbol || '').toUpperCase() && !['CLOSED', 'CANCELLED', 'KAPANDI', 'İPTAL'].includes(String(plan.status || '').toUpperCase())); const isExternal = !recoveryRequired && !activePlanForPosition; return <tr key={`${String(position.symbol)}-${index}`}><td><strong>{text(position.symbol)}</strong></td><td><span className={String(position.direction).toUpperCase() === 'SHORT' ? 'short' : 'long'}>{text(position.direction)}</span></td><td>{text(position.quantity)}</td><td>{text(position.entry_price)}</td><td>{text(position.mark_price)}</td><td>{positionLeverage(position)}</td><td className={Number(position.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{money(position.unrealized_pnl)}</td><td>{text(position.liquidation_price)}</td><td>--</td><td>{isExternal && <button type="button" className="liveArmButton" onClick={() => void previewAdoption(String(position.symbol))} disabled={Boolean(busy) || !connected || recoveryRequired}>ADOPT &amp; MANAGE</button>}</td></tr> })}</tbody></table></div> : <div className="masterTradeLiveEmpty"><strong>No open positions</strong><span>Live position data is read from the V25 account snapshot.</span></div>}</section>

      <section className="masterTradeLiveSection masterTradeLiveAccountTable"><header><div><span className="masterTradeLiveKicker">OPEN ORDERS</span><h3>Pending orders</h3></div><span className="masterTradeLiveCount">{openOrders.length}</span></header>{openOrders.length ? <div className="masterTradeLiveTableWrap"><table><thead><tr><th>Symbol</th><th>Side</th><th>Type</th><th>Price</th><th>Quantity</th><th>Status</th><th>Time</th></tr></thead><tbody>{openOrders.map((order, index) => <tr key={`${String(order.order_id)}-${index}`}><td><strong>{text(order.symbol)}</strong></td><td>{text(order.side)}</td><td>{text(order.type)}</td><td>{text(order.price)}</td><td>{text(order.quantity)}</td><td>{text(order.status)}</td><td>--</td></tr>)}</tbody></table></div> : <div className="masterTradeLiveEmpty"><strong>No open orders</strong><span>Pending order data is read from the V25 account snapshot.</span></div>}</section>

      <section className="masterTradeLiveSection masterTradeLiveSignal"><header><div><span className="masterTradeLiveKicker">LATEST SIGNAL</span><h3>{analysis?.direction || 'WAITING FOR SIGNAL'}</h3></div><strong>{signalStatus}</strong></header><div className="masterTradeLiveSignalSymbol"><b>{symbol}</b><span>{analysis?.direction || '—'}</span></div><div className="masterTradeLiveSignalGrid"><span><small>CONFIDENCE</small><b>{analysis?.confidence !== undefined ? `${analysis.confidence}%` : '—'}</b></span><span><small>RISK</small><b>{numericPolicy('max_loss_per_trade', 1)} USDT</b></span><span><small>ENTRY</small><b title={text(analysis?.entry)}>{signalPrice(analysis?.entry)}</b></span><span><small>STOP</small><b title={text(analysis?.stop_loss)}>{signalPrice(analysis?.stop_loss)}</b></span><span><small>TARGET</small><b title={text(analysis?.tp1)}>{signalPrice(analysis?.tp1)}</b></span><span><small>EXPOSURE</small><b>{money(exposure)}</b></span></div><p>{latestSignal ? `${text(latestSignal.message)} · ${date(latestSignal.created_at)}` : 'No recent signal event is available from the backend.'}</p></section></div>

    <section className="masterTradeLiveSection masterTradeLiveAccountTable" aria-label="External Binance trade history"><header><div><span className="masterTradeLiveKicker">EXTERNAL / NOT MANAGED BY V25</span><h3>Plan-dışı Binance geçmişi</h3></div><span className="masterTradeLiveCount">{externalHistory?.external_trades?.length ?? 0}</span></header><p className="masterTradeLiveOperationalNote">Read-only userTrades and REALIZED_PNL income records without a matching V25 plan.</p>{externalHistory?.external_trades?.length ? <div className="masterTradeLiveTableWrap"><table><thead><tr><th>Symbol</th><th>Side</th><th>Price</th><th>Quantity</th><th>Realized PnL</th><th>Time</th></tr></thead><tbody>{externalHistory.external_trades.slice(0, 30).map((trade, index) => <tr key={`${String(trade.trade_id || trade.order_id)}-${index}`}><td><strong>{text(trade.symbol)}</strong></td><td>{text(trade.side)}</td><td>{text(trade.price)}</td><td>{text(trade.quantity)}</td><td className={Number(trade.realized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{money(Number(trade.realized_pnl || 0))}</td><td>{externalDate(trade.time)}</td></tr>)}</tbody></table></div> : <div className="masterTradeLiveEmpty"><strong>No unmatched Binance trades found</strong><span>Only verified V25 plans appear in managed history; this section is read-only fallback evidence.</span></div>}</section>

    <div className="masterTradeLiveGrid masterTradeLiveLowerGrid"><section className="masterTradeLiveSection masterTradeLiveActivity"><header><div><span className="masterTradeLiveKicker">ACTIVITY</span><h3>Latest events</h3></div><button type="button" className="masterTradeLiveTextButton" onClick={() => setAdvancedOpen(true)}>VIEW ALL</button></header>{activity.length ? <ol>{activity.map((event, index) => <li key={`${event.created_at}-${event.kind}-${index}`}><i /><time>{date(event.created_at)}</time><span>{text(event.message || event.kind).replaceAll('_', ' ')}</span></li>)}</ol> : <div className="masterTradeLiveEmpty"><span>No recent LIVE activity.</span></div>}</section>

      <section className="masterTradeLiveSection masterTradeLiveSafety"><header><div><span className="masterTradeLiveKicker">SAFETY</span><h3>Readiness overview</h3></div><button type="button" className="masterTradeLiveTextButton" onClick={() => setAdvancedOpen(value => !value)}>ADVANCED DETAILS</button></header><div className="masterTradeLiveSafetyList">{safetyRows.map(([label, value, detail]) => <details key={label} className={safetyStatus(value)}><summary><span><i />{label}</span><b>{value}</b></summary><p>{detail}</p></details>)}</div></section></div>

    <section className="masterTradeLiveApiDrawer masterTradeLiveConnectionCard">
      <div className="masterTradeLiveConnectionHeader">
        <div className="masterTradeLiveConnectionHeading">
          <span className="masterTradeLiveKicker">EXCHANGE CONNECTION</span>
          <div className="masterTradeLiveExchangeIdentity">
            <span className="masterTradeLiveExchangeLogo">B</span>
            <div><h3>Connect your trading account</h3><small>Binance · USDT-M Futures</small></div>
          </div>
          <p>Securely connect your trading account. Credentials are encrypted and never displayed.</p>
        </div>
        <strong className={`masterTradeLiveConnectionStatus ${connected ? 'masterTradeLiveConnected' : ''}`}><i className={connected ? 'connected' : ''} /> {connected ? 'CONNECTED' : 'NOT CONNECTED'}</strong>
      </div>

      <div className="masterTradeLiveCredentialBlock">
        <div className="masterTradeLiveCredentialLabel"><span className="masterTradeLiveKicker">CREDENTIALS</span><small>Use a key with Futures permissions only.</small></div>
        <div className="masterTradeLiveApiGrid">
          <label>API KEY<input data-private="true" type="password" autoComplete="new-password" value={credentials.apiKey} onChange={event => setCredentials({...credentials, apiKey: event.target.value})} placeholder="Paste API key" /></label>
          <label>SECRET KEY<div className="masterTradeLiveSecretField"><input data-private="true" type={showSecret ? 'text' : 'password'} autoComplete="new-password" value={credentials.secretKey} onChange={event => setCredentials({...credentials, secretKey: event.target.value})} placeholder="Paste secret key" /><button type="button" aria-label={showSecret ? 'Hide secret key' : 'Show secret key'} onClick={() => setShowSecret(value => !value)}>{showSecret ? <EyeOff/> : <Eye/>}</button></div></label>
        </div>
        <label className="masterTradeLiveCheck"><input type="checkbox" checked={credentials.accepted} onChange={event => setCredentials({...credentials, accepted: event.target.checked})}/><span>I understand that API credentials are stored securely.</span></label>
      </div>

      <div className="masterTradeLiveConnectionActionBar">
        <div className="masterTradeLiveActionMeta"><span>ENVIRONMENT</span><b>LIVE</b><small>Binance Futures Mainnet</small></div>
        <div className="masterTradeLiveActionMeta"><span>PERMISSIONS</span><b>READ · TRADE</b><small>Withdrawals unsupported</small></div>
        <div className="masterTradeLiveApiActions"><button type="button" onClick={testConnection} disabled={Boolean(busy) || !connections?.vault?.ready}>{busy === 'test' ? <RefreshCw className="spin"/> : <ShieldCheck/>} TEST CONNECTION</button><button type="button" className="masterTradeLiveSaveButton" onClick={connectAndSave} disabled={Boolean(busy)}>{busy === 'connect-save' ? <RefreshCw className="spin"/> : <LockKeyhole/>} CONNECT ACCOUNT</button></div>
      </div>

      <div className="masterTradeLiveSecurityStrip" aria-label="Connection security status"><span><CheckCircle2/> Credentials encrypted</span><span><CheckCircle2/> Server-side validation</span><span><CheckCircle2/> API secret never displayed</span></div>

      <div className="masterTradeLiveApiMeta"><span>CONNECTION STATUS <b>{connected ? 'CONNECTED' : 'NOT CONNECTED'}</b></span><span>LAST VERIFIED <b>{date(testResult?.testedAt || status?.connection?.last_checked)}</b></span><span>VAULT FINGERPRINT <b>{text(testResult?.fingerprint || liveConnection?.fingerprint || status?.credentials?.fingerprint)}</b></span><span>ACCOUNT MODE <b>READ-ONLY</b></span></div>
      <small className="masterTradeLiveApiHint">Test verifies access only. Connect Account stores the encrypted credentials, activates the read-only account connection, and does not arm or place orders.</small>
    </section>
    {confirmationModal}
  </section></MasterTradeLiveLayout>
  return <section className="liveTradingPanel liveTerminal liveUx" aria-label={liveCopy.ariaLabel}>
    <header className="liveUxHero"><div><span className="liveKicker">{liveCopy.eyebrow}</span><h2>{liveCopy.title}</h2><p>{liveCopy.description}</p></div><div className={`liveUxState ${liveState.toLowerCase()}`}><ShieldAlert/><strong>{liveState === 'READY' ? liveCopy.ready : liveState === 'UNKNOWN' ? liveCopy.unknown : liveCopy.blocked}</strong><small>AUTO TRADE · {status?.live_auto_trade ? 'AÇIK' : 'KAPALI'}</small></div></header>
    {timeframeWarning}
    <div className="liveUxStatus" aria-label="Canlı durum şeridi">{statusItems.map(([label, value]) => <div key={label} className={`liveStatusItem ${value.toLowerCase()}`}><i/><span>{label}</span><b>{value}</b></div>)}</div>
    <div className={`liveUxNow ${liveState.toLowerCase()}`}><div><small>{liveCopy.now}</small><strong>{ownershipUncertain ? 'SAHİPLİK BELİRSİZ' : liveState === 'BLOCKED' ? 'LIVE ENGELLENDİ' : liveState === 'READY' ? 'LIVE HAZIR' : 'LIVE KİLİTLİ'}</strong><p>{ownershipUncertain ? `${text(ownershipUncertain.symbol)} sahiplik kontrolü başarısız: ${ownershipUncertain.failures?.join(', ') || 'bilinmiyor'}.` : blocker}</p></div><div className="liveUxNowActions"><button type="button" className="liveCredentialCta" onClick={() => setCredentialModalOpen(true)}><KeyRound/> {liveCopy.configureCredentials}</button><button type="button" onClick={() => void refresh(false)} disabled={Boolean(busy) || refreshPending}>{refreshPending ? <RefreshCw className="spin"/> : <RefreshCw/>} {liveCopy.refreshStatus}</button></div></div>

    <div className="liveUxGrid liveUxTopGrid"><div className="liveUxPositionStack">{positions.length ? positions.map((position, index) => { const activePlanForPosition = findActivePlanForPosition(position); const protectionLabel = activePlanForPosition ? protectionLabelForPlan(activePlanForPosition) : 'EXTERNAL / NOT MANAGED'; const isExternal = !recoveryRequired && !activePlanForPosition; return <section className="liveUxCard liveUxPosition" data-direction={text(position.direction).toUpperCase()} key={`${String(position.symbol)}-${index}`}><header><CircleDollarSign/><div><small>CURRENT POSITION</small><h3>{text(position.symbol)}</h3></div><b>OPEN</b></header><div className="liveUxPositionSide"><strong>{text(position.direction)}</strong><span>Position Status · OPEN</span></div><div className="liveUxMetrics"><span><small>ENTRY</small><b>{text(position.entry_price)}</b></span><span><small>MARK PRICE</small><b>{text(position.mark_price)}</b></span><span><small>QUANTITY</small><b>{text(position.quantity)}</b></span><span><small>UNREALIZED PNL</small><b>{money(position.unrealized_pnl)}</b></span><span><small>STOP LOSS</small><b>{text(activePlanForPosition?.stop_loss)}</b></span><span><small>TAKE PROFIT 1</small><b>{text(activePlanForPosition?.targets?.[0])}</b></span><span><small>TAKE PROFIT 2</small><b>{text(activePlanForPosition?.targets?.[1])}</b></span><span><small>TAKE PROFIT 3</small><b>{text(activePlanForPosition?.targets?.[2])}</b></span></div><div className={`liveUxProtectionBadge ${protectionLabel.toLowerCase().replaceAll(' ', '-')}`}>PROTECTION · {protectionLabel}</div>{isExternal && <button type="button" className="liveArmButton" onClick={() => void previewAdoption(String(position.symbol))} disabled={Boolean(busy) || !connected || recoveryRequired}>ADOPT &amp; MANAGE</button>}</section> }) : <section className="liveUxCard liveUxPosition"><header><CircleDollarSign/><div><small>CURRENT POSITION</small><h3>NO OPEN POSITION</h3></div><b>NO ACTIVE POSITION</b></header><p className="liveUxEmpty">No active LIVE position. Position data is read from the backend account snapshot.</p></section>}</div>
      <section className="liveUxCard liveUxControls"><header><UnlockKeyhole/><div><small>LIVE ARM</small><h3>Release control</h3></div><b>{liveArmed ? 'ARMED' : 'LOCKED'}</b></header><div className="liveUxControlState"><strong>{liveArmed ? 'ARMED' : 'LOCKED'}</strong><span>Arming LIVE does not start an order. Auto Trade must be enabled separately.</span></div><button type="button" className="liveArmButton" onClick={liveArmed ? disarmLive : armLive} disabled={Boolean(busy) || (!liveArmed && (!authorizationValid || !configured || !connected || emergency || recoveryRequired || !readinessReady))}>{liveArmed ? <LockKeyhole/> : <UnlockKeyhole/>}{liveArmed ? ' DISARM LIVE' : ' ARM LIVE'}</button><small className="liveUxReason">{liveArmed ? 'New-entry authority is active until the backend arm window expires.' : 'Current backend blocker: ' + (blocker || 'unknown')}</small></section>

    <section className="liveUxCard liveUxAuto"><header><Power/><div><small>AUTO TRADE / LIVE ARM</small><h3>Supervised live automation</h3></div><b>{status?.live_auto_trade ? 'ON' : 'OFF'}</b></header><div className="liveUxActionRow"><button type="button" onClick={autoToggle} disabled={Boolean(busy) || !autoReady || Boolean(status?.live_auto_trade)}><Power/> START LIVE AUTO TRADE</button><button type="button" onClick={stopAutoTrade} disabled={Boolean(busy) || !status?.live_auto_trade}><Power/> STOP AUTO TRADE</button></div><p className="liveUxReason"><strong>Reason:</strong> {status?.live_auto_trade ? 'Automatic trading is enabled by backend state.' : status?.auto?.last_decision || blocker}</p></section></div>

    {renderManualOrderCard()}

    <div className="liveUxGrid liveUxLowerGrid"><section className="liveUxCard"><header><CircleDollarSign/><div><small>{liveCopy.lastOrder}</small><h3>{recentOrder ? 'Son LIVE aktivitesi' : liveCopy.noRecord}</h3></div></header>{recentOrder ? <div className="liveUxMetrics"><span><small>OLAY</small><b>{text(recentOrder.kind)}</b></span><span><small>MESAJ</small><b>{text(recentOrder.message)}</b></span><span><small>ZAMAN</small><b>{date(recentOrder.created_at)}</b></span><span><small>YÜRÜTME</small><b>{text(status?.execution_state)}</b></span></div> : <p className="liveUxEmpty">Backend snapshot içinde son emir kaydı yok.</p>}</section><section className="liveUxCard"><header><ShieldCheck/><div><small>{liveCopy.protectionSummary}</small><h3>{currentProtection}</h3></div><b>{currentProtection}</b></header><div className="liveUxProtectionList"><span>TOPLAM DURUM <b>{currentProtection}</b></span></div></section></div>
    <div className="liveUxGrid liveUxLowerGrid"><section className="liveUxCard"><header><RefreshCw/><div><small>{liveCopy.recovery}</small><h3>{recoveryState === 'READY' ? 'READY' : recoveryRequired ? 'RECOVERY GEREKLİ' : 'BLOCKED'}</h3></div><b>{recoveryState}</b></header><p className="liveUxReason">{status?.reconciliation_diagnostic?.exception_message || status?.recovery_error || (status?.reconciliation_required ? 'Yeni LIVE işlemlerden önce uzlaştırma gerekir.' : 'Recovery durumu backend verisinden okunur.')}</p><button type="button" onClick={checkRecovery} disabled={Boolean(busy)}>{busy === 'recovery' ? 'KONTROL EDİLİYOR...' : liveCopy.checkRecovery}</button></section><section className="liveUxCard liveUxEmergency"><header><ShieldAlert/><div><small>{liveCopy.emergencyStop}</small><h3>{emergency ? 'LIVE ENGELLENDİ' : 'NORMAL'}</h3></div><b>{emergency ? 'BLOCKED' : 'CLEAR'}</b></header><p>Yeni canlı yürütmeyi ve otomatik işlemleri engeller.</p><div className="liveUxActionRow"><button type="button" onClick={emergencyStop} disabled={Boolean(busy)}><ShieldAlert/> {liveCopy.emergencyStop}</button>{emergency && <button type="button" onClick={clearEmergency} disabled={Boolean(busy)}>RECOVERY SONRASI TEMİZLE</button>}</div></section></div>
    <section className="liveUxCard liveUxExternalHistory" aria-label={liveCopy.history}><header><RefreshCw/><div><small>READ-ONLY · V25 DIŞI</small><h3>{liveCopy.history}</h3></div><b>{externalHistory?.external_trades?.length ?? 0}</b></header><p className="liveUxReason">V25 planıyla eşleşmeyen Binance işlemleri salt-okunur gösterilir.</p>{externalHistory?.external_trades?.length ? <div className="liveUxTableWrap"><table><thead><tr><th>Sembol</th><th>Yön</th><th>Fiyat</th><th>Miktar</th><th>Gerçekleşen PnL</th><th>Zaman</th></tr></thead><tbody>{externalHistory.external_trades.slice(0, 30).map((trade, index) => <tr key={`${String(trade.trade_id || trade.order_id)}-${index}`}><td>{text(trade.symbol)}</td><td>{text(trade.side)}</td><td>{text(trade.price)}</td><td>{text(trade.quantity)}</td><td className={Number(trade.realized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{money(Number(trade.realized_pnl || 0))}</td><td>{externalDate(trade.time)}</td></tr>)}</tbody></table></div> : <div className="liveUxEmpty"><strong>{liveCopy.noHistory}</strong><span>Bu alan yalnızca gerçek Binance read-only verisini gösterir.</span></div>}</section>
    <div className={`liveNotice ${notice.kind}`}>{notice.kind === 'error' ? <TriangleAlert/> : notice.kind === 'ok' ? <CheckCircle2/> : <ShieldCheck/>}<span>{notice.text}</span></div>
    {credentialModal}
    {confirmationModal}
  </section>

  return <section className="liveTradingPanel liveTerminal" aria-label="LIVE Trading Operations Terminal">
    <header className="liveTerminalHeader"><div><span className="liveKicker">LIVE OPERATIONS / REAL MONEY</span><h2>LIVE TRADING</h2><p>Binance Futures Mainnet · V25 fail-closed execution</p></div><div className={`liveTerminalLock ${executionLocked ? 'locked' : 'ready'}`}><ShieldAlert/><strong>{executionLocked ? 'LIVE LOCKED' : 'LIVE READY'}</strong><small>{connected ? 'BINANCE CONNECTED' : 'BINANCE DISCONNECTED'} · {status?.readiness?.ready ? 'READINESS PASS' : 'READINESS BLOCKED'}</small></div></header>
    <div className="liveStatusGrid">{stateRows.map(([label, value, safe]) => <div key={label} className={safe ? 'safe' : 'danger'}><small>{label}</small><b>{value}</b></div>)}</div>
    <div className={`liveNotice ${notice.kind}`}>{notice.kind === 'error' ? <TriangleAlert/> : notice.kind === 'ok' ? <CheckCircle2/> : <ShieldCheck/>}<span>{notice.text}</span><button type="button" onClick={() => void refresh(false)} disabled={Boolean(busy)}><RefreshCw/> REFRESH STATUS</button></div>

    <section className="liveTerminalGrid liveConnectionRow"><article className="liveTerminalPanel liveCredentials"><header><KeyRound/><div><span>01 · API SETTINGS</span><h3>Binance account connection</h3></div><strong>{configured ? 'CONFIGURED' : 'MISSING'}</strong></header><p>Secrets are sent only to the existing encrypted backend vault. No browser storage, logs, or URL parameters.</p><div className="liveFormGrid"><label>BINANCE API KEY<input data-private="true" type="password" autoComplete="new-password" value={credentials.apiKey} onChange={event => setCredentials({...credentials, apiKey: event.target.value})}/></label><label>BINANCE SECRET KEY<input data-private="true" type="password" autoComplete="new-password" value={credentials.secretKey} onChange={event => setCredentials({...credentials, secretKey: event.target.value})}/></label></div><label className="liveCheck"><input type="checkbox" checked={credentials.accepted} onChange={event => setCredentials({...credentials, accepted: event.target.checked})}/><span>Store the secret only in the encrypted server vault.</span></label><div className="liveButtons"><button type="button" onClick={testConnection} disabled={Boolean(busy) || (rateLimitSeconds !== null && rateLimitSeconds > 0) || !connections?.vault?.ready}>{busy === 'test' ? <RefreshCw className="spin"/> : <ShieldCheck/>}{rateLimitSeconds === null ? ' TEST CONNECTION' : rateLimitSeconds > 0 ? ` Retry in ${rateLimitSeconds}s` : ' Retry now'}</button><button type="button" onClick={saveCredentials} disabled={Boolean(busy) || (rateLimitSeconds !== null && rateLimitSeconds > 0) || !connections?.vault?.ready}>{busy === 'save' ? <RefreshCw className="spin"/> : <LockKeyhole/>} SAVE TO VAULT</button><button type="button" onClick={connectReadOnly} disabled={Boolean(busy) || (rateLimitSeconds !== null && rateLimitSeconds > 0) || !configured}>{busy === 'connect' ? <RefreshCw className="spin"/> : <UnlockKeyhole/>} CONNECT ACCOUNT</button></div><div className="liveMeta"><span>CONNECTION STATUS</span><b>{connected ? 'READ-ONLY CONNECTED' : 'NOT CONNECTED'}</b><span>ACCOUNT STATUS</span><b>{configured ? text(liveConnection?.fingerprint || status?.credentials?.fingerprint) : 'CREDENTIALS REQUIRED'}</b></div></article>
      <article className="liveTerminalPanel liveReadiness"><header><ShieldCheck/><div><span>02 · SAFETY / READINESS</span><h3>Backend gate monitor</h3></div><strong className={readinessReady ? 'positive' : 'warning'}>{readinessReady ? 'READY' : 'BLOCKED'}</strong></header><div className="liveMetricList"><span><small>RISK GATE</small><b>{riskGate ? riskGate.passed ? 'PASS' : 'BLOCKED' : 'UNKNOWN'}</b></span><span><small>EXPOSURE</small><b>{money(exposure)}</b></span><span><small>ACTIVE PLAN</small><b>{activePlans ? `${activePlans} ACTIVE` : 'NONE'}</b></span><span><small>LIVE LOCK</small><b>{executionLocked ? 'LOCKED' : 'READY'}</b></span><span><small>PROTECTION</small><b>{protectionState}</b></span><span><small>RECOVERY</small><b>{recoveryState}</b></span><span><small>EMERGENCY</small><b>{emergency ? 'ACTIVE / BLOCKED' : 'SAFE'}</b></span></div><div className="liveGateList">{(status?.readiness?.gates || []).map(gate => <div key={gate.key}><i className={gate.passed ? 'passed' : ''}>{gate.passed ? '✓' : '!'}</i><span><b>{gate.label || gate.key}</b><small>{gate.detail || (gate.passed ? 'PASSED' : 'BLOCKED')}</small></span></div>)}</div></article></section>

    <section className="liveTerminalGrid liveControlRow"><article className="liveTerminalPanel liveArmPanel"><header><UnlockKeyhole/><div><span>03 · LIVE ARM</span><h3>Manual release control</h3></div><strong className={executionLocked ? 'warning' : 'positive'}>{executionLocked ? 'LOCKED' : 'ARMED'}</strong></header><div className="liveArmState"><b>{executionLocked ? 'LIVE LOCKED' : 'LIVE READY'}</b><span>{executionLocked ? 'All backend gates must pass before arm.' : 'Arm window active. Auto trade remains separate.'}</span></div><button type="button" className="liveArmButton" onClick={status?.armed ? () => run('disarm', () => call(V25, '/disarm', {method: 'POST'}).then(() => undefined), 'LIVE disarmed; AUTO-TRADE kapalı.') : armLive} disabled={Boolean(busy) || (!status?.armed && (!configured || !connected || emergency || recoveryRequired || !readinessReady || !protectionReady))}>{status?.armed ? <LockKeyhole/> : <UnlockKeyhole/>}{status?.armed ? ' DISARM LIVE' : ' ARM LIVE'}</button><small className="liveGateNote">{status?.armed ? 'Disarm closes new-entry authority.' : (!configured || !connected ? 'Connect and verify the account first.' : !readinessReady ? 'Readiness gates are blocking LIVE ARM.' : !protectionReady ? `Protection is ${protectionState}.` : emergency || recoveryRequired ? 'Emergency or recovery state is blocking LIVE ARM.' : 'Backend confirmation is required.')}</small></article>
      <article className="liveTerminalPanel liveAuto"><header><Power/><div><span>04 · LIVE AUTO TRADE</span><h3>Supervised live automation</h3></div><strong className={status?.live_auto_trade ? 'positive' : 'warning'}>{status?.live_auto_trade ? 'ON' : 'OFF'}</strong></header><div className="liveAutoState"><b>{status?.live_auto_trade ? 'AUTO TRADE ON' : 'AUTO TRADE OFF'}</b><span>{status?.auto?.last_decision || 'Automatic live entries are disabled by default.'}</span></div><div className="liveMetricList"><span><small>LIVE LOCK</small><b>{executionLocked ? 'LOCKED' : 'READY'}</b></span><span><small>BINANCE CONNECTION</small><b>{connectionState}</b></span><span><small>LIVE ARM</small><b>{armState}</b></span><span><small>RISK GATE</small><b>{riskState}</b></span><span><small>EXPOSURE</small><b>{exposureState}</b></span><span><small>ACTIVE PLAN</small><b>{activePlanState}</b></span><span><small>PROTECTION</small><b>{protectionState}</b></span><span><small>RECOVERY</small><b>{recoveryState}</b></span><span><small>EMERGENCY</small><b>{emergencyState}</b></span></div><div className="liveButtons"><button type="button" onClick={autoToggle} disabled={Boolean(busy) || !autoReady || Boolean(status?.live_auto_trade)}><Power/> START LIVE AUTO TRADE</button><button type="button" onClick={stopAutoTrade} disabled={Boolean(busy) || !status?.live_auto_trade}><Power/> STOP AUTO TRADE</button></div></article></section>

    <section className="liveTerminalGrid liveOrderRow"><article className="liveTerminalPanel liveManual"><header><Send/><div><span>05 · MANUAL LIVE ORDER</span><h3>Review before real submission</h3></div><strong>{executionLocked ? 'LOCKED' : 'V25 CHAIN ONLY'}</strong></header><div className="liveFormGrid liveOrderForm"><label>SYMBOL<input value={order.symbol} onChange={event => setOrder({...order, symbol: event.target.value.toUpperCase()})}/></label><div className="liveChoice"><span>SIDE</span><button className={order.direction === 'LONG' ? 'selectedLong' : ''} onClick={() => setOrder({...order, direction: 'LONG'})}>LONG</button><button className={order.direction === 'SHORT' ? 'selectedShort' : ''} onClick={() => setOrder({...order, direction: 'SHORT'})}>SHORT</button></div><label>ORDER TYPE<select value={order.order_type} onChange={event => setOrder({...order, order_type: event.target.value as OrderDraft['order_type']})}><option>MARKET</option><option>LIMIT</option></select></label><label>MARGIN / QUANTITY<input type="number" min="5" value={order.margin_usdt} onChange={event => setOrder({...order, margin_usdt: event.target.value})}/></label><label>LEVERAGE<input type="number" min="1" max="50" value={order.leverage} onChange={event => setOrder({...order, leverage: event.target.value})}/></label>{order.order_type === 'LIMIT' && <label>ENTRY PRICE<input type="number" value={order.limit_price} onChange={event => setOrder({...order, limit_price: event.target.value})}/></label>}<label>STOP LOSS<input type="number" value={order.stop_loss} onChange={event => setOrder({...order, stop_loss: event.target.value})}/></label><label>TAKE PROFIT 1<input type="number" value={order.tp1} onChange={event => setOrder({...order, tp1: event.target.value})}/></label><label>TAKE PROFIT 2<input type="number" value={order.tp2} onChange={event => setOrder({...order, tp2: event.target.value})}/></label><label>TAKE PROFIT 3<input type="number" value={order.tp3} onChange={event => setOrder({...order, tp3: event.target.value})}/></label></div><div className="liveOrderFooter"><span>ORDER STATUS: {executionLocked ? 'BLOCKED BY LIVE LOCK' : 'REVIEW REQUIRED'}</span><button type="button" onClick={fillAnalysis} disabled={!analysis}>FILL ANALYSIS</button><button type="button" onClick={reviewOrder} disabled={Boolean(busy) || executionLocked || !readinessReady}><Send/> REVIEW LIVE ORDER</button></div></article><article className="liveTerminalPanel liveExposure"><header><CircleDollarSign/><div><span>06 · POSITION / EXPOSURE</span><h3>Read-only account summary</h3></div><strong>READ ONLY</strong></header><div className="liveMetricList"><span><small>AVAILABLE BALANCE</small><b>{money(status?.account?.available_balance)}</b></span><span><small>UNREALIZED P&amp;L</small><b>{money(status?.account?.unrealized_pnl)}</b></span><span><small>CURRENT EXPOSURE</small><b>{money(exposure)}</b></span><span><small>OPEN POSITIONS</small><b>{status?.account?.positions?.length ?? 0}</b></span><span><small>DAILY P&amp;L</small><b>{money(status?.daily?.realized_pnl)}</b></span><span><small>LAST ORDER</small><b>{text(status?.events?.find(event => String(event.kind).includes('ORDER'))?.message)}</b></span></div><div className="liveAccountLine"><Wallet/><span><small>ACCOUNT CONNECTIVITY</small><b>{connected ? 'CONNECTED' : 'DISCONNECTED'} · {status?.connection?.last_error || `Last sync ${date(status?.connection?.last_checked)}`}</b></span></div></article></section>

    <section className="liveSafetyRail"><article><ShieldCheck/><div><span>07 · PROTECTION</span><b>PROTECTION {protectionState}</b><small>{protectionGate?.detail || 'Protection readiness is sourced from backend V25 gates.'}</small></div></article><article><RefreshCw/><div><span>08 · RECOVERY</span><b>{recoveryState === 'REQUIRED' ? 'RECOVERY REQUIRED' : `RECOVERY ${recoveryState}`}</b><small>{status?.reconciliation_diagnostic?.exception_message || status?.recovery_error || 'Recovery state is read from the live execution backend.'}</small></div><button type="button" onClick={checkRecovery} disabled={Boolean(busy)}>CHECK RECOVERY</button></article><article><ShieldAlert/><div><span>09 · EMERGENCY STOP</span><b>{emergency ? 'LIVE BLOCKED' : 'EMERGENCY SAFE'}</b><small>{emergency ? (status?.emergency?.reason || 'New live execution is blocked.') : 'Emergency stop is inactive.'}</small></div><button type="button" className="liveEmergencyButton" onClick={emergencyStop} disabled={Boolean(busy)}><ShieldAlert/> EMERGENCY STOP</button></article></section>

    {confirmationModal}
  </section>
  return <section className="liveTradingPanel" aria-label="LIVE Trading Real Money">
    <header className="livePanelHero"><div><span>LIVE / REAL MONEY</span><h2>LIVE TRADING</h2><p>Binance Futures Mainnet</p><strong>REAL MONEY — LIVE ACCOUNT</strong></div><div className="liveHeroState"><ShieldAlert/><b>{executionLocked ? 'LOCKED / OFF' : 'ARMED / READY'}</b><small>V25 FAIL-CLOSED EXECUTION</small></div></header>

    <div className="liveStatusGrid">{stateRows.map(([label, value, safe]) => <div key={label} className={safe ? 'safe' : 'danger'}><small>{label}</small><b>{value}</b></div>)}</div>
    <div className={`liveNotice ${notice.kind}`}>{notice.kind === 'error' ? <TriangleAlert/> : notice.kind === 'ok' ? <CheckCircle2/> : <ShieldCheck/>}<span>{notice.text}</span><button type="button" onClick={() => void refresh(false)} disabled={Boolean(busy)}><RefreshCw/> REFRESH</button></div>

    <div className="livePanelGrid">
      <section className="liveCard liveCredentials"><header><KeyRound/><div><small>LIVE API SETTINGS</small><h3>Binance Futures Mainnet</h3></div><b>{configured ? 'CONFIGURED' : 'MISSING'}</b></header><p>Secret yalnızca mevcut şifreli backend kasasına gönderilir; localStorage/sessionStorage, summary ve loglarda tutulmaz.</p><label>Binance API Key<input data-private="true" type="password" autoComplete="new-password" value={credentials.apiKey} onChange={event => setCredentials({...credentials, apiKey: event.target.value})}/></label><label>Binance API Secret<input data-private="true" type="password" autoComplete="new-password" value={credentials.secretKey} onChange={event => setCredentials({...credentials, secretKey: event.target.value})}/></label><label className="liveCheck"><input type="checkbox" checked={credentials.accepted} onChange={event => setCredentials({...credentials, accepted: event.target.checked})}/><span>Şifreli sunucu kasasını ve secret’ın bir daha gösterilmeyeceğini onaylıyorum.</span></label><div className="liveButtons"><button type="button" onClick={testConnection} disabled={Boolean(busy) || !connections?.vault?.ready}>{busy === 'test' ? <RefreshCw className="spin"/> : <ShieldCheck/>} TEST CONNECTION</button><button type="button" onClick={saveCredentials} disabled={Boolean(busy) || !connections?.vault?.ready}>{busy === 'save' ? <RefreshCw className="spin"/> : <LockKeyhole/>} ENCRYPTED SAVE</button></div><div className="liveMeta"><span>Fingerprint</span><b>{text(liveConnection?.fingerprint || status?.credentials?.fingerprint)}</b><span>Permissions</span><b>Futures only · withdrawals unsupported</b></div></section>

      <section className="liveCard liveReadiness"><header><ShieldCheck/><div><small>LIVE SAFETY GATES</small><h3>Arm / Protection Readiness</h3></div></header><div className="liveGateList">{(status?.readiness?.gates || []).map(gate => <div key={gate.key}><i className={gate.passed ? 'passed' : ''}>{gate.passed ? '✓' : '!'}</i><span><b>{gate.label || gate.key}</b><small>{gate.detail || (gate.passed ? 'PASSED' : 'BLOCKED')}</small></span></div>)}</div><div className="liveButtons"><button type="button" className="liveArmButton" onClick={status?.armed ? () => run('disarm', () => call(V25, '/disarm', {method: 'POST'}).then(() => undefined), 'LIVE disarmed; AUTO-TRADE kapalı.') : armLive} disabled={Boolean(busy) || (!status?.armed && (!configured || !connected || emergency || recoveryRequired || !protectionReady))}>{status?.armed ? <LockKeyhole/> : <UnlockKeyhole/>}{status?.armed ? ' DISARM LIVE' : ' LIVE ARM'}</button><button type="button" className="liveEmergencyButton" onClick={emergencyStop} disabled={Boolean(busy)}><ShieldAlert/> EMERGENCY STOP</button></div><small className="liveGateNote">ARM, AUTO-TRADE’i başlatmaz. Refresh/restart sonrası backend state LOCKED/OFF olarak değerlendirilir.</small></section>
    </div>

    <section className="liveCard liveManual"><header><Send/><div><small>LIVE MANUAL ORDER</small><h3>Review before real submission</h3></div><b>V25 CHAIN ONLY</b></header><div className="liveOrderGrid"><label>Symbol<input value={order.symbol} onChange={event => setOrder({...order, symbol: event.target.value.toUpperCase()})}/></label><div className="liveChoice"><span>Direction</span><button className={order.direction === 'LONG' ? 'selectedLong' : ''} onClick={() => setOrder({...order, direction: 'LONG'})}>LONG</button><button className={order.direction === 'SHORT' ? 'selectedShort' : ''} onClick={() => setOrder({...order, direction: 'SHORT'})}>SHORT</button></div><label>Order type<select value={order.order_type} onChange={event => setOrder({...order, order_type: event.target.value as OrderDraft['order_type']})}><option>MARKET</option><option>LIMIT</option></select></label><label>Margin / risk sizing<input type="number" min="5" value={order.margin_usdt} onChange={event => setOrder({...order, margin_usdt: event.target.value})}/></label><label>Leverage<input type="number" min="1" max="50" value={order.leverage} onChange={event => setOrder({...order, leverage: event.target.value})}/></label>{order.order_type === 'LIMIT' && <label>Entry price<input type="number" value={order.limit_price} onChange={event => setOrder({...order, limit_price: event.target.value})}/></label>}<label>Stop Loss<input type="number" value={order.stop_loss} onChange={event => setOrder({...order, stop_loss: event.target.value})}/></label><label>Take Profit 1<input type="number" value={order.tp1} onChange={event => setOrder({...order, tp1: event.target.value})}/></label><label>Take Profit 2<input type="number" value={order.tp2} onChange={event => setOrder({...order, tp2: event.target.value})}/></label><label>Take Profit 3<input type="number" value={order.tp3} onChange={event => setOrder({...order, tp3: event.target.value})}/></label></div><div className="liveOrderEstimate"><span><small>ESTIMATED NOTIONAL</small><b>{money(Number(order.margin_usdt) * Number(order.leverage))}</b></span><span><small>ESTIMATED RISK</small><b>{money(Math.abs(Number(order.limit_price || analysis?.entry || 0) - Number(order.stop_loss || analysis?.stop_loss || 0)) * Number(order.margin_usdt) * Number(order.leverage) / Math.max(1, Number(order.limit_price || analysis?.entry || 1)))}</b></span><button type="button" onClick={fillAnalysis}><RefreshCw/> FILL FROM ANALYSIS</button></div><button type="button" className="liveReviewButton" onClick={reviewOrder} disabled={Boolean(busy) || !status?.armed || Boolean(status?.real_trading_locked) || !connected || emergency || recoveryRequired || !protectionReady}><Send/> REVIEW LIVE ORDER</button></section>

    <section className="livePanelGrid"><section className="liveCard liveAuto"><header><Power/><div><small>LIVE AUTO-TRADE</small><h3>Supervised automation</h3></div><b className={status?.live_auto_trade ? 'on' : ''}>{status?.live_auto_trade ? 'ON' : 'OFF'}</b></header><div className="liveAutoFields"><label>Allowed USDT pairs<input value={String(policy.allowed_symbols || '')} onChange={event => setPolicy('allowed_symbols', event.target.value.toUpperCase().split(',').map(value => value.trim()).filter(Boolean))}/></label><label>Risk per trade %<input type="number" value={numericPolicy('max_loss_per_trade', 1)} onChange={event => setPolicy('max_loss_per_trade', Number(event.target.value))}/></label><label>Max total exposure<input type="number" min="25" max="350" value={numericPolicy('max_total_exposure_usdt', 350)} onChange={event => setPolicy('max_total_exposure_usdt', Number(event.target.value))}/></label><label>Daily loss limit<input type="number" value={numericPolicy('daily_loss_limit', 20)} onChange={event => setPolicy('daily_loss_limit', Number(event.target.value))}/></label><label>Max simultaneous positions<input type="number" value={numericPolicy('max_positions', 3)} onChange={event => setPolicy('max_positions', Number(event.target.value))}/></label><label>Maximum consecutive losses<input type="number" value={numericPolicy('consecutive_loss_limit', 3)} onChange={event => setPolicy('consecutive_loss_limit', Number(event.target.value))}/></label><label>Minimum signal confidence<input type="number" value={numericPolicy('min_confidence', 85)} onChange={event => setPolicy('min_confidence', Number(event.target.value))}/></label><label>Maximum trap score<input type="number" value={numericPolicy('max_trap_score', 30)} onChange={event => setPolicy('max_trap_score', Number(event.target.value))}/></label><label className="liveToggle"><input type="checkbox" checked={policy.allow_long !== false} onChange={event => setPolicy('allow_long', event.target.checked)}/><span>LONG allowed</span></label><label className="liveToggle"><input type="checkbox" checked={policy.allow_short !== false} onChange={event => setPolicy('allow_short', event.target.checked)}/><span>SHORT allowed</span></label></div><div className="liveButtons"><button type="button" onClick={updatePolicy} disabled={Boolean(busy)}><ShieldCheck/> SAVE CANONICAL POLICY</button><button type="button" className={status?.live_auto_trade ? 'stopButton' : 'autoButton'} onClick={autoToggle} disabled={Boolean(busy) || (!status?.live_auto_trade && (!status?.armed || Boolean(status?.real_trading_locked) || !connected || emergency || recoveryRequired || !protectionReady))}>{status?.live_auto_trade ? 'DISABLE LIVE AUTO-TRADE' : 'LIVE AUTO-TRADE BAŞLAT'}</button></div><p className="liveGateNote">Start hour, end hour, volatility, BTC correlation and cooldown are enforced only where the canonical backend policy exposes them; no duplicate frontend gate is created.</p></section>

      <section className="liveCard liveDashboard"><header><CircleDollarSign/><div><small>LIVE DASHBOARD</small><h3>Read-only operational summary</h3></div></header><div className="liveSummaryGrid"><span><small>Current Exposure</small><b>{money((status?.account?.positions || []).reduce((sum, item) => sum + Number(item.notional || item.positionAmt || 0), 0))}</b></span><span><small>Daily P&amp;L</small><b>{money(status?.daily?.realized_pnl)}</b></span><span><small>Daily Loss Limit</small><b>{money(numericPolicy('daily_loss_limit', 0))}</b></span><span><small>Open Positions</small><b>{status?.account?.positions?.length ?? 0}</b></span><span><small>Active Plans</small><b>{status?.plans?.length ?? 0}</b></span><span><small>Last Decision</small><b>{text(status?.auto?.last_decision)}</b></span><span><small>Last Order</small><b>{text(status?.events?.find(event => String(event.kind).includes('ORDER'))?.message)}</b></span><span><small>Last Safety Event</small><b>{text(status?.events?.find(event => String(event.kind).includes('FAIL') || String(event.kind).includes('UNKNOWN') || String(event.kind).includes('EMERGENCY'))?.message)}</b></span></div><div className="liveAccountLine"><Wallet/><span><small>Available balance</small><b>{money(status?.account?.available_balance)} · {status?.connection?.last_error || `Last sync ${date(status?.connection?.last_checked)}`}</b></span></div></section></section>

    {confirm && <div className="liveConfirmBackdrop"><section className="liveConfirm" role="dialog" aria-modal="true"><h2>{confirm.title}</h2><p>{confirm.message}</p><label>Onay için yazın<input autoFocus value={confirmText} onChange={event => setConfirmText(event.target.value)} onKeyDown={event => {if (event.key === 'Enter') confirmAction()}} placeholder={confirm.expected}/></label><div><button type="button" onClick={() => {setConfirm(null);setConfirmText('')}}>CANCEL</button><button type="button" disabled={confirmText.trim().toUpperCase() !== confirm.expected} onClick={confirmAction}>CONFIRM</button></div></section></div>}
  </section>
}
