import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { Send, Trash2, X } from 'lucide-react'
import type {KaisEyeState} from './KaisEye'
import KaisEye from './frontend/src/KaisEye'
import KaisGreeting from './frontend/src/KaisGreeting'
import {CHAT_CLEAR_EVENT, CHAT_READY_EVENT, clearKaisChatHistory, clearLegacyChatHistory, createKaisChatStorage, isLocalStorageChange} from './frontend/src/kais-chat-storage'
import {useAssistantMotion} from './useAssistantPresentation'
import {useKaisPrivacy} from './useKaisPrivacy'
import {emitKaisReaction} from './kais-reactions'
import { API_BASE, USER_SESSION_KEY } from './api'
import { useMemberAccess } from './premium-access'
import { assistantCopy } from './ui-copy'
import './assistant.css'

type Language = 'tr' | 'en'
type Limits = {max_input_chars: number; history_messages: number; history_message_max_chars: number; page_context_max_chars: number; secret_min_alphanumeric_chars: number}
type Usage = {remaining: number; total: number; resetsAt: string; limits: Limits}
type ProactivePreferences = {enabled: boolean; available: boolean; poll_interval_seconds: number}
type Confirmation = {action: 'get_analysis'; symbol: string; timeframe: string; cost: number; confirmation_token: string}
type Message = {id: string; role: 'user' | 'assistant'; content: string; language: Language; proactive?: boolean; unread?: boolean; confirmation?: Confirmation; decision?: 'pending' | 'confirmed' | 'cancelled' | 'expired'; pending?: boolean; failed?: boolean}
type Issue = {kind: 'network' | 'session' | 'limit' | 'budget' | 'unavailable' | 'invalid' | 'forbidden' | 'credits' | 'secretBlocked'; reply?: string; retry?: number; confirmation?: boolean; checkIn?: boolean}
const launcherIntroKey = 'protrebot-kais-launcher-seen'

function readLauncherIntro(): {show: boolean; problem: boolean} {
  try { return {show: sessionStorage.getItem(launcherIntroKey) !== '1', problem: false} }
  catch (error) {
    if (!(error instanceof DOMException)) throw error
    return {show: false, problem: true}
  }
}

class AssistantFailure extends Error {
  constructor(readonly status: number, readonly code?: string, readonly reply?: string, readonly retry?: number) {
    super('Assistant request failed')
  }
}

const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const languageOfPage = (): Language => document.documentElement.lang.toLowerCase().startsWith('en') ? 'en' : 'tr'
const truncate = (value: string, max: number) => Array.from(value).slice(0, max).join('')
const integer = (value: unknown, minimum = 0): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0

function sensitive(value: string, limits: Limits): boolean {
  return new RegExp(`(?:^|[^a-z])secret(?:[_ -]*key)?(?![a-z])|(?:^|[^a-z0-9])[a-z0-9]{${limits.secret_min_alphanumeric_chars},}(?![a-z0-9])|\\bsk-(?:ant-)?[a-z0-9_-]+|\\bbearer\\s+\\S+|\\b(?:password|parola|api[_ -]*key|token)\\s*[:=]\\s*\\S+`, 'i').test(value)
}

function parseUsage(value: Record<string, unknown>): Usage {
  const limits = value.limits
  if (!integer(value.remaining) || !integer(value.total) || value.remaining > value.total || !text(value.resetsAt) ||
      !object(limits) || !integer(limits.max_input_chars, 1) || !integer(limits.history_messages) ||
      !integer(limits.history_message_max_chars, 1) || !integer(limits.page_context_max_chars, 1) ||
      !integer(limits.secret_min_alphanumeric_chars, 1)) throw new AssistantFailure(502, 'invalid_response')
  return {remaining: value.remaining, total: value.total, resetsAt: value.resetsAt, limits: {
    max_input_chars: limits.max_input_chars, history_messages: limits.history_messages,
    history_message_max_chars: limits.history_message_max_chars, page_context_max_chars: limits.page_context_max_chars,
    secret_min_alphanumeric_chars: limits.secret_min_alphanumeric_chars,
  }}
}

