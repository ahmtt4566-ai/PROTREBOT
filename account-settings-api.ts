import {API_BASE} from './api'
import {fetchWithTimeout} from './master-trade-request'

export const TRADING_TIMEFRAMES = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '1w'] as const

export type TradingPreferences = {
  trading_mode: 'MANUAL' | 'AUTO'; timeframe: string; exchange: 'BINANCE';
  risk_per_trade: number; symbols: string[];
}
export type AccountSession = {id: string; current: boolean; device: string; browser: string; created_at: string; last_seen_at: string; expires_at: string}
export type AccountActivity = {id: string; kind: string; message: string; created_at: string}
export type AccountUser = {
  id: string; display_name: string; email: string; role: string; active: boolean;
  email_verified: boolean; created_at: string; last_login: string | null;
  password_changed_at: string | null; auth_methods: string[];
}
export type AccountSubscription = {
  plan: string; status: string; expires_at: string | null; is_premium: boolean;
  features: Array<{key: string; label: string; included: boolean}>;
}
export type AccountOverview = {
  user: AccountUser; subscription: AccountSubscription; preferences: TradingPreferences;
  security: {two_factor_enabled: boolean; active_sessions: number; email_delivery_available: boolean; can_close_account: boolean; close_blocker: string | null};
  pending_email: {email: string; expires_at: string} | null;
  activity: AccountActivity[]; sessions: AccountSession[];
}
export type AdminAccount = AccountUser & {subscription: AccountSubscription; two_factor_enabled: boolean; active_sessions: number; closed_at: string | null}
export type AdminAccounts = {users: AdminAccount[]; total: number; page: number; page_size: number}
export type SensitiveProof = {current_password?: string; totp_code?: string; challenge_id?: string; email_code?: string}
export type AccountMutation = {ok: boolean; reauthenticate?: boolean; notification_sent?: boolean}

export class AccountRequestError extends Error {
  constructor(message: string, readonly status: number, readonly retryAfter: number) {super(message)}
}

export async function accountRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body) headers.set('Content-Type', 'application/json')
  headers.set('X-Requested-With', 'XMLHttpRequest')
  const response = await fetchWithTimeout(`${API_BASE}/v22${path}`, {...options, headers, credentials: 'same-origin'}, 20000)
  const payload: unknown = await response.json()
  if (!response.ok) {
    const detail = payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : null
    const message = typeof detail === 'string' ? detail : Array.isArray(detail)
      ? detail.map(item => item && typeof item === 'object' && 'msg' in item ? String(item.msg) : 'Geçersiz alan').join(' · ')
      : `Hesap işlemi tamamlanamadı (HTTP ${response.status}).`
    throw new AccountRequestError(message, response.status, Number(response.headers.get('Retry-After')) || 0)
  }
  return payload as T
}

export const accountDate = (value: string | null | undefined) => value && Number.isFinite(Date.parse(value))
  ? new Date(value).toLocaleString('tr-TR', {dateStyle: 'medium', timeStyle: 'short'}) : '—'
export const accountInitials = (name: string) => name.trim().split(/\s+/).slice(0, 2).map(part => part[0]).join('').toLocaleUpperCase('tr-TR') || '—'
