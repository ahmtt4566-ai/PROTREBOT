import { createPortal } from 'react-dom'
import { type FormEvent, type ReactNode, useEffect, useRef, useState } from 'react'
import { Activity, ArrowRight, BarChart3, Bitcoin, CircleDollarSign, Coins, Eye, EyeOff, KeyRound, LoaderCircle, LockKeyhole, LogOut, Mail, MailCheck, Pause, Play, ShieldAlert, ShieldCheck, UserRound, Wrench, Zap } from 'lucide-react'
import { API_BASE, COOKIE_SESSION_PREFIX, USER_SESSION_KEY, clearDemoCredentials, clearUserSessionToken, saveUserSessionToken, userSessionToken } from './api'
import AdminPanel from './AdminPanel'
import ProfileSettings, {AccountRecoveryScreen} from './ProfileSettings'
import { AUTH_PANEL_MARKETS, LIVE_MARKET_CONFIG } from './live-market-config'
import TickerTape from './TickerTape'
import TopMovers from './TopMovers'
import { type LiveTickerData, type LiveTickerStatus, useLiveTickers } from './use-live-tickers'
import { useTopMovers } from './use-top-movers'
import '@fontsource-variable/plus-jakarta-sans'
import './auth.css'

type User = { id:string; email:string; display_name:string; role:string; active:boolean; email_verified?:boolean }
type Session = { user:User; maintenance?:{mode:string} }
type Mode = 'login'|'register'|'bootstrap'|'forgot'|'reset'|'verify'|'google-consent'|'mfa'
type AuthResult = {token: string; user: User; remember?: boolean} | {mfa_required: true; challenge_id: string}


// Only Playwright's dedicated `--mode test` run bypasses login (it mocks every API response).
// Regular local dev must go through the real login/register flow so a genuine session token exists.
const PLAYWRIGHT_TEST_MODE_AUTO_ACCESS = import.meta.env.MODE === 'test'
const LOCAL_OWNER_SETUP_PAGE = import.meta.env.DEV && API_BASE === '/api' && ['localhost','127.0.0.1','[::1]'].includes(window.location.hostname) && window.location.pathname === '/local-owner-setup'

function AuthSparkline({points,positive}:{points:number[];positive:boolean}) {
  const coordinates = points.map((point,index) => `${4 + index * 10},${point}`).join(' ')
  return <svg className={`authSparkline ${positive ? 'isPositive' : 'isNegative'}`} viewBox="0 0 98 40" role="img" aria-label="Piyasa eğilim grafiği"><polyline points={coordinates} fill="none" vectorEffect="non-scaling-stroke"/></svg>
}

function formatPanelPrice(symbol:string,price:number) {
  return symbol === 'USDTTRY' ? `₺${price.toFixed(4)}` : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:2}).format(price)
}

function AuthMarketCards({data,status}:{data:LiveTickerData;status:LiveTickerStatus}) {
  return <div className="authMarketGrid">{AUTH_PANEL_MARKETS.map(config => { const ticker = data[config.symbol]; const history = ticker?.history.slice(-10) || []; const low = Math.min(...history); const high = Math.max(...history); const range = high - low || 1; const points = history.map(value => 8 + ((high - value) / range) * 24); return <article className={`authMarketCard ${ticker?.flash ? `isFlash-${ticker.flash}` : ''}`} key={config.symbol} aria-label={`${config.displaySymbol} ${config.name} piyasa kartı`}><div className="authMarketTop"><div><b>{config.displaySymbol}</b><small>{config.name}</small></div><div className="authMarketQuoteStatus">{ticker && <span className={ticker.changePercent >= 0 ? 'isPositive' : 'isNegative'}>{ticker.changePercent >= 0 ? '▲' : '▼'} {Math.abs(ticker.changePercent).toFixed(2)}%</span>}<i className={`authMarketStatus status-${status}`}><i/> {status === 'live' ? 'CANLI' : status === 'offline' ? 'ÇEVRİMDIŞI' : 'BAĞLANIYOR'}</i></div></div><div className="authMarketBottom">{ticker ? <strong aria-live="off">{formatPanelPrice(config.symbol,ticker.price)}</strong> : <i className="authMarketSkeleton"/>}{ticker ? <AuthSparkline points={points} positive={ticker.changePercent >= 0}/> : <i className="authSparklineSkeleton"/>}</div><small className="authMarketNote">Veriler Binance'ten alınır, bilgi amaçlıdır, yatırım tavsiyesi değildir.</small></article> })}</div>
}

function AuthCoinScreen({data,status}:{data:LiveTickerData;status:LiveTickerStatus}) {
  const [paused,setPaused] = useState(false)
  return <div className="authCoinScreen">
    <header><div><span className={`authCoinLive status-${status}`}/><h2>Canlı piyasa</h2><small>{status === 'live' ? 'CANLI' : status === 'offline' ? 'ÇEVRİMDIŞI' : 'BAĞLANIYOR'}</small></div><button type="button" onClick={() => setPaused(value => !value)} aria-label={paused ? 'Coin akışını devam ettir' : 'Coin akışını duraklat'} title={paused ? 'Devam et' : 'Duraklat'} aria-pressed={paused}>{paused ? <Play/> : <Pause/>}</button></header>
    <div className="authCoinViewport"><div className="authCoinTrack" data-paused={paused}>{[false,true].map(duplicate => <div className="authCoinGroup" key={String(duplicate)} aria-hidden={duplicate || undefined}>{LIVE_MARKET_CONFIG.map(config => { const ticker = data[config.symbol]; const Icon = config.symbol === 'BTCUSDT' ? Bitcoin : config.kind === 'fiat' ? CircleDollarSign : Coins; return <div className="authCoinQuote" key={config.symbol}><span className="authCoinAvatar" data-symbol={config.symbol}><Icon aria-hidden="true"/></span><div><b>{config.displaySymbol}</b><small>{config.name}</small></div><div className="authCoinNumbers">{ticker ? <><strong>{formatPanelPrice(config.symbol,ticker.price)}</strong><span className={ticker.changePercent >= 0 ? 'isPositive' : 'isNegative'}>{ticker.changePercent >= 0 ? '+' : ''}{ticker.changePercent.toFixed(2)}%</span></> : <span className="authCoinPending">—</span>}</div></div> })}</div>)}</div></div>
  </div>
}

function AuthIntroPanel({data,status,movers,premium = false}:{data:LiveTickerData;status:LiveTickerStatus;movers:ReturnType<typeof useTopMovers>;premium?:boolean}) {
  return <section className="authIntro">
    <div className="authIntroCopy"><h1 className="authLogoHeading"><span className="authBrandMark" role="link" tabIndex={0} aria-label="Ana sayfaya git" onClick={() => window.location.assign('/')} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); window.location.assign('/') } }}><img src="/kaistrade-logo.png" alt="KaiStrade"/></span></h1>{premium && <h2 className="authPremiumTitle"><span className="authWordmark" aria-label="KAİSTRADE"><b>KAİS</b><i>TRADE</i></span><span>Piyasanın ritmini yakalayın.</span></h2>}<p>Hızlı kararlar, berrak analiz ve güvenli işlem akışı için ihtiyacınız olan her şey tek çalışma alanında.</p></div>
    {!premium && <div className="authFeatureList"><span><Zap/> Hızlı işlem</span><span><BarChart3/> Gelişmiş analiz</span><span><ShieldCheck/> Güvenli altyapı</span></div>}
    <AuthMarketCards data={data} status={status}/>
    <TopMovers movers={movers} autoScroll={premium}/>
    {premium && <div className="authFeatureList"><span><Zap/> Hızlı işlem</span><span><BarChart3/> Gelişmiş analiz</span><span><ShieldCheck/> Güvenli altyapı</span></div>}
    <div className="authTrustBadges"><span><ShieldCheck/> Hesap yönetimi</span><span><KeyRound/> E-posta ile giriş</span><span><Activity/> Risk bilgilendirmesi</span></div>
    <div className="authCandlePattern" aria-hidden="true"><i/><i/><i/><i/><i/><i/><i/><i/></div>
  </section>
}

