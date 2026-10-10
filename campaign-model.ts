import {ModeratorRequestError} from './moderator-model'

export const CAMPAIGN_STATUSES = ['draft', 'pending_approval', 'sending', 'sent', 'cancelled', 'failed'] as const
export type CampaignStatus = typeof CAMPAIGN_STATUSES[number]
export const campaignStatusLabels: Record<CampaignStatus, string> = {
  draft: 'Taslak', pending_approval: 'Onay bekliyor', sending: 'Gönderiliyor', sent: 'Tamamlandı', cancelled: 'İptal edildi', failed: 'Başarısız',
}
export const AUDIENCES = ['all_users', 'premium_users', 'team_only'] as const
export type CampaignAudience = typeof AUDIENCES[number]
export const audienceLabels: Record<CampaignAudience, string> = {all_users: 'Tüm aktif ve doğrulanmış hesaplar', premium_users: 'Premium erişimi olan hesaplar', team_only: 'Yönetici ve moderatörler'}
export type CampaignDraft = {title: string; subject: string; body: string; audience: CampaignAudience; scheduled_at: string | null}
export type Campaign = CampaignDraft & {
  id: string; kind: 'info'; status: CampaignStatus; created_by: string; created_by_role: 'OWNER' | 'MODERATOR'; content_hash: string;
  approval_request_id: string | null; recipient_count: number; queued_count: number; sent_count: number; failed_count: number; skipped_count: number;
  version: number; created_at: string; updated_at: string; started_at: string | null; finished_at: string | null;
}
export type CampaignPage = {items: Campaign[]; total: number; limit: number; offset: number}
export type CampaignPreview = {subject: string; text: string; audience: CampaignAudience; content_hash: string; recipient_count: number; opt_out_count: number;
  warnings: string[]; approximate_schedule: boolean; personalized_footer: boolean; delivery_enabled: boolean}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ModeratorRequestError(502)
  return value as Record<string, unknown>
}
function text(value: unknown): string {if (typeof value !== 'string') throw new ModeratorRequestError(502); return value}
function id(value: unknown): string {const s = text(value); if (!/^[A-Za-z0-9_-]{1,160}$/.test(s)) throw new ModeratorRequestError(502); return s}
function count(value: unknown, min = 0): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min) throw new ModeratorRequestError(502)
  return value
}
function date(value: unknown): string | null {
  if (value === null) return null
  const s = text(value)
  if (!Number.isFinite(Date.parse(s))) throw new ModeratorRequestError(502)
  return s
}
export function parseCampaign(value: unknown): Campaign {
  const r = object(value)
  const status = CAMPAIGN_STATUSES.find(s => s === r.status)
  const audience = AUDIENCES.find(s => s === r.audience)
  if (!status || !audience || r.kind !== 'info' || !['OWNER', 'MODERATOR'].includes(text(r.created_by_role)) ||
      !/^[0-9a-f]{64}$/.test(text(r.content_hash))) throw new ModeratorRequestError(502)
  const role = r.created_by_role === 'OWNER' ? 'OWNER' : 'MODERATOR'
  return {id: id(r.id), title: text(r.title), subject: text(r.subject), body: text(r.body), audience, kind: 'info', status,
    created_by: id(r.created_by), created_by_role: role, content_hash: text(r.content_hash), scheduled_at: date(r.scheduled_at),
    approval_request_id: r.approval_request_id === null ? null : id(r.approval_request_id),
    recipient_count: count(r.recipient_count), queued_count: count(r.queued_count), sent_count: count(r.sent_count), failed_count: count(r.failed_count), skipped_count: count(r.skipped_count),
    version: count(r.version, 1), created_at: date(r.created_at) ?? fail(), updated_at: date(r.updated_at) ?? fail(),
    started_at: date(r.started_at), finished_at: date(r.finished_at)}
}
function fail(): never {throw new ModeratorRequestError(502)}
export function parseCampaignPage(value: unknown): CampaignPage {
  const r = object(value)
  if (!Array.isArray(r.items) || r.items.length > 50 || count(r.limit, 1) > 50) return fail()
  return {items: r.items.map(parseCampaign), total: count(r.total), limit: count(r.limit, 1), offset: count(r.offset)}
}
export function parseCampaignPreview(value: unknown): CampaignPreview {
  const r = object(value)
  const audience = AUDIENCES.find(value => value === r.audience)
  if (!Array.isArray(r.warnings) || r.warnings.length > 5 || r.warnings.some(s => typeof s !== 'string') ||
    typeof r.approximate_schedule !== 'boolean' || typeof r.personalized_footer !== 'boolean' || typeof r.delivery_enabled !== 'boolean' ||
    !audience || !/^[0-9a-f]{64}$/.test(text(r.content_hash))) return fail()
  const warnings: string[] = r.warnings.map(text)
  return {subject: text(r.subject), text: text(r.text), audience, content_hash: text(r.content_hash), recipient_count: count(r.recipient_count),
    opt_out_count: count(r.opt_out_count), warnings, approximate_schedule: r.approximate_schedule,
    personalized_footer: r.personalized_footer, delivery_enabled: r.delivery_enabled}
}
