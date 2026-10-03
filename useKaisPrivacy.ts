import {useSyncExternalStore} from 'react'

let privateArea = false
let dispose: (() => void) | undefined
const subscribers = new Set<() => void>()
export const getKaisPrivacy = () => privateArea

function observePrivacy() {
  const desktop = window.matchMedia('(hover: hover) and (pointer: fine)')
  let hovered: Element | null = null
  let reopenTimer: number | undefined
  // Inspect only explicit data-private="true" markers on targets/ancestors.
  // Never read values, names, labels or page content, and never send any data.
  const marked = (target: EventTarget | null) => target instanceof Element && target.isConnected &&
    Boolean(target.closest('[data-private="true"]'))
  const current = (focused: EventTarget | null = document.activeElement) =>
    marked(focused) || (desktop.matches && marked(hovered))
  const publish = (next: boolean) => {
    if (privateArea === next) return
    privateArea = next
    subscribers.forEach(listener => listener())
  }
  const cancelReopen = () => {
    if (reopenTimer !== undefined) window.clearTimeout(reopenTimer)
    reopenTimer = undefined
  }
  const refresh = (focused: EventTarget | null = document.activeElement) => {
    if (document.hidden) return
    if (current(focused)) {
      cancelReopen()
      publish(true)
    } else if (privateArea && reopenTimer === undefined) {
      reopenTimer = window.setTimeout(() => {
        reopenTimer = undefined
        if (!document.hidden) publish(current())
      }, 400)
    }
  }
  const focusIn = (event: FocusEvent) => refresh(event.target)
  const focusOut = (event: FocusEvent) => refresh(event.relatedTarget)
  const pointerOver = (event: PointerEvent) => {
    if (!desktop.matches || event.pointerType !== 'mouse') return
    hovered = event.target instanceof Element ? event.target : null
    refresh()
  }
  const pointerOut = (event: PointerEvent) => {
    if (!desktop.matches || event.pointerType !== 'mouse') return
    hovered = event.relatedTarget instanceof Element ? event.relatedTarget : null
    refresh()
  }
  const pointerPreference = () => { hovered = null; refresh() }
  // Observe marker changes (the controlled chat draft) and target removal only;
  // mutation payloads/content are not inspected.
  const observer = new MutationObserver(() => refresh())
  const detach = () => {
    document.removeEventListener('focusin', focusIn)
    document.removeEventListener('focusout', focusOut)
    document.removeEventListener('pointerover', pointerOver)
    document.removeEventListener('pointerout', pointerOut)
    desktop.removeEventListener('change', pointerPreference)
    observer.disconnect()
    cancelReopen()
    hovered = null
  }
  const visibility = () => {
    detach()
    if (document.hidden) return
    document.addEventListener('focusin', focusIn)
    document.addEventListener('focusout', focusOut)
    document.addEventListener('pointerover', pointerOver, {passive: true})
    document.addEventListener('pointerout', pointerOut, {passive: true})
    desktop.addEventListener('change', pointerPreference)
    observer.observe(document.documentElement, {subtree: true, attributes: true, attributeFilter: ['data-private'], childList: true})
    refresh()
  }
  document.addEventListener('visibilitychange', visibility)
  visibility()
  return () => {
    document.removeEventListener('visibilitychange', visibility)
    detach()
  }
}

function subscribe(listener: () => void) {
  subscribers.add(listener)
  if (!dispose) dispose = observePrivacy()
  return () => {
    subscribers.delete(listener)
    if (!subscribers.size) {
      dispose?.()
      dispose = undefined
      privateArea = false
    }
  }
}

export function useKaisPrivacy() {
  return useSyncExternalStore(subscribe, getKaisPrivacy, () => false)
}
