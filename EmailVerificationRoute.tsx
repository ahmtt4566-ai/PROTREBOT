import {lazy, Suspense, type ReactNode, useEffect, useState} from 'react'
import {withRequestDeadline} from './browser-request'

const Screen = lazy(() => import('./EmailVerification'))

export default function EmailVerificationRoute({children}: {children: ReactNode}) {
  const [path, setPath] = useState(window.location.pathname)
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    const navigate = () => {setEnabled(null); setPath(window.location.pathname); setAttempt(value => value + 1)}
    window.addEventListener('protrebot-email-verification', navigate)
    window.addEventListener('popstate', navigate)
    return () => {
      window.removeEventListener('protrebot-email-verification', navigate)
      window.removeEventListener('popstate', navigate)
    }
  }, [])
  useEffect(() => {
    if (path !== '/verify-email') return
    const controller = new AbortController()
    setError('')
    void withRequestDeadline(async signal => {
      const response = await fetch('/api/v22/public', {signal})
      if (!response.ok) throw new Error('Doğrulama ayarları okunamadı. Lütfen tekrar dene.')
      const result: unknown = await response.json()
      if (!result || typeof result !== 'object') throw new Error('Doğrulama ayarları geçersiz.')
      return 'email_verification_v2_enabled' in result && result.email_verification_v2_enabled === true
    }, {signal: controller.signal}).then(value => {
      if (!controller.signal.aborted) setEnabled(value)
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Doğrulama ekranı açılamadı.')
    })
    return () => controller.abort()
  }, [path, attempt])
  if (path !== '/verify-email' || enabled === false) return children
  if (error) return <main className="authLoading"><p role="alert">{error}</p><button onClick={() => setAttempt(value => value + 1)}>Tekrar dene</button></main>
  if (enabled === null) return <main className="authLoading" role="status">Doğrulama ekranı hazırlanıyor…</main>
  return <Suspense fallback={<main className="authLoading" role="status">Doğrulama ekranı hazırlanıyor…</main>}><Screen/></Suspense>
}
