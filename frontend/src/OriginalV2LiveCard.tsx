import {useEffect, useRef, useState, type FormEvent} from 'react'
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

export type OriginalV2LiveStatus = {
  available: boolean
  enabled: boolean
  paused?: boolean
  pause_reason?: string | null
  risk_percent: number
  daily_loss_percent: number
  session?: {hours: number; started_at?: string | null; expires_at?: string | null; remaining_seconds: number; authority_limited: boolean; warning_sent: boolean} | null
  daily?: {day: string; loss: number; limit: number; locked: boolean} | null
  symbols: string[]
  universe?: {requested_count: number; selected_count: number; eligible_count: number; updated_at?: string | null} | null
  scanner?: {stage?: string; last_cycle_at?: string; last_scan_at?: string; scanned_symbol_count?: number; scanned_symbols?: string[]; candidate_count?: number} | null
  fill_lock?: {id: string; active: boolean; symbol: string; violations: string[]; evidence?: {
    stop_percent?: string; risk_usdt?: string; risk_budget_usdt?: string; margin_usdt?: string
  }} | null
}

type Props = {
  className?: string
  status: OriginalV2LiveStatus
  armed: boolean
  automationActive: boolean
  blocked: boolean
  request: (path: string, body?: Record<string, unknown>) => Promise<unknown>
  readDiagnostics: () => Promise<OriginalPersistenceDiagnostics>
  refresh: () => Promise<unknown>
  notice: string
  onNotice: (message: string) => void
}

