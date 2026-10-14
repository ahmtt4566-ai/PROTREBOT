import {useEffect, useRef, useState, type FormEvent, type ReactNode} from 'react'
import {Database} from 'lucide-react'

export type OriginalPersistenceDiagnostics = {
  read_only: boolean
  build_commit: string | null
  database_status: string
  record_exists: boolean | null
  database_revision: number | null
  local_revision: number
  original_record_exists: boolean | null
  diagnosis: string
  last_write: {result: string; submitted_revision: number; checked_at: string} | null
  last_awaited_write: {result: string; submitted_revision: number; checked_at: string} | null
}

const diagnosticMessages: Record<string, string> = {
  DATABASE_UNAVAILABLE: 'Veritabanı bağlantısı hazır değil.',
  TABLE_MISSING: 'Kalıcı kayıt tablosu bulunamadı; migration incelemesi gerekli.',
  READ_FAILED: 'Veritabanı sorgusu tamamlanamadı.',
  FIRST_RECORD_MISSING: 'İlk kalıcı kayıt henüz yok; mevcut veri sıfırlanmamalı.',
  INVALID_DATABASE_REVISION: 'Kayıttaki revision geçersiz; manuel inceleme gerekli.',
  DATABASE_AHEAD: 'Veritabanı yerel durumdan ileride; eski durumla yazma engelleniyor.',
  WRITE_SKIPPED_REQUIRES_REVIEW: 'Son yazma atlandı; gecikmiş yazı veya başka sunucu süreci incelenmeli.',
  PREVIOUS_WRITE_FAILED: 'Önceki kalıcı yazma başarısız; veritabanı logları incelenmeli.',
  NO_REVISION_CONFLICT_OBSERVED: 'Bu kontrolde revision çakışması görülmedi.',
}

export const originalScanLabels: Record<string, string> = {
  UNIVERSE_PENDING: 'Günlük sembol listesi bekleniyor',
  UNIVERSE_REFRESH: 'Günlük sembol listesi yenileniyor',
  SCANNING: 'Tarama sürüyor',
  SCAN_COMPLETE: 'Tarama tamamlandı',
  WAITING_NEXT_CANDLE: 'Sonraki 15m mum bekleniyor',
  RATE_LIMIT_WAIT: 'Borsa istek sınırı bekleniyor',
  NO_ELIGIBLE_SYMBOLS: 'Filtrelere uyan sembol yok',
  BLOCKED: 'Yeni girişler engellendi',
}

const scanRejectionLabels: Record<string, string> = {
  data_missing: 'Eksik mum / 24s piyasa verisi',
  invalid_market_data: 'Bozuk veya yanlış zaman dilimli veri',
  market_volume: '24s hacim eşiği altı',
  market_move: '24s fiyat hareketi eşiği altı',
  market_filter: 'Piyasa / sözleşme filtresi',
  market_shortlist: 'Aday sıralama sınırı',
  signal_conditions: 'Trend / sinyal koşulu yok',
  confidence_low: 'Güven skoru düşük',
  spread_wide: 'Spread geniş',
  stop_too_wide: 'Stop %6 sınırından geniş',
  minimum_margin: 'Teminat sınırı',
  minimum_trade_size: 'Minimum işlem tutarı',
  position_limit: 'Pozisyon / aynı sembol sınırı',
  daily_limit: 'Günlük işlem veya kayıp sınırı',
  risk_or_account_limit: 'Diğer risk / hesap kontrolü',
}

const universeFilterLabels: Record<string, string> = {
  active_contracts: 'Aktif sözleşme',
  age_below_required_history: 'Gerekli geçmişten yeni',
  market_data_missing: 'Eksik günlük veri',
  invalid_market_data: 'Geçersiz günlük veri',
  volume_below_minimum: 'Hacim eşiği altı',
  spread_too_wide: 'Spread geniş',
  eligible: 'Filtrelerden geçen',
}

const qualityCheckLabels: Record<string, string> = {
  direction: 'yön',
  confidence: 'güven',
  trap: 'tuzak radarı',
  breakout: 'kırılım kalitesi',
  mtf: 'çoklu zaman dilimi',
}

type OriginalScanNearMiss = {
  symbol: string
  reason_code: string
  reason?: string
  confidence?: number | null
  confidence_threshold?: number | null
  failed_checks?: string[]
  market_volume_usdt?: number | null
  market_move_percent?: number | null
  stop_percent?: number | null
  total_quantity?: string
  total_notional_usdt?: string
  tp1_quantity?: string
  tp1_notional_usdt?: string
  remaining_quantity?: string
  remaining_notional_usdt?: string
  min_qty?: string
  min_notional_usdt?: string
}

