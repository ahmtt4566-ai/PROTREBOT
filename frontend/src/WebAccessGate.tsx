import { type FormEvent, type ReactNode, useEffect, useState } from 'react'
import { LockKeyhole, ShieldCheck } from 'lucide-react'
import { clearOwnerAccessToken, ownerAccessToken, saveOwnerAccessToken, verifyOwnerAccess } from './api'


const LOCAL_DEVELOPMENT_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]'])
const LOCAL_DEVELOPMENT_BYPASS = import.meta.env.DEV && LOCAL_DEVELOPMENT_HOSTS.has(window.location.hostname)
const ACCESS_REQUIRED = !LOCAL_DEVELOPMENT_BYPASS && import.meta.env.VITE_WEB_ACCESS_REQUIRED !== 'false' && import.meta.env.PROD

export default function WebAccessGate({children}:{children:ReactNode}) {
  const [token,setToken] = useState(ownerAccessToken())
  const [status,setStatus] = useState<'CHECKING'|'LOCKED'|'OPEN'>(ACCESS_REQUIRED ? 'CHECKING' : 'OPEN')
  const [message,setMessage] = useState('Güvenli yönetici oturumu doğrulanıyor…')

  useEffect(() => {
    if (!ACCESS_REQUIRED) return
    if (!token) {
      setStatus('LOCKED')
      setMessage('Sunucuda belirlediğin yönetici erişim kodunu yaz.')
      return
    }
    verifyOwnerAccess(token)
      .then(() => setStatus('OPEN'))
      .catch(error => {
        clearOwnerAccessToken()
        setToken('')
        setStatus('LOCKED')
        setMessage(error instanceof Error ? error.message : 'Erişim doğrulanamadı.')
      })
  }, [])

  const submit = async (event:FormEvent) => {
    event.preventDefault()
    if (token.trim().length < 24) {
      setMessage('Erişim kodu en az 24 karakter olmalı.')
      return
    }
    setStatus('CHECKING')
    setMessage('Sunucu kilidi doğrulanıyor…')
    try {
      await verifyOwnerAccess(token)
      saveOwnerAccessToken(token)
      setStatus('OPEN')
    } catch (error) {
      clearOwnerAccessToken()
      setStatus('LOCKED')
      setMessage(error instanceof Error ? error.message : 'Erişim doğrulanamadı.')
    }
  }

  if (status === 'OPEN') {
    return <>
      {ACCESS_REQUIRED && <button className="webSessionBadge" onClick={() => {clearOwnerAccessToken();location.reload()}}><ShieldCheck size={15}/> Güvenli oturum · Çıkış</button>}
      {children}
    </>
  }

  return <main className="webAccessShell">
    <section className="webAccessCard">
      <div className="webAccessBrand"><span>X</span><div><b>PROTREBOT ELITE X</b><small>V28 · IN-APP EXCHANGE VAULT</small></div></div>
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