function parseConfirmation(value: unknown): Confirmation | undefined {
  if (value === undefined || value === null) return undefined
  if (!object(value) || value.action !== 'get_analysis' || !text(value.symbol) || !text(value.timeframe) ||
      !integer(value.cost) || !text(value.confirmation_token)) throw new AssistantFailure(502, 'invalid_response')
  return {action: 'get_analysis', symbol: value.symbol, timeframe: value.timeframe, cost: value.cost, confirmation_token: value.confirmation_token}
}

function parseProactivePreferences(value: Record<string, unknown>): ProactivePreferences {
  if (typeof value.enabled !== 'boolean' || typeof value.available !== 'boolean' || !integer(value.poll_interval_seconds, 1)) throw new AssistantFailure(502, 'invalid_response')
  return {enabled: value.enabled, available: value.available, poll_interval_seconds: value.poll_interval_seconds}
}

function parseProactiveMessage(value: unknown): Message | null {
  if (value === null) return null
  if (!object(value) || !text(value.id) || !text(value.reply) || !['tr', 'en'].includes(String(value.language)) ||
      typeof value.stale !== 'boolean' || !text(value.fetched_at) || !Array.isArray(value.sources) || !value.sources.every(text)) throw new AssistantFailure(502, 'invalid_response')
  return {id: value.id, role: 'assistant', content: value.reply, language: value.language === 'en' ? 'en' : 'tr', proactive: true}
}

async function request(path: string, signal: AbortSignal, body?: object): Promise<Record<string, unknown>> {
  const response = await fetch(`${API_BASE}/assistant/${path}`, {
    signal, ...(body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {}),
  })
  let payload: unknown
  try { payload = await response.json() }
  catch (error) {
    if (!(error instanceof SyntaxError)) throw error
    if (response.ok) throw new AssistantFailure(502, 'invalid_response')
  }
  if (!response.ok) {
    const retry = response.headers.get('Retry-After')
    throw new AssistantFailure(response.status, object(payload) && typeof payload.error_code === 'string' ? payload.error_code : undefined,
      object(payload) && typeof payload.reply === 'string' ? payload.reply : undefined,
      retry !== null && Number.isFinite(Number(retry)) ? Math.max(0, Number(retry)) : undefined)
  }
  if (!object(payload)) throw new AssistantFailure(502, 'invalid_response')
  return payload
}

function issueFor(error: unknown, confirmation = false): Issue {
  if (!(error instanceof AssistantFailure)) return {kind: error instanceof TypeError || error instanceof DOMException ? 'network' : 'invalid', confirmation}
  const kind = error.status === 401 ? 'session' : error.status === 403 ? 'forbidden'
    : error.code === 'budget' ? 'budget' : error.code === 'invalid_response' ? 'invalid'
    : error.status === 429 ? confirmation ? 'credits' : 'limit' : 'unavailable'
  return {kind, reply: error.reply, retry: error.retry, confirmation}
}

function analysisText(payload: Record<string, unknown>, language: Language): string {
  const data = payload.data
  if (!object(data) || !text(data.symbol) || !text(data.timeframe) || typeof payload.stale !== 'boolean') throw new AssistantFailure(502, 'invalid_response')
  const copy = assistantCopy[language]
  const number = (value: unknown) => typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString(language === 'tr' ? 'tr-TR' : 'en-US', {maximumFractionDigits: 2}) : copy.unknown
  const direction = typeof data.direction === 'string' ? data.direction : copy.unknown
  return [
    `${data.symbol} · ${data.timeframe} · ${direction}`,
    `Final Decision: ${number(data.final_decision_score)} · Confidence: ${number(data.confidence)}`,
    `Opportunity Score: ${number(data.opportunity_score)} · MTF: ${number(data.mtf_alignment)}`,
    integer(data.data_age_seconds) ? copy.age(data.data_age_seconds) : copy.ageUnknown,
    ...(payload.stale ? [copy.stale] : []), copy.levels, '', copy.risk,
  ].join('\n')
}

function PlainText({content}: {content: string}) {
  return <>{content.split(/(\*\*[^*]+\*\*)/g).map((part, index) =>
    part.startsWith('**') && part.endsWith('**') ? <strong key={index}>{part.slice(2, -2)}</strong> : part)}</>
}

