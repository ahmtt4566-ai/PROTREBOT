import {useEffect, useRef, useState} from 'react'
import {useAssistantMotion} from './useAssistantPresentation'
import {getKaisPrivacy, useKaisPrivacy} from './useKaisPrivacy'
import {isKaisReaction, KAIS_REACTION_EVENT, type KaisReaction} from './kais-reactions'
import type {KaisEyeState} from './KaisEye'

export type KaisEyeLook = {x: number; y: number}
const clamp = (value: number) => Math.max(-1, Math.min(1, value))
const randomDelay = (min: number, max: number) => min + Math.random() * (max - min)

export function useKaisEye(requestedState: KaisEyeState, lookAt?: KaisEyeLook) {
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
  const controlledX = lookAt === undefined ? undefined : clamp(lookAt.x)
  const controlledY = lookAt === undefined ? undefined : clamp(lookAt.y)

  useEffect(() => {
    const eye = eyeRef.current
    const gaze = gazeRef.current
    if (!eye || !gaze) return
    const interactionHost = eye.closest<HTMLButtonElement>('button') ?? eye
    let stopped = false
    let frame: number | undefined
    let pending: KaisEyeLook | undefined
    let hovered = false
    let lastPointer = -Infinity
    let glanceReturn: number | undefined
    let touchReturn: number | undefined
    let engagementEnd: number | undefined
    let reactionEnd: number | undefined
    let inactivityTimer: number | undefined
    let pointReaction = false
    let sleepy = false
    let drowsy = false
    let lastActivity = performance.now()
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
    const timers = new Set<number>()
    const paint = ({x, y}: KaisEyeLook) => {
      const next = {x: clamp(x), y: clamp(y)}
      eye.dataset.lookX = String(next.x)
      eye.dataset.lookY = String(next.y)
      gaze.style.transform = `translate(${next.x * 3}px, ${next.y * 2}px)`
    }
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
    // No page content/field values are read or sent: only event types/occurrence and coordinates,
    // our own SVG geometry and the shared explicit-marker privacy signal are used.
    const mayInteract = () => motion && !stopped && !document.hidden && !reduced.matches &&
      !getKaisPrivacy()
    const center = () => {
      if (frame !== undefined) window.cancelAnimationFrame(frame)
      frame = undefined
      pending = undefined
      paint({x: controlledX ?? 0, y: controlledY ?? 0})
    }
    setBlinking(false)
    setEngaged(false)
    setSlowBlinking(false)
    setAttention('awake')
    setReaction('none')
    paint(motion ? {x: controlledX ?? 0, y: controlledY ?? 0} : {x: 0, y: 0})
    if (!motion) return

    const desktopPointer = window.matchMedia('(hover: hover) and (pointer: fine)')
    const slowBlink = () => {
      setBlinking(false)
      setSlowBlinking(true)
      schedule(() => setSlowBlinking(false), 600)
    }
    const watchInactivity = () => {
      if (!mayInteract()) return
      const elapsed = performance.now() - lastActivity
      if (elapsed >= 180000) {
        drowsy = true
        setAttention('drowsy')
        setBlinking(false); setSlowBlinking(false)
        center()
        return
      }
      if (elapsed >= 60000 && !sleepy) {
        sleepy = true
        setAttention('sleepy')
        slowBlink()
      }
      inactivityTimer = schedule(watchInactivity, Math.max(1, (sleepy ? 180000 : 60000) - elapsed))
    }
    // Activity is occurrence-only: never inspect keys, targets or field values.
    const activity = () => {
      if (!mayInteract() || state !== 'idle') return
      lastActivity = performance.now()
      if (!sleepy && !drowsy) return
      sleepy = false; drowsy = false
      setAttention('awake'); setBlinking(false); setSlowBlinking(false)
      cancel(inactivityTimer)
      inactivityTimer = schedule(watchInactivity, 60000)
    }
    const stopPointReaction = () => {
      if (!pointReaction) return
      pointReaction = false
      cancel(reactionEnd)
      setReaction('none')
    }
    const queueLook = (event: PointerEvent) => {
      pending = {x: event.clientX, y: event.clientY}
      if (frame !== undefined) return
      frame = window.requestAnimationFrame(() => {
        frame = undefined
        const point = pending
        pending = undefined
        if (!point || !mayInteract()) return
        const rect = eye.getBoundingClientRect()
        paint({x: (point.x - rect.x - rect.width / 2) / Math.max(1, window.innerWidth / 2),
          y: (point.y - rect.y - rect.height / 2) / Math.max(1, window.innerHeight / 2)})
      })
    }
    const move = (event: PointerEvent) => {
      activity()
      if (event.pointerType !== 'mouse' || controlledX !== undefined || !mayInteract() ||
          !desktopPointer.matches) return
      lastPointer = performance.now()
      stopPointReaction()
      cancel(glanceReturn); cancel(touchReturn)
      queueLook(event)
    }
    const touch = (event: PointerEvent) => {
      activity()
      if (event.pointerType === 'mouse' || controlledX !== undefined || !mayInteract()) return
      lastPointer = performance.now()
      stopPointReaction()
      cancel(glanceReturn); cancel(touchReturn)
      queueLook(event)
      touchReturn = schedule(center, 650)
    }
    const enter = (event: Event) => {
      if (!(event instanceof PointerEvent) || event.pointerType !== 'mouse' || !mayInteract()) return
      hovered = true
      activity()
      setBlinking(false); setEngaged(true)
    }
    const leave = () => { hovered = false; setEngaged(false) }
    const press = () => {
      if (!mayInteract()) return
      cancel(engagementEnd)
      setBlinking(false); setEngaged(true)
      engagementEnd = schedule(() => setEngaged(hovered), 300)
    }
    const reset = () => {
      cancel(glanceReturn); cancel(touchReturn); cancel(engagementEnd); cancel(reactionEnd)
      pointReaction = false; setReaction('none')
      center(); leave()
    }
    const out = (event: PointerEvent) => { if (!event.relatedTarget) reset() }
    const blink = () => {
      if (!hovered && !drowsy && mayInteract()) {
        if (sleepy) slowBlink()
        else { setBlinking(true); schedule(() => setBlinking(false), 150) }
      }
      schedule(blink, randomDelay(3000, 6000))
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
      if (!isKaisReaction(event.detail)) {
        console.warn('Ignored invalid Kais visual reaction')
        return
      }
      const detail = event.detail
      const directed = detail.type === 'premium-open' || detail.type === 'navigation'
      if (directed && controlledX !== undefined) return
      cancel(reactionEnd)
      if (pointReaction) center()
      pointReaction = false
      setReaction(detail.type)
      if (detail.type === 'premium-open' || detail.type === 'navigation') {
        cancel(glanceReturn); cancel(touchReturn)
        center()
        const rect = eye.getBoundingClientRect()
        paint({x: (detail.point.x - rect.x - rect.width / 2) / Math.max(1, window.innerWidth / 2),
          y: (detail.point.y - rect.y - rect.height / 2) / Math.max(1, window.innerHeight / 2)})
        pointReaction = true
        lastPointer = performance.now()
      } else if (detail.type === 'error') {
        setBlinking(false); setSlowBlinking(false)
      }
      reactionEnd = schedule(() => {
        if (pointReaction) center()
        pointReaction = false
        setReaction('none')
      }, detail.type === 'unread' ? 1200 : detail.type === 'error' ? 600 : 650)
    }
    if (state === 'idle') {
      inactivityTimer = schedule(watchInactivity, 60000)
      schedule(blink, randomDelay(3000, 6000))
      if (controlledX === undefined) schedule(glance, randomDelay(30000, 60000))
    }
    window.addEventListener('pointermove', move, {passive: true})
    window.addEventListener('pointerdown', touch, {passive: true})
    if (controlledX === undefined) {
      window.addEventListener('pointerout', out, {passive: true})
      window.addEventListener('pointercancel', reset, {passive: true})
    }
    window.addEventListener(KAIS_REACTION_EVENT, react)
    window.addEventListener('keydown', activity)
    window.addEventListener('scroll', activity, {passive: true, capture: true})
    interactionHost.addEventListener('pointerenter', enter, {passive: true})
    interactionHost.addEventListener('pointerleave', leave, {passive: true})
    interactionHost.addEventListener('pointerdown', press, {passive: true})
    window.addEventListener('blur', reset)
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
      interactionHost.removeEventListener('pointerenter', enter)
      interactionHost.removeEventListener('pointerleave', leave)
      interactionHost.removeEventListener('pointerdown', press)
      window.removeEventListener('blur', reset)
    }
  }, [motion, state, controlledX, controlledY])

  return {eyeRef, gazeRef, state, motion, transitions: !paused && state !== 'error' && state !== 'off',
    blinking: motion && blinking, engaged: motion && engaged, slowBlinking: motion && slowBlinking,
    attention: motion && state === 'idle' ? attention : 'awake', reaction: motion ? reaction : 'none'}
}
