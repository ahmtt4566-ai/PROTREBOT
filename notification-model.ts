import {ModeratorRequestError} from './moderator-model'

export type NotificationSummary = {pending: number | null; failed: number | null}
export function parseNotificationSummary(value: unknown): NotificationSummary {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ModeratorRequestError(502)
  const count = (value: unknown): number | null => {
    if (value === null) return null
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) throw new ModeratorRequestError(502)
    return value
  }
  return {
    pending: count('pending' in value ? value.pending : undefined),
    failed: count('failed' in value ? value.failed : undefined),
  }
}