function AssistantSession({userId, pageContext, language, launcherTarget}: {userId: string; pageContext: string; language: Language; launcherTarget?: HTMLElement | null}) {
  const copy = assistantCopy[language]
  const motionPaused = useAssistantMotion()
  const privateFocus = useKaisPrivacy()
  const [initial] = useState(() => {
    clearLegacyChatHistory()
    const storage = createKaisChatStorage(userId)
    return {storage, messages: storage.read()}
  })
  const [intro] = useState(readLauncherIntro)
  const [showIntro, setShowIntro] = useState(intro.show)
  const [messages, setMessages] = useState<Message[]>(initial.messages)
  const [storageProblem, setStorageProblem] = useState(intro.problem)
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [usage, setUsage] = useState<Usage | null>(null)
  const [issue, setIssue] = useState<Issue | null>(null)
  const [usageIssue, setUsageIssue] = useState<Issue | null>(null)
  const [proactivePreferences, setProactivePreferences] = useState<ProactivePreferences | null>(null)
  const [proactiveIssue, setProactiveIssue] = useState<Issue | null>(null)
  const [preferenceBusy, setPreferenceBusy] = useState(false)
  const [proactiveReload, setProactiveReload] = useState(0)
  const [expired, setExpired] = useState(false)
  const [busy, setBusy] = useState(false)
  const [lengthError, setLengthError] = useState(false)
  const [viewport, setViewport] = useState({height: window.visualViewport?.height ?? window.innerHeight, top: window.visualViewport?.offsetTop ?? 0,
    width: window.visualViewport?.width ?? window.innerWidth, left: window.visualViewport?.offsetLeft ?? 0})
  const [placement, setPlacement] = useState({left: 12, top: 12, height: 520, arrowLeft: 24, above: false})
  const dialog = useRef<HTMLDialogElement>(null)
  const launcherRef = useRef<HTMLButtonElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const previousLastMessage = useRef(messages.at(-1)?.id)
  const input = useRef<HTMLTextAreaElement>(null)
  const working = useRef(false)
  const activeRequest = useRef<AbortController | null>(null)
  const usageRequest = useRef<AbortController | null>(null)
  const preferenceRequest = useRef<AbortController | null>(null)
  const preferenceReadRequest = useRef<AbortController | null>(null)
  const preferenceWorking = useRef(false)
  const chatOpen = useRef(false)
  const syncedMessages = useRef<Message[]>(initial.messages)

  useEffect(() => { setOpen(false) }, [pageContext])

  useEffect(() => {
    if (!intro.show) return
    try { sessionStorage.setItem(launcherIntroKey, '1') }
    catch (error) {
      if (!(error instanceof DOMException)) throw error
      setStorageProblem(true); setShowIntro(false)
      return
    }
    const timer = window.setTimeout(() => setShowIntro(false), 5000)
    return () => window.clearTimeout(timer)
  }, [intro.show])

  useLayoutEffect(() => {
    const field = input.current
    if (!open || !field) return
    field.style.height = '0px'
    field.style.height = `${Math.min(120, field.scrollHeight + 2)}px`
  }, [draft, open, viewport])

  useEffect(() => {
    if (expired) { initial.storage.clear(); initial.storage.stop(); return }
    if (messages === syncedMessages.current) return
    syncedMessages.current = messages
    const saved = messages.map(message => {
      const labels = assistantCopy[message.language]
      const notice = message.decision === 'pending' ? labels.renewed : message.decision === 'cancelled' ? labels.cancelled : message.decision === 'confirmed' ? labels.confirmed : message.decision === 'expired' ? labels.renewed : ''
      return {id: message.id, role: message.role, language: message.language, content: message.content + (notice ? `\n\n${notice}` : ''),
        pending: message.pending, failed: message.failed,
        ...(message.proactive ? {proactive: true, unread: message.unread === true} : {})}
    })
    initial.storage.write(saved, messages.flatMap(message => message.confirmation ? [message.confirmation.confirmation_token] : []))
  }, [messages, initial, expired])

  useEffect(() => {
    const replaceHistory = () => {
      activeRequest.current?.abort()
      working.current = false; setBusy(false)
      const loaded = initial.storage.read()
      syncedMessages.current = loaded
      setMessages(loaded)
    }
    const storageChanged = (event: StorageEvent) => {
      if (!expired && initial.storage.matches(event)) replaceHistory()
    }
    const cleared = (event: Event) => {
      if (!(event instanceof CustomEvent)) return
      const detail: unknown = event.detail
      if (!object(detail) || (detail.userId !== undefined && detail.userId !== userId)) return
      activeRequest.current?.abort()
      initial.storage.stop()
      working.current = false; setBusy(false); setExpired(true); setUsage(null); setMessages([]); setDraft('')
    }
    window.addEventListener('storage', storageChanged)
    window.addEventListener(CHAT_CLEAR_EVENT, cleared)
    return () => { window.removeEventListener('storage', storageChanged); window.removeEventListener(CHAT_CLEAR_EVENT, cleared) }
  }, [initial, userId, expired])

  useEffect(() => () => { activeRequest.current?.abort(); usageRequest.current?.abort(); preferenceRequest.current?.abort() }, [])
  useEffect(() => {
    chatOpen.current = open
    if (open) setMessages(previous => previous.some(message => message.unread) ? previous.map(message => ({...message, unread: false})) : previous)
  }, [open])
  useEffect(() => {
    if (expired) return
    const controller = new AbortController()
    preferenceReadRequest.current = controller
    void (async () => {
      try {
        const preferences = parseProactivePreferences(await request('proactive/preferences', controller.signal))
        if (!controller.signal.aborted && !preferenceWorking.current) { setProactivePreferences(preferences); setProactiveIssue(previous => previous?.checkIn ? previous : null) }
      } catch (error) {
        if (controller.signal.aborted) return
        const next = issueFor(error)
        setProactiveIssue(next)
        if (next.kind === 'session') { setExpired(true); setUsage(null); setMessages([]); setDraft('') }
      }
    })()
    return () => controller.abort()
  }, [expired, proactiveReload])
  useEffect(() => {
    if (expired || !proactivePreferences?.enabled || !proactivePreferences.available) return
    let stopped = false
    let checking = false
    let timer: ReturnType<typeof setTimeout> | undefined
    let controller: AbortController | undefined
    const check = async () => {
      if (stopped || checking) return
      clearTimeout(timer)
      checking = true
      try {
        if (document.visibilityState !== 'visible' || preferenceWorking.current) return
        controller = new AbortController()
        const response = await request('proactive/check-in', controller.signal, {language})
        if (controller.signal.aborted || preferenceWorking.current) return
        const preferences = parseProactivePreferences(response)
        const message = parseProactiveMessage(response.message)
        setProactivePreferences(preferences); setProactiveIssue(null)
        if (message && preferences.enabled && preferences.available) {
          setMessages(previous => previous.some(row => row.id === message.id) ? previous : [...previous, {...message, unread: !chatOpen.current}])
        }
      } catch (error) {
        if (stopped || controller?.signal.aborted) return
        const next = {...issueFor(error), checkIn: true}
        setProactiveIssue(next)
        if (next.kind === 'session') { setExpired(true); setUsage(null); setMessages([]); setDraft('') }
      } finally {
        checking = false
        if (!stopped) timer = setTimeout(() => void check(), Math.min(proactivePreferences.poll_interval_seconds * 1000, 2_147_483_647))
      }
    }
    const visible = () => { if (document.visibilityState === 'visible') void check() }
    document.addEventListener('visibilitychange', visible)
    void check()
    return () => { stopped = true; clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', visible) }
  }, [expired, language, proactivePreferences?.enabled, proactivePreferences?.available, proactivePreferences?.poll_interval_seconds, proactiveReload])
  useLayoutEffect(() => {
    if (!open || !dialog.current) return
    const previousFocus = document.activeElement
    const element = dialog.current
    element.showModal()
    const updateViewport = () => {
      const next = {height: window.visualViewport?.height ?? window.innerHeight, top: window.visualViewport?.offsetTop ?? 0,
        width: window.visualViewport?.width ?? window.innerWidth, left: window.visualViewport?.offsetLeft ?? 0}
      setViewport(previous => previous.height === next.height && previous.top === next.top &&
        previous.width === next.width && previous.left === next.left ? previous : next)
      const anchor = launcherRef.current?.getBoundingClientRect()
      if (!anchor || next.width < 768) return
      const gap = 12
      const width = Math.min(360, Math.max(0, next.width - gap * 2))
      const maximumHeight = Math.max(0, next.height - gap * 2)
      const below = next.top + next.height - gap - anchor.bottom - gap
      const above = anchor.top - gap - next.top - gap
      const useAbove = below < Math.min(200, maximumHeight) && above > below
      const available = useAbove ? above : below
      const height = Math.min(520, maximumHeight, available >= Math.min(200, maximumHeight) ? available : maximumHeight)
      const left = Math.max(next.left + gap, Math.min(anchor.right - width, next.left + next.width - gap - width))
      const top = Math.max(next.top + gap, Math.min(useAbove ? anchor.top - gap - height : anchor.bottom + gap,
        next.top + next.height - gap - height))
      const arrowLeft = Math.max(Math.min(24, width / 2), Math.min(anchor.left + anchor.width / 2 - left, width - Math.min(24, width / 2)))
      setPlacement(previous => previous.left === left && previous.top === top && previous.height === height &&
        previous.arrowLeft === arrowLeft && previous.above === useAbove ? previous : {left, top, height, arrowLeft, above: useAbove})
    }
    updateViewport()
    window.visualViewport?.addEventListener('resize', updateViewport)
    window.visualViewport?.addEventListener('scroll', updateViewport)
    window.addEventListener('resize', updateViewport)
    window.addEventListener('scroll', updateViewport, {passive: true, capture: true})
    return () => {
      window.visualViewport?.removeEventListener('resize', updateViewport)
      window.visualViewport?.removeEventListener('scroll', updateViewport)
      window.removeEventListener('resize', updateViewport)
      window.removeEventListener('scroll', updateViewport, true)
      element.close()
      if (launcherRef.current?.isConnected) launcherRef.current.focus()
      else if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus()
    }
  }, [open])
  const composerEnabled = Boolean(usage) && !expired
  useEffect(() => {
    if (open && composerEnabled) input.current?.focus({preventScroll: true})
  }, [open, composerEnabled])
  useEffect(() => {
    const lastMessage = messages.at(-1)?.id
    const appended = lastMessage !== undefined && lastMessage !== previousLastMessage.current
    previousLastMessage.current = lastMessage
    if (!open || !list.current) return
    if (!messages.length) list.current.scrollTop = 0
    else if (appended) list.current.scrollTop = list.current.scrollHeight
  }, [messages, open])

  const expire = () => { setExpired(true); setUsage(null); setMessages([]); setDraft('') }
  const refreshUsage = async () => {
    usageRequest.current?.abort()
    const controller = new AbortController()
    usageRequest.current = controller
    try {
      const value = parseUsage(await request('usage', controller.signal))
      if (controller.signal.aborted) return
      setUsage(value); setUsageIssue(null)
    } catch (error) {
      if (controller.signal.aborted) return
      const next = issueFor(error)
      setUsageIssue(next)
      if (next.kind === 'session') expire()
    }
  }
  const openChat = () => { setOpen(true); void refreshUsage() }
  const savePreference = async (enabled: boolean) => {
    if (preferenceWorking.current || expired) return
    preferenceReadRequest.current?.abort()
    preferenceWorking.current = true; setPreferenceBusy(true)
    const controller = new AbortController()
    preferenceRequest.current = controller
    try {
      const preferences = parseProactivePreferences(await request('proactive/preferences', controller.signal, {enabled}))
      if (controller.signal.aborted) return
      setProactivePreferences(preferences); setProactiveIssue(null)
    } catch (error) {
      if (controller.signal.aborted) return
      const next = issueFor(error)
      setProactiveIssue(next)
      if (next.kind === 'session') expire()
    } finally {
      preferenceWorking.current = false
      if (!controller.signal.aborted) setPreferenceBusy(false)
    }
  }
  const append = (content: string, replyLanguage: Language, confirmation?: Confirmation) => setMessages(previous => [...previous, {
    id: crypto.randomUUID(), role: 'assistant', content, language: replyLanguage, ...(confirmation ? {confirmation, decision: 'pending' as const} : {}),
  }])
  const decide = (id: string, decision: Message['decision']) => setMessages(previous => previous.map(message => message.id === id ? {...message, decision} : message))

  const sendMessage = async (value: string) => {
    if (working.current || !usage || expired || !value.trim()) return
    const message = value.trim()
    if (Array.from(message).length > usage.limits.max_input_chars) { setLengthError(true); return }
    if (sensitive(message, usage.limits)) { setIssue({kind: 'secretBlocked'}); return }
    working.current = true; setBusy(true); setIssue(null); setLengthError(false); setDraft('')
    // Confirmation proofs remain only in memory; no model request contains them.
    const eligible = messages.filter(row => !row.proactive && !row.pending && !row.failed && row.content.trim() && !sensitive(row.content, usage.limits))
    const history = (usage.limits.history_messages ? eligible.slice(-Math.min(12, usage.limits.history_messages)) : [])
      .map(row => ({role: row.role, content: truncate(row.content, usage.limits.history_message_max_chars)}))
    const userMessageId = crypto.randomUUID()
    setMessages(previous => [...previous.map(row => row.decision === 'pending' ? {...row, decision: 'expired' as const} : row),
      {id: userMessageId, role: 'user', content: message, language, pending: true}])
    const controller = new AbortController()
    activeRequest.current = controller
    try {
      const response = await request('chat', controller.signal, {message, history, page_context: truncate(pageContext, usage.limits.page_context_max_chars)})
      if (controller.signal.aborted) return
      if (!text(response.reply) || !['tr', 'en'].includes(String(response.language))) throw new AssistantFailure(502, 'invalid_response')
      const confirmation = parseConfirmation(response.needs_confirmation)
      setMessages(previous => previous.map(row => row.id === userMessageId ? {...row, pending: false} : row))
      append(response.reply, response.language === 'en' ? 'en' : 'tr', confirmation)
    } catch (error) {
      if (controller.signal.aborted) return
      setMessages(previous => previous.map(row => row.id === userMessageId ? {...row, pending: false, failed: true} : row))
      const next = issueFor(error)
      setIssue(next)
      if (next.kind === 'session') expire()
    } finally {
      if (!controller.signal.aborted) { working.current = false; setBusy(false); void refreshUsage() }
    }
  }

  const confirm = async (message: Message) => {
    const approval = message.confirmation
    if (working.current || expired || !approval || message.decision !== 'pending') return
    working.current = true; setBusy(true); setIssue(null)
    const controller = new AbortController()
    activeRequest.current = controller
    try {
      const response = await request('analysis/confirm', controller.signal, {
        symbol: approval.symbol, timeframe: approval.timeframe, confirmation_token: approval.confirmation_token, confirm: true,
      })
      if (controller.signal.aborted) return
      const summary = analysisText(response, message.language)
      decide(message.id, 'confirmed')
      append(summary, message.language)
    } catch (error) {
      if (controller.signal.aborted) return
      const next = issueFor(error, true)
      setIssue(next)
      if (next.kind === 'session') expire()
      if (next.kind === 'forbidden' || next.kind === 'credits') decide(message.id, 'expired')
    } finally {
      if (!controller.signal.aborted) { working.current = false; setBusy(false); void refreshUsage() }
    }
  }
  const submit = (event: FormEvent) => { event.preventDefault(); void sendMessage(draft) }
  const displayedIssue = issue ?? usageIssue ?? proactiveIssue
  // Presentation only: reuse the existing secret rule and server-supplied threshold.
  const privateDraft = Boolean(usage && sensitive(draft, usage.limits))
  const eyeState: KaisEyeState = privateFocus ? 'private' : expired || displayedIssue?.kind === 'unavailable' ? 'off'
    : busy ? 'thinking' : displayedIssue || usage?.remaining === 0 ? 'error' : 'idle'
  const unread = messages.filter(message => message.proactive && message.unread).length
  const previousUnread = useRef(unread)
  useEffect(() => {
    if (unread > previousUnread.current) emitKaisReaction({type: 'unread'})
    previousUnread.current = unread
  }, [unread])
  const status = privateFocus ? copy.status.private : busy ? copy.status.thinking
    : eyeState === 'off' || eyeState === 'error' ? copy.status.unavailable : copy.status.ready
  const style = {'--assistant-viewport-height': `${viewport.height}px`, '--assistant-viewport-top': `${viewport.top}px`,
    '--assistant-viewport-width': `${viewport.width}px`, '--assistant-viewport-left': `${viewport.left}px`,
    '--assistant-dialog-left': `${placement.left}px`, '--assistant-dialog-top': `${placement.top}px`,
    '--assistant-dialog-height': `${placement.height}px`, '--assistant-arrow-left': `${placement.arrowLeft}px`,
    '--assistant-origin-y': placement.above ? '100%' : '0%'} as CSSProperties

  const launcher = <button ref={launcherRef} type="button" className="assistantLauncher" aria-label={copy.open} data-motion-paused={motionPaused} aria-describedby={unread ? 'assistant-checkin-unread' : undefined} aria-haspopup="dialog" aria-expanded={open} onClick={openChat}><KaisEye size={56} state={eyeState} unreadBadge={unread > 0}/>
      {showIntro && !open && !privateFocus && <span className="assistantIntro" aria-hidden="true">{copy.title}</span>}
      {unread > 0 && <span id="assistant-checkin-unread" className="assistantBadge" role="status" aria-label={copy.proactiveUnread}>{unread}</span>}
    </button>
  return <>
    {launcherTarget === undefined ? launcher : launcherTarget && createPortal(launcher, launcherTarget)}
    <KaisGreeting userId={userId} enabled={!expired && launcherTarget !== null && eyeState !== 'off' && eyeState !== 'error'}
      chatOpen={open} anchorRef={launcherRef} anchorHost={launcherTarget} motionPaused={motionPaused} onOpen={openChat}/>
    {open && createPortal(<dialog ref={dialog} className="assistantDialog" style={style} data-assistant-chat data-side={placement.above ? 'above' : 'below'} data-motion-paused={motionPaused} data-compact-viewport={viewport.height < 500 || undefined} role="dialog" aria-label={copy.title} aria-labelledby="assistant-title"
      onCancel={event => {event.preventDefault(); setOpen(false)}}
      onClick={event => {if (event.target === event.currentTarget) setOpen(false)}}>
      <div className="assistantLayout">
        <header className="assistantHeader"><KaisEye size={36} state={eyeState}/><div><h2 id="assistant-title">{copy.title}</h2><small className="assistantState" role="status">{status}</small></div>
          <button type="button" aria-label={copy.clear} disabled={busy} onClick={() => {
            const empty: Message[] = []
            initial.storage.clear(); syncedMessages.current = empty; setMessages(empty); setIssue(null)
          }}><Trash2 aria-hidden="true"/></button>
          <button type="button" aria-label={copy.close} onClick={() => setOpen(false)}><X aria-hidden="true"/></button>
        </header>
        <div className="assistantMeta">
          <div className={`assistantUsage${usage?.remaining === 0 ? ' exhausted' : ''}`} role="status" title={usage ? copy.usage(usage.remaining, usage.total) : undefined}>{usage ? copy.usage(usage.remaining, usage.total) : expired ? copy.session : copy.loading}</div>
          <label className="assistantPreference" title={copy.proactiveHint}>
            <input type="checkbox" aria-label={copy.proactiveSetting} aria-describedby="assistant-checkin-hint" checked={proactivePreferences?.enabled ?? false} disabled={!proactivePreferences || preferenceBusy || expired} onChange={event => void savePreference(event.target.checked)}/>
            <span>{copy.proactiveSetting}</span>
          </label>
          <span id="assistant-checkin-hint" className="assistantSrOnly" role="status">{preferenceBusy ? copy.proactiveSaving : copy.proactiveHint}</span>
        </div>
        <p className="assistantSubtitle">{copy.subtitle}</p>
        <div className="assistantMessages" ref={list} role="log" aria-live="polite" aria-relevant="additions" aria-busy={busy}>
          {!messages.length && <div className="assistantEmpty"><h3>{copy.welcome}</h3><p>{copy.empty}</p><small>{copy.eyePrivacy}</small></div>}
          {messages.map(message => <article key={message.id} className={`assistantMessage ${message.role}`} lang={message.language}>
            {message.role === 'assistant' && <KaisEye size={24} state="idle"/>}
            <div className="assistantBubble">
            <small className="assistantMessageRole">{message.role === 'assistant' ? copy.title : copy.you}</small>
            {message.proactive && <small className="assistantCheckinLabel">{copy.proactiveLabel}</small>}
            <div className="assistantText"><PlainText content={message.content}/></div>
            {message.confirmation && <div className="assistantConfirmation">
              <p>{message.decision === 'pending' && `${copy.cost(message.confirmation.cost)} · `}{message.confirmation.symbol} / {message.confirmation.timeframe}</p>
              {message.decision === 'pending' ? <div>
                <button type="button" className="assistantPrimary" disabled={busy || expired} onClick={() => void confirm(message)}>{copy.confirm}</button>
                <button type="button" disabled={busy} onClick={() => {decide(message.id, 'cancelled'); setIssue(null)}}>{copy.cancel}</button>
              </div> : <small>{message.decision === 'confirmed' ? copy.confirmed : message.decision === 'cancelled' ? copy.cancelled : copy.renewed}</small>}
            </div>}
            </div>
          </article>)}
          {busy && <p className="assistantTyping" role="status"><span className="assistantTypingDots" aria-hidden="true"><i/><i/><i/></span>{copy.typing}</p>}
        </div>
        {storageProblem && <p className="assistantNotice" role="status">{copy.storage}</p>}
        {displayedIssue && <div className="assistantError" role="alert">
          {displayedIssue === proactiveIssue && <p>{copy.proactiveError}</p>}
          <p>{copy[displayedIssue.kind]}</p>
          {displayedIssue.reply && <div className="assistantText"><PlainText content={displayedIssue.reply}/></div>}
          {displayedIssue.retry !== undefined && <small>{copy.wait(displayedIssue.retry)}</small>}
          {displayedIssue.confirmation && ['network', 'invalid', 'unavailable'].includes(displayedIssue.kind) && <small>{copy.uncertain}</small>}
          {displayedIssue.kind === 'session' ? <a href="/login">{copy.login}</a> : displayedIssue === proactiveIssue
            ? <button type="button" disabled={preferenceBusy} onClick={() => setProactiveReload(previous => previous + 1)}>{copy.retry}</button>
            : usageIssue && !issue && <button type="button" onClick={() => void refreshUsage()}>{copy.retry}</button>}
        </div>}
        <div className="assistantSuggestions" aria-label={language === 'tr' ? 'Hazır sorular' : 'Suggested questions'}>
          {copy.questions.map(question => <button key={question} type="button" disabled={busy || !usage || expired} onClick={() => void sendMessage(question)}>{question}</button>)}
        </div>
        <form className="assistantComposer" onSubmit={submit}>
          <label className="assistantInputLabel" htmlFor="assistant-input">{copy.input}</label>
          <div><textarea ref={input} id="assistant-input" autoFocus rows={1} value={draft} placeholder={copy.placeholder} disabled={!usage || expired}
            data-private={privateDraft ? 'true' : undefined}
            aria-describedby={`${privateDraft ? 'assistant-secret-warning' : 'assistant-secret'} assistant-length`} onChange={event => {setDraft(event.target.value); setLengthError(false)}}
            onKeyDown={event => {if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {event.preventDefault(); void sendMessage(draft)}}}/>
            <button type="submit" className="assistantPrimary" disabled={busy || !usage || expired || !draft.trim()} aria-label={copy.send}><Send aria-hidden="true"/></button></div>
          <footer><small id="assistant-secret">{!privateDraft && copy.secret}</small><small id="assistant-length">{usage && `${Array.from(draft).length}/${usage.limits.max_input_chars}`}</small></footer>
          {privateDraft && <p id="assistant-secret-warning" className="assistantError" role="status">{copy.secret}</p>}
          {lengthError && usage && <p className="assistantError" role="alert">{copy.tooLong(usage.limits.max_input_chars)}</p>}
        </form>
      </div>
    </dialog>, document.body)}
  </>
}

export default function AssistantChat({pageContext, launcherTarget}: {pageContext: string; launcherTarget?: HTMLElement | null}) {
  const {ready, userId} = useMemberAccess()
  const [historyEpoch, setHistoryEpoch] = useState(0)
  const [language, setLanguage] = useState<Language>(languageOfPage)
  useEffect(() => {
    const sessionChanged = (event: StorageEvent) => {
      if (isLocalStorageChange(event, USER_SESSION_KEY) && (event.key === null || event.oldValue !== event.newValue)) clearKaisChatHistory()
    }
    const verified = () => setHistoryEpoch(previous => previous + 1)
    window.addEventListener('storage', sessionChanged)
    window.addEventListener(CHAT_READY_EVENT, verified)
    return () => { window.removeEventListener('storage', sessionChanged); window.removeEventListener(CHAT_READY_EVENT, verified) }
  }, [])
  useEffect(() => {
    const observer = new MutationObserver(() => setLanguage(languageOfPage()))
    observer.observe(document.documentElement, {attributes: true, attributeFilter: ['lang']})
    return () => observer.disconnect()
  }, [])
  return ready && userId ? <AssistantSession key={`${userId}:${historyEpoch}`} userId={userId} pageContext={pageContext} language={language} launcherTarget={launcherTarget}/> : null
}
