import { Children, cloneElement, createContext, isValidElement, type ReactElement, type ReactNode, useContext, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Check, LockKeyhole, X } from 'lucide-react'
import { API_BASE, userSessionToken } from './api'
import {emitKaisTargetReaction} from './kais-reactions'
import './premium-access.css'

type MemberAccess = {premium: boolean; ready: boolean; userId: string | null; error: string; openUpgrade: (label: string) => void}
const missingProvider = () => { throw new Error('MemberAccessProvider is required for premium interactions.') }
const AccessContext = createContext<MemberAccess>({premium: false, ready: false, userId: null, error: '', openUpgrade: missingProvider})
export const useMemberAccess = () => useContext(AccessContext)

export function MemberAccessProvider({children}: {children: ReactNode}) {
  const [access, setAccess] = useState<Omit<MemberAccess, 'openUpgrade'>>({premium: false, ready: false, userId: null, error: ''})
  const [upgradeOpen, setUpgradeOpen] = useState(false)
  const [lockedLabel, setLockedLabel] = useState('')
  const [toast, setToast] = useState('')
  const toastTimer = useRef<number | null>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    let controller = new AbortController()
    const refreshAccess = () => {
      controller.abort()
      controller = new AbortController()
      const requestController = controller
      if (!userSessionToken()) {
        setAccess({premium: false, ready: true, userId: null, error: 'Üyelik oturumu gerekli.'})
        return
      }
      void fetch(`${API_BASE}/v22/profile`, {signal: requestController.signal})
        .then(async response => {
          const payload = await response.json() as {user?: {id?: string; role?: string}; access?: {isPremium?: boolean}; detail?: string}
          if (!response.ok || !payload.user?.id) throw new Error(payload.detail || 'Üyelik yetkisi doğrulanamadı.')
          if (!requestController.signal.aborted) setAccess({premium: payload.access?.isPremium === true || payload.user.role === 'OWNER', ready: true, userId: payload.user.id, error: ''})
        })
        .catch((error: unknown) => {
          if (requestController.signal.aborted) return
          setAccess({premium: false, ready: true, userId: null, error: error instanceof Error ? error.message : 'Üyelik yetkisi doğrulanamadı.'})
        })
    }
    refreshAccess()
    const timer = window.setInterval(refreshAccess, 60000)
    window.addEventListener('focus', refreshAccess)
    window.addEventListener('protrebot-access-refresh', refreshAccess)
    return () => {
      controller.abort()
      window.clearInterval(timer)
      window.removeEventListener('focus', refreshAccess)
      window.removeEventListener('protrebot-access-refresh', refreshAccess)
    }
  }, [])
  useEffect(() => {
    if (!upgradeOpen || !dialog.current) return
    const previousFocus = document.activeElement
    const element = dialog.current
    element.showModal()
    emitKaisTargetReaction('premium-open', element)
    return () => {
      element.close()
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus()
    }
  }, [upgradeOpen])
  useEffect(() => () => { if (toastTimer.current !== null) window.clearTimeout(toastTimer.current) }, [])
  const openUpgrade = (label: string) => {
    if (!access.ready || !access.userId) return
    const seenKey = `protrebot-premium-card-seen:${access.userId}`
    if (sessionStorage.getItem(seenKey)) {
      setToast(`${label} için Premium üyelik gerekli.`)
      if (toastTimer.current !== null) window.clearTimeout(toastTimer.current)
      toastTimer.current = window.setTimeout(() => setToast(''), 4500)
    } else {
      sessionStorage.setItem(seenKey, '1')
      setLockedLabel(label)
      setUpgradeOpen(true)
    }
  }
  return <AccessContext.Provider value={{...access, openUpgrade}}>
    {access.error && <p className="memberAccessError" role="alert">{access.error}</p>}{children}
    {createPortal(<>
      {upgradeOpen && <dialog ref={dialog} className="premiumDialog" aria-labelledby="premium-title" aria-describedby="premium-description" onCancel={event => { event.preventDefault(); setUpgradeOpen(false) }} onClick={event => { if (event.target === event.currentTarget) setUpgradeOpen(false) }}>
        <div className="premiumCard">
          <button className="premiumClose" type="button" aria-label="Premium kartını kapat" onClick={() => setUpgradeOpen(false)}><X aria-hidden="true"/></button>
          <LockKeyhole className="premiumCardIcon" aria-hidden="true"/>
          <small>PREMIUM</small><h2 id="premium-title">Daha fazlasını aç</h2>
          <p id="premium-description">{lockedLabel} Premium üyeliğe dahildir. Mevcut işlem güvenlik kapıları geçerliliğini korur.</p>
          <ul><li><Check aria-hidden="true"/>Sınırsız Analyst analizi</li><li><Check aria-hidden="true"/>Ayrıntılı giriş, stop ve hedef seviyeleri</li><li><Check aria-hidden="true"/>Master Trade ve canlı işlem araçları</li></ul>
          <div className="premiumCardActions"><a className="premiumPrimary" href="/pricing">Premium'a geç</a><button className="premiumSecondary" type="button" onClick={() => setUpgradeOpen(false)}>Şimdi değil</button></div>
        </div>
      </dialog>}
      {toast && <aside className="premiumToast" role="status"><LockKeyhole aria-hidden="true"/><span>{toast}</span><a href="/pricing">Premium'a geç</a><button type="button" aria-label="Bildirimi kapat" onClick={() => setToast('')}><X aria-hidden="true"/></button></aside>}
    </>, document.body)}
  </AccessContext.Provider>
}

