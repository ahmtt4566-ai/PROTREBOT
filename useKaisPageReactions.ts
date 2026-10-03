import {useEffect, useRef, type RefObject} from 'react'
import {emitKaisReaction, emitKaisTargetReaction, kaisReactionsEnabled} from './kais-reactions'

export function useKaisWorkspaceReaction(identity: string, target: RefObject<HTMLElement | null>, ready = true) {
  const previous = useRef(identity)
  useEffect(() => {
    if (!ready || previous.current === identity) return
    if (!kaisReactionsEnabled()) { previous.current = identity; return }
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)')
    const detach = () => {
      document.removeEventListener('visibilitychange', pause)
      preference.removeEventListener('change', pause)
    }
    const pause = () => {
      if (!kaisReactionsEnabled()) {
        previous.current = identity
        window.cancelAnimationFrame(frame)
        detach()
      }
    }
    const frame = window.requestAnimationFrame(() => {
      detach()
      if (!target.current) return
      previous.current = identity
      emitKaisTargetReaction('navigation', target.current)
    })
    document.addEventListener('visibilitychange', pause)
    preference.addEventListener('change', pause)
    return () => { window.cancelAnimationFrame(frame); detach() }
  }, [identity, target, ready])
}

export function useKaisErrorReaction(notice: {kind: string} | null) {
  const previous = useRef<typeof notice>(null)
  useEffect(() => {
    if (notice !== previous.current && notice?.kind === 'error') emitKaisReaction({type: 'error'})
    previous.current = notice
  }, [notice])
}
