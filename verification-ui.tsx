import {useEffect, useRef, useState} from 'react'
import {Check, Copy, Download, Mail, Shield} from 'lucide-react'
import './verification-ui.css'

export function VerificationSymbol({kind = 'shield', success = false}: {kind?: 'shield' | 'mail'; success?: boolean}) {
  const Icon = kind === 'mail' ? Mail : Shield
  return <div className={`verificationSymbol ${success ? 'is-success' : ''}`} aria-hidden="true">
    <Icon className="verificationSymbolOutline"/>
    {success && <Check className="verificationSymbolCheck"/>}
  </div>
}

export function VerificationSteps({step}: {step: 1 | 2 | 3}) {
  return <ol className="verificationSteps" aria-label="Kurulum adımları">
    {['Uygulamayı bağla', 'Kodu doğrula', 'Yedek kodları kaydet'].map((label, index) =>
      <li key={label} className={index + 1 < step ? 'is-complete' : ''} aria-current={index + 1 === step ? 'step' : undefined}>
        <span>{index + 1 < step ? <Check aria-label="Tamamlandı"/> : index + 1}</span><small>{label}</small>
      </li>)}
  </ol>
}

export function RecoveryCodes({codes, onSaved}: {codes: string[]; onSaved: () => void}) {
  const [saved, setSaved] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [working, setWorking] = useState(false)
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => {heading.current?.focus()}, [])
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 3000)
    return () => window.clearTimeout(timer)
  }, [notice])
  const copy = async () => {
    setWorking(true); setError(''); setNotice('')
    try {await navigator.clipboard.writeText(codes.join('\n')); setNotice('Kopyalandı')}
    catch {setError('Kodlar kopyalanamadı. Tarayıcının pano iznini kontrol et veya indir.')}
    finally {setWorking(false)}
  }
  const download = () => {
    setError(''); setNotice('')
    let url: string | undefined
    try {
      url = URL.createObjectURL(new Blob(['KaisTrade — Tek kullanımlık yedek kodlar\n\n', codes.join('\n'), '\n'], {type: 'text/plain;charset=utf-8'}))
      const link = document.createElement('a')
      link.href = url; link.download = 'kaistrade-yedek-kodlar.txt'
      document.body.append(link); link.click(); link.remove()
      setNotice('İndirme başlatıldı')
    } catch {setError('Kodlar indirilemedi. Kopyala seçeneğini kullan.')}
    finally {if (url) {const downloadUrl = url; window.setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000)}}
  }
  return <div className="verificationFlow">
    <VerificationSteps step={3}/>
    <VerificationSymbol success/>
    <h3 ref={heading} tabIndex={-1}>Yedek kodlarını kaydet</h3>
    <p>Telefonuna erişemediğinde bu kodlarla giriş yapabilirsin. Her kod yalnızca bir kez kullanılabilir ve bu ekran kapandıktan sonra tekrar gösterilmez.</p>
    <ul className="verificationRecoveryCodes" data-private="true">{codes.map(value => <li key={value}><code>{value}</code></li>)}</ul>
    <div className="verificationActions"><button type="button" disabled={working} onClick={() => void copy()}><Copy/>Kopyala</button><button type="button" onClick={download}><Download/>İndir</button></div>
    {notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
    <label className="verificationSaved"><input type="checkbox" checked={saved} onChange={event => setSaved(event.target.checked)}/>Kodları güvenli bir yere kaydettim</label>
    <button type="button" className="verificationPrimary" disabled={!saved} onClick={onSaved}>Bitir</button>
  </div>
}