function LoginMarketShell({children}:{children:(market:{data:LiveTickerData;status:LiveTickerStatus;movers:ReturnType<typeof useTopMovers>}) => ReactNode}) {
  const live = useLiveTickers(LIVE_MARKET_CONFIG.map(config => config.symbol))
  const movers = useTopMovers()
  return <div className="authRoot"><TickerTape data={live.data} status={live.status}/>{children({data:live.data,status:live.status,movers})}<footer className="authFooter"><span>© 2026 KalsTrade</span><a href="/privacy">Gizlilik Politikası</a><a href="/terms">Kullanım Şartları</a><a href="/risk">Risk Uyarısı</a></footer></div>
}

function detail(payload:unknown):string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const value = (payload as {detail:unknown}).detail
    if (typeof value === 'string') return value
  }
  return 'İşlem tamamlanamadı. Bilgilerinizi kontrol edip tekrar deneyin.'
}

class AuthRequestError extends Error {
  constructor(message:string, readonly status:number, readonly retryAfter = 0, readonly emailDelivery = false) { super(message) }
}

function TermsConsent({accepted,error,onChange}:{accepted:boolean;error:string;onChange:(value:boolean)=>void}) {
  return <><label className={`authCheck ${error ? 'authCheckError' : ''}`}><input type="checkbox" checked={accepted} onChange={event => onChange(event.target.checked)}/><span><a href="/terms" onClick={event => event.stopPropagation()}>Kullanım Koşulları</a> ve <a href="/privacy" onClick={event => event.stopPropagation()}>Gizlilik Politikası'nı</a> kabul ediyorum.</span></label>{error && <p className="authInlineError" role="alert">{error}</p>}</>
}

async function request<T>(path:string, options:RequestInit = {}):Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body) headers.set('Content-Type','application/json')
  const response = await fetch(`${API_BASE}/v22${path}`, {...options, headers})
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const retryHeader = Number(response.headers.get('Retry-After'))
    const retryAfter = Number.isFinite(retryHeader) && retryHeader > 0 ? Math.min(Math.ceil(retryHeader),86400) : 0
    if (response.headers.get('X-Email-Delivery-Error') === '1') throw new AuthRequestError(detail(payload), response.status, retryAfter, true)
    if (response.status === 409) throw new AuthRequestError('Bu e-posta adresiyle zaten bir hesap bulunuyor.', response.status)
    if (response.status >= 500) throw new AuthRequestError('Sunucuda geçici bir sorun oluştu. Lütfen biraz sonra tekrar deneyin.', response.status)
    throw new AuthRequestError(detail(payload), response.status, retryAfter)
  }
  return payload as T
}

function strength(password:string):{label:string;score:number} {
  const score = passwordRules(password).filter(([,passed]) => passed).length
  return {score, label:score < 3 ? 'Zayıf' : score < 5 ? 'Orta' : 'Güçlü'}
}

function passwordRules(password:string) {
  return [
    ['En az 10 karakter', password.length >= 10],
    ['Büyük harf', /[A-Z]/.test(password)],
    ['Küçük harf', /[a-z]/.test(password)],
    ['Rakam', /\d/.test(password)],
    ['Sembol', /[^A-Za-z0-9]/.test(password)],
  ] as const
}

function MaintenanceScreen({mode}:{mode:string}) {
  const emergency = mode === 'EMERGENCY'
  return <main className={`authMaintenance${emergency ? ' emergency' : ''}`}>
    <section className="authMaintenancePanel" role="status" aria-live="polite">
      <div className="authMaintenanceMark">{emergency ? <ShieldAlert/> : <Wrench/>}</div>
      <span className="authMaintenanceBrand"><img src="/kaistrade-logo.png" alt="KaiStrade"/></span>
      <h1>{emergency ? 'SİSTEM GEÇİCİ OLARAK KULLANILAMIYOR' : 'SİSTEM BAKIMDA'}</h1>
      <p>{emergency ? 'Sistem şu anda korunuyor.' : 'Kısa bir sistem bakımı yapıyoruz.'}</p>
      <div className="authMaintenanceStatus"><i/>{emergency ? 'Koruma modu aktif' : 'Bakım devam ediyor'}</div>
      <p className="authMaintenanceNote">{emergency ? 'Yeni işlem girişleri geçici olarak kapalı. Mevcut pozisyonlar ve koruma mekanizmaları aktif çalışmaya devam ediyor.' : 'Yeni işlem girişleri geçici olarak kapalı. Mevcut pozisyonlar ve risk korumaları aktif.'}</p>
      <p className="authMaintenanceHint">Lütfen kısa süre sonra tekrar deneyin.</p>
    </section>
  </main>
}



