import {useEffect, useRef, useState, type ClipboardEvent, type ReactNode} from 'react'
import {Copy, LoaderCircle} from 'lucide-react'
import {QRCodeSVG} from 'qrcode.react'
import {AccountRequestError, accountRequest} from './account-settings-api'
import {VerificationSteps, VerificationSymbol} from './verification-ui'

export type EnrollmentResult = {recovery_codes: string[]; reauthenticate?: boolean; notification_sent?: boolean}
export default function TwoFactorSetup({enrollment, setupForm, onBusyChange, onEnabled}: {
  enrollment: {secret: string; otpauth_uri: string} | null; setupForm: ReactNode;
  onBusyChange: (busy: boolean) => void; onEnabled: (result: EnrollmentResult) => void;
}) {
  const [step, setStep] = useState<1 | 2>(1)
  const [visible, setVisible] = useState(() => window.matchMedia('(max-width:600px)').matches)
  const [digits, setDigits] = useState<string[]>(Array<string>(6).fill(''))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [invalid, setInvalid] = useState(false)
  const [notice, setNotice] = useState('')
  const [wait, setWait] = useState(0)
  const [verified, setVerified] = useState(false)
  const inputs = useRef<Array<HTMLInputElement | null>>([])
  const nextButton = useRef<HTMLButtonElement>(null)
  const inFlight = useRef(false)
  const mounted = useRef(false)
  useEffect(() => {mounted.current = true; return () => {mounted.current = false}}, [])
  useEffect(() => {
    if (step === 2) inputs.current[0]?.focus()
    else if (enrollment) nextButton.current?.focus({preventScroll: true})
  }, [step, enrollment])
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 3000)
    return () => window.clearTimeout(timer)
  }, [notice])
  useEffect(() => {
    if (wait <= 0) return
    const timer = window.setInterval(() => setWait(value => Math.max(0, value - 1)), 1000)
    return () => window.clearInterval(timer)
  }, [wait > 0])
  const verify = async (code: string) => {
    if (inFlight.current || wait > 0 || !/^\d{6}$/.test(code)) return
    inFlight.current = true; setBusy(true); onBusyChange(true); setError(''); setInvalid(false)
    try {
      const result = await accountRequest<EnrollmentResult>('/account/2fa/enable', {method: 'POST', body: JSON.stringify({code})})
      if (!Array.isArray(result.recovery_codes) || result.recovery_codes.length !== 10
          || result.recovery_codes.some(value => typeof value !== 'string' || !value)) {
        throw new Error('Yedek kodlar okunamadı. Kurulumu tekrar başlatma; hesabının güvenlik durumunu kontrol et.')
      }
      if (mounted.current) {
        setVerified(true)
        if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) await new Promise<void>(resolve => window.setTimeout(resolve, 180))
        if (mounted.current) onEnabled(result)
      }
    } catch (reason) {
      if (!mounted.current) return
      const message = reason instanceof Error ? reason.message : 'Doğrulama tamamlanamadı. Lütfen tekrar dene.'
      const normalized = message.toLocaleLowerCase('tr-TR')
      const badCode = reason instanceof AccountRequestError && reason.status === 401
        && normalized.includes('kod') && (normalized.includes('geçersiz') || normalized.includes('hatalı'))
      setInvalid(badCode)
      setError(badCode ? 'Kod hatalı. Telefonunun saatinin otomatik ayarda olduğundan emin ol.'
        : message)
      if (reason instanceof AccountRequestError && reason.retryAfter) setWait(reason.retryAfter)
      setDigits(Array<string>(6).fill(''))
      inputs.current[0]?.focus()
    } finally {
      inFlight.current = false
      if (mounted.current) {setBusy(false); onBusyChange(false)}
    }
  }
  const update = (index: number, value: string) => {
    if (inFlight.current || wait > 0) return
    const numeric = value.replace(/\D/g, '')
    const next = [...digits]
    if (numeric.length > 1) {
      const start = numeric.length === 6 ? 0 : index
      numeric.slice(0, 6 - start).split('').forEach((digit, offset) => {next[start + offset] = digit})
      inputs.current[Math.min(5, start + numeric.length)]?.focus()
    } else {
      next[index] = numeric
      inputs.current[numeric ? Math.min(5, index + 1) : Math.max(0, index - 1)]?.focus()
    }
    setDigits(next); setError(''); setInvalid(false)
    if (next.every(Boolean)) void verify(next.join(''))
  }
  const paste = (event: ClipboardEvent<HTMLInputElement>, index: number) => {
    const value = event.clipboardData.getData('text').replace(/\s/g, '')
    event.preventDefault()
    if (/^\d{6}$/.test(value)) update(0, value)
    else if (/^\d+$/.test(value)) update(index, value)
    else setError('Altı rakamdan oluşan kodu yapıştır.')
  }
  const copy = async () => {
    if (!enrollment) return
    setNotice(''); setError('')
    try {await navigator.clipboard.writeText(enrollment.secret); setNotice('Kopyalandı')}
    catch {setError('Kurulum anahtarı kopyalanamadı. Anahtarı gösterip elle girebilirsin.')}
  }
  return <div className="verificationFlow">
    <VerificationSteps step={step}/>
    <VerificationSymbol success={verified}/>
    {step === 1 ? <>
      <h3>Uygulamayı bağla</h3><p>Telefonundaki doğrulama uygulamasını hesabına bağla.</p>
      {!enrollment ? setupForm : <>
        <div className="verificationConnect">
        <div className="verificationQR" data-private="true"><QRCodeSVG value={enrollment.otpauth_uri} size={180} title="Doğrulama uygulaması bağlantı kodu"/></div>
        <p>Uygulamada QR kodunu tara. Bu telefondaysan kurulum anahtarını kopyalayıp uygulamaya ekle.</p>
        <div className="verificationKey" data-private="true"><strong>Kurulum anahtarı</strong><code aria-label={visible ? 'Kurulum anahtarı' : 'Kurulum anahtarı gizli'}>{visible ? enrollment.secret : '•••• •••• •••• ••••'}</code>
          <div className="verificationActions"><button type="button" aria-pressed={visible} onClick={() => setVisible(value => !value)}>{visible ? 'Gizle' : 'Göster'}</button><button type="button" onClick={() => void copy()}><Copy/>Kopyala</button></div>
        </div>
        </div>
        <button ref={nextButton} type="button" className="verificationPrimary" onClick={() => {setStep(2); setNotice(''); setError('')}}>Devam et</button>
      </>}
    </> : <div className={`verificationCodeScreen ${verified ? 'is-verified' : ''}`}>
      <h3>Kodu doğrula</h3><p>Doğrulama uygulamandaki altı rakamlı kodu gir.</p>
      <div className={`verificationDigits ${invalid ? 'is-error' : ''}`} role="group" aria-label="Altı rakamlı doğrulama kodu" data-private="true">
        {digits.map((digit, index) => <input key={index} ref={element => {inputs.current[index] = element}} aria-label={`Doğrulama kodu ${index + 1}. rakam`}
          aria-invalid={invalid} aria-describedby={error ? 'verification-code-error' : undefined} inputMode="numeric" autoComplete={index === 0 ? 'one-time-code' : 'off'} pattern="[0-9]*"
          value={digit} readOnly={busy || wait > 0} onChange={event => update(index, event.target.value)} onPaste={event => paste(event, index)}
          onKeyDown={event => {if (event.key === 'Backspace' && !digit && !busy && !wait) {event.preventDefault(); const next = [...digits]; next[Math.max(0, index - 1)] = ''; setDigits(next); inputs.current[Math.max(0, index - 1)]?.focus()}}}/>)}
      </div>
      {busy && <p role="status"><LoaderCircle size={16}/>Kod doğrulanıyor…</p>}
      {wait > 0 && <p role="status">Yeni deneme için {wait} saniye bekle.</p>}
      <button type="button" disabled={busy} onClick={() => {setStep(1); setDigits(Array<string>(6).fill('')); setError('')}}>Uygulamayı bağla adımına dön</button>
    </div>}
    {error && <p id="verification-code-error" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </div>
}
