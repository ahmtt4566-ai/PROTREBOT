import type {NotificationSummary} from './notification-model'

export function NotificationSummaryLine({summary}: {summary: NotificationSummary | null}) {
  return <p className="mod-secondary" role="status">E-posta bildirimi: {summary?.pending ?? 'veri yok'} bekliyor, {summary?.failed ?? 'veri yok'} gönderilemedi</p>
}
