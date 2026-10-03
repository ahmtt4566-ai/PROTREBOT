export const KAIS_REACTION_EVENT = 'kais:react'
export type KaisReaction =
  | {type: 'premium-open' | 'navigation'; point: {x: number; y: number}}
  | {type: 'error' | 'unread'}
export const kaisReactionsEnabled = () => !document.hidden && !window.matchMedia('(prefers-reduced-motion: reduce)').matches

declare global {
  interface WindowEventMap {
    'kais:react': CustomEvent<KaisReaction>
  }
}

export function isKaisReaction(value: unknown): value is KaisReaction {
  if (typeof value !== 'object' || value === null || !('type' in value)) return false
  if (value.type === 'error' || value.type === 'unread') return true
  if (value.type !== 'premium-open' && value.type !== 'navigation') return false
  if (!('point' in value) || typeof value.point !== 'object' || value.point === null) return false
  return 'x' in value.point && 'y' in value.point &&
    typeof value.point.x === 'number' && Number.isFinite(value.point.x) &&
    typeof value.point.y === 'number' && Number.isFinite(value.point.y)
}

export function emitKaisReaction(reaction: KaisReaction): void {
  if (!isKaisReaction(reaction)) throw new TypeError('Invalid Kais visual reaction')
  if (!kaisReactionsEnabled()) return
  // Copy only event type and coordinates. Never read/pass text, field values,
  // element references or arbitrary payloads; no network, LLM or storage calls.
  const detail = reaction.type === 'premium-open' || reaction.type === 'navigation'
    ? {type: reaction.type, point: Object.freeze({x: reaction.point.x, y: reaction.point.y})}
    : {type: reaction.type}
  window.dispatchEvent(new CustomEvent(KAIS_REACTION_EVENT, {detail: Object.freeze(detail)}))
}

export function emitKaisTargetReaction(type: 'premium-open' | 'navigation', target: Element): void {
  if (!kaisReactionsEnabled()) return
  // The publisher supplies its own component ref: measure geometry only,
  // without querying the page or inspecting any content/attributes/values.
  const rect = target.getBoundingClientRect()
  emitKaisReaction({type, point: {
    x: Math.max(0, Math.min(window.innerWidth, (Math.max(0, rect.left) + Math.min(window.innerWidth, rect.right)) / 2)),
    y: Math.max(0, Math.min(window.innerHeight, (Math.max(0, rect.top) + Math.min(window.innerHeight, rect.bottom)) / 2)),
  }})
}