export type OriginalV2LiveStatus = {
  available: boolean
  enabled: boolean
  paused?: boolean
  pause_reason?: string | null
  risk_percent: number
  daily_loss_percent: number
  risk_revision?: number
  risk_limits?: {max_loss_per_trade_usdt: number; daily_loss_limit_usdt: number; max_margin_usdt: number; min_margin_usdt: number; leverage: number}
  last_risk_adjustment?: {symbol: string; selected_percent: number; effective_percent: number; requested_loss_usdt: number; effective_loss_usdt: number; reasons: string[]} | null
  session?: {hours: number; started_at?: string | null; expires_at?: string | null; remaining_seconds: number; authority_limited: boolean; warning_sent: boolean} | null
  daily?: {day: string; opening_wallet?: number; loss?: number; limit?: number; locked: boolean} | null
  symbols: string[]
  universe?: {requested_count: number; selected_count: number; eligible_count: number; minimum_age_days?: number; updated_at?: string | null; filter_counts?: Record<string, number>} | null
  scanner?: {
    stage?: string
    last_cycle_at?: string
    last_scan_at?: string
    scanned_symbol_count?: number
    scanned_symbols?: string[]
    data_complete_count?: number
    candidate_count?: number
    rejection_counts?: Record<string, number>
    near_misses?: OriginalScanNearMiss[]
    last_24h?: {scans: number; candidates: number; trades: number}
    last_failure?: {stage: string; reason_code: string; symbol?: string; interval?: string; message: string} | null
  } | null
  fill_lock?: {id: string; active: boolean; symbol: string; violations: string[]; evidence?: {
    stop_percent?: string; risk_usdt?: string; risk_budget_usdt?: string; margin_usdt?: string
  }} | null
}

export type OriginalV2EntryCheck = {
  key: string
  label: string
  passed: boolean
  reason: string
}

type Props = {
  className?: string
  status: OriginalV2LiveStatus
  entryChecks: OriginalV2EntryCheck[]
  armed: boolean
  automationActive: boolean
  blocked: boolean
  request: (path: string, body?: Record<string, unknown>) => Promise<unknown>
  readDiagnostics: () => Promise<OriginalPersistenceDiagnostics>
  refresh: () => Promise<unknown>
  notice: string
  onNotice: (message: string) => void
  walletBalance?: number | null
  connected?: boolean
  statusVerified?: boolean
  prepareStart?: () => Promise<void>
  overview?: ReactNode
}

const highRiskPhrase = 'YÜKSEK RİSKİ KABUL EDİYORUM'
const riskLevel = (risk: number) => risk > 5 ? 'RED' : risk > 2 ? 'ORANGE' : risk > 1 ? 'YELLOW' : 'NORMAL'
const dailyLevel = (risk: number) => risk > 10 ? 'RED' : risk > 5 ? 'ORANGE' : 'NORMAL'
const warningNames = {NORMAL: 'Normal', YELLOW: 'Sarı uyarı', ORANGE: 'Turuncu uyarı', RED: 'Kırmızı uyarı'}
const usdt = (value: number) => `${value.toLocaleString('tr-TR', {minimumFractionDigits: 2, maximumFractionDigits: 2})} USDT`

