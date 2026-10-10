import {ModeratorRequestError, parseCustomerProfile, type CustomerProfile} from './moderator-model'

export const CASE_STATUSES = ['NEW', 'OPEN', 'WAITING', 'RESOLVED', 'CLOSED'] as const
export type CaseStatus = typeof CASE_STATUSES[number]
export const statusLabels: Record<CaseStatus, string> = {NEW: 'Yeni', OPEN: 'Açık', WAITING: 'Beklemede', RESOLVED: 'Çözüldü', CLOSED: 'Kapalı'}
export const PRIORITIES = ['LOW', 'NORMAL', 'HIGH'] as const
export type SupportPriority = typeof PRIORITIES[number]
export const priorityLabels: Record<SupportPriority, string> = {LOW: 'Düşük', NORMAL: 'Normal', HIGH: 'Yüksek'}
export type SupportCase = {
  id: string; user_id: string; subject: string; priority: SupportPriority; case_status: CaseStatus;
  assignee_user_id: string | null; created_at: string; version: number; assigned_to_me: boolean;
}
export type SupportNote = {id: string; author_user_id: string; body: string; created_at: string}
export type SupportDetail = SupportCase & {
  message: string; legacy_status: string; legacy_response_note: string; notes: SupportNote[]; customer: CustomerProfile;
}
export type SupportPage = {items: SupportCase[]; total: number; limit: number; offset: number}
export type SupportSummary = {open_cases: number | null; unassigned: number | null; assigned_to_me: number | null}
export type SupportFilter = 'all' | 'unassigned' | 'mine' | 'open' | 'waiting' | 'resolved'
export const supportFilters: {id: SupportFilter; label: string}[] = [
  {id: 'all', label: 'Tümü'}, {id: 'unassigned', label: 'Atanmamış'}, {id: 'mine', label: 'Bana atanmış'},
  {id: 'open', label: 'Açık'}, {id: 'waiting', label: 'Beklemede'}, {id: 'resolved', label: 'Çözüldü'},
]
export function supportFilterQuery(filter: SupportFilter, priority: SupportPriority | '', offset: number): string {
  const params = new URLSearchParams({limit: '25', offset: String(offset)})
  if (filter === 'mine' || filter === 'unassigned') params.set('assignment', filter)
  if (filter === 'open') params.set('status', 'OPEN')
  if (filter === 'waiting') params.set('status', 'WAITING')
  if (filter === 'resolved') params.set('status', 'RESOLVED')
  if (priority) params.set('priority', priority)
  return params.toString()
}
export function supportCanEdit(case_: SupportCase, manage: boolean): boolean {
  return manage && case_.assigned_to_me
}

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
export function parseSupportCase(value: unknown): SupportCase {
  const row = object(value)
  const priority = PRIORITIES.find(item => item === row.priority)
  const status = CASE_STATUSES.find(item => item === row.case_status)
  if (!priority || !status) throw new ModeratorRequestError(502)
  return {
    id: id(row.id), user_id: id(row.user_id), subject: text(row.subject), priority, case_status: status,
    assignee_user_id: row.assignee_user_id === null ? null : id(row.assignee_user_id),
    created_at: date(row.created_at), version: integer(row.version, 1), assigned_to_me: boolean(row.assigned_to_me),
  }
}
export function parseSupportPage(value: unknown): SupportPage {
  const row = object(value)
  if (!Array.isArray(row.items) || row.items.length > 50) throw new ModeratorRequestError(502)
  const limit = integer(row.limit, 1)
  if (limit > 50) throw new ModeratorRequestError(502)
  return {items: row.items.map(parseSupportCase), total: integer(row.total), limit, offset: integer(row.offset)}
}
export function parseSupportDetail(value: unknown): SupportDetail {
  const row = object(value)
  if (!Array.isArray(row.notes) || !['OPEN', 'IN_PROGRESS', 'RESOLVED', 'CLOSED'].includes(text(row.legacy_status))) throw new ModeratorRequestError(502)
  return {
    ...parseSupportCase(row), message: text(row.message), legacy_status: text(row.legacy_status),
    legacy_response_note: text(row.legacy_response_note), customer: parseCustomerProfile(row.customer),
    notes: row.notes.map(value => {
      const note = object(value)
      return {id: id(note.id), author_user_id: id(note.author_user_id), body: text(note.body), created_at: date(note.created_at)}
    }),
  }
}
export function parseSupportSummary(value: unknown): SupportSummary {
  const row = object(value)
  const count = (value: unknown) => value === null ? null : integer(value)
  return {open_cases: count(row.open_cases), unassigned: count(row.unassigned), assigned_to_me: count(row.assigned_to_me)}
}
