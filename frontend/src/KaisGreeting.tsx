import {useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type RefObject} from 'react'
import {createPortal} from 'react-dom'
import {X} from 'lucide-react'
import './kais-greeting.css'

const SHOW_DELAY_MS = 2500
const VISIBLE_MS = 7000
const MAX_WIDTH = 300
const GAP = 16
const SESSION_KEY = 'kaisAiHintShown'
const DISMISSED_KEY = 'kaisAiHintDismissedUntil'
const DISMISS_MS = 7 * 24 * 60 * 60 * 1000
const GREETING = 'Merhaba, ben Kais AI. Bugün sana nasıl yardımcı olabilirim?'
type Phase = 'hidden' | 'entering' | 'visible' | 'closing'

type Props = {
  enabled: boolean
  chatOpen: boolean
  anchorRef: RefObject<HTMLButtonElement | null>
  anchorHost?: HTMLElement | null
  motionPaused?: boolean
  openRequest?: number
  onVisibilityChange?: (shown: boolean) => void
  onOpen: () => void
}

function storageWarning(error: unknown) {
  console.warn('Kais AI hint storage unavailable; continuing without persistence', error instanceof Error ? error.name : 'StorageError')
}

function automaticAllowed() {
  let seen = false
  let dismissedUntil = 0
  try { seen = sessionStorage.getItem(SESSION_KEY) === '1' }
  catch (error) { storageWarning(error) }
  try {
    const value = localStorage.getItem(DISMISSED_KEY)
    if (value !== null) {
      const timestamp = Number(value)
      if (Number.isFinite(timestamp) && timestamp >= 0) dismissedUntil = timestamp
      else console.warn('Kais AI hint dismissal timestamp invalid; ignoring presentation preference')
    }
  } catch (error) { storageWarning(error) }
  return !seen && dismissedUntil <= Date.now()
}

