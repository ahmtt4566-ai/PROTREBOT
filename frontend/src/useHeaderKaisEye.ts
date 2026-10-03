import {useEffect, useRef, useState} from 'react'
import type {KaisEyeState} from '../../KaisEye'
import {useAssistantMotion} from '../../useAssistantPresentation'
import {getKaisPrivacy, useKaisPrivacy} from '../../useKaisPrivacy'
import {isKaisReaction, KAIS_REACTION_EVENT, type KaisReaction} from '../../kais-reactions'

export const BLINK_MIN_MS = 9000
export const BLINK_MAX_MS = 11000
export const FIRST_BLINK_MS = 3000
export const BLINK_DURATION_MS = 200

type Point = {x: number; y: number}
const clamp = (value: number) => Math.max(-1, Math.min(1, value))
const randomDelay = (min: number, max: number) => min + Math.random() * (max - min)

export function useHeaderKaisEye(requestedState: KaisEyeState) {
  const eyeRef = useRef<SVGSVGElement>(null)
  const gazeRef = useRef<SVGGElement>(null)
  const paused = useAssistantMotion()
  const privateArea = useKaisPrivacy()
  const state = privateArea ? 'private' : requestedState
  const motion = !paused && (state === 'idle' || state === 'thinking')
  const [blinking, setBlinking] = useState(false)
  const [engaged, setEngaged] = useState(false)
  const [slowBlinking, setSlowBlinking] = useState(false)
  const [attention, setAttention] = useState<'awake' | 'sleepy' | 'drowsy'>('awake')
  const [reaction, setReaction] = useState<KaisReaction['type'] | 'none'>('none')

  useEffect(() => {
    const eye = eyeRef.current
    const gaze = gazeRef.current
    if (!eye || !gaze) return
    const host = eye.closest<HTMLButtonElement>('button') ?? eye
    let stopped = false
    let frame: number | undefined
    let pending: Point | undefined
    let touchReturn: number | undefined
    let glanceReturn: number | undefined
    let reactionEnd: number | undefined
    let engagementEnd: number | undefined
    let inactivityTimer: number | undefined
    let hovered = false
    let directed = false
    let sleepy = false
    let drowsy = false
    let lastActivity = performance.now()
    let lastPointer = -Infinity
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
    const timers = new Set<number>()
    const cancel = (timer: number | undefined) => {
      if (timer !== undefined) { window.clearTimeout(timer); timers.delete(timer) }
    }
    const schedule = (callback: () => void, delay: number) => {
      const timer = window.setTimeout(() => {
        timers.delete(timer)
        if (!stopped && !document.hidden && !reduced.matches) callback()
      }, delay)
      timers.add(timer)
      return timer
    }
    // Only pointer coordinates, occurrence-only events, our own SVG geometry and
    // explicit data-private markers are used. No field values/content or network I/O.
    const mayInteract = () => motion && !stopped && !document.hidden && !reduced.matches && !getKaisPrivacy()
    const paint = ({x, y}: Point) => {
      const boundedX = clamp(x)
      const boundedY = clamp(y)
      const length = Math.max(1, Math.hypot(boundedX, boundedY))
      const nextX = boundedX / length
      const nextY = boundedY / length
      eye.dataset.lookX = String(nextX)
      eye.dataset.lookY = String(nextY)
      gaze.style.transform = `translate(${nextX * 7}px, ${nextY * 7 * .6}px)`
    }
    const center = () => {
      if (frame !== undefined) window.cancelAnimationFrame(frame)
      frame = undefined
      pending = undefined
      paint({x: 0, y: 0})
    }
    const pointLook = ({x, y}: Point) => {
      const rect = eye.getBoundingClientRect()
      paint({x: (x - rect.x - rect.width / 2) / Math.max(1, window.innerWidth / 2),
        y: (y - rect.y - rect.height / 2) / Math.max(1, window.innerHeight / 2)})
    }
    setBlinking(false); setEngaged(false); setSlowBlinking(false)
    setAttention('awake'); setReaction('none')
    center()
    if (!motion) return

    const slowBlink = () => {
      setBlinking(false); setSlowBlinking(true)
      schedule(() => setSlowBlinking(false), 600)
    }
    const watchInactivity = () => {
      if (!mayInteract()) return
      const elapsed = performance.now() - lastActivity
      if (elapsed >= 180000) {
        drowsy = true
        setAttention('drowsy'); setBlinking(false); setSlowBlinking(false)
        center()
        return
      }
      if (elapsed >= 60000 && !sleepy) {
        sleepy = true; setAttention('sleepy'); slowBlink()
      }
      inactivityTimer = schedule(watchInactivity, Math.max(1, (sleepy ? 180000 : 60000) - elapsed))
    }
    const activity = () => {
      if (!mayInteract() || state !== 'idle') return
      lastActivity = performance.now()
      if (!sleepy && !drowsy) return
      sleepy = false; drowsy = false
      setAttention('awake'); setBlinking(false); setSlowBlinking(false)
      cancel(inactivityTimer)
      inactivityTimer = schedule(watchInactivity, 60000)
    }
    const queueLook = (event: PointerEvent) => {
      pending = {x: event.clientX, y: event.clientY}
      if (frame !== undefined) return
      frame = window.requestAnimationFrame(() => {
        frame = undefined
        const point = pending
        pending = undefined
        if (point && mayInteract()) pointLook(point)
      })
    }
    const move = (event: PointerEvent) => {
      activity()
      if (!mayInteract()) return
      lastPointer = performance.now()
      if (directed) { directed = false; cancel(reactionEnd); setReaction('none') }
      cancel(glanceReturn); cancel(touchReturn)
      queueLook(event)
      if (event.pointerType !== 'mouse') touchReturn = schedule(center, 650)
    }
    const touch = (event: PointerEvent) => {
      if (event.pointerType !== 'mouse') move(event)
    }
    const enter = (event: Event) => {
      if (!(event instanceof PointerEvent) || event.pointerType !== 'mouse' || !mayInteract()) return
      hovered = true; activity(); setBlinking(false); setEngaged(true)
    }
    const leave = () => { hovered = false; setEngaged(false) }
    const press = () => {
      if (!mayInteract()) return
      cancel(engagementEnd)
      setBlinking(false); setEngaged(true)
      engagementEnd = schedule(() => setEngaged(hovered), 300)
    }
    const reset = () => {
      cancel(glanceReturn); cancel(touchReturn); cancel(reactionEnd); cancel(engagementEnd)
      directed = false; setReaction('none'); center(); leave()
    }
    const out = (event: PointerEvent) => { if (!event.relatedTarget) reset() }
    const blink = () => {
      if (!hovered && !drowsy && mayInteract()) {
        if (sleepy) slowBlink()
        else { setBlinking(true); schedule(() => setBlinking(false), BLINK_DURATION_MS) }
      }
      schedule(blink, randomDelay(BLINK_MIN_MS, BLINK_MAX_MS))
    }
    const glance = () => {
      if (!hovered && !drowsy && mayInteract() && performance.now() - lastPointer > 1200) {
        paint({x: Math.random() < .5 ? -.6 : .6, y: 0})
        glanceReturn = schedule(center, 500)
      }
      schedule(glance, randomDelay(30000, 60000))
    }
    const react = (event: CustomEvent<KaisReaction>) => {
      if (!mayInteract()) return
      if (!isKaisReaction(event.detail)) { console.warn('Ignored invalid Kais visual reaction'); return }
      const detail = event.detail
      cancel(reactionEnd)
      if (directed) center()
      directed = false
      setReaction(detail.type)
      if (detail.type === 'premium-open' || detail.type === 'navigation') {
        cancel(glanceReturn); cancel(touchReturn); center()
        pointLook(detail.point)
        directed = true; lastPointer = performance.now()
      } else if (detail.type === 'error') {
        setBlinking(false); setSlowBlinking(false)
      }
      reactionEnd = schedule(() => {
        if (directed) center()
        directed = false; setReaction('none')
      }, detail.type === 'unread' ? 1200 : detail.type === 'error' ? 600 : 650)
    }
    if (state === 'idle') {
      inactivityTimer = schedule(watchInactivity, 60000)
      schedule(blink, FIRST_BLINK_MS)
      schedule(glance, randomDelay(30000, 60000))
    }
    window.addEventListener('pointermove', move, {passive: true})
    window.addEventListener('pointerdown', touch, {passive: true})
    window.addEventListener('pointerout', out, {passive: true})
    window.addEventListener('pointercancel', reset, {passive: true})
    window.addEventListener(KAIS_REACTION_EVENT, react)
    window.addEventListener('keydown', activity)
    window.addEventListener('scroll', activity, {passive: true, capture: true})
    window.addEventListener('blur', reset)
    host.addEventListener('pointerenter', enter, {passive: true})
    host.addEventListener('pointerleave', leave, {passive: true})
    host.addEventListener('pointerdown', press, {passive: true})
    return () => {
      stopped = true
      timers.forEach(timer => window.clearTimeout(timer))
      if (frame !== undefined) window.cancelAnimationFrame(frame)
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerdown', touch)
      window.removeEventListener('pointerout', out)
      window.removeEventListener('pointercancel', reset)
      window.removeEventListener(KAIS_REACTION_EVENT, react)
      window.removeEventListener('keydown', activity)
      window.removeEventListener('scroll', activity, true)
      window.removeEventListener('blur', reset)
      host.removeEventListener('pointerenter', enter)
      host.removeEventListener('pointerleave', leave)
      host.removeEventListener('pointerdown', press)
    }
  }, [motion, state])

  return {eyeRef, gazeRef, state, motion, transitions: !paused && state !== 'error' && state !== 'off',
    blinking: motion && blinking, engaged: motion && engaged, slowBlinking: motion && slowBlinking,
    attention: motion && state === 'idle' ? attention : 'awake', reaction: motion ? reaction : 'none'}
}
