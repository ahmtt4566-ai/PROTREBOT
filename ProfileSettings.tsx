import {useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode} from 'react'
import {Activity, ArrowLeft, ArrowRight, CalendarDays, Check, ChevronDown, Copy, KeyRound, LoaderCircle, LockKeyhole, LogOut, Mail, Monitor, Pencil, Save, ShieldCheck, Trash2, UserRound, X} from 'lucide-react'
import {QRCodeSVG} from 'qrcode.react'
import {accountDate, accountInitials, accountRequest, TRADING_TIMEFRAMES, type AccountMutation, type AccountOverview, type SensitiveProof, type TradingPreferences} from './account-settings-api'
import './profile-settings.css'
import {PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, passwordPolicyError, passwordRules} from './password-policy'

type DialogKind = 'profile' | 'email' | 'password' | 'two-factor' | 'sessions' | 'activity' | 'close'
type Notice = {kind: 'success' | 'error'; text: string}
const failureMessage = (error: unknown) => error instanceof Error ? error.message : 'Hesap işlemi tamamlanamadı.'

function SettingsDialog({title, children, onClose, busy}: {title: string; children: ReactNode; onClose: () => void; busy: boolean}) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const previous = document.activeElement
    const element = ref.current
    element?.showModal()
    return () => {element?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus()}
  }, [])
  return <dialog ref={ref} className="accountDialog" aria-labelledby="account-dialog-title" onCancel={event => {event.preventDefault(); if (!busy) onClose()}}>
    <header><h2 id="account-dialog-title">{title}</h2><button type="button" aria-label="Pencereyi kapat" disabled={busy} onClick={onClose}><X/></button></header>{children}
  </dialog>
}

export function AccountRecoveryScreen({codes, onSaved}: {codes: string[]; onSaved: () => void}) {
  return <main className="accountSettings"><div className="accountWorkspace"><section className="accountCard accountRecovery"><ShieldCheck/><h1>2FA etkinleştirildi</h1><p>Eski oturumlar kapatıldı. Tek kullanımlık kurtarma kodlarını güvenli bir yere kaydedin; sonra 2FA ile yeniden giriş yapın.</p><p>Kurulumda kullandığınız kod tekrar kullanılamaz. Girişte sonraki Authenticator kodunu veya bir kurtarma kodunu kullanın.</p><ul data-private="true">{codes.map(value => <li key={value}>{value}</li>)}</ul><button className="accountPrimary" onClick={onSaved}>Kodları kaydettim</button></section></div></main>
}