export default function KaisGreeting({enabled, chatOpen, anchorRef, anchorHost, motionPaused = false,
  openRequest = 0, onVisibilityChange, onOpen}: Props) {
  const [phase, setPhase] = useState<Phase>('hidden')
  const [cycle, setCycle] = useState(0)
  const [hovered, setHovered] = useState(false)
  const [touched, setTouched] = useState(false)
  const [focused, setFocused] = useState(false)
  const [hidden, setHidden] = useState(document.hidden)
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  const [progress, setProgress] = useState(1)
  const automaticShown = useRef(false)
  const waitingRemaining = useRef(SHOW_DELAY_MS)
  const visibleRemaining = useRef(VISIBLE_MS)
  const previousCycle = useRef(0)
  const previousRequest = useRef(openRequest)
  const bubbleRef = useRef<HTMLDivElement>(null)
  const [placement, setPlacement] = useState({left: GAP, top: GAP, width: MAX_WIDTH, arrow: GAP, fits: false})
  const shown = phase !== 'hidden' && !chatOpen
  const paused = hovered || touched || focused
  const pathname = window.location.pathname
  const authPage = /^\/(?:login|register|signup|forgot-password|reset-password|verify-email|email-verification)(?:\/|$)/i.test(pathname)

  const show = useCallback(() => {
    if (!anchorRef.current?.isConnected) return
    automaticShown.current = true
    try { sessionStorage.setItem(SESSION_KEY, '1') }
    catch (error) { storageWarning(error) }
    setCycle(value => value + 1)
    setPhase(value => value === 'hidden' ? 'entering' : value === 'closing' ? 'visible' : value)
  }, [anchorRef])

  const close = useCallback(() => setPhase(value => value === 'hidden' ? value : 'closing'), [])

  useEffect(() => {
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)')
    const visibility = () => setHidden(document.hidden)
    const motion = () => setReducedMotion(preference.matches)
    document.addEventListener('visibilitychange', visibility)
    preference.addEventListener('change', motion)
    return () => {
      document.removeEventListener('visibilitychange', visibility)
      preference.removeEventListener('change', motion)
    }
  }, [])

  useEffect(() => {
    if (previousRequest.current === openRequest) return
    previousRequest.current = openRequest
    if (!chatOpen) show()
  }, [openRequest, chatOpen, show])

  useEffect(() => { if (chatOpen) setPhase('hidden') }, [chatOpen])
  useEffect(() => { onVisibilityChange?.(shown && placement.fits) }, [shown, placement.fits, onVisibilityChange])

  useEffect(() => {
    if (!enabled || chatOpen || authPage || automaticShown.current || !automaticAllowed()) return
    let started: number | undefined
    let timer: number | undefined
    let fired = false
    const pause = () => {
      if (timer !== undefined) window.clearTimeout(timer)
      timer = undefined
      if (started !== undefined) waitingRemaining.current = Math.max(0, waitingRemaining.current - (performance.now() - started))
      started = undefined
    }
    const resume = () => {
      if (document.hidden || fired || automaticShown.current || timer !== undefined) return
      started = performance.now()
      timer = window.setTimeout(() => {
        fired = true
        timer = undefined
        started = undefined
        waitingRemaining.current = 0
        if (!document.hidden && !automaticShown.current && automaticAllowed()) show()
      }, waitingRemaining.current)
    }
    const visibility = () => { if (document.hidden) pause(); else resume() }
    document.addEventListener('visibilitychange', visibility)
    resume()
    return () => {
      pause()
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [enabled, chatOpen, authPage, cycle, show])

  useEffect(() => {
    if (phase !== 'entering' && phase !== 'closing') return
    const timer = window.setTimeout(() => {
      if (phase === 'entering') setPhase('visible')
      else {
        if (bubbleRef.current?.contains(document.activeElement)) anchorRef.current?.focus({preventScroll: true})
        setHovered(false); setTouched(false); setFocused(false)
        setPhase('hidden')
      }
    }, reducedMotion ? 150 : phase === 'entering' ? 200 : 300)
    return () => window.clearTimeout(timer)
  }, [phase, reducedMotion, anchorRef])

  // Cleanup captures elapsed time before pause/reset, so resuming never grants a fresh lifetime.
  useEffect(() => {
    if (previousCycle.current !== cycle) {
      previousCycle.current = cycle
      visibleRemaining.current = VISIBLE_MS
      setProgress(1)
    }
    if (phase !== 'visible' || paused || chatOpen) return
    let started: number | undefined
    let timer: number | undefined
    let frame: number | undefined
    const tick = () => {
      if (started === undefined) return
      setProgress(Math.max(0, visibleRemaining.current - (performance.now() - started)) / VISIBLE_MS)
      frame = requestAnimationFrame(tick)
    }
    const pause = () => {
      if (timer !== undefined) window.clearTimeout(timer)
      if (frame !== undefined) cancelAnimationFrame(frame)
      timer = undefined
      frame = undefined
      if (started !== undefined) visibleRemaining.current = Math.max(0, visibleRemaining.current - (performance.now() - started))
      started = undefined
      setProgress(visibleRemaining.current / VISIBLE_MS)
    }
    const resume = () => {
      if (document.hidden || timer !== undefined) return
      started = performance.now()
      if (!reducedMotion) tick()
      timer = window.setTimeout(() => {
        timer = undefined
        started = undefined
        visibleRemaining.current = 0
        if (frame !== undefined) cancelAnimationFrame(frame)
        frame = undefined
        setProgress(0)
        close()
      }, visibleRemaining.current)
    }
    const visibility = () => { if (document.hidden) pause(); else resume() }
    document.addEventListener('visibilitychange', visibility)
    resume()
    return () => { pause(); document.removeEventListener('visibilitychange', visibility) }
  }, [phase, paused, chatOpen, cycle, reducedMotion, close])

  useEffect(() => {
    if (!shown) return
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      close()
    }
    document.addEventListener('keydown', escape)
    return () => document.removeEventListener('keydown', escape)
  }, [shown, close])

  useLayoutEffect(() => {
    if (!shown || !bubbleRef.current) return
    const element = bubbleRef.current
    let frame: number | undefined
    const update = () => {
      if (document.hidden) return
      const button = anchorRef.current
      const anchor = (button?.querySelector('.kaisEye') ?? button)?.getBoundingClientRect()
      if (!anchor) {
        setPlacement(previous => previous.fits ? {...previous, fits: false} : previous)
        return
      }
      const viewport = window.visualViewport
      const viewLeft = viewport?.offsetLeft ?? 0
      const viewTop = viewport?.offsetTop ?? 0
      const viewWidth = viewport?.width ?? window.innerWidth
      const viewHeight = viewport?.height ?? window.innerHeight
      const width = viewWidth <= 480 ? Math.max(0, viewWidth - GAP * 2) : Math.min(MAX_WIDTH, viewWidth - GAP * 2)
      element.style.width = `${width}px`
      const left = Math.max(viewLeft + GAP, Math.min(anchor.right - width, viewLeft + viewWidth - GAP - width))
      const top = anchor.bottom + 10
      const arrow = Math.max(GAP, Math.min(anchor.left + anchor.width / 2 - left, width - GAP))
      const fits = anchor.width > 0 && anchor.height > 0 && width > 0 &&
        top >= viewTop + GAP && top + element.offsetHeight <= viewTop + viewHeight - GAP
      setPlacement(previous => previous.left === left && previous.top === top && previous.width === width &&
        previous.arrow === arrow && previous.fits === fits ? previous : {left, top, width, arrow, fits})
    }
    const trackLayout = () => {
      frame = undefined
      if (document.hidden) return
      update()
      frame = requestAnimationFrame(trackLayout)
    }
    const visibility = () => {
      if (frame !== undefined) cancelAnimationFrame(frame)
      frame = undefined
      if (!document.hidden) trackLayout()
    }
    visibility()
    document.addEventListener('visibilitychange', visibility)
    const anchorLayout = new ResizeObserver(update)
    for (let host: HTMLElement | null = anchorRef.current; host; host = host.parentElement) anchorLayout.observe(host)
    for (const control of anchorRef.current?.closest('header')?.querySelectorAll('button, a, input, [role="button"]') ?? []) anchorLayout.observe(control)
    window.addEventListener('resize', update)
    window.addEventListener('scroll', update, {passive: true, capture: true})
    document.addEventListener('transitionrun', update, true)
    document.addEventListener('transitionend', update, true)
    document.addEventListener('transitioncancel', update, true)
    window.visualViewport?.addEventListener('resize', update)
    window.visualViewport?.addEventListener('scroll', update)
    return () => {
      anchorLayout.disconnect()
      if (frame !== undefined) cancelAnimationFrame(frame)
      document.removeEventListener('visibilitychange', visibility)
      window.removeEventListener('resize', update)
      window.removeEventListener('scroll', update, true)
      document.removeEventListener('transitionrun', update, true)
      document.removeEventListener('transitionend', update, true)
      document.removeEventListener('transitioncancel', update, true)
      window.visualViewport?.removeEventListener('resize', update)
      window.visualViewport?.removeEventListener('scroll', update)
    }
  }, [shown, anchorRef, anchorHost])

  const style: CSSProperties & {'--kais-greeting-arrow': string} = {left: placement.left, top: placement.top,
    width: placement.width, visibility: placement.fits ? 'visible' : 'hidden', '--kais-greeting-arrow': `${placement.arrow}px`}
  return createPortal(<div ref={bubbleRef} role="status" aria-live="polite" aria-atomic="true" lang="tr"
    className={shown ? 'kaisGreeting' : 'kaisGreetingAnnouncer'} data-kais-greeting={shown || undefined}
    data-phase={phase} data-paused={paused || hidden} data-motion-paused={motionPaused} style={shown ? style : undefined}
    onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
    onTouchStart={() => setTouched(true)} onTouchEnd={() => setTouched(false)} onTouchCancel={() => setTouched(false)}
    onFocus={() => setFocused(true)} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false) }}>
    {shown && <>
      <span className="kaisGreetingArrow" aria-hidden="true"/>
      <div className="kaisGreetingBrand"><span aria-hidden="true"/>Kais AI</div>
      <button type="button" className="kaisGreetingOpen" aria-label="Karşılama mesajı, sohbeti aç"
        onClick={() => { close(); onOpen() }}>{GREETING}</button>
      <button type="button" className="kaisGreetingLink" onClick={() => { close(); onOpen() }}>Sohbeti aç →</button>
      <button type="button" className="kaisGreetingClose" aria-label="Bildirimi kapat"
        onClick={() => {
          try { localStorage.setItem(DISMISSED_KEY, String(Date.now() + DISMISS_MS)) }
          catch (error) { storageWarning(error) }
          close()
        }}><X aria-hidden="true" size={12} strokeWidth={1.5}/></button>
      <div className="kaisGreetingProgress" aria-hidden="true"><span style={{transform: `scaleX(${progress})`}}/></div>
    </>}
  </div>, document.body)
}
