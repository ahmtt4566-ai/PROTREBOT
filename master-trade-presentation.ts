export const MASTER_LAYOUT_DESKTOP_WIDTH = 1200

export function masterLayoutV2Enabled(search: string, width: number): boolean {
  return width >= MASTER_LAYOUT_DESKTOP_WIDTH && new URLSearchParams(search).get('masterLayoutV2') === '1'
}

export function analysisPrice(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? `$${value.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`
    : '—'
}

export function autoTradePresentation(channel: 'LIVE' | 'DEMO', enabled: boolean | undefined) {
  return {
    label: enabled === undefined ? '—' : !enabled ? 'Kapalı' : channel === 'LIVE' ? "Live'da açık" : "Demo'da açık",
    action: enabled ? "Auto Trade'e git" : "Auto Trade'i aç",
  }
}
