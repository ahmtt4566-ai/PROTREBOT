import {useCallback, useEffect, useRef, useState} from 'react'
import {Mail} from 'lucide-react'
import {saveUserSessionToken} from './api'
import {withRequestDeadline} from './browser-request'
import {VerificationSymbol} from './verification-ui'
import './email-verification.css'

type Status = {verified: boolean; email: string; can_exchange: boolean; already_authenticated: boolean; retry_after: number; can_change_email: boolean}
type Outcome = {email: string; target: 'dashboard' | 'login'; animate: boolean}
class VerificationError extends Error {
  constructor(message: string, readonly status: number, readonly code: string, readonly retryAfter = 0) {super(message)}
}
function object(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}
async function request(path: string, body?: object): Promise<unknown> {
  return withRequestDeadline(async signal => {
    const response = await fetch('/api/v22' + path, {
      method: body ? 'POST' : 'GET', credentials: 'same-origin', signal,
      headers: {'X-Requested-With': 'XMLHttpRequest', ...(body ? {'Content-Type': 'application/json'} : {})},
      ...(body ? {body: JSON.stringify(body)} : {}),
    })
    const value: unknown = await response.json()
    if (!response.ok) {
      const detail = object(value) ? value.detail : null
      throw new VerificationError(
        typeof detail === 'string' ? detail : object(detail) && typeof detail.message === 'string' ? detail.message : 'İşlem tamamlanamadı. Lütfen tekrar dene.',
        response.status, object(detail) && typeof detail.code === 'string' ? detail.code : '',
        Number(response.headers.get('Retry-After')) || 0)
    }
    return value
  })
}
function status(value: unknown): Status {
  if (!object(value) || typeof value.verified !== 'boolean' || typeof value.email !== 'string'
      || typeof value.can_exchange !== 'boolean' || typeof value.already_authenticated !== 'boolean'
      || typeof value.retry_after !== 'number') throw new Error('Doğrulama durumu okunamadı.')
  return {verified: value.verified, email: value.email, can_exchange: value.can_exchange,
    already_authenticated: value.already_authenticated, retry_after: value.retry_after, can_change_email: value.can_change_email === true}
}
function login(email: string) {
  if (email) sessionStorage.setItem('kaistrade-verification-login-email', email)
  window.location.assign('/login')
}
export default function EmailVerification() {
  const [linkToken] = useState(() => new URLSearchParams(window.location.search).get('token') || '')
  const [phase, setPhase] = useState<'waiting' | 'verifying' | 'success' | 'error'>(linkToken ? 'verifying' : 'waiting')
  const [email, setEmail] = useState('')
  const [seconds, setSeconds] = useState(60)
  const [outcome, setOutcome] = useState<Outcome | null>(null)
  const [error, setError] = useState({code: '', message: ''})
  const [notice, setNotice] = useState('')
  const [sending, setSending] = useState(false)
  const [retry, setRetry] = useState(0)
  const [ready, setReady] = useState(false)
  const [editing, setEditing] = useState(false)
  const [newEmail, setNewEmail] = useState('')
  const [password, setPassword] = useState('')
  const [canChangeEmail, setCanChangeEmail] = useState(false)
  const initial = useRef<Promise<Status | Outcome> | null>(null)
  const confirmedResult = useRef<{email: string; already: boolean} | null>(null)
  const exchanging = useRef<Promise<Outcome> | null>(null)
  const checking = useRef(false)
  const finished = useRef(false)

  const finish = useCallback(async (current: Status, animate: boolean): Promise<Outcome> => {
    if (current.already_authenticated) return {email: current.email, target: 'dashboard', animate}
    if (!exchanging.current) exchanging.current = request('/auth/registration/exchange', {}).then(value => {
      if (!object(value) || typeof value.requires_login !== 'boolean') throw new Error('Oturum yanıtı okunamadı.')
      if (value.requires_login) return {email: current.email, target: 'login' as const, animate}
      if (typeof value.token !== 'string') throw new Error('Oturum yanıtı okunamadı.')
      saveUserSessionToken(value.token, false)
      return {email: current.email, target: 'dashboard' as const, animate}
    })
    return exchanging.current
  }, [])
  const showSuccess = useCallback((value: Outcome) => {
    finished.current = true
    setEmail(value.email); setOutcome(value); setPhase('success')
    window.history.replaceState(null, '', '/verify-email')
  }, [])
  const showError = useCallback((reason: unknown) => {
    console.error('E-posta doğrulama işlemi başarısız', reason)
    setPhase('error')
    setSeconds(reason instanceof VerificationError ? reason.retryAfter : 0)
    setError({code: reason instanceof VerificationError ? reason.code : '',
      message: reason instanceof TypeError || reason instanceof SyntaxError || reason instanceof DOMException
        ? 'Sunucuya ulaşılamadı veya yanıt okunamadı. Lütfen tekrar dene.'
        : reason instanceof Error ? reason.message : 'Doğrulama tamamlanamadı.'})
  }, [])

  useEffect(() => {
    let active = true
    if (!initial.current) initial.current = (async () => {
      let confirmed = confirmedResult.current
      if (linkToken && !confirmed) {
        const result = await request('/auth/verify-email', {token: linkToken})
        if (!object(result) || result.verified !== true || typeof result.email !== 'string') throw new Error('Sunucu doğrulama onayı vermedi.')
        confirmed = {email: result.email, already: result.already_verified === true}
        confirmedResult.current = confirmed
        window.history.replaceState(null, '', '/verify-email')
      }
      try {
        const current = status(await request('/auth/registration/status'))
        if (confirmed && confirmed.email !== current.email) return {email: confirmed.email, target: 'login' as const, animate: !confirmed.already}
        if (current.verified) return finish(current, confirmed ? !confirmed.already : false)
        if (confirmed) return {email: confirmed.email, target: 'login' as const, animate: !confirmed.already}
        return current
      } catch (reason) {
        if (confirmed && reason instanceof VerificationError && [401, 403].includes(reason.status)) return {email: confirmed.email, target: 'login' as const, animate: !confirmed.already}
        throw reason
      }
    })()
    void initial.current.then(value => {
      if (!active) return
      if ('target' in value) showSuccess(value)
      else {setEmail(value.email); setSeconds(value.retry_after); setCanChangeEmail(value.can_change_email); setPhase('waiting'); setReady(true)}
    }).catch(reason => {if (active) showError(reason)})
    return () => {active = false}
  }, [linkToken, finish, showSuccess, showError, retry])

  useEffect(() => {
    if (!ready || phase !== 'waiting' || finished.current || editing) return
    let active = true
    const check = async () => {
      if (checking.current || finished.current || document.visibilityState === 'hidden') return
      checking.current = true
      try {
        const current = status(await request('/auth/registration/status'))
        if (!active) return
        setEmail(current.email); setSeconds(current.retry_after)
        if (current.verified) {
          const result = await finish(current, true)
          if (active) showSuccess(result)
        }
      } catch (reason) {if (active) showError(reason)}
      finally {checking.current = false}
    }
    const timer = window.setInterval(() => void check(), 4000)
    const focus = () => void check()
    window.addEventListener('focus', focus)
    document.addEventListener('visibilitychange', focus)
    return () => {
      active = false; window.clearInterval(timer)
      window.removeEventListener('focus', focus); document.removeEventListener('visibilitychange', focus)
    }
  }, [ready, phase, editing, finish, showSuccess, showError])
  useEffect(() => {
    if (seconds <= 0) return
    const timer = window.setInterval(() => setSeconds(value => Math.max(0, value - 1)), 1000)
    return () => window.clearInterval(timer)
  }, [seconds > 0])
  useEffect(() => {
    if (!outcome || outcome.target !== 'dashboard') return
    const timer = window.setTimeout(() => window.location.assign('/dashboard'), outcome.animate ? 3000 : 250)
    return () => window.clearTimeout(timer)
  }, [outcome])

  const resend = async () => {
    if (sending || seconds > 0) return
    setSending(true); setNotice('')
    try {
      const value = await request('/auth/registration/resend', linkToken ? {token: linkToken} : {})
      if (!object(value) || value.ok !== true || typeof value.retry_after !== 'number') throw new Error('Gönderim yanıtı okunamadı.')
      setSeconds(value.retry_after); setNotice('Yeni bağlantı gönderildi — gelen kutunu kontrol et')
    } catch (reason) {
      if (reason instanceof VerificationError && reason.retryAfter) setSeconds(reason.retryAfter)
      setNotice(reason instanceof Error ? reason.message : 'Bağlantı gönderilemedi.')
    } finally {setSending(false)}
  }
  const changeEmail = async () => {
    if (sending) return
    setSending(true); setNotice('')
    try {
      const value = await request('/auth/registration/email', {new_email: newEmail, current_password: password})
      if (!object(value) || value.ok !== true || typeof value.retry_after !== 'number' || typeof value.email !== 'string') throw new Error('Adres değişikliği yanıtı okunamadı.')
      setEmail(value.email); setSeconds(value.retry_after); setEditing(false); setPassword(''); setNewEmail('')
      setNotice('Yeni adresine bir doğrulama bağlantısı gönderdik.')
    } catch (reason) {
      if (reason instanceof VerificationError && reason.retryAfter) setSeconds(reason.retryAfter)
      setNotice(reason instanceof Error ? reason.message : 'E-posta adresi değiştirilemedi.')
    } finally {setSending(false)}
  }
  const isError = phase === 'error'
  const success = phase === 'success'
  const title = success ? 'E-posta Doğrulandı' : isError
    ? error.code === 'expired' ? 'Bağlantının süresi doldu' : error.code === 'used' ? 'Bu bağlantı zaten kullanılmış' : 'Doğrulama tamamlanamadı'
    : phase === 'verifying' ? 'Doğrulanıyor…' : 'E-postanı doğrula'
  const explanation = success ? outcome?.target === 'dashboard' ? 'Hesabın güvende. Panele yönlendiriliyorsun…' : 'Hesabın doğrulandı. Devam etmek için giriş yap.'
    : isError ? error.code === 'expired' ? 'Güvenliğin için doğrulama bağlantıları tek kullanımlıktır ve 30 dakika geçerlidir.'
      : error.code === 'used' ? 'Her bağlantı yalnızca bir kez çalışır. Devam etmek için yenisini iste.' : error.message
    : phase === 'verifying' ? 'Bağlantını güvenli şekilde kontrol ediyoruz.' : 'E-postana bir doğrulama bağlantısı gönderdik.'

  return <main className="ev-page"><section className={`ev-card verificationCard ${success && outcome?.animate ? 'ev-animated' : 'ev-still'}`} aria-labelledby="ev-title">
    <div className="ev-brand"><Mail size={16}/><span>KaisTrade · Güvenli doğrulama</span></div>
    <VerificationSymbol kind="mail" success={success}/>
    <div className="ev-copy" aria-live="polite"><h1 id="ev-title">{title}</h1><p>{explanation}</p></div>
    {phase === 'waiting' && <><p className="ev-notice">{email}</p><p className="ev-wait">Onay bekleniyor — sayfa kendiliğinden güncellenir</p>
      {!editing ? <><button className="ev-primary" disabled={sending || seconds > 0} onClick={() => void resend()}>Tekrar gönder{seconds > 0 ? ` (${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')})` : ''}</button>
        <p className="ev-footnote">30 dk geçerli · tek kullanımlık</p>{canChangeEmail && <button className="ev-link" onClick={() => {setEditing(true); setNewEmail(email); setNotice('')}}>E-postanı mı yanlış yazdın? Değiştir</button>}</>
        : <form className="ev-email-form" onSubmit={event => {event.preventDefault(); void changeEmail()}}>
          <label>Yeni e-posta<input type="email" autoComplete="email" required maxLength={180} value={newEmail} onChange={event => setNewEmail(event.target.value)} disabled={sending}/></label>
          <label>Parolan<input type="password" autoComplete="current-password" data-private="true" required value={password} onChange={event => setPassword(event.target.value)} disabled={sending}/></label>
          <p className="ev-footnote">Hesabını korumak için parolanı doğruluyoruz. Önceki bağlantı geçersizleşir.</p>
          <button className="ev-primary" disabled={sending || seconds > 0}>Adresi değiştir ve gönder{seconds > 0 ? ` (${seconds} sn)` : ''}</button>
          <button type="button" className="ev-link" disabled={sending} onClick={() => {setEditing(false); setPassword(''); setNotice('')}}>Vazgeç</button>
        </form>}</>}
    {phase === 'verifying' && <p className="ev-footnote" role="status">Sunucu onayı bekleniyor</p>}
    {success && outcome && <>{outcome.target === 'dashboard' && <div className={`ev-progress ${outcome.animate ? 'is-running' : 'is-complete'}`} role="progressbar" aria-label="Panele yönlendirme"><i/></div>}
      <button className="ev-primary" onClick={() => outcome.target === 'dashboard' ? window.location.assign('/dashboard') : login(outcome.email)}>{outcome.target === 'dashboard' ? 'Panele geç' : 'Giriş yap'}</button></>}
    {isError && <><button className="ev-primary" disabled={sending || seconds > 0} onClick={() => void resend()}>Yeni bağlantı gönder{seconds > 0 ? ` (${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')})` : ''}</button>
      <button className="ev-link" onClick={() => login(email)}>Giriş sayfasına dön</button>
      {!error.code && <button className="ev-link" onClick={() => {initial.current = null; exchanging.current = null; setReady(false); setPhase(linkToken ? 'verifying' : 'waiting'); setRetry(value => value + 1)}}>Tekrar dene</button>}</>}
    {notice && <p className="ev-notice" role="status">{notice}</p>}
  </section></main>
}
