import type {TriggerLifecycle} from './masterTradeDecision'

export const MASTER_LAYOUT_DESKTOP_WIDTH = 1200
export const MASTER_MTF_INTERVALS = ['1m', '5m', '15m', '1h', '4h'] as const

export function masterLayoutV2Enabled(search: string, width: number): boolean {
  return width > 0 && new URLSearchParams(search).get('masterLayoutV2') !== '0'
}

export function analysisValue(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('en-US', Math.abs(value) > 0 && Math.abs(value) < 1
      ? {minimumSignificantDigits: 3, maximumSignificantDigits: 5}
      : {minimumFractionDigits: 2, maximumFractionDigits: 2})
    : '—'
}

export function analysisPrice(value: number | null | undefined): string {
  const formatted = analysisValue(value)
  return formatted === '—' ? formatted : `$${formatted}`
}

export function autoTradePresentation(channel: 'LIVE' | 'DEMO', enabled: boolean | undefined) {
  return {
    label: enabled === undefined ? '—' : !enabled ? 'Kapalı' : channel === 'LIVE' ? "Live'da açık" : "Demo'da açık",
    action: enabled ? "Auto Trade'e git" : "Auto Trade'i aç",
  }
}

export function analysisChartRange(candles: ReadonlyArray<{high: number; low: number}>) {
  if (!candles.length || candles.some(candle => !Number.isFinite(candle.high) || !Number.isFinite(candle.low) || candle.high < candle.low)) return null
  const low = Math.min(...candles.map(candle => candle.low))
  const high = Math.max(...candles.map(candle => candle.high))
  const span = high - low || Math.max(Math.abs(high) * .001, 1e-8)
  return {low: low - span * .08, high: high + span * .08}
}

export function analysisChartLabels(lines: ReadonlyArray<{label: string; value: number; tone: string}>, range: {low: number; high: number}, height: number, priceHeight = height) {
  const toY = (value: number) => (range.high - value) / (range.high - range.low) * priceHeight
  const labels = lines.map(line => ({
    ...line, priceY: toY(line.value),
    edge: line.value > range.high ? 'above' as const : line.value < range.low ? 'below' as const : 'inside' as const,
  })).sort((left, right) => left.priceY - right.priceY)
  const positions: number[] = []
  labels.forEach((line, index) => positions.push(Math.max(11, Math.min(height - 11, line.edge === 'above' ? 11 : line.edge === 'below' ? height - 11 : line.priceY), index ? positions[index - 1] + 42 : 11)))
  for (let index = positions.length - 1; index >= 0; index--) {
    positions[index] = Math.min(positions[index], index === positions.length - 1 ? height - 11 : positions[index + 1] - 42)
  }
  return labels.map((line, index) => ({...line, y: positions[index]}))
}

export function triggerPresentation(lifecycle: TriggerLifecycle, available: boolean) {
  const statuses: Record<TriggerLifecycle, {label: string; tone: string}> = {
    WAITING: {label: 'Waiting', tone: 'warning'},
    ARMED: {label: 'Armed', tone: 'warning'},
    TRIGGERED: {label: 'Triggered', tone: 'positive'},
    CONFIRMED: {label: 'Confirmed', tone: 'positive'},
    INVALIDATED: {label: 'Invalidated', tone: 'negative'},
    EXPIRED: {label: 'Expired', tone: 'neutral'},
  }
  return available ? statuses[lifecycle] : {label: '—', tone: 'neutral'}
}