export default function ProfileSettings({onLogout, onSessionEnded, onEnrollmentComplete}: {onLogout: () => void; onSessionEnded: () => void; onEnrollmentComplete: (codes: string[]) => void}) {
  const [data, setData] = useState<AccountOverview | null>(null)
  const [preferences, setPreferences] = useState<TradingPreferences | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const actionInFlight = useRef(false)
  const [dialog, setDialog] = useState<DialogKind | null>(null)
  const [name, setName] = useState('')
  const [newEmail, setNewEmail] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [proof, setProof] = useState<SensitiveProof>({})
  const [enrollment, setEnrollment] = useState<{secret: string; otpauth_uri: string} | null>(null)
  const [code, setCode] = useState('')
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([])
  const [confirmation, setConfirmation] = useState('')
  const [symbolInput, setSymbolInput] = useState('')
  const [emailToken, setEmailToken] = useState(() => new URLSearchParams(location.search).get('email_token') || '')
  const [emailTokenVisible, setEmailTokenVisible] = useState(Boolean(emailToken))
  const refresh = useCallback(async (signal?: AbortSignal, replacePreferences = false) => {
    const overview = await accountRequest<AccountOverview>('/account/overview', {signal})
    if (signal?.aborted) return
    setData(overview)
    setName(overview.user.display_name)
    setPreferences(current => current === null || replacePreferences ? overview.preferences : current)
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    void refresh(controller.signal).catch(error => {if (!controller.signal.aborted) setNotice({kind: 'error', text: failureMessage(error)})}).finally(() => {if (!controller.signal.aborted) setLoading(false)})
    return () => controller.abort()
  }, [refresh])
  useEffect(() => {
    if (!emailToken) return
    const url = new URL(location.href)
    url.searchParams.delete('email_token')
    history.replaceState(history.state, '', `${url.pathname}${url.search}${url.hash}`)
  }, [emailToken])
  const closeDialog = () => {
    setDialog(null); setProof({}); setNewPassword(''); setConfirmPassword('')
    setEnrollment(null); setCode(''); setRecoveryCodes([]); setConfirmation('')
  }
  const openDialog = (kind: DialogKind) => {
    closeDialog(); setDialog(kind); setNotice(null)
    setName(data?.user.display_name || ''); setNewEmail('')
  }
  const run = async (work: () => Promise<void>) => {
    if (actionInFlight.current) return
    actionInFlight.current = true
    setBusy(true); setNotice(null)
    try {await work()} catch (error) {setNotice({kind: 'error', text: failureMessage(error)})}
    finally {actionInFlight.current = false; setBusy(false)}
  }
  const mutate = async (path: string, body: object, message: string, method = 'POST') => {
    const result = await accountRequest<AccountMutation>(path, {method, body: JSON.stringify(body)})
    if (result.reauthenticate) {closeDialog(); onSessionEnded(); return}
    await refresh(undefined, path === '/account/preferences')
    setNotice({kind: 'success', text: message})
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    void run(async () => {
      if (dialog === 'profile') {await mutate('/account/profile', {display_name: name}, 'Profil kaydedildi.', 'PATCH'); closeDialog()}
      else if (dialog === 'email') {await mutate('/account/email/request', {new_email: newEmail, ...proof}, 'Yeni adres için doğrulama e-postası gönderildi. Mevcut adresiniz henüz değişmedi.'); closeDialog()}
      else if (dialog === 'password') {
        const error = passwordPolicyError(newPassword)
        if (error) throw new Error(error)
        if (newPassword !== confirmPassword) throw new Error('Yeni parolalar eşleşmiyor.')
        await mutate('/account/password', {...proof, new_password: newPassword, confirm_password: confirmPassword}, 'Parola güncellendi; diğer cihazlardaki oturumlar kapatıldı.')
        closeDialog()
      } else if (dialog === 'close') {
        await mutate('/account/close', {...proof, confirmation}, 'Hesap kapatıldı.')
      } else if (dialog === 'two-factor') {
        if (data?.security.two_factor_enabled) {await mutate('/account/2fa/disable', proof, 'İki aşamalı doğrulama kapatıldı.'); closeDialog()}
        else if (!enrollment) {
          setEnrollment(await accountRequest<{secret: string; otpauth_uri: string}>('/account/2fa/setup', {method: 'POST', body: JSON.stringify(proof)}))
          setProof({})
        } else {
          const result = await accountRequest<{recovery_codes: string[]; reauthenticate?: boolean}>('/account/2fa/enable', {method: 'POST', body: JSON.stringify({code})})
          if (result.reauthenticate) {onEnrollmentComplete(result.recovery_codes); return}
          setRecoveryCodes(result.recovery_codes); setEnrollment(null); setCode(''); await refresh()
        }
      }
    })
  }
  const requestReauthEmail = () => void run(async () => {
    const result = await accountRequest<{challenge_id: string}>('/account/reauth/email', {method: 'POST', body: '{}'})
    setProof(current => ({...current, challenge_id: result.challenge_id, email_code: ''}))
    setNotice({kind: 'success', text: 'Mevcut doğrulanmış adresinize doğrulama kodu gönderildi.'})
  })
  const proofFields = () => <fieldset className="accountProof"><legend>Kimliğinizi doğrulayın</legend>
    {data?.user.auth_methods.includes('password') ? <label>Mevcut parola<input data-private="true" type="password" autoComplete="current-password" required value={proof.current_password || ''} onChange={event => setProof({...proof, current_password: event.target.value})}/></label>
      : <><p>Google hesabınız için mevcut doğrulanmış e-posta adresine kod göndererek devam edin.</p><button type="button" disabled={busy || !data?.security.email_delivery_available} onClick={requestReauthEmail}>Doğrulama kodu gönder</button><label>E-posta kodu<input data-private="true" autoComplete="one-time-code" required value={proof.email_code || ''} onChange={event => setProof({...proof, email_code: event.target.value})}/></label></>}
    {data?.security.two_factor_enabled && <label>2FA veya kurtarma kodu<input data-private="true" autoComplete="one-time-code" required value={proof.totp_code || ''} onChange={event => setProof({...proof, totp_code: event.target.value})}/></label>}
  </fieldset>
  const savePreferences = (event: FormEvent) => {
    event.preventDefault()
    if (!preferences) return
    void run(async () => {
      await mutate('/account/preferences', preferences, 'İşlem tercihleri kaydedildi. Açık işlemler ve canlı güvenlik ayarları değiştirilmedi.', 'PATCH')
      window.dispatchEvent(new Event('protrebot-preferences-refresh'))
    })
  }
  const addSymbol = () => {
    const symbol = symbolInput.trim().toUpperCase()
    if (!/^[A-Z0-9]{1,30}USDT$/.test(symbol)) {setNotice({kind: 'error', text: 'BTCUSDT gibi bir USDT sembolü yazın.'}); return}
    if (preferences && !preferences.symbols.includes(symbol)) setPreferences({...preferences, symbols: [...preferences.symbols, symbol]})
    setSymbolInput('')
  }
  const titles: Record<DialogKind, string> = {profile: 'Profili düzenle', email: 'E-posta adresini değiştir', password: 'Parolayı değiştir', 'two-factor': 'İki aşamalı doğrulama', sessions: 'Aktif oturumlar', activity: 'Son aktiviteler', close: 'Hesabı kapat'}
  const activity = (all = false) => <ol className="accountActivities">{(all ? data?.activity : data?.activity.slice(0, 5))?.map(item => <li key={item.id}><Activity/><div><b>{item.message}</b><small>{item.kind}</small></div><time dateTime={item.created_at}>{accountDate(item.created_at)}</time></li>)}</ol>
  return <main className="accountSettings">
    <header className="accountTopbar"><a href="/" className="accountBrand">kais<b>trade</b></a><button type="button" className="accountTopIdentity" aria-label="Profil bilgilerini düzenle" aria-haspopup="dialog" aria-expanded={dialog === 'profile'} disabled={busy || !data} onClick={() => openDialog('profile')}><i>{accountInitials(data?.user.display_name || '')}</i><span>{data?.user.display_name || '—'}</span><ChevronDown/></button><button type="button" onClick={onLogout} aria-label="Çıkış"><LogOut/></button></header>
    <div className="accountWorkspace"><a href="/" className="accountBack"><ArrowLeft/>Ana sayfaya dön</a>
      <div className="accountHeading"><div><h1>Profil &amp; Ayarlar</h1><p>Hesabınızı, güvenliğinizi ve işlem tercihlerinizi yönetin.</p></div>{data && <span className={`accountBadge ${data.user.active ? 'positive' : 'muted'}`}><i/>{data.user.active ? 'Hesap aktif' : 'Hesap kapalı'}</span>}</div>
      {notice && !dialog && <p className={`accountNotice ${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}>{notice.text}</p>}
      {loading && <p role="status"><LoaderCircle className="spin"/>Hesap bilgileri yükleniyor...</p>}
      {!loading && !data && <button type="button" disabled={busy} onClick={() => void run(() => refresh())}>Yeniden dene</button>}
      {data && preferences && <>
        {emailTokenVisible && <section className="accountCard accountEmailConfirm"><h2>Yeni e-posta adresini doğrula</h2><p>Bu bağlantı yeni adresinizi onaylar; doğrulamadan sonra yeniden giriş gerekir.</p><button disabled={busy || !emailToken} onClick={() => void run(async () => {await mutate('/account/email/confirm', {token: emailToken}, 'E-posta doğrulandı.'); setEmailToken(''); setEmailTokenVisible(false)})}>E-posta değişikliğini onayla</button></section>}
        <div className="accountGrid">
          <section className="accountCard accountOverview"><h2>Hesap özeti</h2><div className="accountIdentity"><i className="accountAvatar">{accountInitials(data.user.display_name)}</i><div><h3>{data.user.display_name}</h3><p>{data.user.email}</p><span className={`accountBadge ${data.user.email_verified ? 'positive' : 'muted'}`}>{data.user.email_verified ? <Check/> : <Mail/>}{data.user.email_verified ? 'E-posta doğrulandı' : 'Doğrulama bekleniyor'}</span></div><button disabled={busy} onClick={() => openDialog('profile')}><Pencil/>Profili düzenle</button></div>
            <dl className="accountFacts"><div><dt><LockKeyhole/>Hesap kimliği</dt><dd>{data.user.id.slice(0, 12)}<button aria-label="Hesap kimliğini kopyala" onClick={() => void run(async () => {await navigator.clipboard.writeText(data.user.id); setNotice({kind: 'success', text: 'Hesap kimliği kopyalandı.'})})}><Copy/></button></dd></div><div><dt><CalendarDays/>Üyelik tarihi</dt><dd>{accountDate(data.user.created_at)}</dd></div><div><dt><Activity/>Son giriş</dt><dd>{accountDate(data.user.last_login)}</dd></div><div><dt><UserRound/>Hesap rolü</dt><dd>{data.user.role === 'OWNER' ? 'Yönetici' : 'Kullanıcı'}</dd></div></dl>
          </section>
          <section className="accountCard accountPlan"><h2>Mevcut paket <span className="accountBadge muted">{data.subscription.status}</span></h2><div><div><strong>{data.subscription.plan}</strong><p>{data.subscription.is_premium ? 'Premium erişimi mevcut' : 'Premium erişimi yok'}</p><small>Bitiş: {accountDate(data.subscription.expires_at)}</small>{data.user.role === 'OWNER' && <small>Yönetici erişimi ücretli abonelik anlamına gelmez.</small>}</div><ul>{data.subscription.features.map(feature => <li key={feature.key}>{feature.included ? <Check/> : <LockKeyhole/>}<span>{feature.label}</span><small>{feature.included ? 'Dahil' : 'Yükselt'}</small></li>)}</ul></div><a className="accountPrimary" href="/pricing">Paketleri görüntüle<ArrowRight/></a></section>
          <section className="accountCard"><h2>Güvenlik</h2><div className="accountSecurityRows">
            <div><i><KeyRound/></i><span><b>Parola</b><small>{data.user.auth_methods.includes('password') ? `Son değişiklik: ${accountDate(data.user.password_changed_at)}` : 'Google hesabı · yerel parola yok'}</small></span><button disabled={busy} onClick={() => openDialog('password')}>{data.user.auth_methods.includes('password') ? 'Parolayı değiştir' : 'Parola oluştur'}</button></div>
            <div><i><ShieldCheck/></i><span><b>İki aşamalı doğrulama</b><small>{data.security.two_factor_enabled ? 'Etkin' : 'Hesabınıza ek koruma ekleyin'}</small></span><button disabled={busy} onClick={() => openDialog('two-factor')}>{data.security.two_factor_enabled ? '2FA yönet' : '2FA etkinleştir'}</button></div>
            <div><i><Mail/></i><span><b>E-posta adresi</b><small>{data.user.email_verified ? 'Doğrulandı' : 'Doğrulama gerekli'}</small></span><button disabled={busy || !data.security.email_delivery_available} onClick={() => openDialog('email')}>E-postayı değiştir</button></div>
            {!data.user.email_verified && <button disabled={busy || !data.security.email_delivery_available} onClick={() => void run(() => mutate('/account/verification/resend', {}, 'Doğrulama e-postası gönderildi.'))}>Doğrulama e-postasını tekrar gönder</button>}
            <div><i><Monitor/></i><span><b>Aktif oturumlar</b><small>{data.security.active_sessions} oturum</small></span><button disabled={busy} onClick={() => openDialog('sessions')}>Oturumları yönet</button></div>
          </div>{!data.security.email_delivery_available && <p className="accountMuted">E-posta hizmeti kullanılamıyor. E-posta değişikliği ve e-posta ile doğrulama şu anda yapılamaz.</p>}
            {data.pending_email && <div className="accountPending"><b>Doğrulama bekleyen yeni adres</b><span>{data.pending_email.email}</span><small>Son geçerlilik: {accountDate(data.pending_email.expires_at)}</small><div><button disabled={busy || !data.security.email_delivery_available} onClick={() => void run(() => mutate('/account/email/resend', {}, 'Yeni adrese doğrulama tekrar gönderildi.'))}>Tekrar gönder</button><button disabled={busy} onClick={() => void run(() => mutate('/account/email/cancel', {}, 'E-posta değişikliği iptal edildi.'))}>İptal et</button></div></div>}
          </section>
          <section className="accountCard"><h2>İşlem tercihleri</h2><form className="accountPreferences" onSubmit={savePreferences}>
            <label>Varsayılan işlem bölümü<select value={preferences.trading_mode} onChange={event => setPreferences({...preferences, trading_mode: event.target.value === 'AUTO' ? 'AUTO' : 'MANUAL'})}><option value="MANUAL">Manuel işlem</option><option value="AUTO">Auto Trade bölümü</option></select></label>
            <label>Varsayılan zaman dilimi<select value={preferences.timeframe} onChange={event => setPreferences({...preferences, timeframe: event.target.value})}>{TRADING_TIMEFRAMES.map(value => <option key={value}>{value}</option>)}</select></label>
            <label>Varsayılan borsa<select value={preferences.exchange} onChange={() => undefined}><option value="BINANCE">Binance</option></select></label>
            <label>Tercih edilen işlem riski (%)<input type="number" min="0.1" max="1" step="0.1" required value={preferences.risk_per_trade} onChange={event => setPreferences({...preferences, risk_per_trade: event.target.valueAsNumber})}/></label>
            <label className="accountSymbolsLabel">Varsayılan semboller<div className="accountSymbols">{preferences.symbols.map(symbol => <span key={symbol}>{symbol}<button type="button" aria-label={`${symbol} kaldır`} disabled={busy} onClick={() => setPreferences({...preferences, symbols: preferences.symbols.filter(value => value !== symbol)})}><X/></button></span>)}</div></label>
            <div className="accountSymbolInput"><input aria-label="Sembol ekle" placeholder="BTCUSDT" value={symbolInput} maxLength={34} onChange={event => setSymbolInput(event.target.value)}/><button type="button" disabled={busy} onClick={addSymbol}>Ekle</button></div>
            <p>Yalnızca yeni ekranların başlangıç tercihleri. Auto Trade açılmaz; açık işlemler, risk politikası ve onay kapıları değişmez.</p><button className="accountPrimary" disabled={busy}><Save/>Tercihleri kaydet</button>
          </form></section>
          <section className="accountCard"><h2>Son aktiviteler<button onClick={() => openDialog('activity')}>Tümünü görüntüle<ArrowRight/></button></h2>{data.activity.length ? activity() : <p className="accountMuted">Kayıtlı hesap aktivitesi yok.</p>}</section>
          <section className="accountCard accountDanger"><h2>Tehlikeli işlemler</h2><div><Trash2/><span><b>Hesabı kapat</b><p>Giriş ve erişim hakkınız sonlandırılır. Kayıtlarınız yetkili yönetici panelinde saklanmaya devam eder; borsadaki emirler otomatik kapatılmaz.</p></span><button disabled={busy || !data.security.can_close_account} onClick={() => openDialog('close')}>Hesabı kapat</button></div>{data.security.close_blocker && <p className="accountMuted">{data.security.close_blocker}</p>}</section>
        </div>
      </>}
    </div>
    {dialog && data && <SettingsDialog title={titles[dialog]} busy={busy} onClose={closeDialog}>
      {notice && <p className={`accountNotice ${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'}>{notice.text}</p>}
      {dialog === 'sessions' ? <><p>Diğer oturumları kapatmak bu cihazdaki oturumunuzu korur.</p><button disabled={busy} onClick={() => void run(() => mutate('/account/sessions/revoke-others', {}, 'Diğer oturumlar kapatıldı.'))}>Diğer oturumları kapat</button><ul className="accountSessions">{data.sessions.map(session => <li key={session.id}><Monitor/><div><b>{session.device} · {session.browser}{session.current && ' · Bu cihaz'}</b><small>Son etkinlik: {accountDate(session.last_seen_at)}</small><small>Başlangıç: {accountDate(session.created_at)} · Bitiş: {accountDate(session.expires_at)}</small></div><button disabled={busy} onClick={() => void run(() => mutate('/account/sessions/revoke', {session_id: session.id}, 'Oturum kapatıldı.'))}>Oturumu kapat</button></li>)}</ul>{!data.sessions.length && <p>Kayıtlı aktif oturum yok.</p>}</>
        : dialog === 'activity' ? <>{data.activity.length ? activity(true) : <p>Kayıtlı hesap aktivitesi yok.</p>}</>
        : recoveryCodes.length ? <div className="accountRecovery"><ShieldCheck/><h3>2FA etkinleştirildi</h3><p>Bu tek kullanımlık kurtarma kodlarını güvenli bir yere kaydedin. Bu pencere kapandıktan sonra tekrar gösterilmez.</p><ul data-private="true">{recoveryCodes.map(value => <li key={value}>{value}</li>)}</ul><button onClick={closeDialog}>Kodları kaydettim</button></div>
        : <form onSubmit={submit} className="accountForm">
          {dialog === 'profile' && <label>Ad soyad<input autoComplete="name" minLength={2} maxLength={80} required value={name} onChange={event => setName(event.target.value)}/></label>}
          {dialog === 'email' && <label>Yeni e-posta<input type="email" autoComplete="email" required maxLength={180} value={newEmail} onChange={event => setNewEmail(event.target.value)}/></label>}
          {dialog === 'password' && <><label>Yeni parola<input data-private="true" aria-label="Yeni parola" type="password" autoComplete="new-password" minLength={PASSWORD_MIN_LENGTH} maxLength={PASSWORD_MAX_LENGTH} required value={newPassword} onChange={event => setNewPassword(event.target.value)}/><div className="passwordRules">{passwordRules(newPassword).map(([label,passed]) => <span className={passed ? 'passed' : 'missing'} key={label}>{passed ? '✓' : '•'} {label}</span>)}</div></label><label>Yeni parola tekrar<input data-private="true" type="password" autoComplete="new-password" minLength={PASSWORD_MIN_LENGTH} maxLength={PASSWORD_MAX_LENGTH} required value={confirmPassword} onChange={event => setConfirmPassword(event.target.value)}/></label><p>Bu cihazdaki oturumunuz korunur; yalnızca hesabınızın diğer cihazlardaki oturumları kapanır.</p><a href="/forgot-password">Parolamı unuttum</a></>}
          {dialog === 'close' && <><p>Hesabınız kapatılacak; verileriniz yönetici panelinde kalacaktır.</p><label>Onay için HESABI KAPAT yazın<input required pattern="HESABI KAPAT" value={confirmation} onChange={event => setConfirmation(event.target.value)}/></label></>}
          {dialog === 'two-factor' && enrollment ? <div className="accountEnrollment" data-private="true"><QRCodeSVG value={enrollment.otpauth_uri} size={180}/><p>Authenticator uygulamanızla tarayın veya anahtarı girin:</p><code>{enrollment.secret}</code><label>Authenticator kodu<input data-private="true" autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]{6}" required value={code} onChange={event => setCode(event.target.value)}/></label></div>
            : dialog !== 'profile' && proofFields()}
          <button className="accountPrimary" disabled={busy}>{busy ? <LoaderCircle className="spin"/> : <Check/>}{dialog === 'two-factor' ? data.security.two_factor_enabled ? '2FA devre dışı bırak' : enrollment ? 'Kodu doğrula ve etkinleştir' : 'Kurulumu başlat' : dialog === 'close' ? 'Hesabı kapat' : dialog === 'email' ? 'Doğrulama gönder' : 'Kaydet'}</button>
        </form>}
    </SettingsDialog>}
  </main>
}
