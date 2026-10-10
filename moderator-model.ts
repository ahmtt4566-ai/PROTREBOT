export const MODERATOR_PERMISSIONS = [
  'customers.view', 'subscriptions.view', 'payments.view', 'support.view',
  'support.manage', 'events.view', 'approvals.create',
] as const
export type ModeratorPermission = typeof MODERATOR_PERMISSIONS[number]
export const permissionLabels: Record<ModeratorPermission, string> = {
  'customers.view': 'Müşterileri görüntüle',
  'subscriptions.view': 'Abonelikleri görüntüle',
  'payments.view': 'Ödemeleri görüntüle',
  'support.view': 'Destek taleplerini görüntüle',
  'support.manage': 'Destek taleplerini yönet',
  'events.view': 'Etkinliği görüntüle',
  'approvals.create': 'Onay talebi oluştur',
}