export function PremiumBoundary({label, children, compact = false}: {label: string; children?: ReactNode; compact?: boolean}) {
  const {premium, ready, userId, openUpgrade} = useMemberAccess()
  if (premium) return children
  return <section className={`premiumLocked${compact ? ' premiumLockedCompact' : ''}`} data-premium-locked={label}>
    {!compact && <div className="premiumPlaceholder" aria-hidden="true"><span/><span/><span/></div>}
    <button type="button" disabled={!ready || !userId} onClick={() => openUpgrade(label)}><LockKeyhole aria-hidden="true"/><span>{label} · Premium</span></button>
  </section>
}

type PresentationElement = ReactElement<{className?: string; children?: ReactNode; 'aria-label'?: string}>
const PRIVATE_CLASSES = new Set([
  'decisionList', 'masterTradeCases', 'triggerMonitor', 'masterTradeScannerDetails',
  'liveUxOrderGrid', 'masterTradeLiveConnectionCard', 'masterTradeLiveConfirmations',
  'masterTradeLiveControl', 'masterTradeLiveSignalGrid',
])
const LOCK_LABELS: Record<string, string> = {
  decisionList: 'Why this decision', masterTradeCases: 'Long / Short case',
  triggerMonitor: 'Trigger monitor', masterTradeScannerDetails: 'İlk 3 fırsat detayı',
  liveUxOrderGrid: 'Manuel emir ve dry-run', masterTradeLiveConnectionCard: 'Connect Account',
  masterTradeLiveConfirmations: 'LIVE onay akışı', masterTradeLiveControl: 'Arm ve Auto Trade',
  masterTradeLiveSignalGrid: 'Giriş / SL / TP seviyeleri',
}

function buttonText(node: ReactNode): string {
  return Children.toArray(node).map(child => typeof child === 'string' ? child : isValidElement(child) ? buttonText((child as PresentationElement).props.children) : '').join(' ')
}

function restrictTree(node: ReactNode): ReactNode {
  if (!isValidElement(node)) return node
  const element = node as PresentationElement
  const privateClass = element.props.className?.split(' ').find(className => PRIVATE_CLASSES.has(className))
  if (privateClass) return <PremiumBoundary key={element.key} label={LOCK_LABELS[privateClass]}/>
  const label = element.type === 'button' ? buttonText(element.props.children) : ''
  if (label && /START LIVE AUTO TRADE|CONNECT ACCOUNT|ARM LIVE|DRY.RUN|LIVE ORDER|LEVELS|SAVE SETUP|24 SAAT İZİN VER|LİMİTLERİ ONAYLA/i.test(label)) {
    return <PremiumBoundary key={element.key} label={label.trim()} compact/>
  }
  if (element.props.children === undefined) return element
  return cloneElement(element, {children: Children.map(element.props.children, restrictTree)})
}

export function PremiumWorkspace({children}: {children: ReactNode}) {
  const {premium} = useMemberAccess()
  return premium ? children : Children.map(children, restrictTree)
}
