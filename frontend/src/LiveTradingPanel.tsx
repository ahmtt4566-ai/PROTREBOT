import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, CircleDollarSign, KeyRound, LockKeyhole, Power, RefreshCw, Send, ShieldAlert, ShieldCheck, TriangleAlert, UnlockKeyhole, Wallet } from 'lucide-react'
import { API_BASE } from './api'

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
  auto?: {enabled?: boolean; last_decision?: string; last_error?: string | null; last_scan?: string | null}
  policy?: LivePolicy
  policy_acknowledged?: boolean
  readiness?: {ready?: boolean; gates?: Array<{key?: string; label?: string; passed?: boolean; detail?: string}>}
  account?: {wallet_balance?: number | null; available_balance?: number | null; unrealized_pnl?: number | null; positions?: Array<Record<string, unknown>>}
  daily?: {realized_pnl?: number; remaining_loss_budget?: number; entries?: number}
  plans?: Array<Record<string, unknown>>
  events?: Array<{kind?: string; message?: string; created_at?: string}>
  emergency?: {active?: boolean; reason?: string | null}
  recovery_ready?: boolean
  recovery_error?: string | null
  execution_state?: string
  reconciliation_required?: boolean
}

type ConnectionStatus = {
  vault?: {ready?: boolean}
  connections?: {LIVE?: {configured?: boolean; active?: boolean; last_test_ok?: boolean; fingerprint?: string | null; last_error?: string | null; account?: {wallet_balance?: number}}}
  safety?: {secrets_returned_to_browser?: boolean; connection_test_creates_orders?: boolean; live_orders_require_v25_gates?: boolean}
}

type OrderDraft = {symbol: string; direction: 'LONG' | 'SHORT'; order_type: 'MARKET' | 'LIMIT'; margin_usdt: string; leverage: string; limit_price: string; stop_loss: string; tp1: string; tp2: string; tp3: string}

type Props = {active: boolean; symbol: string; analysis?: {direction?: string | null; entry?: number; stop_loss?: number; tp1?: number; tp2?: number; tp3?: number} | null}

