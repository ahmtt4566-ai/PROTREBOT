import {clearKaisChatHistory, clearLegacyChatHistory} from './frontend/src/kais-chat-storage'
import {withRequestDeadline} from './browser-request'

const TOKEN_KEY = 'protrebot.web.owner-access'
export const USER_SESSION_KEY = 'protrebot-v25-session'

export const API_BASE = '/api'
export const COOKIE_SESSION_PREFIX = 'cookie-session:'
const OWNER_COOKIE_MARKER = 'cookie-owner'
const TEST_SESSION_STORAGE = import.meta.env.MODE === 'test'

const originalFetch = window.fetch.bind(window)
let installed = false
let errorMonitoringInstalled = false

export function ownerAccessToken(): string {
  const value = sessionStorage.getItem(TOKEN_KEY) || ''
  if (value && value !== OWNER_COOKIE_MARKER && !TEST_SESSION_STORAGE) {
    sessionStorage.removeItem(TOKEN_KEY)
    return ''
  }
  return value
}

export function saveOwnerAccessToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token.trim() ? OWNER_COOKIE_MARKER : '')
}

export async function clearOwnerAccessToken(): Promise<void> {
  clearKaisChatHistory()
  clearLegacyChatHistory()
  sessionStorage.removeItem(TOKEN_KEY)
  await withRequestDeadline(async signal => {
    const response = await originalFetch(`${API_BASE}/web/access/logout`, {
      method: 'POST', credentials: 'include', headers: {'X-Requested-With': 'XMLHttpRequest'}, signal,
    })
    if (!response.ok) throw new Error('Yönetici oturumu sunucuda kapatılamadı.')
  })
}

export function userSessionToken(): string {
  try {
    const remembered = localStorage.getItem(USER_SESSION_KEY)
    if (remembered?.startsWith(COOKIE_SESSION_PREFIX) || (remembered && TEST_SESSION_STORAGE)) return remembered
    if (remembered) localStorage.removeItem(USER_SESSION_KEY)
  } catch (error) { console.warn('Browser session storage unavailable:', error instanceof Error ? error.name : 'StorageError') }
  const value = sessionStorage.getItem(USER_SESSION_KEY) || ''
  if (value && !value.startsWith(COOKIE_SESSION_PREFIX) && !TEST_SESSION_STORAGE) {
    sessionStorage.removeItem(USER_SESSION_KEY)
    return ''
  }
  return value
}
function userSessionId(token=userSessionToken()): string {
  if (token.startsWith(COOKIE_SESSION_PREFIX)) return token.slice(COOKIE_SESSION_PREFIX.length)
  try {
    const encoded = token.split('.')[0]
    const payload = JSON.parse(atob(encoded.replace(/-/g,'+').replace(/_/g,'/') + '='.repeat((4 - encoded.length % 4) % 4)))
    return typeof payload.sub === 'string' && payload.sub ? payload.sub : ''
  } catch { return '' }
}

const demoCredentialsKey = (token=userSessionToken()) => {
  const id = userSessionId(token)
  return id ? `protrebot.binance-demo.credentials.${id}` : ''
}

export function loadDemoCredentials(): {apiKey:string;secretKey:string} {
  return {apiKey:'',secretKey:''}
}

export function saveDemoCredentials(_apiKey:string, _secretKey:string): void {
  // Credentials are intentionally kept in the authenticated server-side session vault.
}

export function clearDemoCredentials(_token=userSessionToken()): void {
  // No frontend secret persistence is used for demo credentials.
}

export function saveUserSessionToken(token: string, remember: boolean): void {
  if (token.trim() && !token.startsWith(COOKIE_SESSION_PREFIX) && !TEST_SESSION_STORAGE) {
    throw new Error('Sunucu güvenli tarayıcı oturumu oluşturmadı. Yeniden giriş yapın.')
  }
  localStorage.removeItem(USER_SESSION_KEY)
  sessionStorage.removeItem(USER_SESSION_KEY)
  if (token.trim()) (remember ? localStorage : sessionStorage).setItem(USER_SESSION_KEY, token.trim())
  window.dispatchEvent(new Event('protrebot-session-changed'))
}

export function clearUserSessionToken(): void {
  clearKaisChatHistory()
  clearLegacyChatHistory()
  try { localStorage.removeItem(USER_SESSION_KEY) }
  catch (error) { console.warn('Browser session storage unavailable:', error instanceof Error ? error.name : 'StorageError') }
  sessionStorage.removeItem(USER_SESSION_KEY)
  window.dispatchEvent(new Event('protrebot-session-changed'))
}

export const DEMO_CONNECTION_MODE = 'TESTNET' as const
export const DEMO_SAVE_CONFIRMATION = 'TESTNET KASAYA KAYDET' as const

