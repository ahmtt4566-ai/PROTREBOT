import {ModeratorRequestError} from './moderator-model'

export const EVENT_KINDS = [
  'auth.login_failed', 'auth.login_succeeded', 'auth.mfa_failed', 'auth.password_reset_requested',
  'auth.password_reset_completed', 'auth.email_verification_sent', 'auth.email_verification_failed',
  'auth.email_verified', 'account.session_revoked', 'api.error',
] as const
export type EventKind = typeof EVENT_KINDS[number]
export const eventKindLabels: Record<EventKind, string> = {
  'auth.login_failed': 'Giriş hatası', 'auth.login_succeeded': 'Başarılı giriş', 'auth.mfa_failed': 'MFA hatası',
  'auth.password_reset_requested': 'Şifre sıfırlama', 'auth.password_reset_completed': 'Şifre sıfırlama',
  'auth.email_verification_sent': 'E-posta doğrulama', 'auth.email_verification_failed': 'E-posta doğrulama',
  'auth.email_verified': 'E-posta doğrulama', 'account.session_revoked': 'Oturum iptali', 'api.error': 'Sistem hatası',
}
export const eventFilterLabels: Record<EventKind, string> = {
  ...eventKindLabels,
  'auth.password_reset_requested': 'Şifre sıfırlama isteği', 'auth.password_reset_completed': 'Şifre sıfırlama tamamlandı',
  'auth.email_verification_sent': 'Doğrulama gönderildi', 'auth.email_verification_failed': 'Doğrulama başarısız',
  'auth.email_verified': 'E-posta doğrulandı',
}
const labels: Record<string, string> = {
  invalid_credentials: 'Geçersiz giriş bilgileri', login_ok: 'Giriş tamamlandı', invalid_mfa: 'Doğrulama kodu geçersiz',
  reset_requested: 'Şifre sıfırlama istendi', reset_completed: 'Şifre sıfırlama tamamlandı',
  verification_sent: 'Doğrulama e-postası gönderildi', verification_failed: 'E-posta doğrulama başarısız',
  email_verified: 'E-posta doğrulandı', session_revoked: 'Oturum sonlandırıldı',
  server_error: 'İşlem sırasında sistem hatası', request_failed: 'İstek tamamlanamadı',
  event_limit: 'Yoğun olay kaydı: ek olaylar toplandı',
}
export function eventCodeLabel(code: string): string {return Object.hasOwn(labels, code) ? labels[code] : 'Diğer olay'}
export type CustomerEvent = {
  kind: EventKind; code: string; feature: string; http_status: number | null; count: number;
  first_at: string; last_at: string; request_ref: string | null;
  source: 'customer_event' | 'system_error'; severity: 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL'; resolved: boolean | null;
}
export type CustomerEventsPage = {items: CustomerEvent[]; total: number; limit: number; offset: number}
function record(value: unknown): value is Record<string, unknown> {return !!value && typeof value === 'object' && !Array.isArray(value)}
function integer(value: unknown, minimum: number, maximum = Number.MAX_SAFE_INTEGER): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum && value <= maximum
}
function date(value: unknown): value is string {return typeof value === 'string' && Number.isFinite(Date.parse(value))}
function fail(): never {throw new ModeratorRequestError(502)}
function parseEvent(value: unknown): CustomerEvent {
  if (!record(value)) return fail()
  const kind = EVENT_KINDS.find(item => item === value.kind)
  const source = value.source === 'customer_event' || value.source === 'system_error' ? value.source : null
  const severity = (['INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const).find(item => item === value.severity)
  if (!kind || !source || !severity || typeof value.code !== 'string' || !/^[a-z_]{1,40}$/.test(value.code) ||
    typeof value.feature !== 'string' || !['auth', 'security', 'verification', 'account', 'api', 'events'].includes(value.feature) ||
    (value.http_status !== null && !integer(value.http_status, 100, 599)) || !integer(value.count, 1) ||
    !date(value.first_at) || !date(value.last_at) ||
    (value.request_ref !== null && (typeof value.request_ref !== 'string' || !/^[0-9a-f]{8}$/.test(value.request_ref))) ||
    (value.resolved !== null && typeof value.resolved !== 'boolean')) return fail()
  return {kind, code: value.code, feature: value.feature, http_status: value.http_status, count: value.count,
    first_at: value.first_at, last_at: value.last_at, request_ref: value.request_ref, source, severity, resolved: value.resolved}
}
export function parseCustomerEvents(value: unknown): CustomerEventsPage {
  if (!record(value) || !Array.isArray(value.items) || !integer(value.limit, 1, 50) || value.items.length > value.limit ||
    !integer(value.total, 0) || !integer(value.offset, 0)) return fail()
  return {items: value.items.map(parseEvent), total: value.total, limit: value.limit, offset: value.offset}
}