const V25 = `${API_BASE}/v25`
const CONNECTIONS = `${API_BASE}/exchange-connections`
const initialOrder = (symbol: string): OrderDraft => ({symbol, direction: 'LONG', order_type: 'MARKET', margin_usdt: '10', leverage: '1', limit_price: '', stop_loss: '', tp1: '', tp2: '', tp3: ''})
const text = (value: unknown) => value === null || value === undefined || value === '' ? '—' : String(value)
const money = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${value.toLocaleString('tr-TR', {maximumFractionDigits: 2})} USDT` : '—'
const date = (value: unknown) => value ? new Date(String(value)).toLocaleString('tr-TR', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—'

function errorMessage(payload: unknown, fallback: string): string {
  if (typeof payload === 'string' && payload.trim()) return payload
  if (payload && typeof payload === 'object') {
    const row = payload as {detail?: unknown; message?: unknown}
    if (typeof row.detail === 'string') return row.detail
    if (typeof row.message === 'string') return row.message
  }
  return fallback
}

export default function LiveTradingPanel({active, symbol, analysis}: Props) {
  const [status, setStatus] = useState<LiveStatus | null>(null)
  const [connections, setConnections] = useState<ConnectionStatus | null>(null)
  const [credentials, setCredentials] = useState({apiKey: '', secretKey: '', accepted: false})
  const [order, setOrder] = useState<OrderDraft>(initialOrder(symbol))
  const [policyDraft, setPolicyDraft] = useState<LivePolicy | null>(null)
  const [confirm, setConfirm] = useState<{title: string;message: string;expected: string;action: () => Promise<void>} | null>(null)
  const [confirmText, setConfirmText] = useState('')
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState<{kind: 'info' | 'ok' | 'error'; text: string}>({kind: 'info', text: 'LIVE başlatılmadı. Gerçek emir kilidi varsayılan olarak kapalıdır.'})

  const call = async <T,>(base: string, path: string, options: RequestInit = {}): Promise<T> => {
    const headers = new Headers(options.headers)
    if (options.body) headers.set('Content-Type', 'application/json')
    const response = await fetch(`${base}${path}`, {...options, headers})
    const payload = await response.json().catch(() => ({})) as unknown
    if (!response.ok) throw new Error(errorMessage(payload, 'LIVE isteği reddedildi.'))
    return payload as T
  }

  const refresh = async (quiet = true) => {
    try {
      const [nextStatus, nextConnections] = await Promise.all([
        call<LiveStatus>(V25, '/status'),
        call<ConnectionStatus>(CONNECTIONS, '/status'),
      ])
      setStatus(nextStatus)
      setConnections(nextConnections)
      setPolicyDraft(current => current || nextStatus.policy || {})
    } catch (error) {
      if (!quiet) setNotice({kind: 'error', text: error instanceof Error ? error.message : 'LIVE durumu okunamadı.'})
    }
  }

  useEffect(() => {
    if (!active) return
    setOrder(initialOrder(symbol))
    setConfirm(null)
    setConfirmText('')
    void refresh()
    const timer = window.setInterval(() => void refresh(), 5000)
    return () => window.clearInterval(timer)
  }, [active, symbol])

  const run = async (key: string, action: () => Promise<void>, success: string) => {
    setBusy(key)
    try { await action(); setNotice({kind: 'ok', text: success}); await refresh() }
    catch (error) { setNotice({kind: 'error', text: error instanceof Error ? error.message : 'LIVE işlemi tamamlanamadı.'}) }
    finally { setBusy('') }
  }

  const liveConnection = connections?.connections?.LIVE
  const configured = Boolean(liveConnection?.configured || status?.credentials?.configured)
  const connected = Boolean(status?.connected)
  const emergency = Boolean(status?.emergency?.active)
  const recoveryRequired = Boolean(status?.reconciliation_required || status?.recovery_error || status?.execution_state === 'UNKNOWN')
  const protectionReady = status?.readiness?.gates?.find(gate => String(gate.key).toLowerCase().includes('protection'))?.passed !== false && !recoveryRequired
  const locked = status?.real_trading_locked !== false || !status?.armed
  const policy = policyDraft || status?.policy || {}
  const setPolicy = (key: string, value: unknown) => setPolicyDraft(current => ({...(current || {}), [key]: value}))
  const numericPolicy = (key: string, fallback: number) => Number(policy[key] ?? fallback)
  const updatePolicy = () => run('policy', async () => {
    await call<LiveStatus>(V25, '/policy', {method: 'PUT', body: JSON.stringify(policy)})
    await call<LiveStatus>(V25, '/policy/acknowledge', {method: 'POST', body: JSON.stringify({confirmation: 'RİSK LİMİTLERİNİ ONAYLIYORUM'})})
  }, 'LIVE risk ve strateji politikası kaydedildi; arm kapısı yeniden doğrulama istiyor.')

  const testConnection = () => run('test', async () => {
    const body = credentials.apiKey && credentials.secretKey ? {mode: 'LIVE', api_key: credentials.apiKey, secret_key: credentials.secretKey} : {mode: 'LIVE'}
    await call(CONNECTIONS, '/test', {method: 'POST', body: JSON.stringify(body)})
  }, 'LIVE API erişimi doğrulandı; test bağlantısı emir oluşturmadı.')

  const saveCredentials = () => run('save', async () => {
    if (!credentials.apiKey || !credentials.secretKey) throw new Error('LIVE API Key ve Secret Key birlikte girilmelidir.')
    if (!credentials.accepted) throw new Error('Secret yalnızca şifreli sunucu kasasında tutulur onayını verin.')
    await call(CONNECTIONS, '/save', {method: 'POST', body: JSON.stringify({mode: 'LIVE', api_key: credentials.apiKey, secret_key: credentials.secretKey, confirmation: 'CANLI KASAYA KAYDET'})})
    setCredentials({apiKey: '', secretKey: '', accepted: false})
  }, 'LIVE credential şifreli kasaya kaydedildi; secret tarayıcıda tutulmuyor.')

  const connectReadOnly = () => run('connect', async () => {
    await call(CONNECTIONS, '/activate', {method: 'POST', body: JSON.stringify({mode: 'LIVE', confirmation: 'CANLI SALT OKUNUR BAĞLANTIYI AÇ'})})
    await call<LiveStatus>(V25, '/connect/read-only', {method: 'POST'})
  }, 'LIVE hesap salt-okunur bağlandı; gerçek emir kilidi kapalı kaldı.')

  const armLive = () => {
    setConfirm({title: 'LIVE ARM onayı', message: 'REAL MONEY — LIVE ACCOUNT. Backend readiness gate’leri geçmeden kilit açılmaz. LIVE ARM AUTO-TRADE başlatmaz.', expected: 'CANLI EMİR RİSKİNİ KABUL EDİYORUM', action: async () => {
      await call(V25, '/consent', {method: 'POST', body: JSON.stringify({confirmation: 'CANLI İŞLEM RİSKİNİ 24 SAAT KABUL EDİYORUM'})})
      if (!status?.policy_acknowledged) await call(V25, '/policy/acknowledge', {method: 'POST', body: JSON.stringify({confirmation: 'RİSK LİMİTLERİNİ ONAYLIYORUM'})})
      await call(V25, '/arm', {method: 'POST', body: JSON.stringify({confirmation: 'CANLI EMİR RİSKİNİ KABUL EDİYORUM'})})
    }})
    setConfirmText('')
  }

  const autoToggle = () => {
    if (status?.live_auto_trade || status?.auto?.enabled) return run('auto-stop', () => call(V25, '/auto/stop', {method: 'POST'}).then(() => undefined), 'LIVE AUTO-TRADE kapatıldı; mevcut protection yönetimi backend’de devam eder.')
    setConfirm({title: 'LIVE AUTO-TRADE onayı', message: 'REAL MONEY WILL BE USED. Otomatik işlemler yalnız mevcut V25 live safety chain üzerinden ilerler.', expected: 'CANLI OTOMATİK', action: async () => { await call(V25, '/auto/start', {method: 'POST', body: JSON.stringify({confirmation: 'CANLI OTOMATİK'})}) }})
    setConfirmText('')
  }

  const emergencyStop = () => {
    setConfirm({title: 'EMERGENCY STOP', message: 'Yeni LIVE submissions bloklanacak, AUTO-TRADE kapanacak ve recovery gerekecek. Mevcut protection yönetimi merkezi backend kurallarına bırakılır.', expected: 'CANLI ACİL DURDUR', action: async () => { await call(V25, '/emergency', {method: 'POST', body: JSON.stringify({confirmation: 'CANLI ACİL DURDUR', close_tracked_positions: true})}) }})
    setConfirmText('')
  }

  const fillAnalysis = () => setOrder(current => ({...current, direction: analysis?.direction === 'SHORT' ? 'SHORT' : 'LONG', limit_price: text(analysis?.entry).replace('—', ''), stop_loss: text(analysis?.stop_loss).replace('—', ''), tp1: text(analysis?.tp1).replace('—', ''), tp2: text(analysis?.tp2).replace('—', ''), tp3: text(analysis?.tp3).replace('—', '')}))
  const reviewOrder = () => {
    const values = [order.margin_usdt, order.leverage, order.stop_loss, order.tp1, order.tp2, order.tp3]
    if (!order.symbol.endsWith('USDT') || values.some(value => !Number.isFinite(Number(value)) || Number(value) <= 0)) {
      setNotice({kind: 'error', text: 'LIVE order için symbol, margin, leverage, Stop ve TP seviyelerini geçerli girin.'}); return
    }
    setConfirm({title: 'REVIEW LIVE ORDER', message: `${order.symbol} ${order.direction} ${order.order_type} · ${order.margin_usdt} USDT margin · gerçek Binance emri için ikinci açık onay gerekir.`, expected: 'CANLI EMİR GÖNDER', action: async () => {
      await call(V25, '/order', {method: 'POST', body: JSON.stringify({...order, margin_usdt: Number(order.margin_usdt), leverage: Number(order.leverage), limit_price: order.order_type === 'LIMIT' ? Number(order.limit_price) : null, stop_loss: Number(order.stop_loss), tp1: Number(order.tp1), tp2: Number(order.tp2), tp3: Number(order.tp3), confirmation: 'CANLI EMİR GÖNDER', intent_id: `ui-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`})})
    }})
    setConfirmText('')
  }

  const confirmAction = () => {
    if (!confirm || confirmText.trim().toUpperCase() !== confirm.expected) return
    const action = confirm.action
    setConfirm(null); setConfirmText('')
    void run('confirmed', action, 'LIVE backend safety zinciri işlemi tamamlandı.')
  }

  const stateRows = [
    ['LIVE CONNECTION', connected ? 'CONNECTED' : 'DISCONNECTED', connected],
    ['LIVE TRADING', status?.armed && !status?.real_trading_locked ? 'ARMED' : 'LOCKED', Boolean(status?.armed && !status?.real_trading_locked)],
    ['LIVE AUTO-TRADE', status?.live_auto_trade ? 'ON' : 'OFF', Boolean(status?.live_auto_trade)],
    ['EMERGENCY STOP', emergency ? 'ACTIVE' : 'SAFE', !emergency],
    ['RECOVERY', recoveryRequired ? 'REQUIRED' : 'READY', !recoveryRequired],
    ['PROTECTION', protectionReady ? 'READY' : 'UNKNOWN', protectionReady],
  ] as const

  if (!active) return null
  return <section className="liveTradingPanel" aria-label="LIVE Trading Real Money">
    <header className="livePanelHero"><div><span>LIVE / REAL MONEY</span><h2>LIVE TRADING</h2><p>Binance Futures Mainnet</p><strong>REAL MONEY — LIVE ACCOUNT</strong></div><div className="liveHeroState"><ShieldAlert/><b>{locked ? 'LOCKED / OFF' : 'ARMED / READY'}</b><small>V25 FAIL-CLOSED EXECUTION</small></div></header>

    <div className="liveStatusGrid">{stateRows.map(([label, value, safe]) => <div key={label} className={safe ? 'safe' : 'danger'}><small>{label}</small><b>{value}</b></div>)}</div>
    <div className={`liveNotice ${notice.kind}`}>{notice.kind === 'error' ? <TriangleAlert/> : notice.kind === 'ok' ? <CheckCircle2/> : <ShieldCheck/>}<span>{notice.text}</span><button type="button" onClick={() => void refresh(false)} disabled={Boolean(busy)}><RefreshCw/> REFRESH</button></div>

    <div className="livePanelGrid">
      <section className="liveCard liveCredentials"><header><KeyRound/><div><small>LIVE API SETTINGS</small><h3>Binance Futures Mainnet</h3></div><b>{configured ? 'CONFIGURED' : 'MISSING'}</b></header><p>Secret yalnızca mevcut şifreli backend kasasına gönderilir; localStorage/sessionStorage, summary ve loglarda tutulmaz.</p><label>Binance API Key<input type="password" autoComplete="new-password" value={credentials.apiKey} onChange={event => setCredentials({...credentials, apiKey: event.target.value})}/></label><label>Binance API Secret<input type="password" autoComplete="new-password" value={credentials.secretKey} onChange={event => setCredentials({...credentials, secretKey: event.target.value})}/></label><label className="liveCheck"><input type="checkbox" checked={credentials.accepted} onChange={event => setCredentials({...credentials, accepted: event.target.checked})}/><span>Şifreli sunucu kasasını ve secret’ın bir daha gösterilmeyeceğini onaylıyorum.</span></label><div className="liveButtons"><button type="button" onClick={testConnection} disabled={Boolean(busy) || !connections?.vault?.ready}>{busy === 'test' ? <RefreshCw className="spin"/> : <ShieldCheck/>} TEST CONNECTION</button><button type="button" onClick={saveCredentials} disabled={Boolean(busy) || !connections?.vault?.ready}>{busy === 'save' ? <RefreshCw className="spin"/> : <LockKeyhole/>} ENCRYPTED SAVE</button></div><div className="liveMeta"><span>Fingerprint</span><b>{text(liveConnection?.fingerprint || status?.credentials?.fingerprint)}</b><span>Permissions</span><b>Futures only · withdrawals unsupported</b></div></section>

      <section className="liveCard liveReadiness"><header><ShieldCheck/><div><small>LIVE SAFETY GATES</small><h3>Arm / Protection Readiness</h3></div></header><div className="liveGateList">{(status?.readiness?.gates || []).map(gate => <div key={gate.key}><i className={gate.passed ? 'passed' : ''}>{gate.passed ? '✓' : '!'}</i><span><b>{gate.label || gate.key}</b><small>{gate.detail || (gate.passed ? 'PASSED' : 'BLOCKED')}</small></span></div>)}</div><div className="liveButtons"><button type="button" className="liveArmButton" onClick={status?.armed ? () => run('disarm', () => call(V25, '/disarm', {method: 'POST'}).then(() => undefined), 'LIVE disarmed; AUTO-TRADE kapalı.') : armLive} disabled={Boolean(busy) || (!status?.armed && (!configured || !connected || emergency || recoveryRequired || !protectionReady))}>{status?.armed ? <LockKeyhole/> : <UnlockKeyhole/>}{status?.armed ? ' DISARM LIVE' : ' LIVE ARM'}</button><button type="button" className="liveEmergencyButton" onClick={emergencyStop} disabled={Boolean(busy)}><ShieldAlert/> EMERGENCY STOP</button></div><small className="liveGateNote">ARM, AUTO-TRADE’i başlatmaz. Refresh/restart sonrası backend state LOCKED/OFF olarak değerlendirilir.</small></section>
    </div>

    <section className="liveCard liveManual"><header><Send/><div><small>LIVE MANUAL ORDER</small><h3>Review before real submission</h3></div><b>V25 CHAIN ONLY</b></header><div className="liveOrderGrid"><label>Symbol<input value={order.symbol} onChange={event => setOrder({...order, symbol: event.target.value.toUpperCase()})}/></label><div className="liveChoice"><span>Direction</span><button className={order.direction === 'LONG' ? 'selectedLong' : ''} onClick={() => setOrder({...order, direction: 'LONG'})}>LONG</button><button className={order.direction === 'SHORT' ? 'selectedShort' : ''} onClick={() => setOrder({...order, direction: 'SHORT'})}>SHORT</button></div><label>Order type<select value={order.order_type} onChange={event => setOrder({...order, order_type: event.target.value as OrderDraft['order_type']})}><option>MARKET</option><option>LIMIT</option></select></label><label>Margin / risk sizing<input type="number" min="5" value={order.margin_usdt} onChange={event => setOrder({...order, margin_usdt: event.target.value})}/></label><label>Leverage<input type="number" min="1" max="3" value={order.leverage} onChange={event => setOrder({...order, leverage: event.target.value})}/></label>{order.order_type === 'LIMIT' && <label>Entry price<input type="number" value={order.limit_price} onChange={event => setOrder({...order, limit_price: event.target.value})}/></label>}<label>Stop Loss<input type="number" value={order.stop_loss} onChange={event => setOrder({...order, stop_loss: event.target.value})}/></label><label>Take Profit 1<input type="number" value={order.tp1} onChange={event => setOrder({...order, tp1: event.target.value})}/></label><label>Take Profit 2<input type="number" value={order.tp2} onChange={event => setOrder({...order, tp2: event.target.value})}/></label><label>Take Profit 3<input type="number" value={order.tp3} onChange={event => setOrder({...order, tp3: event.target.value})}/></label></div><div className="liveOrderEstimate"><span><small>ESTIMATED NOTIONAL</small><b>{money(Number(order.margin_usdt) * Number(order.leverage))}</b></span><span><small>ESTIMATED RISK</small><b>{money(Math.abs(Number(order.limit_price || analysis?.entry || 0) - Number(order.stop_loss || analysis?.stop_loss || 0)) * Number(order.margin_usdt) * Number(order.leverage) / Math.max(1, Number(order.limit_price || analysis?.entry || 1)))}</b></span><button type="button" onClick={fillAnalysis}><RefreshCw/> FILL FROM ANALYSIS</button></div><button type="button" className="liveReviewButton" onClick={reviewOrder} disabled={Boolean(busy) || !status?.armed || Boolean(status?.real_trading_locked) || !connected || emergency || recoveryRequired || !protectionReady}><Send/> REVIEW LIVE ORDER</button></section>

    <section className="livePanelGrid"><section className="liveCard liveAuto"><header><Power/><div><small>LIVE AUTO-TRADE</small><h3>Supervised automation</h3></div><b className={status?.live_auto_trade ? 'on' : ''}>{status?.live_auto_trade ? 'ON' : 'OFF'}</b></header><div className="liveAutoFields"><label>Allowed USDT pairs<input value={String(policy.allowed_symbols || '')} onChange={event => setPolicy('allowed_symbols', event.target.value.toUpperCase().split(',').map(value => value.trim()).filter(Boolean))}/></label><label>Risk per trade %<input type="number" value={numericPolicy('max_loss_per_trade', 1)} onChange={event => setPolicy('max_loss_per_trade', Number(event.target.value))}/></label><label>Max total exposure<input type="number" value={numericPolicy('max_total_exposure_usdt', 250)} onChange={event => setPolicy('max_total_exposure_usdt', Number(event.target.value))}/></label><label>Daily loss limit<input type="number" value={numericPolicy('daily_loss_limit', 20)} onChange={event => setPolicy('daily_loss_limit', Number(event.target.value))}/></label><label>Max simultaneous positions<input type="number" value={numericPolicy('max_positions', 3)} onChange={event => setPolicy('max_positions', Number(event.target.value))}/></label><label>Maximum consecutive losses<input type="number" value={numericPolicy('consecutive_loss_limit', 3)} onChange={event => setPolicy('consecutive_loss_limit', Number(event.target.value))}/></label><label>Minimum signal confidence<input type="number" value={numericPolicy('min_confidence', 85)} onChange={event => setPolicy('min_confidence', Number(event.target.value))}/></label><label>Maximum trap score<input type="number" value={numericPolicy('max_trap_score', 30)} onChange={event => setPolicy('max_trap_score', Number(event.target.value))}/></label><label className="liveToggle"><input type="checkbox" checked={policy.allow_long !== false} onChange={event => setPolicy('allow_long', event.target.checked)}/><span>LONG allowed</span></label><label className="liveToggle"><input type="checkbox" checked={policy.allow_short !== false} onChange={event => setPolicy('allow_short', event.target.checked)}/><span>SHORT allowed</span></label></div><div className="liveButtons"><button type="button" onClick={updatePolicy} disabled={Boolean(busy)}><ShieldCheck/> SAVE CANONICAL POLICY</button><button type="button" className={status?.live_auto_trade ? 'stopButton' : 'autoButton'} onClick={autoToggle} disabled={Boolean(busy) || (!status?.live_auto_trade && (!status?.armed || Boolean(status?.real_trading_locked) || !connected || emergency || recoveryRequired || !protectionReady))}>{status?.live_auto_trade ? 'DISABLE LIVE AUTO-TRADE' : 'LIVE AUTO-TRADE BAŞLAT'}</button></div><p className="liveGateNote">Start hour, end hour, volatility, BTC correlation and cooldown are enforced only where the canonical backend policy exposes them; no duplicate frontend gate is created.</p></section>

      <section className="liveCard liveDashboard"><header><CircleDollarSign/><div><small>LIVE DASHBOARD</small><h3>Read-only operational summary</h3></div></header><div className="liveSummaryGrid"><span><small>Current Exposure</small><b>{money((status?.account?.positions || []).reduce((sum, item) => sum + Number(item.notional || item.positionAmt || 0), 0))}</b></span><span><small>Daily P&amp;L</small><b>{money(status?.daily?.realized_pnl)}</b></span><span><small>Daily Loss Limit</small><b>{money(numericPolicy('daily_loss_limit', 0))}</b></span><span><small>Open Positions</small><b>{status?.account?.positions?.length ?? 0}</b></span><span><small>Active Plans</small><b>{status?.plans?.length ?? 0}</b></span><span><small>Last Decision</small><b>{text(status?.auto?.last_decision)}</b></span><span><small>Last Order</small><b>{text(status?.events?.find(event => String(event.kind).includes('ORDER'))?.message)}</b></span><span><small>Last Safety Event</small><b>{text(status?.events?.find(event => String(event.kind).includes('FAIL') || String(event.kind).includes('UNKNOWN') || String(event.kind).includes('EMERGENCY'))?.message)}</b></span></div><div className="liveAccountLine"><Wallet/><span><small>Available balance</small><b>{money(status?.account?.available_balance)} · {status?.connection?.last_error || `Last sync ${date(status?.connection?.last_checked)}`}</b></span></div></section></section>

    {confirm && <div className="liveConfirmBackdrop"><section className="liveConfirm" role="dialog" aria-modal="true"><h2>{confirm.title}</h2><p>{confirm.message}</p><label>Onay için yazın<input autoFocus value={confirmText} onChange={event => setConfirmText(event.target.value)} onKeyDown={event => {if (event.key === 'Enter') confirmAction()}} placeholder={confirm.expected}/></label><div><button type="button" onClick={() => {setConfirm(null);setConfirmText('')}}>CANCEL</button><button type="button" disabled={confirmText.trim().toUpperCase() !== confirm.expected} onClick={confirmAction}>CONFIRM</button></div></section></div>}
  </section>
}
