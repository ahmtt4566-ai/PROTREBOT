import {ModeratorRequestError} from './moderator-model'
import {parseCampaignPreview, type CampaignPreview} from './campaign-model'

export const APPROVAL_STATUSES = ['pending', 'approved', 'rejected', 'cancelled', 'expired', 'stale', 'executing', 'executed', 'failed'] as const
export type ApprovalStatus = typeof APPROVAL_STATUSES[number]
export type ApprovalAction = 'account.deactivate' | 'account.reactivate' | 'campaign.send'
export const approvalLabels: Record<ApprovalStatus, string> = {
  pending: 'Bekliyor', approved: 'Onaylandı', rejected: 'Reddedildi', cancelled: 'İptal',
  expired: 'Süresi doldu', stale: 'Yeniden inceleme', executing: 'Kontrol gerekli', executed: 'Uygulandı', failed: 'Başarısız',
}
export const actionLabels: Record<ApprovalAction, string> = {'account.deactivate': 'Hesabı pasifleştirme', 'account.reactivate': 'Hesabı aktif etme', 'campaign.send': 'Bilgilendirme duyurusu gönderimi'}
export type ApprovalTarget = {active: boolean; role: 'CUSTOMER' | 'MODERATOR' | 'OWNER'; auth_version: number}
type AccountApproval = {
  id: string; action_type: 'account.deactivate' | 'account.reactivate'; target_user_id: string; requester_user_id: string;
  requester_role: 'MODERATOR'; target_snapshot: ApprovalTarget; reason: string; status: ApprovalStatus;
  decided_by: string | null; decided_at: string | null; decision_note: string | null;
  executed_at: string | null; result_code: string | null; expires_at: string; version: number; created_at: string; needs_review: boolean;
}
export type CampaignApproval = Omit<AccountApproval, 'action_type' | 'target_user_id' | 'target_snapshot'> & {
  action_type: 'campaign.send'; target_user_id: null; target_snapshot: null; payload: {campaign_id: string; content_hash: string};
}
export type Approval = AccountApproval | CampaignApproval
export type ApprovalPage = {items: Approval[]; total: number; limit: number; offset: number; pending_count: number}
export type ApprovalDetail = {request: Approval; current_target: ApprovalTarget | null; target_changed: boolean; active_subscription: boolean; campaign_preview?: CampaignPreview}
const resultCodes = ['ok', 'already_target_state', 'canonical_applied', 'protected_positions', 'agents_revoke_pending', 'execution_failed', 'target_changed', 'requester_changed']

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ModeratorRequestError(502)
  return value as Record<string, unknown>
}
function text(value: unknown): string {
  if (typeof value !== 'string') throw new ModeratorRequestError(502)
  return value
}
function id(value: unknown): string {
  const result = text(value)
  if (!/^[A-Za-z0-9_-]{1,160}$/.test(result)) throw new ModeratorRequestError(502)
  return result
}
function integer(value: unknown, min = 0): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min) throw new ModeratorRequestError(502)
  return value
}
function boolean(value: unknown): boolean {
  if (typeof value !== 'boolean') throw new ModeratorRequestError(502)
  return value
}
function date(value: unknown): string {
  const result = text(value)
  if (!Number.isFinite(Date.parse(result))) throw new ModeratorRequestError(502)
  return result
}
function target(value: unknown): ApprovalTarget {
  const row = object(value)
  const role = (['CUSTOMER', 'MODERATOR', 'OWNER'] as const).find(item => item === row.role)
  if (!role) throw new ModeratorRequestError(502)
  return {active: boolean(row.active), role, auth_version: integer(row.auth_version, 1)}
}
export function parseApproval(value: unknown): Approval {
  const row = object(value)
  const status = APPROVAL_STATUSES.find(item => item === row.status)
  const action = (['account.deactivate', 'account.reactivate', 'campaign.send'] as const).find(item => item === row.action_type)
  if (!status || !action || row.requester_role !== 'MODERATOR' || row.result_code !== null && !resultCodes.includes(text(row.result_code)) &&
      !(action === 'campaign.send' && row.result_code === 'campaign_changed')) throw new ModeratorRequestError(502)
  if (action === 'campaign.send') {
    const data = object(row.payload)
    if (row.target_user_id !== null || Object.keys(object(row.target_snapshot)).length ||
      !/^[0-9a-f]{64}$/.test(text(data.content_hash))) throw new ModeratorRequestError(502)
    return {id: id(row.id), action_type: action, target_user_id: null, requester_user_id: id(row.requester_user_id),
      requester_role: 'MODERATOR', target_snapshot: null, payload: {campaign_id: id(data.campaign_id), content_hash: text(data.content_hash)},
      reason: text(row.reason), status, decided_by: row.decided_by === null ? null : id(row.decided_by),
      decided_at: row.decided_at === null ? null : date(row.decided_at), decision_note: row.decision_note === null ? null : text(row.decision_note),
      executed_at: row.executed_at === null ? null : date(row.executed_at), result_code: row.result_code === null ? null : text(row.result_code),
      expires_at: date(row.expires_at), version: integer(row.version, 1), created_at: date(row.created_at), needs_review: boolean(row.needs_review)}
  }
  return {
    id: id(row.id), action_type: action, target_user_id: id(row.target_user_id), requester_user_id: id(row.requester_user_id),
    requester_role: 'MODERATOR', target_snapshot: target(row.target_snapshot), reason: text(row.reason), status,
    decided_by: row.decided_by === null ? null : id(row.decided_by), decided_at: row.decided_at === null ? null : date(row.decided_at),
    decision_note: row.decision_note === null ? null : text(row.decision_note), executed_at: row.executed_at === null ? null : date(row.executed_at),
    result_code: row.result_code === null ? null : text(row.result_code), expires_at: date(row.expires_at),
    version: integer(row.version, 1), created_at: date(row.created_at), needs_review: boolean(row.needs_review),
  }
}
export function parseApprovalPage(value: unknown): ApprovalPage {
  const row = object(value)
  const limit = integer(row.limit, 1)
  if (!Array.isArray(row.items) || row.items.length > 50 || limit > 50) throw new ModeratorRequestError(502)
  return {items: row.items.map(parseApproval), total: integer(row.total), limit, offset: integer(row.offset), pending_count: integer(row.pending_count)}
}
export function parseApprovalDetail(value: unknown): ApprovalDetail {
  const row = object(value)
  const request = parseApproval(row.request)
  if (request.action_type === 'campaign.send') {
    if (row.current_target !== null || row.active_subscription !== false) throw new ModeratorRequestError(502)
    return {request, current_target: null, target_changed: boolean(row.target_changed), active_subscription: false, campaign_preview: parseCampaignPreview(row.campaign_preview)}
  }
  return {request, current_target: row.current_target === null ? null : target(row.current_target),
    target_changed: boolean(row.target_changed), active_subscription: boolean(row.active_subscription)}
}
export function approvalErrorMessage(error: unknown): string {
  if (error instanceof ModeratorRequestError) {
    if (error.status === 404) return 'Talep veya müşteri bulunamadı. Listeyi yenileyin.'
    if (error.status === 409) return 'Bu talep güncellendi veya hesap durumu değişti. Yenileyip yeniden inceleyin.'
    if (error.status === 422) return 'Gerekçe ve karar notunu kontrol edip yeniden deneyin.'
    return error.message
  }
  return 'Onay işlemi tamamlanamadı. Listeyi yenileyip yeniden deneyin.'
}
export function canRetryAgents(row: Approval): boolean {
  return row.status === 'failed' && row.result_code === 'agents_revoke_pending' && row.action_type === 'account.deactivate'
}