export default function AuthGate({children}:{children:ReactNode}) {
  const [token,setToken] = useState(userSessionToken())
  const [session,setSession] = useState<Session|null>(null)
  const [mode,setMode] = useState<Mode>(() => new URLSearchParams(location.search).has('mfa_challenge') ? 'mfa' : window.location.pathname === '/register' ? 'register' : window.location.pathname === '/forgot-password' ? 'forgot' : window.location.pathname === '/reset-password' ? 'reset' : window.location.pathname === '/verify-email' ? 'verify' : 'login')
  const isRegistration = mode === 'register' || mode === 'bootstrap'
  const isGoogleConsent = mode === 'google-consent'
  const [ownerSetupAvailable,setOwnerSetupAvailable] = useState(false)
  const [busy,setBusy] = useState(true)
  const [sessionLoading,setSessionLoading] = useState(true)
  const [submitFailed,setSubmitFailed] = useState(false)
  const [message,setMessage] = useState('Oturum doğrulanıyor…')
  const [showPassword,setShowPassword] = useState(false)
  const [showConfirmPassword,setShowConfirmPassword] = useState(false)
  const [remember,setRemember] = useState(true)
  const [googleReturn] = useState(() => {
    const parameters = new URLSearchParams(window.location.search)
    return {result:parameters.get('google_login'),remember:parameters.get('google_remember') === '1'}
  })
  const [login,setLogin] = useState({email:'',password:''})
  const [mfaChallenge,setMfaChallenge] = useState(() => new URLSearchParams(location.search).get('mfa_challenge') || '')
  const [mfaCode,setMfaCode] = useState('')
  const [enrollmentRecoveryCodes,setEnrollmentRecoveryCodes] = useState<string[]>([])
  const [register,setRegister] = useState({display_name:'',email:'',password:'',confirm_password:'',terms_accepted:false})
  const [email,setEmail] = useState('')
  const queryToken = new URLSearchParams(window.location.search).get('token') || ''
  const [resetToken,setResetToken] = useState(window.location.pathname === '/reset-password' ? queryToken : '')
  const [verificationLinkToken,setVerificationLinkToken] = useState(window.location.pathname === '/verify-email' ? queryToken : '')
  const [verificationStatusToken,setVerificationStatusToken] = useState('')
  const [verificationInput,setVerificationInput] = useState('')
  const [resetPassword,setResetPassword] = useState({password:'',confirm_password:'',totp_code:''})
  const [fieldErrors,setFieldErrors] = useState<Record<string,string>>({})
  const [termsError,setTermsError] = useState('')
  const [verificationNotice,setVerificationNotice] = useState(false)
  const [verificationMailFailed,setVerificationMailFailed] = useState(false)
  const [resendSeconds,setResendSeconds] = useState(0)
  const registrationFormRef = useRef<HTMLFormElement>(null)
  const autoVerificationStarted = useRef(false)
  const [autoVerifying,setAutoVerifying] = useState(false)
  const sessionRoleRef = useRef<string|null>(null)
  const sessionMaintenanceRef = useRef<string|null>(null)
  const [expiredMemberMaintenance,setExpiredMemberMaintenance] = useState<string|null>(null)
  const [memberMenuOpen,setMemberMenuOpen] = useState(false)
  const memberTriggerRef = useRef<HTMLButtonElement>(null)
  const memberMenuRef = useRef<HTMLDivElement>(null)
  const [memberMenuPosition,setMemberMenuPosition] = useState({top:0,left:12})
  const [profileHeaderSlot,setProfileHeaderSlot] = useState<HTMLElement | null>(null)

  useEffect(() => {
    if (resendSeconds <= 0) return
    const timer = window.setInterval(() => setResendSeconds(seconds => Math.max(0,seconds - 1)),1000)
    return () => window.clearInterval(timer)
  },[resendSeconds])

  const resendVerification = async () => {
    if (busy || resendSeconds > 0 || !verificationStatusToken) return
    setBusy(true); setSubmitFailed(false); setResendSeconds(60)
    try {
      const result = await request<{message:string;retry_after:number}>('/auth/resend-verification',{method:'POST',body:JSON.stringify({token:verificationStatusToken})})
      if (!result || typeof result.message !== 'string' || !Number.isFinite(result.retry_after) || result.retry_after < 0) throw new AuthRequestError('Doğrulama mailinin sonucu doğrulanamadı. Lütfen tekrar dene.',502)
      setMessage(result.message); setVerificationNotice(true); setVerificationMailFailed(false); setResendSeconds(Math.max(60,result.retry_after))
    } catch (error) {
      setSubmitFailed(true); setVerificationNotice(false); setVerificationMailFailed(true)
      setMessage(error instanceof Error ? error.message : 'Doğrulama maili gönderilemedi.')
      if (error instanceof AuthRequestError && error.retryAfter) setResendSeconds(Math.max(60,error.retryAfter))
    } finally { setBusy(false) }
  }

  useEffect(() => {
    if (!LOCAL_OWNER_SETUP_PAGE) {
      setOwnerSetupAvailable(false)
      setMode(current => current === 'bootstrap' ? 'login' : current)
      return
    }
    let active = true
    request<{setup_required:boolean;auth_available:boolean}>('/public')
      .then(result => { if (active) setOwnerSetupAvailable(result.setup_required && result.auth_available) })
      .catch(() => { if (active) setOwnerSetupAvailable(false) })
    return () => { active = false }
  },[])

  useEffect(() => {
    let currentApp:HTMLElement|null = null
    let appObserver:MutationObserver|undefined
    const updateProfileHeaderSlot = () => {
      const slot = currentApp?.querySelector<HTMLElement>('.v26HomeHeader .v26HeaderProfileSlot') || null
      setProfileHeaderSlot(current => current === slot ? current : slot)
    }
    const observeApp = () => {
      const app = document.querySelector<HTMLElement>('#root > main.v26App')
      if (app !== currentApp) {
        appObserver?.disconnect()
        currentApp = app
        if (app) {
          appObserver = new MutationObserver(updateProfileHeaderSlot)
          appObserver.observe(app,{childList:true})
        }
      }
      updateProfileHeaderSlot()
    }
    const root = document.getElementById('root')
    const rootObserver = new MutationObserver(observeApp)
    if (root) rootObserver.observe(root,{childList:true})
    observeApp()
    window.addEventListener('resize',observeApp)
    return () => {
      rootObserver.disconnect()
      appObserver?.disconnect()
      window.removeEventListener('resize',observeApp)
    }
  },[busy])

  useEffect(() => {
    if (!memberMenuOpen) return
    const updateMenuPosition = () => {
      const trigger = memberTriggerRef.current
      if (!trigger) return
      const rect = trigger.getBoundingClientRect()
      const menuWidth = window.innerWidth <= 600 ? 300 : 276
      setMemberMenuPosition({top:rect.bottom + 9,left:Math.min(Math.max(12,rect.right - menuWidth),Math.max(12,window.innerWidth - menuWidth - 12))})
    }
    const closeMenu = (event:MouseEvent) => {
      const target = event.target as Node
      if (!memberTriggerRef.current?.contains(target) && !memberMenuRef.current?.contains(target)) setMemberMenuOpen(false)
    }
    const closeOnEscape = (event:KeyboardEvent) => { if (event.key === 'Escape') setMemberMenuOpen(false) }
    updateMenuPosition()
    document.addEventListener('mousedown',closeMenu)
    document.addEventListener('keydown',closeOnEscape)
    window.addEventListener('resize',updateMenuPosition)
    window.addEventListener('scroll',updateMenuPosition,true)
    return () => { document.removeEventListener('mousedown',closeMenu); document.removeEventListener('keydown',closeOnEscape); window.removeEventListener('resize',updateMenuPosition); window.removeEventListener('scroll',updateMenuPosition,true) }
  },[memberMenuOpen])

  const loadSession = async (value:string, persist?:boolean) => {
    try {
      const current = await request<Session>('/session', value ? {headers:{Authorization:`Bearer ${value}`}} : {})
      const marker = `${COOKIE_SESSION_PREFIX}${current.user.id}`
      saveUserSessionToken(marker, persist ?? Boolean(localStorage.getItem(USER_SESSION_KEY)))
      setToken(marker)
      setSession(current); setMessage('')
    } catch (error) {
      clearUserSessionToken(); setToken(''); setSession(null)
      setMessage(error instanceof Error ? error.message : 'Oturum açmak için devam edin.')
    } finally { setBusy(false); setSessionLoading(false) }
  }

  useEffect(() => {
    const parameters = new URLSearchParams(window.location.search)
    const googleResult = googleReturn.result
    const googleRemember = googleReturn.remember
    if (googleResult || mfaChallenge) {
      parameters.delete('google_login'); parameters.delete('google_remember')
      parameters.delete('mfa_challenge')
      const query = parameters.toString()
      history.replaceState(null,'',`${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`)
    }
    const restore = async () => {
      if (mfaChallenge) {setMode('mfa'); setRemember(googleRemember); setBusy(false); setSessionLoading(false); return}
      if (googleResult === 'consent') {
        try {
          const pending = await request<{email:string;display_name:string}>('/auth/google/pending')
          setRegister({display_name:pending.display_name,email:pending.email,password:'',confirm_password:'',terms_accepted:false})
          setMode('google-consent'); setMessage('Google hesabınız doğrulandı. Kaydı tamamlamak için koşulları kabul edin.')
        } catch (error) {
          setSubmitFailed(true); setMessage(error instanceof Error ? error.message : 'Google kayıt isteği doğrulanamadı.')
        } finally { setBusy(false); setSessionLoading(false) }
        return
      }
      await loadSession(token,googleResult === 'success' ? googleRemember : undefined)
      if (googleResult && googleResult !== 'success') {
        const errors:Record<string,string> = {
          invalid_attempt:'Google giriş isteği geçersiz, kullanılmış veya süresi dolmuş. Yeniden deneyin.',
          cancelled:'Google girişi iptal edildi.',
          account_link_required:'Bu e-postayla mevcut hesabınız var. E-posta/parola ile giriş yapın; Google hesabı otomatik bağlanmaz.',
          account_unavailable:'Bu hesap Google girişi için kullanılamıyor.',
          service_unavailable:'Google giriş servisi şu anda kullanılamıyor.',
        }
        setSubmitFailed(true); setMessage(errors[googleResult] || 'Google kimliği doğrulanamadı. Yeniden deneyin.')
      }
    }
    void restore()
  }, [])

  useEffect(() => { sessionRoleRef.current = session?.user.role ?? null; sessionMaintenanceRef.current = session?.maintenance?.mode ?? null }, [session])

  // An expired member can keep the maintenance screen, but never the private session.
  useEffect(() => {
    if (!token) return
    const pollMaintenance = async () => {
      if (document.visibilityState === 'hidden') return
      try {
        const response = await fetch(`${API_BASE}/v22/session`,{headers:{Authorization:`Bearer ${token}`}})
        if (response.status === 401) {
          const mode = sessionMaintenanceRef.current
          if (sessionRoleRef.current !== 'OWNER' && (mode === 'MAINTENANCE' || mode === 'EMERGENCY')) setExpiredMemberMaintenance(mode)
          finishSession()
          return
        }
        if (!response.ok) { console.warn('Session refresh failed:', response.status); return }
        const current = await response.json() as Session
        setSession(previous => previous ? {...previous,maintenance:current.maintenance} : previous)
      } catch (error) {
        console.warn('Session refresh failed:', error instanceof Error ? error.name : 'SessionRefreshError')
      }
    }
    const timer = window.setInterval(() => void pollMaintenance(),45000)
    return () => window.clearInterval(timer)
  },[token])

  useEffect(() => {
    if (mode !== 'verify' || autoVerificationStarted.current) return
    if (!verificationLinkToken && !verificationStatusToken) return
    autoVerificationStarted.current = true
    setAutoVerifying(true)
    let active = true
    const verifyEmailLink = async () => {
      try {
        await request('/auth/verify-email',{method:'POST',body:JSON.stringify({token:verificationLinkToken})})
        if (active) { setVerificationLinkToken(''); setMessage('E-posta doğrulandı. Şimdi giriş yapabilirsiniz.'); setMode('login') }
      } catch (error) {
        if (active) setMessage(error instanceof Error ? error.message : 'İşlem başarısız.')
      }
      if (active) setAutoVerifying(false)
    }
    const checkStatus = async () => {
      try {
        const result = await request<{verified:boolean}>(`/auth/verification-status?token=${encodeURIComponent(verificationStatusToken)}`)
        if (active && result.verified) {
          setVerificationStatusToken(''); setMessage('E-posta doğrulandı. Şimdi giriş yapabilirsiniz.'); setMode('login')
        }
      } catch (error) {
        if (active) setMessage(error instanceof Error ? error.message : 'İşlem başarısız.')
      } finally {
        if (active) setAutoVerifying(false)
      }
    }
    if (verificationLinkToken) void verifyEmailLink()
    else {
      void checkStatus()
      const interval = window.setInterval(() => void checkStatus(),1000)
      return () => { active = false; window.clearInterval(interval) }
    }
    return () => { active = false }
  },[mode,verificationLinkToken,verificationStatusToken])

  const validateForm = ():boolean => {
    const errors:Record<string,string> = {}
    const emailValue = mode === 'login' ? login.email : isRegistration || mode === 'forgot' ? (isRegistration ? register.email : email) : ''
    if (mode === 'login' || isRegistration || mode === 'forgot') {
      if (!emailValue.trim()) errors.email = 'E-posta girin.'
      else if (!/^\S+@\S+\.\S+$/.test(emailValue)) errors.email = 'E-posta geçersiz.'
    }
    if (mode === 'login' && !login.password) errors.password = 'Parola girin.'
    if (mode === 'mfa' && !mfaCode.trim()) errors.mfa = 'Authenticator veya kurtarma kodunu girin.'
    if (isRegistration) {
      const missingPasswordRules = passwordRules(register.password).filter(([,passed]) => !passed).map(([label]) => label)
      if (!register.password) errors.password = 'Şifrenizi girin.'
      else if (!passwordRules(register.password).every(([,passed]) => passed)) errors.password = `Eksik parola şartları: ${missingPasswordRules.join(', ')}.`
      if (!register.confirm_password) errors.confirm_password = 'Şifrenizi tekrar girin.'
      else if (register.password !== register.confirm_password) errors.confirm_password = 'Şifreler eşleşmiyor.'
      if (!register.terms_accepted) setTermsError('Devam etmek için kullanım koşullarını ve gizlilik politikasını kabul etmelisiniz.')
      else setTermsError('')
    }
    if (isGoogleConsent) setTermsError(register.terms_accepted ? '' : 'Devam etmek için kullanım koşullarını ve gizlilik politikasını kabul etmelisiniz.')
    if (mode === 'verify' && !verificationInput && !verificationStatusToken && !verificationLinkToken) errors.verification = 'Doğrulama kodunu girin.'
    setFieldErrors(errors)
    return !Object.keys(errors).length && (!(isRegistration || isGoogleConsent) || register.terms_accepted)
  }

  const startGoogle = async () => {
    if (busy) return
    setBusy(true); setSubmitFailed(false); setMessage('')
    try {
      const result = await request<{authorization_url:string}>('/auth/google/start',{method:'POST',body:JSON.stringify({remember})})
      const target = new URL(result.authorization_url)
      if (target.origin !== 'https://accounts.google.com' || target.pathname !== '/o/oauth2/v2/auth' || target.username || target.password) throw new Error('Google giriş yönlendirmesi doğrulanamadı.')
      window.location.assign(target.href)
    } catch (error) {
      setBusy(false); setSubmitFailed(true); setMessage(error instanceof Error ? error.message : 'Google girişi başlatılamadı.')
    }
  }

  const submit = async (event:FormEvent) => {
    event.preventDefault()
    if (busy) return
    setSubmitFailed(false)
    if (!validateForm()) return
    setBusy(true); setMessage('')
    try {
      if (mode === 'login') {
        const result = await request<AuthResult>('/auth/login',{method:'POST',body:JSON.stringify({...login,remember})})
        setLogin({email:'',password:''})
        if ('mfa_required' in result) {
          setMfaChallenge(result.challenge_id); setMfaCode(''); setMode('mfa'); setMessage('Girişi tamamlamak için iki aşamalı doğrulama kodunuzu girin.')
        } else if (result.user.role !== 'OWNER' && result.user.email_verified === false) {
          setEmail(result.user.email); setResetToken(''); setMode('verify'); setMessage('Önce e-posta adresinizi doğrulayın. Doğrulama kodunu e-postanızdan alın.')
        } else {
          saveUserSessionToken(result.token,remember); setMemberMenuOpen(false); setToken(result.token); setSession({user:result.user})
        }
      } else if (mode === 'mfa') {
        const result = await request<{token:string;user:User;remember?:boolean}>('/auth/2fa/login',{method:'POST',body:JSON.stringify({challenge_id:mfaChallenge,code:mfaCode})})
        saveUserSessionToken(result.token,result.remember ?? remember)
        setMfaChallenge(''); setMfaCode(''); setMode('login')
        await loadSession(result.token,result.remember ?? remember)
      } else if (isGoogleConsent) {
        const result = await request<AuthResult>('/auth/google/complete',{method:'POST',body:JSON.stringify({terms_accepted:register.terms_accepted})})
        if ('mfa_required' in result) {setMfaChallenge(result.challenge_id); setMfaCode(''); setMode('mfa')}
        else {saveUserSessionToken(result.token,result.remember ?? remember); await loadSession(result.token,result.remember)}
        setRegister({display_name:'',email:'',password:'',confirm_password:'',terms_accepted:false})
        if (!('mfa_required' in result)) setMode('login')
      } else if (mode === 'bootstrap') {
        if (!ownerSetupAvailable) throw new Error('Yerel yönetici kurulumu kullanılamıyor.')
        const result = await request<{token:string;user:User}>('/bootstrap',{method:'POST',body:JSON.stringify({...register,remember})})
        saveUserSessionToken(result.token,remember); setMemberMenuOpen(false); setToken(result.token); setSession({user:result.user}); setOwnerSetupAvailable(false)
        setRegister({display_name:'',email:'',password:'',confirm_password:'',terms_accepted:false})
      } else if (mode === 'register') {
        const result = await request<{verification_status_token?:string;message:string}>('/auth/register',{method:'POST',body:JSON.stringify(register)})
        setEmail(register.email); setVerificationStatusToken(result.verification_status_token || ''); setVerificationInput(''); setVerificationNotice(true); setVerificationMailFailed(false); setResendSeconds(60); setMode('verify'); setMessage(result.message)
        setRegister({display_name:'',email:'',password:'',confirm_password:'',terms_accepted:false})
      } else if (mode === 'forgot') {
        const result = await request<{development_reset_token?:string;message:string}>('/auth/forgot-password',{method:'POST',body:JSON.stringify({email})})
        if (result.development_reset_token) setResetToken(result.development_reset_token)
        setMessage(result.message); if (result.development_reset_token) setMode('reset')
      } else if (mode === 'reset') {
        await request('/auth/reset-password',{method:'POST',body:JSON.stringify({token:resetToken,password:resetPassword.password,confirm_password:resetPassword.confirm_password,...(resetPassword.totp_code ? {totp_code:resetPassword.totp_code} : {})})})
        setMessage('Parolanız güncellendi. Giriş yapabilirsiniz.'); setMode('login')
      } else {
        await request('/auth/verify-email',{method:'POST',body:JSON.stringify({token:verificationInput})})
        setVerificationNotice(false); setMessage('E-posta doğrulandı. Şimdi giriş yapabilirsiniz.'); setMode('login')
      }
    } catch (error) {
      setSubmitFailed(true); setMessage(error instanceof Error ? error.message : 'İşlem başarısız.')
      if (mode === 'register' && error instanceof AuthRequestError && error.emailDelivery) {
        setVerificationMailFailed(true); setResendSeconds(Math.max(60,error.retryAfter))
      } else if (verificationMailFailed && error instanceof AuthRequestError && error.retryAfter) {
        setResendSeconds(error.retryAfter)
      }
    }
    finally { setBusy(false) }
  }

  const finishSession = () => {
    clearDemoCredentials(token)
    setLogin({email:'',password:''}); setRegister({display_name:'',email:'',password:'',confirm_password:'',terms_accepted:false})
    setEmail(''); setResetToken(''); setResetPassword({password:'',confirm_password:'',totp_code:''}); setMfaChallenge(''); setMfaCode('')
    setVerificationLinkToken(''); setVerificationStatusToken(''); setVerificationInput('')
    clearUserSessionToken(); setMemberMenuOpen(false); setToken(''); setSession(null); setMode('login'); setMessage('Oturum sonlandırıldı.')
  }
  const logout = async () => {
    const sessionToken = token
    let vaultProblem = ''
    if (sessionToken) {
      try {
        const response = await fetch(`${API_BASE}/exchange-connections/session`,{method:'DELETE',headers:{Authorization:`Bearer ${sessionToken}`}})
        if (!response.ok) vaultProblem = 'Borsa oturumunun temizlenmesi doğrulanamadı.'
      } catch (error) {
        vaultProblem = 'Borsa oturumunun temizlenmesi doğrulanamadı.'
        console.warn('Exchange session logout failed:', error instanceof Error ? error.name : 'LogoutError')
      }
    }
    clearDemoCredentials(sessionToken)
    try { if (token) await request('/auth/logout',{method:'POST',headers:{Authorization:`Bearer ${token}`}}) }
    catch (error) {
      if (error instanceof AuthRequestError && error.status === 401) { finishSession(); return }
      setMessage(error instanceof Error ? error.message : 'Sunucu oturumu kapatılamadı; yeniden deneyin.')
      console.warn('Server logout failed:', error instanceof Error ? error.message : 'LogoutError')
      return
    }
    finishSession(); setMessage(vaultProblem || 'Oturum kapatıldı.')
  }

  if (enrollmentRecoveryCodes.length) return <AccountRecoveryScreen codes={enrollmentRecoveryCodes} onSaved={() => setEnrollmentRecoveryCodes([])}/>
  if (PLAYWRIGHT_TEST_MODE_AUTO_ACCESS) return <>{children}</>
  if (!session && expiredMemberMaintenance) return <MaintenanceScreen mode={expiredMemberMaintenance}/>
  if (sessionLoading && !session) return <main className="authLoading"><div className="authLoader"><ShieldCheck/><b>GÜVENLİ OTURUM</b><span>Hesap durumu kontrol ediliyor…</span></div></main>
  if (autoVerifying) return <main className="authLoading"><div className="authLoader"><MailCheck/><b>E-POSTA DOĞRULANIYOR</b><span>E-posta doğrulanıyor...</span></div></main>
  if (!session || (mode === 'reset' && resetToken) || (mode === 'forgot' && window.location.pathname === '/forgot-password')) {
    const registrationStrength = strength(register.password)
    return <LoginMarketShell>{market => <main className={`authPage${isRegistration ? ' authPageRegister' : ''}${mode === 'login' ? ' authPageLogin' : ''}`}>
      {mode !== 'login' && <p className="authMobileSlogan">KAİSTRADE piyasayı tek yerden yönetin</p>}
      {mode === 'login' && <header className="authMobileIdentity"><img src="/kaistrade-logo.png" alt="KaiStrade"/><p>KAİSTRADE piyasayı tek yerden yönetin</p></header>}
      <AuthIntroPanel data={market.data} status={market.status} movers={market.movers} premium={mode === 'login'}/>
      <section className={`authCard ${mode === 'verify' ? 'authCardVerify' : ''}${isRegistration ? ' authCardRegister' : ''}${mode === 'login' ? ' authCardLogin' : ''}`}>
        <div className="authLoginBrand"><img src="/kaistrade-logo.png" alt="KaiStrade"/></div>
        <div className="authCardHead"><div className="authMark">{mode === 'verify' ? <MailCheck/> : mode === 'mfa' ? <ShieldCheck/> : <UserRound/>}</div><div><span>{mode === 'bootstrap' ? 'YEREL YÖNETİCİ' : mode === 'register' || isGoogleConsent ? 'HESAP OLUŞTUR' : 'ÜYE GİRİŞİ'}</span><h2>{mode === 'mfa' ? 'İki aşamalı doğrulama' : mode === 'login' ? 'Hesabınıza giriş yapın' : mode === 'bootstrap' ? 'İlk yönetici hesabı' : mode === 'register' || isGoogleConsent ? 'Hesabını oluştur' : mode === 'forgot' ? 'Parolanızı yenileyin' : mode === 'reset' ? 'Yeni parola belirleyin' : 'E-postanızı kontrol edin'}</h2></div></div>
        <p className="authCardLead">{mode === 'mfa' ? 'Authenticator uygulamasındaki kodu veya tek kullanımlık kurtarma kodunuzu girin.' : mode === 'login' ? 'Çalışma alanınıza güvenli şekilde erişin.' : mode === 'bootstrap' ? 'Yerel yönetici hesabınız için güçlü bir parola belirleyin.' : mode === 'register' || isGoogleConsent ? 'KAISTrade hesabını oluştur ve piyasaları tek bir yerden takip et.' : mode === 'forgot' ? 'Hesabınıza yeniden erişmek için güvenli bir bağlantı gönderelim.' : mode === 'reset' ? 'Yeni ve güçlü bir parola belirleyin.' : 'Gelen kutunuzdaki bağlantıyla hesabınızı güvenle etkinleştirin.'}</p>
        <form ref={registrationFormRef} onSubmit={submit} noValidate={mode === 'login'} aria-busy={busy}>
          {mode === 'login' && <><label className={fieldErrors.email ? 'authFieldError' : ''}>E-posta<div className="authInputWithIcon"><Mail aria-hidden="true"/><input type="email" required autoComplete="email" aria-invalid={Boolean(fieldErrors.email)} aria-describedby={fieldErrors.email ? 'login-email-error' : undefined} value={login.email} onChange={event => {setLogin({...login,email:event.target.value});setFieldErrors(current => ({...current,email:''}))}} placeholder="siz@ornek.com"/></div>{fieldErrors.email && <em id="login-email-error" role="alert">{fieldErrors.email}</em>}</label><label className={fieldErrors.password ? 'authFieldError' : ''}>Parola<div className="authPassword authInputWithIcon"><LockKeyhole aria-hidden="true"/><input data-private="true" required type={showPassword ? 'text' : 'password'} autoComplete="current-password" aria-invalid={Boolean(fieldErrors.password)} aria-describedby={fieldErrors.password ? 'login-password-error' : undefined} placeholder="Parolanız" value={login.password} onChange={event => {setLogin({...login,password:event.target.value});setFieldErrors(current => ({...current,password:''}))}}/><button type="button" onClick={() => setShowPassword(value => !value)} aria-label={showPassword ? 'Parolayı gizle' : 'Parolayı göster'} aria-pressed={showPassword}>{showPassword ? <EyeOff/> : <Eye/>}</button></div>{fieldErrors.password && <em id="login-password-error" role="alert">{fieldErrors.password}</em>}</label><label className="authCheck"><input type="checkbox" checked={remember} onChange={event => setRemember(event.target.checked)}/><span>Bu cihazda oturumu hatırla</span></label></>}
          {isRegistration && <>
            <label>Ad soyad<input required autoComplete="name" value={register.display_name} onChange={event => setRegister({...register,display_name:event.target.value})} placeholder="Ada Yılmaz"/></label>
            <label className={fieldErrors.email ? 'authFieldError' : ''}>E-posta<input type="email" autoComplete="email" value={register.email} onChange={event => {setRegister({...register,email:event.target.value});setFieldErrors(current => ({...current,email:''}))}} placeholder="siz@ornek.com"/>{fieldErrors.email && <em role="alert">{fieldErrors.email}</em>}</label>
            <label className={fieldErrors.password ? 'authFieldError' : ''}>Parola<div className="authPassword"><input data-private="true" type={showPassword ? 'text' : 'password'} autoComplete="new-password" value={register.password} onChange={event => {setRegister({...register,password:event.target.value});setFieldErrors(current => ({...current,password:''}))}}/><button type="button" onClick={() => setShowPassword(value => !value)} aria-label="Parolayı göster veya gizle">{showPassword ? <EyeOff/> : <Eye/>}</button></div><div className={`passwordMeter strength${registrationStrength.score}`}><i style={{width:`${registrationStrength.score * 20}%`}}/><span>{registrationStrength.label} · tüm şartlar sağlanmalı</span></div><div className="passwordRules">{passwordRules(register.password).map(([label,passed]) => <span className={passed ? 'passed' : 'missing'} key={label}>{passed ? '✓' : '•'} {label}</span>)}</div>{fieldErrors.password && <em role="alert">{fieldErrors.password}</em>}</label>
            <label className={fieldErrors.confirm_password ? 'authFieldError' : ''}>Parola tekrar<div className="authPassword"><input data-private="true" type={showConfirmPassword ? 'text' : 'password'} autoComplete="new-password" value={register.confirm_password} onChange={event => {setRegister({...register,confirm_password:event.target.value});setFieldErrors(current => ({...current,confirm_password:''}))}}/><button type="button" onClick={() => setShowConfirmPassword(value => !value)} aria-label="Parola tekrarını göster veya gizle">{showConfirmPassword ? <EyeOff/> : <Eye/>}</button></div>{fieldErrors.confirm_password && <em role="alert">{fieldErrors.confirm_password}</em>}</label>
            <TermsConsent accepted={register.terms_accepted} error={termsError} onChange={value => {setRegister({...register,terms_accepted:value});setTermsError('')}}/>
          </>}
          {isGoogleConsent && <><label>Ad soyad<input value={register.display_name} readOnly/></label><label>E-posta<input type="email" value={register.email} readOnly/></label><TermsConsent accepted={register.terms_accepted} error={termsError} onChange={value => {setRegister({...register,terms_accepted:value});setTermsError('')}}/></>}
          {mode === 'mfa' && <label>2FA veya kurtarma kodu<input data-private="true" required autoComplete="one-time-code" value={mfaCode} onChange={event => setMfaCode(event.target.value)} placeholder="Authenticator veya kurtarma kodu"/><small>İlk doğrulama tamamlandı; oturum henüz açılmadı.</small>{fieldErrors.mfa && <em role="alert">{fieldErrors.mfa}</em>}</label>}
          {mode === 'forgot' && <label>E-posta<input required type="email" autoComplete="email" value={email} onChange={event => setEmail(event.target.value)} placeholder="siz@ornek.com"/><small>Kayıtlıysa yenileme bağlantısı hazırlanır.</small></label>}
          {mode === 'verify' && <label className={fieldErrors.verification ? 'authFieldError' : ''}>Doğrulama kodu<input required value={verificationInput} onChange={event => {setVerificationInput(event.target.value);setFieldErrors(current => ({...current,verification:''}))}} placeholder="E-posta doğrulama kodu"/><small>{message || 'Kayıt sonrası e-postanızdaki kodu girin.'}</small>{fieldErrors.verification && <em>{fieldErrors.verification}</em>}</label>}
          {mode === 'reset' && <><label>Doğrulama kodu<input required value={resetToken} onChange={event => setResetToken(event.target.value)}/></label><label>Yeni parola<input data-private="true" required type="password" autoComplete="new-password" value={resetPassword.password} onChange={event => setResetPassword({...resetPassword,password:event.target.value})}/></label><label>Yeni parola tekrar<input data-private="true" required type="password" autoComplete="new-password" value={resetPassword.confirm_password} onChange={event => setResetPassword({...resetPassword,confirm_password:event.target.value})}/></label></>}
          {mode === 'reset' && <label>2FA veya kurtarma kodu (etkinse)<input data-private="true" autoComplete="one-time-code" value={resetPassword.totp_code} onChange={event => setResetPassword({...resetPassword,totp_code:event.target.value})}/></label>}
          <button className="authSubmit" disabled={busy} aria-busy={busy}>{busy ? 'İŞLENİYOR…' : mode === 'mfa' ? 'DOĞRULA VE GİRİŞ YAP' : mode === 'login' ? 'GÜVENLİ GİRİŞ' : mode === 'bootstrap' ? 'YÖNETİCİ HESABINI OLUŞTUR' : mode === 'register' || isGoogleConsent ? 'HESAP OLUŞTUR' : mode === 'forgot' ? 'YENİLEME BAĞLANTISI GÖNDER' : mode === 'reset' ? 'PAROLAYI GÜNCELLE' : 'E-POSTAYI DOĞRULA'}{busy ? <LoaderCircle className="spin"/> : <ArrowRight/>}</button>
        </form>
        {mode === 'login' && <div className="authSocial"><span>veya</span><button type="button" className="authGoogle" aria-label="Google ile devam et" disabled={busy} aria-busy={busy} onClick={() => void startGoogle()}><span className="authGoogleMark" aria-hidden="true">G</span>Google ile devam et</button></div>}
        {(mode === 'verify' || (mode === 'register' && verificationMailFailed)) && <div className="authVerificationPanel">
          <b>{verificationNotice && mode === 'verify' ? 'E-posta kayıt için uygunsa doğrulama bağlantısı gönderildi.' : verificationMailFailed ? 'Doğrulama maili gönderilemedi.' : 'E-posta doğrulaması bekleniyor.'}</b>
          <span>Spam klasörünü kontrol et. Gereksiz / Tanıtımlar klasörüne de bakabilirsin.</span>
          {mode === 'verify' && <small>Doğrulama bekleniyor... Bu sayfa başka cihazdan yapılan doğrulamayı otomatik algılar.</small>}
          {(mode === 'register' || verificationStatusToken) && <button type="button" className="authSubmit" disabled={busy || resendSeconds > 0} onClick={() => mode === 'register' ? registrationFormRef.current?.requestSubmit() : void resendVerification()}>Doğrulama mailini tekrar gönder{resendSeconds > 0 ? ` (${resendSeconds} sn)` : ''}</button>}
          {mode === 'verify' && <button type="button" className="authGoogle" disabled={busy} onClick={() => void startGoogle()}>Google ile devam et</button>}
        </div>}
        {message && message !== 'Oturum doğrulanıyor…' && <p className={`authMessage${submitFailed ? ' authMessageError' : ''}`} role={submitFailed ? 'alert' : 'status'}>{message}</p>}
        <div className={`authLinks${mode === 'register' ? ' authRegisterLinks' : ''}`}>{mode === 'login' && <><button onClick={() => setMode('forgot')}>Parolamı unuttum</button><button onClick={() => setMode('register')}>Yeni hesap oluştur</button></>}{mode === 'register' ? <><span>Zaten hesabın var mı?</span><button type="button" onClick={() => setMode('login')}>Giriş yap</button></> : mode !== 'login' && <button onClick={() => setMode('login')}>Giriş ekranına dön</button>}</div>
        {mode === 'login' && LOCAL_OWNER_SETUP_PAGE && ownerSetupAvailable && <div className="authLinks"><button type="button" onClick={() => {setRegister(current => ({...current,email:login.email,password:'',confirm_password:''}));setMessage('');setFieldErrors({});setTermsError('');setMode('bootstrap')}}><ShieldCheck aria-hidden="true"/> İlk yönetici kurulumu</button></div>}
        {mode === 'login' && <section className="authMobileMarkets" aria-label="Canlı fiyatlar"><AuthCoinScreen data={market.data} status={market.status}/></section>}
      </section>
    </main>}</LoginMarketShell>
  }

  const path = window.location.pathname
  const sessionNotice = message ? createPortal(<p className="profileNotice" role="alert" style={{position:'fixed',bottom:12,left:12,right:12,zIndex:10000}}>{message}</p>,document.body) : null
  if (['/login','/register','/forgot-password','/reset-password','/verify-email'].includes(path)) { history.replaceState(null,'','/dashboard') }
  if (path.startsWith('/admin') && session.user.role !== 'OWNER') return <main className="authLoading"><div className="authLoader"><ShieldCheck/><b>403 · ERİŞİM YOK</b><span>Bu alan yalnızca yönetici hesaplarına açıktır.</span><button onClick={() => {history.replaceState(null,'','/dashboard'); location.reload()}}>Dashboard'a dön</button></div></main>
  if (path.startsWith('/admin')) return <>{sessionNotice}<div className="authSessionBar"><span><ShieldCheck/> {session.user.display_name} <b>ADMIN</b></span><button onClick={() => void logout()}><LogOut/> Çıkış</button></div><AdminPanel token={token} onBack={() => {history.replaceState(null,'','/dashboard');location.reload()}}/></>
  if (path.startsWith('/settings') || path === '/profile') return <>{sessionNotice}<ProfileSettings onLogout={() => void logout()} onSessionEnded={finishSession} onEnrollmentComplete={codes => {setEnrollmentRecoveryCodes(codes); finishSession()}}/></>
  const maintenanceMode = session.maintenance?.mode
  if (session.user.role !== 'OWNER' && (maintenanceMode === 'MAINTENANCE' || maintenanceMode === 'EMERGENCY')) {
    return <MaintenanceScreen mode={maintenanceMode as string}/>
  }
  const memberMenu = memberMenuOpen ? createPortal(<div ref={memberMenuRef} className="authMemberMenu authMemberPortalMenu" role="menu" style={{top:memberMenuPosition.top,left:memberMenuPosition.left}}><div className="authMemberMenuHead"><small>SECURE ACCOUNT</small><strong>{session.user.email}</strong></div>{session.user.role === 'OWNER' && <button type="button" role="menuitem" onClick={() => {setMemberMenuOpen(false);location.assign('/admin')}}><ShieldCheck/><span><b>Admin Dashboard</b><small>Control center</small></span></button>}<button type="button" role="menuitem" onClick={() => {setMemberMenuOpen(false);location.assign('/settings')}}><UserRound/><span><b>Profile &amp; Settings</b><small>Identity and security</small></span></button><button className="authMemberLogout" type="button" role="menuitem" onClick={() => void logout()}><LogOut/><span><b>Çıkış</b><small>End secure session</small></span></button></div>,document.body) : null
  const profileControl = <div className="authSessionBar"><button ref={memberTriggerRef} className="authMemberTrigger" type="button" aria-label="Profil menüsünü aç" aria-expanded={memberMenuOpen} aria-haspopup="menu" onClick={() => setMemberMenuOpen(value => !value)}><svg className="authProfileGlyph" viewBox="3.5 3.8 17 17.9" preserveAspectRatio="xMidYMid meet" aria-hidden="true"><circle cx="12" cy="8" r="3.15"/><path d="M4.7 20.1c.55-3.55 3.35-5.55 7.3-5.55s6.75 2 7.3 5.55c.05.32-.2.6-.52.6H5.22c-.32 0-.57-.28-.52-.6Z"/></svg></button></div>
  const adminMaintenanceBanner = session.user.role === 'OWNER' && (maintenanceMode === 'MAINTENANCE' || maintenanceMode === 'EMERGENCY') ? <div className="authAdminMaintenanceBar" role="status"><span>{maintenanceMode === 'EMERGENCY' ? 'Acil durum modu aktif.' : 'Bakım modu aktif.'} Üyeler bakım ekranını görüyor.</span><button type="button" onClick={() => location.assign('/admin')}>Admin Panel</button></div> : null
  return <>{sessionNotice}{adminMaintenanceBanner}{profileHeaderSlot ? createPortal(profileControl,profileHeaderSlot) : profileControl}{memberMenu}{children}</>
}
