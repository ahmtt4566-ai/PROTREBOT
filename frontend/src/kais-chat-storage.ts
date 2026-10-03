export const CHAT_PREFIX = 'kais-chat:'
export const CHAT_VERSION = 1
export const CHAT_MAX_MESSAGES = 50
export const CHAT_MAX_MESSAGE_CHARS = 4000
export const CHAT_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000
export const CHAT_CLEAR_EVENT = 'kais-chat:clear'
export const CHAT_READY_EVENT = 'kais-chat:ready'
const MASK = '[MASKED]'
let generation = 0
let suspended = false

export type StoredChatMessage = {
  id: string
  role: 'user' | 'assistant'
  content: string
  language: 'tr' | 'en'
  proactive?: boolean
  unread?: boolean
}
type PersistableMessage = StoredChatMessage & {pending?: boolean; failed?: boolean}
type ChatRecord = {version: typeof CHAT_VERSION; savedAt: number; messages: StoredChatMessage[]}

export const chatStorageKey = (userId: string) => `${CHAT_PREFIX}v${CHAT_VERSION}:${encodeURIComponent(userId)}`
const truncate = (value: string) => Array.from(value).slice(0, CHAT_MAX_MESSAGE_CHARS).join('')
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const nonempty = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0

export function maskChatSecrets(value: string, proofs: readonly string[] = []): string {
  let masked = value
  for (const proof of [...new Set(proofs)].filter(Boolean).sort((a, b) => b.length - a.length)) {
    masked = masked.split(proof).join(MASK)
  }
  return masked
    .replace(/\b(Bearer|Basic)\s+[^\s,;]+/gi, `$1 ${MASK}`)
    .replace(/((?:["']?)(?:password|parola|authorization|api(?:[_ -]?key)?|secret(?:[_ -]?key)?|(?:access[_ -]?|refresh[_ -]?|confirmation[_ -]?|consent[_ -]?|arm[_ -]?)?token|key|signed[_ -]?approval)["']?\s*[:=]\s*)(?!\[MASKED\])(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)/gi, `$1${MASK}`)
    .replace(/\b(api[_ -]?key|secret(?:[_ -]?key)?|token|key|password|parola)\s+[a-z0-9_./+=-]{8,}/gi, `$1 ${MASK}`)
    .replace(/\b[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b/gi, MASK)
    .replace(/\b(?:sk-(?:ant-)?|(?:api|secret|token|key)[_-])[a-z0-9_./+=-]+/gi, MASK)
    .replace(/\b(?:api|secret|token|key)[a-z0-9_-]{6,}\b/gi, MASK)
    .replace(/[a-z0-9_+/=-]{32,}/gi, MASK)
}

function storedMessages(messages: readonly PersistableMessage[], proofs: readonly string[]): StoredChatMessage[] {
  return messages.filter(message => !message.pending && !message.failed && nonempty(message.content))
    .slice(-CHAT_MAX_MESSAGES).map(message => ({
      id: message.id, role: message.role, language: message.language,
      content: truncate(maskChatSecrets(message.content, proofs)),
      ...(message.proactive ? {proactive: true, unread: message.unread === true} : {}),
    }))
}

export function parseChatRecord(raw: string, now: number): ChatRecord | null {
  if (raw.length > 1_000_000) return null
  let value: unknown
  try { value = JSON.parse(raw) }
  catch (error) { if (error instanceof SyntaxError) return null; throw error }
  if (!object(value) || Object.keys(value).some(key => !['version', 'savedAt', 'messages'].includes(key)) ||
      value.version !== CHAT_VERSION || typeof value.savedAt !== 'number' || !Number.isSafeInteger(value.savedAt) ||
      value.savedAt < 0 || value.savedAt > now || now - value.savedAt > CHAT_MAX_AGE_MS ||
      !Array.isArray(value.messages) || value.messages.length > CHAT_MAX_MESSAGES) return null
  const messages: StoredChatMessage[] = []
  const ids = new Set<string>()
  for (const message of value.messages) {
    if (!object(message) || Object.keys(message).some(key => !['id', 'role', 'content', 'language', 'proactive', 'unread'].includes(key)) ||
        !nonempty(message.id) || message.id.length > 128 || ids.has(message.id) || !nonempty(message.content) ||
        Array.from(message.content).length > CHAT_MAX_MESSAGE_CHARS || !['user', 'assistant'].includes(String(message.role)) ||
        !['tr', 'en'].includes(String(message.language)) ||
        (message.proactive !== undefined && typeof message.proactive !== 'boolean') ||
        (message.unread !== undefined && typeof message.unread !== 'boolean')) return null
    ids.add(message.id)
    messages.push({id: message.id, role: message.role === 'user' ? 'user' : 'assistant', content: message.content,
      language: message.language === 'en' ? 'en' : 'tr',
      ...(message.proactive ? {proactive: true, unread: message.unread === true} : {})})
  }
  return {version: CHAT_VERSION, savedAt: value.savedAt, messages: storedMessages(messages, [])}
}

function warnStorage(error: unknown): void {
  console.warn('Kais AI chat persistence disabled:', error instanceof Error ? error.name : 'StorageError')
}

function removeChatKeys(storage: Storage, keep?: string): void {
  for (let index = storage.length - 1; index >= 0; index--) {
    const key = storage.key(index)
    if (key?.startsWith(CHAT_PREFIX) && key !== keep) storage.removeItem(key)
  }
}

export function createKaisChatStorage(userId: string | null, getStorage = () => window.localStorage, now = () => Date.now()) {
  const key = userId ? chatStorageKey(userId) : null
  const createdAtGeneration = generation
  let disabled = !key
  const access = <T,>(operation: (storage: Storage) => T, fallback: T): T => {
    if (disabled || suspended || generation !== createdAtGeneration) return fallback
    try { return operation(getStorage()) }
    catch (error) { disabled = true; warnStorage(error); return fallback }
  }
  if (key) access(storage => removeChatKeys(storage, key), undefined)
  return {
    key,
    read(): StoredChatMessage[] {
      return access(storage => {
        if (!key) return []
        const raw = storage.getItem(key)
        if (!raw) return []
        const record = parseChatRecord(raw, now())
        if (!record) { storage.removeItem(key); return [] }
        const sanitized = JSON.stringify(record)
        if (sanitized !== raw) storage.setItem(key, sanitized)
        return record.messages
      }, [])
    },
    write(messages: readonly PersistableMessage[], proofs: readonly string[] = []): void {
      access(storage => {
        if (!key) return
        const saved = storedMessages(messages, proofs)
        if (!saved.length) storage.removeItem(key)
        else storage.setItem(key, JSON.stringify({version: CHAT_VERSION, savedAt: now(), messages: saved}))
      }, undefined)
    },
    matches(event: StorageEvent): boolean {
      return access(storage => event.storageArea === storage && (event.key === key || event.key === null), false)
    },
    clear(): void { access(storage => { if (key) storage.removeItem(key) }, undefined) },
    stop(): void { disabled = true },
  }
}

export function clearKaisChatHistory(userId?: string): void {
  generation++
  suspended = true
  try {
    if (userId) window.localStorage.removeItem(chatStorageKey(userId))
    else removeChatKeys(window.localStorage)
  } catch (error) { warnStorage(error) }
  window.dispatchEvent(new CustomEvent(CHAT_CLEAR_EVENT, {detail: {userId}}))
}

export function resumeKaisChatHistory(userId: string): void {
  if (!suspended || !userId) return
  generation++
  suspended = false
  window.dispatchEvent(new CustomEvent(CHAT_READY_EVENT, {detail: {userId}}))
}

export function isLocalStorageChange(event: StorageEvent, key: string): boolean {
  try { return event.storageArea === window.localStorage && (event.key === key || event.key === null) }
  catch (error) { warnStorage(error); return false }
}

export function clearLegacyChatHistory(): void {
  try {
    for (let index = sessionStorage.length - 1; index >= 0; index--) {
      const key = sessionStorage.key(index)
      if (key?.startsWith('protrebot-assistant-chat:')) sessionStorage.removeItem(key)
    }
  } catch (error) { warnStorage(error) }
}
