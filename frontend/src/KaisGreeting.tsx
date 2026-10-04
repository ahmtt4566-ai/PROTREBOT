import {useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type RefObject} from 'react'
import {createPortal} from 'react-dom'
import {X} from 'lucide-react'
import './kais-greeting.css'

const SHOW_DELAY_MS = 2000
const VISIBLE_MS = 8000
const MAX_WIDTH = 280
const GAP = 12
const GREETING = 'Merhaba, ben Kais AI. Bugün sana nasıl yardımcı olabilirim?'

type Props = {
  userId?: string
  enabled: boolean
  chatOpen: boolean
  anchorRef: RefObject<HTMLButtonElement | null>
  anchorHost?: HTMLElement | null
  motionPaused?: boolean
  onOpen: () => void
}

function localDate() {
  const date = new Date()
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

export default function KaisGreeting({userId, enabled, chatOpen, anchorRef, anchorHost, motionPaused = false, onOpen}: Props) {
  const key = `protrebot-kais-greeting:${userId ? encodeURIComponent(userId) : 'general'}`
  const [visible, setVisible] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  const bubbleRef = useRef<HTMLDivElement>(null)
  const [placement, setPlacement] = useState({left: GAP, top: GAP, width: MAX_WIDTH, arrow: GAP, fits: false})
  const shown = visible && enabled && !chatOpen && !dismissed

  useEffect(() => {
    if (chatOpen) setDismissed(true)
    if (!enabled || chatOpen || dismissed) { setVisible(false); return }
    // Local presentation metadata only: user-scoped date, no assistant/network request.
    try {
      if (localStorage.getItem(key) === localDate()) return
    } catch (error) {
      if (!(error instanceof DOMException)) throw error
      console.warn('Kais AI greeting disabled: local storage unavailable', error.name)
      return
    }
    let phase: 'waiting' | 'showing' | 'done' = 'waiting'
    let remaining = SHOW_DELAY_MS
    let started: number | undefined
    let timer: number | undefined
    const pause = () => {
      if (timer !== undefined) window.clearTimeout(timer)
      timer = undefined
      if (started !== undefined) remaining = Math.max(0, remaining - (performance.now() - started))
      started = undefined
    }
    const advance = () => {
      timer = undefined
      started = undefined
      if (document.hidden) { remaining = 0; return }
      if (phase === 'showing') {
        phase = 'done'
        setVisible(false)
        return
      }
      if (!anchorRef.current?.isConnected) { phase = 'done'; return }
      try {
        const today = localDate()
        if (localStorage.getItem(key) === today) { phase = 'done'; return }
        localStorage.setItem(key, today)
      } catch (error) {
        if (!(error instanceof DOMException)) throw error
        console.warn('Kais AI greeting disabled: local storage unavailable', error.name)
        phase = 'done'
        return
      }
      phase = 'showing'
      remaining = VISIBLE_MS
      setVisible(true)
      resume()
    }
    const resume = () => {
      if (document.hidden || phase === 'done' || timer !== undefined) return
      started = performance.now()
      timer = window.setTimeout(advance, remaining)
    }
    const visibility = () => { if (document.hidden) pause(); else resume() }
    document.addEventListener('visibilitychange', visibility)
    resume()
    return () => { phase = 'done'; pause(); document.removeEventListener('visibilitychange', visibility) }
  }, [key, enabled, chatOpen, dismissed, anchorRef])

  useLayoutEffect(() => {
    if (!shown || !bubbleRef.current) return
    const element = bubbleRef.current
    const update = () => {
      const anchor = anchorRef.current?.getBoundingClientRect()
      if (!anchor) return
      const viewport = window.visualViewport
      const viewLeft = viewport?.offsetLeft ?? 0
      const viewTop = viewport?.offsetTop ?? 0
      const viewWidth = viewport?.width ?? window.innerWidth
      const viewHeight = viewport?.height ?? window.innerHeight
      const width = Math.min(MAX_WIDTH, Math.max(0, viewWidth - GAP * 2))
      element.style.width = `${width}px`
      const left = Math.max(viewLeft + GAP, Math.min(anchor.right - width, viewLeft + viewWidth - GAP - width))
      const top = anchor.bottom + GAP
      const arrow = Math.max(GAP, Math.min(anchor.left + anchor.width / 2 - left, width - GAP))
      const fits = anchor.width > 0 && anchor.height > 0 && width > 0 &&
        top >= viewTop + GAP && top + element.offsetHeight <= viewTop + viewHeight - GAP
      setPlacement(previous => previous.left === left && previous.top === top && previous.width === width &&
        previous.arrow === arrow && previous.fits === fits ? previous : {left, top, width, arrow, fits})
    }
    update()
    const anchorLayout = new ResizeObserver(update)
    for (let host: HTMLElement | null = anchorRef.current; host; host = host.parentElement) anchorLayout.observe(host)
    window.addEventListener('resize', update)
    window.addEventListener('scroll', update, {passive: true, capture: true})
    window.visualViewport?.addEventListener('resize', update)
    window.visualViewport?.addEventListener('scroll', update)
    return () => {
      anchorLayout.disconnect()
      window.removeEventListener('resize', update)
      window.removeEventListener('scroll', update, true)
      window.visualViewport?.removeEventListener('resize', update)
      window.visualViewport?.removeEventListener('scroll', update)
    }
  }, [shown, anchorRef, anchorHost])

  const dismiss = () => { setDismissed(true); setVisible(false) }
  const style: CSSProperties & {'--kais-greeting-arrow': string} = {left: placement.left, top: placement.top,
    width: placement.width, visibility: placement.fits ? 'visible' : 'hidden', '--kais-greeting-arrow': `${placement.arrow}px`}
  return createPortal(<div ref={bubbleRef} role="status" aria-live="polite" aria-atomic="true" lang="tr"
    className={shown ? 'kaisGreeting' : 'kaisGreetingAnnouncer'} data-kais-greeting={shown || undefined}
    data-motion-paused={motionPaused} style={shown ? style : undefined}>
    {shown && <>
      <button type="button" className="kaisGreetingOpen" aria-label="Karşılama mesajı, sohbeti aç"
        onClick={() => { dismiss(); onOpen() }}>{GREETING}</button>
      <button type="button" className="kaisGreetingClose" aria-label="Kais AI karşılama balonunu kapat"
        onClick={() => { dismiss(); anchorRef.current?.focus({preventScroll: true}) }}><X aria-hidden="true" size={16}/></button>
    </>}
  </div>, document.body)
}