export default function OriginalV2LiveCard({className, status, entryChecks, armed, automationActive, blocked, request, readDiagnostics, refresh, notice, onNotice, walletBalance, connected = true, statusVerified = true, prepareStart, overview}: Props) {
  const [risk, setRisk] = useState(String(status.risk_percent))
  const [dailyLoss, setDailyLoss] = useState(String(status.daily_loss_percent))
  const [symbolCount, setSymbolCount] = useState(String(status.universe?.requested_count ?? 40))
  const [hours, setHours] = useState('1')
  const [riskAcknowledged, setRiskAcknowledged] = useState(false)
  const [highRiskConfirmation, setHighRiskConfirmation] = useState('')
  const [understood, setUnderstood] = useState(false)
  const ownRiskRevision = useRef<number | null>(null)
  const [draftRiskRevision, setDraftRiskRevision] = useState(status.risk_revision ?? 0)
  useEffect(() => {
    setRisk(String(status.risk_percent))
    setDailyLoss(String(status.daily_loss_percent))
    setDraftRiskRevision(status.risk_revision ?? 0)
    if (ownRiskRevision.current !== status.risk_revision) {
      setRiskAcknowledged(false)
      setHighRiskConfirmation('')
      setUnderstood(false)
    }
    ownRiskRevision.current = null
  }, [status.risk_revision, status.risk_percent, status.daily_loss_percent])
  useEffect(() => {
    setRiskAcknowledged(false)
    setHighRiskConfirmation('')
    setUnderstood(false)
  }, [walletBalance])
  const changeRisk = (value: string, daily = false) => {
    if (daily) setDailyLoss(value)
    else setRisk(value)
    setRiskAcknowledged(false)
    setHighRiskConfirmation('')
    setUnderstood(false)
  }
  const selectedRisk = Number(risk)
  const selectedDaily = Number(dailyLoss)
  const validRisk = Number.isFinite(selectedRisk) && selectedRisk >= 0.5 && selectedRisk <= 10
    && Number.isFinite(selectedDaily) && selectedDaily >= 1 && selectedDaily <= 25 && selectedDaily >= selectedRisk
  const wallet = typeof walletBalance === 'number' && Number.isFinite(walletBalance) && walletBalance > 0 ? walletBalance : null
  const openingWallet = status.daily?.day === new Date().toISOString().slice(0, 10)
    && typeof status.daily.opening_wallet === 'number' && Number.isFinite(status.daily.opening_wallet) && status.daily.opening_wallet > 0
    ? status.daily.opening_wallet : wallet
  const consentReady = selectedRisk > 5 ? highRiskConfirmation === highRiskPhrase
    : selectedRisk > 2 ? riskAcknowledged : true
  const riskBody = {
    risk_percent: selectedRisk, daily_loss_percent: selectedDaily,
    risk_acknowledged: riskAcknowledged, high_risk_confirmation: highRiskConfirmation,
    warning_balance_usdt: wallet, expected_risk_revision: draftRiskRevision,
    understood,
  }
  const riskApproved = (status.risk_revision ?? 0) > 0 && selectedRisk === status.risk_percent
    && selectedDaily === status.daily_loss_percent && consentReady && understood
  const [clock, setClock] = useState(Date.now)
  const [sessionClock, setSessionClock] = useState({received: Date.now(), remaining: status.session?.remaining_seconds ?? 0})
  useEffect(() => {
    setSessionClock({received: Date.now(), remaining: status.session?.remaining_seconds ?? 0})
  }, [status.session?.expires_at, status.session?.remaining_seconds, status.enabled])
  useEffect(() => {
    if (!status.enabled || !status.session?.expires_at) return
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [status.enabled, status.session?.expires_at])
  const remaining = status.enabled ? Math.max(0, Math.ceil(sessionClock.remaining - Math.max(0, clock - sessionClock.received) / 1000)) : 0
  const sessionExpired = !!status.session?.expires_at && Date.parse(status.session.expires_at) <= Date.now()
  const remainingText = remaining > 0
    ? `${Math.floor(remaining / 3600)} sa ${Math.floor(remaining % 3600 / 60)} dk ${remaining % 60} sn`
    : sessionExpired ? 'Süre doldu / giriş kapalı'
    : status.paused ? 'Güvenlik nedeniyle durduruldu / giriş kapalı'
    : status.session?.expires_at ? 'Oturum kapalı / giriş kapalı' : 'Oturum yok'
  const renewing = !status.enabled && sessionExpired
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [approval, setApproval] = useState('')
  const [diagnostics, setDiagnostics] = useState<OriginalPersistenceDiagnostics | null>(null)
  const [diagnosing, setDiagnosing] = useState(false)
  const [diagnosticError, setDiagnosticError] = useState('')
  const diagnosticFlight = useRef(false)
  const inFlight = useRef(false)
  const diagnose = async () => {
    if (diagnosticFlight.current) return
    diagnosticFlight.current = true
    setDiagnosing(true)
    setDiagnostics(null)
    setDiagnosticError('')
    try {
      const result = await readDiagnostics()
      if (result.read_only !== true || !Number.isSafeInteger(result.local_revision)) throw new Error('Teşhis yanıtı doğrulanamadı.')
      setDiagnostics(result)
    } catch {
      setDiagnosticError('Kalıcı kayıt teşhisi alınamadı; oturum ve sunucu bağlantısını kontrol edin.')
    } finally {
      diagnosticFlight.current = false
      setDiagnosing(false)
    }
  }
  const fillLocked = status.fill_lock?.active === true
  const approve = async () => {
    if (inFlight.current || !fillLocked || approval !== 'DOLUM RİSKİNİ İNCELEDİM') return
    inFlight.current = true
    setBusy(true)
    onNotice('')
    try {
      const payload = await request('/original-v2/fill-lock/approve', {
        lock_id:status.fill_lock?.id, confirmation:approval, totp_code:code,
      })
      if (!payload || typeof payload !== 'object' || !('fill_lock' in payload)
          || !payload.fill_lock || typeof payload.fill_lock !== 'object' || !('active' in payload.fill_lock)
          || payload.fill_lock.active !== false || !('enabled' in payload) || payload.enabled !== false) {
        throw new Error('Dolum kilidi onayı doğrulanamadı; güncel LIVE durumunu kontrol edin.')
      }
      setCode('')
      setApproval('')
      onNotice('Dolum kilidi onayınla kaldırıldı; otomasyon başlamadı. Yeniden başlatma için risk onayı ve ARM gerekir.')
      await refresh()
    } catch (error: unknown) {
      onNotice(error instanceof Error ? error.message : 'Dolum kilidi kaldırılamadı.')
    } finally {
      setBusy(false)
      inFlight.current = false
    }
  }
  const act = async (stop: boolean) => {
    if (!stop && (!validRisk || wallet === null || !consentReady || !riskApproved)) {
      onNotice('Geçerli risk ayarı, güncel bakiye ve gerekli risk onayı olmadan strateji başlatılamaz.')
      return
    }
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    onNotice('')
    try {
      if (!stop) await prepareStart?.()
      const payload = await request(stop ? '/original-v2/stop' : '/original-v2/start', stop ? undefined : {
        ...riskBody,
        symbol_count: Number(symbolCount), session_hours: Number(hours),
      })
      if (!payload || typeof payload !== 'object' || !('enabled' in payload) || payload.enabled !== !stop) {
        throw new Error('Original V2 LIVE sonucu doğrulanamadı. LIVE durumunu yenileyin.')
      }
      setCode('')
      onNotice(stop ? 'Yeni girişler durduruldu; açık pozisyon ve Stop/TP korumaları korundu.' : 'Original V2 LIVE gözetimli oturumu açıldı.')
      await refresh()
    } catch (error: unknown) {
      onNotice(error instanceof Error ? error.message : 'Original V2 LIVE isteği başarısız.')
    } finally {
      setBusy(false)
      inFlight.current = false
    }
  }
  const saveRisk = async () => {
    if (inFlight.current || !validRisk || wallet === null || !consentReady || !understood) return
    inFlight.current = true
    setBusy(true)
    onNotice('')
    try {
      const payload = await request('/original-v2/risk-settings', riskBody)
      if (!payload || typeof payload !== 'object' || !('risk_percent' in payload) || payload.risk_percent !== selectedRisk
          || !('daily_loss_percent' in payload) || payload.daily_loss_percent !== selectedDaily
          || !('risk_revision' in payload) || !Number.isSafeInteger(payload.risk_revision) || Number(payload.risk_revision) < draftRiskRevision) {
        throw new Error('Risk değişikliğinin kalıcı kaydı doğrulanamadı; güncel ayarları kontrol edin.')
      }
      ownRiskRevision.current = Number(payload.risk_revision)
      setCode('')
      onNotice('Risk ayarı ve onayın kaydedildi. Yalnızca yeni işlemlere uygulanır; açık pozisyon, Stop ve TP değişmedi.')
      await refresh()
    } catch (error: unknown) {
      onNotice(error instanceof Error ? error.message : 'Risk ayarı kaydedilemedi.')
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    void act(false)
  }
  const entryBlocker = entryChecks.find(check => !check.passed)
  const entriesReady = status.enabled && automationActive && !blocked && !fillLocked && entryChecks.length > 0 && entryChecks.every(check => check.passed)
  const entryStatus = entriesReady
    ? 'Açık – sinyal bekleniyor'
    : `Engelli – ${entryBlocker?.reason || 'Giriş kontrolleri henüz doğrulanmadı.'}`
  const technicalDetails = <>
    <div className="masterTradeLiveControlMeta" data-testid="original-runtime-status">
      <span><small>STRATEJİ</small><b>Original V2</b></span>
      <span><small>SEÇİLEN EVREN</small><b>{status.symbols.length} / {status.universe?.requested_count ?? 40} sembol</b></span>
      <span><small>ONAYLI RİSK</small><b>%{status.risk_percent}</b></span>
      <span><small>TARAMA</small><b>{!status.enabled ? 'Otomasyon kapalı' : originalScanLabels[status.scanner?.stage || ''] || 'İlk tarama bekleniyor'}</b></span>
      <span><small>SON TAMAMLANAN TARAMA</small><b>{status.scanner?.last_scan_at ? new Date(status.scanner.last_scan_at).toLocaleString('tr-TR') : 'Henüz tamamlanmadı'}</b></span>
      <span><small>SON DÖNGÜ</small><b>{status.scanner?.last_cycle_at ? new Date(status.scanner.last_cycle_at).toLocaleString('tr-TR') : 'Henüz ölçülmedi'}</b></span>
      <span><small>TARANAN / ADAY</small><b>{status.scanner?.scanned_symbol_count ?? 0} / {status.scanner?.candidate_count ?? 0}</b></span>
      <span><small>YENİ GİRİŞ</small><b data-testid="original-entry-status">{entryStatus}</b></span>
      <span><small>OTURUM BİTİŞİ</small><b>{status.session?.expires_at ? new Date(status.session.expires_at).toLocaleString('tr-TR') : 'Oturum yok'}</b></span>
      <span><small>KALAN SÜRE</small><b data-testid="original-session-remaining">{remainingText}</b></span>
      <span><small>SEÇİLEN OTURUM</small><b>{status.session?.hours ?? 1} saat{status.session?.authority_limited ? ' · Yetki süresiyle sınırlı' : ''}</b></span>
    </div>
    <details className="originalEntryChecks" data-testid="original-entry-checks">
      <summary>Giriş kontrolleri · {entryChecks.filter(check => check.passed).length}/{entryChecks.length} geçti</summary>
      <ul>
        {entryChecks.map(check => <li key={check.key} data-result={check.passed ? 'passed' : 'failed'}>
          <strong>{check.passed ? 'GEÇTİ' : 'GEÇMEDİ'} · {check.label}</strong>
          <span>{check.reason}</span>
        </li>)}
      </ul>
      <p>ARM başlatma aşamasında kullanılır; açık otomasyon kendi süresi ve yukarıdaki kapılarla kontrol edilir. Her adayda sembol, sinyal, risk, bakiye ve Binance emir kuralları sunucuda yeniden doğrulanır. “Açık” emir garantisi değildir.</p>
    </details>
    <p>USDT-M Futures · 24 saatlik hacimde ilk N; hacim en az 1.000.000 USDT, liste yaşı en az {status.universe?.minimum_age_days ?? 45} gün (en uzun mum geçmişine göre), spread en fazla 8 bps. Evren UTC gününde bir kez yenilenir. Geçmişi eksik sembol veri eksik olarak elenir; tarama diğer sembollerle devam eder. Strateji: 3x · en fazla %6 stop · TP1’de %60 kapanır; kalan TP3 veya ilk stopta kapanır. Stop taşınmaz.</p>
    <small>Son taramadaki semboller: {(status.scanner?.scanned_symbols ?? status.symbols).map(symbol => symbol.replace('USDT', '')).join(' · ') || 'Günlük seçim bekleniyor'}</small>
    <small>Uygun: {status.universe?.eligible_count ?? 0} · Seçilen: {status.universe?.selected_count ?? status.symbols.length} · Liste yenileme: {status.universe?.updated_at ? new Date(status.universe.updated_at).toLocaleString('tr-TR') : 'Henüz yapılmadı'}</small>
    <details data-testid="original-scan-diagnostics">
      <summary>Tarama teşhisi</summary>
      <p data-testid="original-scan-24h">
        Son 24 saat: {status.scanner?.last_24h?.scans ?? 0} tarama · {status.scanner?.last_24h?.candidates ?? 0} aday · {status.scanner?.last_24h?.trades ?? 0} işleme dönüştü
      </p>
      <div aria-label="Son taramadaki eleme sayıları">
        {Object.entries(scanRejectionLabels).map(([key, label]) =>
          <span key={key}>{label}: {status.scanner?.rejection_counts?.[key] ?? 0} · </span>)}
      </div>
      <p>Günlük evren filtresi: {Object.entries(universeFilterLabels).map(([key, label]) =>
        `${label} ${status.universe?.filter_counts?.[key] ?? 0}`).join(' · ')}</p>
      <p>Sinyale en yakın 3 sembol:</p>
      <ol data-testid="original-scan-near-misses">
        {(status.scanner?.near_misses ?? []).map(item => <li key={item.symbol}>
          <b>{item.symbol}</b> — {scanRejectionLabels[item.reason_code] ?? item.reason ?? item.reason_code}
          {item.confidence != null && <> · Güven %{item.confidence}{item.confidence_threshold != null && ` / eşik %${item.confidence_threshold}`}</>}
          {item.stop_percent != null && <> · Stop %{item.stop_percent.toFixed(2)}</>}
          {item.market_volume_usdt != null && <> · 24s hacim {item.market_volume_usdt.toLocaleString('tr-TR')} USDT</>}
          {item.market_move_percent != null && <> · 24s hareket %{item.market_move_percent.toFixed(2)}</>}
          {item.total_notional_usdt != null && <>
            {' '}· Pozisyon {item.total_notional_usdt} USDT ({item.total_quantity})
            {' '}· TP1 %60 {item.tp1_notional_usdt} USDT ({item.tp1_quantity})
            {' '}· Kalan {item.remaining_notional_usdt} USDT ({item.remaining_quantity})
            {' '}· Borsa minimumu {item.min_notional_usdt} USDT / {item.min_qty}
          </>}
          {!!item.failed_checks?.length && <> · Geçmeyen: {item.failed_checks.map(key => qualityCheckLabels[key] ?? key).join(', ')}</>}
        </li>)}
      </ol>
      {status.scanner?.last_failure && <p role="status" data-testid="original-scan-failure">
        Tarama tamamlanmadı: {status.scanner.last_failure.message}
        {status.scanner.last_failure.symbol && ` · ${status.scanner.last_failure.symbol}`}
        {status.scanner.last_failure.interval && ` · ${status.scanner.last_failure.interval}`}
      </p>}
    </details>
  </>
  return <section className={`masterTradeLiveSection originalLiveCard ${className ?? ''}`} aria-label="Kais Original V2 LIVE">
    <header className="originalStatusHero"><div><span className="masterTradeLiveKicker">KAIS ORIGINAL V2 · CANLI</span>
      <h2>{!statusVerified ? 'Durum doğrulanamadı' : entriesReady ? 'Çalışıyor – sinyal bekleniyor' : status.enabled ? 'Çalışıyor – girişler engelli' : status.paused ? 'Durdu – yeni girişler kapalı' : 'Canlı işlemi 3 adımda başlat'}</h2>
      <p>{status.enabled ? `${status.universe?.requested_count ?? status.symbols.length} coin · Son tarama: ${status.scanner?.last_scan_at ? new Date(status.scanner.last_scan_at).toLocaleTimeString('tr-TR', {hour: '2-digit', minute: '2-digit'}) : 'bekleniyor'} · ${remainingText}` : status.pause_reason || 'Sıradaki adımı tamamla; açık pozisyonların korumaları değiştirilmez.'}</p>
      {status.enabled && !entriesReady && <p role="status">{entryBlocker?.reason || 'Giriş kontrolleri doğrulanamadı.'}</p>}
    </div>{status.enabled && <button type="button" onClick={() => void act(true)} disabled={busy}>Stratejiyi durdur</button>}</header>
    {overview}
    {fillLocked && <div role="alert">
      <p>Dolum güvenlik kilidi · {status.fill_lock?.symbol}. Yeni LIVE girişler OWNER onayına kadar kapalıdır.</p>
      <p>{status.fill_lock?.violations.join(' · ')}</p>
      <p>Stop mesafesi: {status.fill_lock?.evidence?.stop_percent ?? '—'}% · Risk: {status.fill_lock?.evidence?.risk_usdt ?? '—'} / {status.fill_lock?.evidence?.risk_budget_usdt ?? '—'} USDT · Teminat: {status.fill_lock?.evidence?.margin_usdt ?? '—'} USDT</p>
      <p>Açık pozisyon değiştirilmedi. Kilidi kaldırmak pozisyon riskini düzeltmez ve otomasyonu başlatmaz.</p>
      <label>Kurtarma için 2FA kodu<input type="password" data-private="true" autoComplete="one-time-code" value={code} onChange={event => setCode(event.target.value)} disabled={busy}/></label>
      <label>DOLUM RİSKİNİ İNCELEDİM yazın<input value={approval} onChange={event => setApproval(event.target.value)} disabled={busy}/></label>
      <button type="button" onClick={() => void approve()} disabled={busy || approval !== 'DOLUM RİSKİNİ İNCELEDİM' || code.length < 6}>Dolum kilidini onayımla kaldır</button>
    </div>}
    {status.daily?.locked && <p>Bugün günlük kayıp kilidi nedeniyle yeni giriş kapalı.</p>}
    <details className="originalSettings" open={!status.enabled}><summary>{status.enabled ? 'Risk ayarlarını değiştir' : 'İlk kurulum · 3 adım'}</summary>
    <form onSubmit={submit} aria-label="Risk ayarları ve kurulum">
      <div className="originalSetupStep" data-active={!connected} data-complete={connected}><h3>{connected ? '✓' : '1'} · Binance hesabını bağla</h3><a href="/master-trade?tab=baglanti">Bağlantı ayarları →</a></div>
      <fieldset className="originalSetupStep" data-active={connected && !riskApproved} data-complete={riskApproved} disabled={busy || !connected}><legend>{riskApproved ? '✓' : '2'} · Risk ayarlarını onayla</legend>
      <div className="masterTradeLiveSetupGrid">
        <label>İşlem riski (%)<input type="number" min="0.5" max="10" step="0.01" required value={risk} onChange={event => changeRisk(event.target.value)} disabled={busy}/></label>
        <label>Günlük kayıp sınırı (%)<input type="number" min={Math.max(1, selectedRisk || 1)} max="25" step="0.01" required value={dailyLoss} onChange={event => changeRisk(event.target.value, true)} disabled={busy}/></label>
        <label>İlk N sembolü tara<input type="number" min="10" max="100" step="1" required value={symbolCount} onChange={event => setSymbolCount(event.target.value)} disabled={busy || status.enabled}/></label>
        <label>Oturum süresi<select aria-label="Oturum süresi" value={status.enabled ? String(status.session?.hours ?? 1) : hours} onChange={event => setHours(event.target.value)} disabled={busy || status.enabled}>
          {[1, 4, 12, 24].map(value => <option key={value} value={value}>{value} saat</option>)}
        </select></label>
      </div>
      {!validRisk && <p role="alert">İşlem riski %0.5–10, günlük sınır %1–25 olmalı. Günlük sınır işlem riskinden küçük olamaz.</p>}
      {wallet === null ? <p role="alert">Güncel Binance bakiyesi doğrulanamadı. Kayıp tahmini ve risk onayı için bağlantıyı yenileyin.</p> : validRisk && <div className="originalRiskWarning" data-level={riskLevel(selectedRisk)} data-testid="original-risk-warning">
        {riskLevel(selectedRisk) !== 'NORMAL' && <strong>{warningNames[riskLevel(selectedRisk)]} · İşlem riski %{selectedRisk}</strong>}
        <p>Gerçek bakiye: {usdt(wallet)} · Seçilen işlem riskiyle tahmini kayıp: <b>{usdt(wallet * selectedRisk / 100)}</b></p>
        <p data-level={dailyLevel(selectedDaily)}>Günlük sınır: <b>{usdt((openingWallet ?? wallet) * selectedDaily / 100)}</b> · %{selectedDaily} · {warningNames[dailyLevel(selectedDaily)]}{openingWallet !== wallet ? ' (gün başlangıç bakiyesine göre)' : ' (güncel bakiye ile tahmin; girişte gün başlangıcı doğrulanır)'}</p>
        <div className="originalRiskSeries" aria-label="Bileşik kayıp karşılaştırması">
          {[0, 5, 10].map(count => {
            const selectedBalance = wallet * (1 - selectedRisk / 100) ** count
            const baselineBalance = wallet * 0.99 ** count
            return <div key={count}>
              <b>{count === 0 ? 'Şimdi' : `${count} kayıp sonrası`}</b>
              <span>Seçilen %{selectedRisk}: {usdt(selectedBalance)}</span>
              <div className="originalRiskBar" role="meter" aria-label={`Seçilen risk ${count} kayıp sonrası bakiye`} aria-valuemin={0} aria-valuemax={wallet} aria-valuenow={selectedBalance}><i style={{width: `${selectedBalance / wallet * 100}%`}}/></div>
              <span>%1 ile: {usdt(baselineBalance)}</span>
              <div className="originalRiskBar baseline" role="meter" aria-label={`Yüzde 1 risk ${count} kayıp sonrası bakiye`} aria-valuemin={0} aria-valuemax={wallet} aria-valuenow={baselineBalance}><i style={{width: `${baselineBalance / wallet * 100}%`}}/></div>
            </div>
          })}
        </div>
        <p>Bu hesap seçilen yüzdeyle, her kayıptan sonra kalan bakiye üzerinden yapılır; uygulanmış işlem sonucu veya garanti değildir.</p>
        <p>Bu stratejide üst üste 5–10 kayıp olağan dışı değildir. Kayıplar tahminidir, ani fiyat hareketlerinde daha büyük olabilir.</p>
        {selectedRisk > 5 && <p><b>Ani fiyat hareketlerinde kayıp daha büyük olabilir.</b></p>}
        {status.risk_limits && <>
          {wallet * selectedRisk / 100 > status.risk_limits.max_loss_per_trade_usdt && <p data-testid="original-risk-cap">Seçtiğin risk, mevcut Original/V25 işlem kaybı tavanı nedeniyle en fazla %{(status.risk_limits.max_loss_per_trade_usdt / wallet * 100).toLocaleString('tr-TR', {maximumFractionDigits: 4})} ({usdt(status.risk_limits.max_loss_per_trade_usdt)}) olarak uygulanabilir. Bu bir aday öncesi üst sınırdır.</p>}
          {(openingWallet ?? wallet) * selectedDaily / 100 > status.risk_limits.daily_loss_limit_usdt && <p>Seçtiğin günlük sınır {usdt((openingWallet ?? wallet) * selectedDaily / 100)} olsa da diğer günlük güvenlik kapıları en fazla {usdt(status.risk_limits.daily_loss_limit_usdt)} kayıpta yeni girişleri durdurur.</p>}
          <p>Teminat {status.risk_limits.min_margin_usdt}–{status.risk_limits.max_margin_usdt} USDT, kaldıraç {status.risk_limits.leverage}x ve Binance minimumları korunur. Adaya göre risk daha da düşebilir; minimumlar karşılanmazsa işlem açılmaz.</p>
        </>}
        {selectedRisk > 2 && selectedRisk <= 5 && <label><input type="checkbox" checked={riskAcknowledged && understood} onChange={event => {setRiskAcknowledged(event.target.checked); setUnderstood(event.target.checked)}} disabled={busy}/> Bu riski anladım</label>}
        {selectedRisk > 5 && <label>{highRiskPhrase} yazın<input value={highRiskConfirmation} onChange={event => setHighRiskConfirmation(event.target.value)} disabled={busy} autoComplete="off"/></label>}
      </div>}
      {status.last_risk_adjustment && <p data-testid="original-applied-risk">Son aday {status.last_risk_adjustment.symbol}: seçilen %{status.last_risk_adjustment.selected_percent}, hesaplanan risk %{status.last_risk_adjustment.effective_percent.toLocaleString('tr-TR', {maximumFractionDigits: 4})} ({usdt(status.last_risk_adjustment.effective_loss_usdt)}). {status.last_risk_adjustment.reasons.length > 0 && `Seçtiğin risk şu sebeple düşürüldü: ${status.last_risk_adjustment.reasons.join(' · ')}.`}</p>}
      {!(selectedRisk > 2 && selectedRisk <= 5) && <label className="originalUnderstanding"><input type="checkbox" checked={understood} onChange={event => setUnderstood(event.target.checked)} disabled={busy}/> Gerçek parayla işlem açılacağını ve kayıp olabileceğini anladım</label>}
      <button type="button" className="masterTradeLivePrimary" onClick={() => void saveRisk()} disabled={busy || !status.available || !validRisk || wallet === null || !consentReady || !understood || blocked}>Onayla ve devam et · Risk ayarını kaydet</button>
      </fieldset>
      <div className="originalSetupStep" data-active={connected && riskApproved} data-complete={status.enabled}>
        <h3>{status.enabled ? '✓' : '3'} · Stratejiyi başlat</h3>
        <p>Onay ve ARM arka planda tamamlanır. Her adayda sunucu güvenlik kontrolleri yeniden uygulanır.</p>
        <button type="submit" className="masterTradeLivePrimary" disabled={busy || status.enabled || fillLocked || !status.available || blocked || (!prepareStart && !armed) || automationActive || !riskApproved || !validRisk || wallet === null || !consentReady}>{renewing ? 'Original V2 oturumunu yenile' : 'Kais Original V2’yi başlat'}</button>
      </div>
    </form>
    </details>
    {notice && <p role="status">{notice}</p>}
    <details className="originalDetails" data-testid="original-details"><summary>Detaylar · kontroller {entryChecks.filter(check => check.passed).length}/{entryChecks.length}</summary>
    {technicalDetails}
    <button type="button" className="masterTradeLiveSecondary" onClick={() => void diagnose()} disabled={diagnosing || busy} title="Kalıcı kayıt teşhisi"><Database size={16} aria-hidden="true"/> {diagnosing ? 'Kayıt kontrol ediliyor...' : 'Kalıcı kayıt teşhisi'}</button>
    {diagnosticError && <p role="alert">{diagnosticError}</p>}
    {diagnostics && <div role="status" data-testid="original-persistence-diagnostics">
      <p>{diagnosticMessages[diagnostics.diagnosis] || 'Teşhis sonucu için sunucu incelemesi gerekli.'}</p>
      <p>Kayıt: {diagnostics.record_exists === null ? 'Doğrulanamadı' : diagnostics.record_exists ? 'Var' : 'Yok'} · Original kaydı: {diagnostics.original_record_exists === null ? 'Doğrulanamadı' : diagnostics.original_record_exists ? 'Var' : 'Yok'}</p>
      <p>Veritabanı revision: {diagnostics.database_revision ?? 'Doğrulanamadı'} · Yerel revision: {diagnostics.local_revision}</p>
      <p>Son yazma: {diagnostics.last_write ? `${diagnostics.last_write.result} · revision ${diagnostics.last_write.submitted_revision}` : 'Bu süreçte ölçülmedi'} · Sürüm: {diagnostics.build_commit?.slice(0, 7) || 'Bilinmiyor'}</p>
      <p>Son beklenen yazma: {diagnostics.last_awaited_write ? `${diagnostics.last_awaited_write.result} · revision ${diagnostics.last_awaited_write.submitted_revision}` : 'Bu süreçte ölçülmedi'}</p>
    </div>}
    </details>
    <div className="originalEmergencyBar"><button type="button" onClick={() => void act(true)} disabled={busy || !status.enabled && !automationActive}>Acil durdur · Yeni girişleri kapat</button><small>Açık pozisyon ve Binance Stop/TP korunur.</small></div>
  </section>
}