export function buildDemoSavePayload(apiKey: string, secretKey: string): { mode: typeof DEMO_CONNECTION_MODE; api_key: string; secret_key: string; confirmation: typeof DEMO_SAVE_CONFIRMATION } {
  return {
    mode: DEMO_CONNECTION_MODE,
    api_key: apiKey.trim(),
    secret_key: secretKey.trim(),
    confirmation: DEMO_SAVE_CONFIRMATION,
  }
}

function apiRequestPath(input: RequestInfo | URL): string | null {
  try {
    const target = new URL(input instanceof Request ? input.url : String(input), window.location.href)
    const api = new URL(API_BASE, window.location.href)
    return target.origin === api.origin && (target.pathname === api.pathname || target.pathname.startsWith(`${api.pathname}/`)) ? target.pathname : null
  } catch {
    return null
  }
}

function isOwnerAccessCheckRequest(input: RequestInfo | URL): boolean {
  return apiRequestPath(input) === `${new URL(API_BASE, window.location.href).pathname}/web/access/check`
}

function isOwnerProtectedApiRequest(input: RequestInfo | URL): boolean {
  const path = apiRequestPath(input)
  if (!path || path === `${new URL(API_BASE, window.location.href).pathname}/health`) return false
  if (path === '/api/v22/bootstrap') return true
  return !['/api/v22', '/api/v24'].some(prefix => path === prefix || path.startsWith(`${prefix}/`))
}

export function installAuthorizedFetch(): void {
  if (installed) return
  installed = true
  window.fetch = (input: RequestInfo | URL, init: RequestInit = {}) => {
    const headers = new Headers(input instanceof Request ? input.headers : undefined)
    new Headers(init.headers).forEach((value, key) => headers.set(key, value))
    const apiRequest = apiRequestPath(input) !== null
    if (apiRequest) {
      headers.set('X-Requested-With', 'XMLHttpRequest')
      if (headers.get('Authorization')?.startsWith(`Bearer ${COOKIE_SESSION_PREFIX}`)) headers.delete('Authorization')
      if (headers.get('X-ProTreBot-Session')?.startsWith(COOKIE_SESSION_PREFIX)) headers.delete('X-ProTreBot-Session')
      if (headers.get('X-ProTreBot-Owner') === OWNER_COOKIE_MARKER) headers.delete('X-ProTreBot-Owner')
    }
    const token = ownerAccessToken()
    if (token && token !== OWNER_COOKIE_MARKER && isOwnerProtectedApiRequest(input) && !headers.has('X-ProTreBot-Owner')) {
      headers.set('X-ProTreBot-Owner', token)
    }
    const sessionHint = userSessionToken()
    const userToken = sessionHint.startsWith(COOKIE_SESSION_PREFIX) ? '' : sessionHint
    if (userToken && apiRequestPath(input) && !headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${userToken}`)
    }
    if (userToken && apiRequestPath(input) && !headers.has('X-ProTreBot-Session')) {
      headers.set('X-ProTreBot-Session', userToken)
    }
    return originalFetch(input, {...init, headers, ...(apiRequest ? {credentials: 'include'} : {})})
  }
}

export function reportClientError(error: unknown, context: Record<string, unknown> = {}): void {
  const value = error instanceof Error ? error : new Error(String(error))
  void originalFetch(`${API_BASE}/client-errors`, {
    method: 'POST', credentials: 'include', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
    body: JSON.stringify({kind:value.name || 'ClientError', message:value.message, stack:value.stack, route:window.location.pathname, context}),
    keepalive: true,
  }).then(response => {
    if (!response.ok) console.warn('Client error report rejected:', response.status)
  }).catch(error => console.warn('Client error report failed:', error instanceof Error ? error.name : 'ReportError'))
}

export function installErrorMonitoring(): void {
  if (errorMonitoringInstalled) return
  errorMonitoringInstalled = true
  window.addEventListener('error', event => reportClientError(event.error || event.message, {filename:event.filename, lineno:event.lineno}))
  window.addEventListener('unhandledrejection', event => reportClientError(event.reason, {type:'unhandledrejection'}))
}

export async function verifyOwnerAccess(token: string, signal?:AbortSignal): Promise<{authorized: boolean}> {
  return withRequestDeadline(async deadlineSignal => {
    const response = await originalFetch(`${API_BASE}/web/access/check`, {
      credentials: 'include',
      signal: deadlineSignal,
      headers: {'X-Requested-With': 'XMLHttpRequest', ...(token.trim() && token !== OWNER_COOKIE_MARKER ? {'X-ProTreBot-Owner': token.trim()} : {})},
    })
    const payload = await response.json().catch(() => null) as {authorized?: boolean;detail?: string}|null
    if (!response.ok || !payload?.authorized) {
      throw new Error(payload?.detail || 'Yönetici erişimi doğrulanamadı.')
    }
    return {authorized: true}
  },{signal})
}