export default function OriginalV2LiveCard({className, status, armed, automationActive, blocked, request, readDiagnostics, refresh, notice, onNotice}: Props) {
  const [risk, setRisk] = useState(String(status.risk_percent))
  const [dailyLoss, setDailyLoss] = useState(String(status.daily_loss_percent))
  const [symbolCount, setSymbolCount] = useState(String(status.universe?.requested_count ?? 40))
  const [hours, setHours] = useState('1')
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
  const remainingText = remaining > 0 ? `${Math.floor(remaining / 3600)} sa ${Math.floor(remaining % 3600 / 60)} dk ${remaining % 60} sn` : status.session?.expires_at ? 'Süre doldu / giriş kapalı' : 'Oturum yok'
  const renewing = !status.enabled && !!status.session?.expires_at && Date.parse(status.session.expires_at) <= Date.now()
  const [code, setCode] = useState('')
  const [confirmation, setConfirmation] = useState('')
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
      onNotice('Dolum kilidi onayınla kaldırıldı; otomasyon başlamadı. Yeniden açmak için ARM ve güncel 2FA gerekir.')
      await refresh()
    } catch (error: unknown) {
      onNotice(error instanceof Error ? error.message : 'Dolum kilidi kaldırılamadı.')
    } finally {
      setBusy(false)
      inFlight.current = false
    }
  }
  const act = async (stop: boolean) => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    onNotice('')
    try {
      const payload = await request(stop ? '/original-v2/stop' : '/original-v2/start', stop ? undefined : {
        confirmation, totp_code: code, risk_percent: Number(risk), daily_loss_percent: Number(dailyLoss),
        symbol_count: Number(symbolCount), session_hours: Number(hours),
      })
      if (!payload || typeof payload !== 'object' || !('enabled' in payload) || payload.enabled !== !stop) {
        throw new Error('Original V2 LIVE sonucu doğrulanamadı. LIVE durumunu yenileyin.')
      }
      setCode('')
      setConfirmation('')
      onNotice(stop ? 'Yeni girişler durduruldu; açık pozisyon ve Stop/TP korumaları korundu.' : 'Original V2 LIVE gözetimli oturumu açıldı.')
      await refresh()
    } catch (error: unknown) {
      onNotice(error instanceof Error ? error.message : 'Original V2 LIVE isteği başarısız.')
    } finally {
      setBusy(false)
      inFlight.current = false
    }
  }
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    void act(false)
  }
  return <section className={`masterTradeLiveSection ${className ?? ''}`} aria-label="Kais Original V2 LIVE">
    <header><div><span className="masterTradeLiveKicker">OWNER · ORIGINAL V2</span><h3>Kais Original V2 LIVE</h3></div>
      <strong>{status.enabled ? 'OTURUM AÇIK' : status.paused ? 'GÜVENLİK KİLİDİ' : 'KAPALI'}</strong></header>
    <div className="masterTradeLiveControlMeta" data-testid="original-runtime-status">
      <span><small>STRATEJİ</small><b>Original V2</b></span>
      <span><small>SEÇİLEN EVREN</small><b>{status.symbols.length} / {status.universe?.requested_count ?? 40} sembol</b></span>
      <span><small>ONAYLI RİSK</small><b>%{status.risk_percent}</b></span>
      <span><small>TARAMA</small><b>{!status.enabled ? 'Otomasyon kapalı' : originalScanLabels[status.scanner?.stage || ''] || 'İlk tarama bekleniyor'}</b></span>
      <span><small>SON TAMAMLANAN TARAMA</small><b>{status.scanner?.last_scan_at ? new Date(status.scanner.last_scan_at).toLocaleString('tr-TR') : 'Henüz tamamlanmadı'}</b></span>
      <span><small>SON DÖNGÜ</small><b>{status.scanner?.last_cycle_at ? new Date(status.scanner.last_cycle_at).toLocaleString('tr-TR') : 'Henüz ölçülmedi'}</b></span>
      <span><small>TARANAN / ADAY</small><b>{status.scanner?.scanned_symbol_count ?? 0} / {status.scanner?.candidate_count ?? 0}</b></span>
      <span><small>YENİ GİRİŞ</small><b>{!status.enabled || blocked || !armed || fillLocked ? 'Kapalı / engelli' : 'Her emir için sunucu kontrolü'}</b></span>
      <span><small>OTURUM BİTİŞİ</small><b>{status.session?.expires_at ? new Date(status.session.expires_at).toLocaleString('tr-TR') : 'Oturum yok'}</b></span>
      <span><small>KALAN SÜRE</small><b data-testid="original-session-remaining">{remainingText}</b></span>
      <span><small>SEÇİLEN OTURUM</small><b>{status.session?.hours ?? 1} saat{status.session?.authority_limited ? ' · Yetki süresiyle sınırlı' : ''}</b></span>
    </div>
    <p>USDT-M Futures · 24 saatlik hacimde ilk N; hacim en az 1.000.000 USDT, liste yaşı en az 30 gün, spread en fazla 8 bps. Evren UTC gününde bir kez yenilenir. Strateji: 3x · en fazla %6 stop · TP1’de %60 kapanır; kalan TP3 veya ilk stopta kapanır. Stop taşınmaz.</p>
    <small>Son taramadaki semboller: {(status.scanner?.scanned_symbols ?? status.symbols).map(symbol => symbol.replace('USDT', '')).join(' · ') || 'Günlük seçim bekleniyor'}</small>
    <small>Uygun: {status.universe?.eligible_count ?? 0} · Seçilen: {status.universe?.selected_count ?? status.symbols.length} · Liste yenileme: {status.universe?.updated_at ? new Date(status.universe.updated_at).toLocaleString('tr-TR') : 'Henüz yapılmadı'}</small>
    {status.pause_reason && <p role="status">{status.pause_reason}</p>}
    {fillLocked && <div role="alert">
      <p>Dolum güvenlik kilidi · {status.fill_lock?.symbol}. Yeni LIVE girişler OWNER onayına kadar kapalıdır.</p>
      <p>{status.fill_lock?.violations.join(' · ')}</p>
      <p>Stop mesafesi: {status.fill_lock?.evidence?.stop_percent ?? '—'}% · Risk: {status.fill_lock?.evidence?.risk_usdt ?? '—'} / {status.fill_lock?.evidence?.risk_budget_usdt ?? '—'} USDT · Teminat: {status.fill_lock?.evidence?.margin_usdt ?? '—'} USDT</p>
      <p>Açık pozisyon değiştirilmedi. Kilidi kaldırmak pozisyon riskini düzeltmez ve otomasyonu başlatmaz.</p>
      <label>DOLUM RİSKİNİ İNCELEDİM yazın<input value={approval} onChange={event => setApproval(event.target.value)} disabled={busy}/></label>
      <button type="button" onClick={() => void approve()} disabled={busy || approval !== 'DOLUM RİSKİNİ İNCELEDİM' || code.length < 6}>Dolum kilidini onayımla kaldır</button>
    </div>}
    {status.daily && <p>Günlük kayıp: {status.daily.loss.toFixed(2)} / {status.daily.limit.toFixed(2)} USDT{status.daily.locked ? ' · Bugün yeni giriş kapalı' : ''}</p>}
    <form onSubmit={submit}>
      <div className="masterTradeLiveSetupGrid">
        <label>İşlem riski (%)<input type="number" min="0.01" max="1" step="0.01" required value={risk} onChange={event => setRisk(event.target.value)} disabled={busy || status.enabled}/></label>
        <label>Günlük kayıp sınırı (%)<input type="number" min="0.01" max="5" step="0.01" required value={dailyLoss} onChange={event => setDailyLoss(event.target.value)} disabled={busy || status.enabled}/></label>
        <label>İlk N sembolü tara<input type="number" min="10" max="100" step="1" required value={symbolCount} onChange={event => setSymbolCount(event.target.value)} disabled={busy || status.enabled}/></label>
        <label>Oturum süresi<select aria-label="Oturum süresi" value={status.enabled ? String(status.session?.hours ?? 1) : hours} onChange={event => setHours(event.target.value)} disabled={busy || status.enabled}>
          {[1, 4, 12, 24].map(value => <option key={value} value={value}>{value} saat</option>)}
        </select></label>
        <label>2FA kodu<input type="password" data-private="true" autoComplete="one-time-code" required minLength={6} maxLength={32} value={code} onChange={event => setCode(event.target.value)} disabled={busy || status.enabled}/></label>
        <label>ORIGINAL V2 CANLI yazın<input required value={confirmation} onChange={event => setConfirmation(event.target.value)} disabled={busy || status.enabled}/></label>
      </div>
      <p>Önce mevcut LIVE bağlantısı, izinler, politika onayı ve ARM adımını tamamlayın. Borsa ve hesap limitleri ayrıca uygulanır. Açık pozisyon otomatik kapatılmaz.</p>
      <div className="masterTradeLiveSetupFooter">
        <button type="submit" className="masterTradeLivePrimary" disabled={busy || status.enabled || fillLocked || !status.available || blocked || !armed || automationActive || confirmation !== 'ORIGINAL V2 CANLI'}>{renewing ? 'Original V2 oturumunu yenile' : 'Original V2 LIVE aç'}</button>
        <button type="button" className="masterTradeLiveSecondary" onClick={() => void act(true)} disabled={busy || !status.enabled}>Acil durdur · Yeni girişleri kapat</button>
      </div>
    </form>
    {notice && <p role="status">{notice}</p>}
    <button type="button" className="masterTradeLiveSecondary" onClick={() => void diagnose()} disabled={diagnosing || busy} title="Kalıcı kayıt teşhisi"><Database size={16} aria-hidden="true"/> {diagnosing ? 'Kayıt kontrol ediliyor...' : 'Kalıcı kayıt teşhisi'}</button>
    {diagnosticError && <p role="alert">{diagnosticError}</p>}
    {diagnostics && <div role="status" data-testid="original-persistence-diagnostics">
      <p>{diagnosticMessages[diagnostics.diagnosis] || 'Teşhis sonucu için sunucu incelemesi gerekli.'}</p>
      <p>Kayıt: {diagnostics.record_exists === null ? 'Doğrulanamadı' : diagnostics.record_exists ? 'Var' : 'Yok'} · Original kaydı: {diagnostics.original_record_exists === null ? 'Doğrulanamadı' : diagnostics.original_record_exists ? 'Var' : 'Yok'}</p>
      <p>Veritabanı revision: {diagnostics.database_revision ?? 'Doğrulanamadı'} · Yerel revision: {diagnostics.local_revision}</p>
      <p>Son yazma: {diagnostics.last_write ? `${diagnostics.last_write.result} · revision ${diagnostics.last_write.submitted_revision}` : 'Bu süreçte ölçülmedi'} · Sürüm: {diagnostics.build_commit?.slice(0, 7) || 'Bilinmiyor'}</p>
      <p>Son beklenen yazma: {diagnostics.last_awaited_write ? `${diagnostics.last_awaited_write.result} · revision ${diagnostics.last_awaited_write.submitted_revision}` : 'Bu süreçte ölçülmedi'}</p>
    </div>}
  </section>
}
