import {API_BASE, userSessionToken} from './api'
import type {DecisionAnalysis, MtfAnalysis} from './masterTradeDecision'

export const CHART_AI_PRIVACY = 'Resmin sitemizde saklanmaz; analiz için yapay zeka sağlayıcımıza gönderilir.'
export const CHART_AI_DISCLAIMER = 'Bu analiz yatırım tavsiyesi değildir.'
export type ChartUsage = {
  analysis_enabled: boolean
  quota: {remaining: number | null; unlimited: boolean}
  acknowledgement: {required: boolean; policy_version: string; text: string}
  availability: {message: string}
}
export type ChartResult = {
  request_key: string
  status: 'processing' | 'succeeded' | 'failed' | 'expired'
  result_text: string | null
  error: {code: string; message: string} | null
}
export type ChartSnapshot = {
  symbol: string; timeframe: string; captured_at: string
  price: number | null; price_updated_at: string | null; market_updated_at: string | null
  indicators: Pick<DecisionAnalysis, 'rsi' | 'macd' | 'adx' | 'atr' | 'volume_ratio'>
  levels: Pick<DecisionAnalysis, 'entry' | 'stop_loss' | 'tp1' | 'tp2' | 'tp3' | 'support' | 'resistance'>
  direction?: string; confidence?: number; signal?: string
  mtf: Array<{symbol: string; timeframe: string; available: boolean; direction?: string; confidence?: number; updated_at: string | null}>
}

export class ChartClientError extends Error {
  constructor(public readonly code: string, message: string, public readonly status?: number) { super(message) }
}
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null
const nullableNumber = (value: unknown) => value === null || typeof value === 'number' && Number.isFinite(value)

export async function chartRequest(path: string, init: RequestInit = {}): Promise<unknown> {
  const token = userSessionToken()
  const signal = AbortSignal.any([AbortSignal.timeout(85000), ...(init.signal ? [init.signal] : [])])
  const response = await fetch(`${API_BASE}/chart-ai${path}`, {...init, signal})
  const value: unknown = await response.json()
  if (token !== userSessionToken()) throw new ChartClientError('session_changed', 'Oturum değişti; eski analiz gösterilmedi.')
  const storedFailure = record(value) && typeof value.request_key === 'string' && ['failed', 'expired'].includes(String(value.status))
  if (!response.ok && !storedFailure) {
    const error = record(value) ? (record(value.error) ? value.error : record(value.detail) ? value.detail : null) : null
    throw new ChartClientError(
      error && typeof error.code === 'string' ? error.code : 'request_failed',
      error && typeof error.message === 'string' ? error.message : 'Grafik AI isteği doğrulanamadı.',
      response.status,
    )
  }
  return value
}

export async function chartUsage(signal?: AbortSignal): Promise<ChartUsage> {
  const value = await chartRequest('/usage', {signal})
  if (!record(value) || typeof value.analysis_enabled !== 'boolean'
      || !record(value.quota) || !nullableNumber(value.quota.remaining) || typeof value.quota.unlimited !== 'boolean'
      || !record(value.acknowledgement) || typeof value.acknowledgement.required !== 'boolean'
      || typeof value.acknowledgement.policy_version !== 'string' || typeof value.acknowledgement.text !== 'string'
      || !record(value.availability) || typeof value.availability.message !== 'string') {
    throw new ChartClientError('usage_invalid', 'Grafik AI kullanım durumu doğrulanamadı.')
  }
  return {
    analysis_enabled: value.analysis_enabled,
    quota: {remaining: typeof value.quota.remaining === 'number' ? value.quota.remaining : null, unlimited: value.quota.unlimited},
    acknowledgement: {required: value.acknowledgement.required, policy_version: value.acknowledgement.policy_version, text: value.acknowledgement.text},
    availability: {message: value.availability.message},
  }
}

export function chartResult(value: unknown): ChartResult {
  if (!record(value) || typeof value.request_key !== 'string'
      || !['processing', 'succeeded', 'failed', 'expired'].includes(String(value.status))
      || !(value.result_text === null || typeof value.result_text === 'string')
      || !(value.error === null || record(value.error) && typeof value.error.code === 'string' && typeof value.error.message === 'string')) {
    throw new ChartClientError('result_invalid', 'Grafik AI sonucu doğrulanamadı.')
  }
  const status = value.status
  if (status !== 'processing' && status !== 'succeeded' && status !== 'failed' && status !== 'expired') {
    throw new ChartClientError('result_invalid', 'Grafik AI sonuç durumu geçersiz.')
  }
  if (status === 'succeeded' && (typeof value.result_text !== 'string' || !value.result_text.trim())) {
    throw new ChartClientError('result_invalid', 'Başarılı analiz metni boş olamaz.')
  }
  return {
    request_key: value.request_key, status,
    result_text: typeof value.result_text === 'string' ? value.result_text : null,
    error: record(value.error) && typeof value.error.code === 'string' && typeof value.error.message === 'string'
      ? {code: value.error.code, message: value.error.message} : null,
  }
}

