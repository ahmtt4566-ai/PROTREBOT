import {useRef, useState, type FormEvent} from 'react'

export type OriginalV2LiveStatus = {
  available: boolean
  enabled: boolean
  paused?: boolean
  pause_reason?: string | null
  risk_percent: number
  daily_loss_percent: number
  daily?: {day: string; loss: number; limit: number; locked: boolean} | null
  symbols: string[]
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
  refresh: () => Promise<unknown>
  notice: string
  onNotice: (message: string) => void
}

export default function OriginalV2LiveCard({className, status, armed, automationActive, blocked, request, refresh, notice, onNotice}: Props) {
  const [risk, setRisk] = useState(String(status.risk_percent))
  const [dailyLoss, setDailyLoss] = useState(String(status.daily_loss_percent))
  const [code, setCode] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [approval, setApproval] = useState('')
  const inFlight = useRef(false)
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
      <strong>{status.enabled ? 'AÇIK' : status.paused ? 'GÜVENLİK KİLİDİ' : 'KAPALI'}</strong></header>
    <p>8 sembol · 3x · en fazla %6 stop. TP1’de %60 kapanır; kalan TP3 veya ilk stopta kapanır. Stop taşınmaz.</p>
    <small>{status.symbols.map(symbol => symbol.replace('USDT', '')).join(' · ')}</small>
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
        <label>2FA kodu<input type="password" data-private="true" autoComplete="one-time-code" required minLength={6} maxLength={32} value={code} onChange={event => setCode(event.target.value)} disabled={busy || status.enabled}/></label>
        <label>ORIGINAL V2 CANLI yazın<input required value={confirmation} onChange={event => setConfirmation(event.target.value)} disabled={busy || status.enabled}/></label>
      </div>
      <p>Önce mevcut LIVE bağlantısı, izinler, politika onayı ve ARM adımını tamamlayın. Borsa ve hesap limitleri ayrıca uygulanır. Açık pozisyon otomatik kapatılmaz.</p>
      <div className="masterTradeLiveSetupFooter">
        <button type="submit" className="masterTradeLivePrimary" disabled={busy || fillLocked || !status.available || blocked || !armed || automationActive || confirmation !== 'ORIGINAL V2 CANLI'}>Original V2 LIVE aç</button>
        <button type="button" className="masterTradeLiveSecondary" onClick={() => void act(true)} disabled={busy || !status.enabled}>Acil durdur · Yeni girişleri kapat</button>
      </div>
    </form>
    {notice && <p role="status">{notice}</p>}
  </section>
}
