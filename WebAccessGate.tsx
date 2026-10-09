import { type FormEvent, type ReactNode, useEffect, useRef, useState } from 'react'
import { LockKeyhole, ShieldCheck } from 'lucide-react'
import { clearOwnerAccessToken, ownerAccessToken, saveOwnerAccessToken, userSessionToken, verifyOwnerAccess } from './api'
import './web-access.css'


const LOCAL_DEVELOPMENT_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]'])
const LOCAL_DEVELOPMENT_BYPASS = import.meta.env.DEV && LOCAL_DEVELOPMENT_HOSTS.has(window.location.hostname)
const ACCESS_REQUIRED = !LOCAL_DEVELOPMENT_BYPASS && import.meta.env.VITE_WEB_ACCESS_REQUIRED !== 'false' && import.meta.env.PROD

export default function WebAccessGate({children}:{children:ReactNode}) {
  const [token,setToken] = useState(ownerAccessToken())
  const [status,setStatus] = useState<'CHECKING'|'LOCKED'|'OPEN'>(ACCESS_REQUIRED ? 'CHECKING' : 'OPEN')
  const [message,setMessage] = useState('Güvenli yönetici oturumu doğrulanıyor…')
  const [memberSession,setMemberSession] = useState(Boolean(userSessionToken()))
  const requestController = useRef<AbortController|null>(null)

  useEffect(() => {
    const refresh = () => setMemberSession(Boolean(userSessionToken()))
    window.addEventListener('protrebot-session-changed',refresh)
    window.addEventListener('storage',refresh)
    return () => { window.removeEventListener('protrebot-session-changed',refresh); window.removeEventListener('storage',refresh) }
  },[])

  useEffect(() => {
    if (!ACCESS_REQUIRED) return
    const controller = new AbortController()
    requestController.current = controller
    verifyOwnerAccess(token,controller.signal)
      .then(() => { if (!controller.signal.aborted) { setStatus('OPEN'); setMessage('') } })
      .catch(error => {
        if (controller.signal.aborted) return
        setToken('')
        setStatus('LOCKED')
        setMessage(error instanceof Error ? error.message : 'Erişim doğrulanamadı.')
        void clearOwnerAccessToken().catch(logoutError => console.warn('Owner cookie logout failed:', logoutError instanceof Error ? logoutError.message : 'LogoutError'))
      })
    return () => { controller.abort(); requestController.current?.abort() }
  }, [])

  const submit = async (event:FormEvent) => {
    event.preventDefault()
    if (token.trim().length < 24) {
      setMessage('Erişim kodu en az 24 karakter olmalı.')
      return
    }
    setStatus('CHECKING')
    setMessage('Sunucu kilidi doğrulanıyor…')
    requestController.current?.abort()
    const controller = new AbortController()
    requestController.current = controller
    try {
      await verifyOwnerAccess(token,controller.signal)
      if (controller.signal.aborted) return
      saveOwnerAccessToken(token)
      setToken(ownerAccessToken())
      setStatus('OPEN')
      setMessage('')
    } catch (error) {
      if (controller.signal.aborted) return
      setStatus('LOCKED')
      setMessage(error instanceof Error ? error.message : 'Erişim doğrulanamadı.')
      void clearOwnerAccessToken().catch(logoutError => console.warn('Owner cookie logout failed:', logoutError instanceof Error ? logoutError.message : 'LogoutError'))
    }
  }

  if (status === 'OPEN') {
    return <>
      {ACCESS_REQUIRED && !memberSession && <button className="webSessionBadge" onClick={() => { void clearOwnerAccessToken().then(() => location.reload()).catch(error => setMessage(error instanceof Error ? error.message : 'Oturum kapatılamadı.')) }}><ShieldCheck size={15}/> Güvenli oturum · Çıkış</button>}
      {message && message !== 'Güvenli yönetici oturumu doğrulanıyor…' && <p role="alert">{message}</p>}
      {children}
    </>
  }

  return <main className="webAccessShell">
    <section className="webAccessCard">
      <div className="webAccessBrand"><span className="webAccessBrandLogo"><img src="/kaistrade-logo.png" alt="KaisTrade"/></span></div>
      <div className="webAccessIcon"><LockKeyhole/></div>
      <h1>Yönetici erişimi</h1>
      <p>Bot paneli ve API uçları internete karşı kilitlidir. Bu ekran Binance anahtarı istemez.</p>
      <form onSubmit={submit}>
        <label htmlFor="owner-access">Yönetici erişim kodu</label>
        <input data-private="true" id="owner-access" type="password" value={token} onChange={event => setToken(event.target.value)} autoComplete="current-password" placeholder="En az 24 karakter" disabled={status === 'CHECKING'}/>
        <button disabled={status === 'CHECKING'}>{status === 'CHECKING' ? 'DOĞRULANIYOR…' : 'GÜVENLİ PANELE GİR'}</button>
      </form>
      <em>{message}</em>
      <footer><ShieldCheck size={14}/> Testnet ana çalışma modudur; gerçek emir kanalı ayrıca kilitlidir.</footer>
    </section>
  </main>
}