export function chartSnapshot(symbol: string, timeframe: string, price: number | null, analysis: DecisionAnalysis | null,
  mtf: readonly MtfAnalysis[], priceUpdatedAt: string | null, marketUpdatedAt: string | null): ChartSnapshot {
  return {
    symbol, timeframe, captured_at: new Date().toISOString(), price,
    price_updated_at: priceUpdatedAt, market_updated_at: marketUpdatedAt,
    indicators: {rsi: analysis?.rsi, macd: analysis?.macd, adx: analysis?.adx, atr: analysis?.atr, volume_ratio: analysis?.volume_ratio},
    levels: {entry: analysis?.entry, stop_loss: analysis?.stop_loss, tp1: analysis?.tp1, tp2: analysis?.tp2, tp3: analysis?.tp3,
      support: analysis?.support, resistance: analysis?.resistance},
    direction: analysis?.direction, confidence: analysis?.confidence, signal: analysis?.normalized_signal,
    mtf: mtf.map(row => ({symbol, timeframe: row.timeframe, available: true,
      direction: row.direction, confidence: row.confidence, updated_at: marketUpdatedAt})),
  }
}

export async function captureChart(svg: SVGSVGElement | null): Promise<Blob> {
  if (!svg || svg.getBoundingClientRect().width <= 0) throw new Error('Görünür grafik henüz hazır değil.')
  const clone = svg.cloneNode(true)
  if (!(clone instanceof SVGSVGElement)) throw new Error('Grafik kopyalanamadı.')
  const originals = [svg, ...svg.querySelectorAll('*')]
  const copies = [clone, ...clone.querySelectorAll('*')]
  const properties = ['fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'opacity', 'fill-opacity', 'stroke-opacity',
    'font-family', 'font-size', 'font-weight', 'text-anchor', 'dominant-baseline', 'visibility', 'display']
  originals.forEach((element, index) => {
    const style = getComputedStyle(element)
    for (const property of properties) copies[index].setAttribute(property, style.getPropertyValue(property))
  })
  clone.querySelectorAll('script, foreignObject, image, use, a').forEach(element => element.remove())
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  const box = svg.getBoundingClientRect()
  const width = Math.min(1600, Math.max(320, Math.round(box.width)))
  const height = Math.max(180, Math.round(width * box.height / box.width))
  clone.setAttribute('width', String(width))
  clone.setAttribute('height', String(height))
  const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(clone)], {type: 'image/svg+xml'}))
  try {
    const image = new Image()
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve()
      image.onerror = () => reject(new Error('Grafik görsele dönüştürülemedi.'))
      image.src = url
    })
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const context = canvas.getContext('2d')
    if (!context) throw new Error('Tarayıcı görsel oluşturmayı desteklemiyor.')
    context.fillStyle = '#101820'
    context.fillRect(0, 0, width, height)
    context.drawImage(image, 0, 0, width, height)
    return await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('Grafik görseli oluşturulamadı.')), 'image/png'))
  } finally { URL.revokeObjectURL(url) }
}

export async function validateChartUpload(file: Blob) {
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error('Yalnız PNG, JPEG veya WebP seç.')
  if (!file.size || file.size > 5 * 1024 * 1024) throw new Error('Görsel en fazla 5 MiB olabilir.')
  const bitmap = await createImageBitmap(file).catch(() => {
    throw new Error('Görsel çözümlenemedi; geçerli bir PNG, JPEG veya WebP seç.')
  })
  try {
    if (bitmap.width * bitmap.height > 16_000_000) throw new Error('Görsel en fazla 16 megapiksel olabilir.')
  } finally { bitmap.close() }
}

export function imageBase64(image: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('Görsel okunamadı.'))
    reader.onload = () => {
      if (typeof reader.result !== 'string' || !reader.result.includes(',')) reject(new Error('Görsel kodlanamadı.'))
      else resolve(reader.result.slice(reader.result.indexOf(',') + 1))
    }
    reader.readAsDataURL(image)
  })
}
