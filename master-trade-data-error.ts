export type MarketDataFailureKind = 'history' | 'inactive' | 'backend' | 'frontend' | 'preview' | 'session'

export class MarketDataFailure extends Error {
  readonly kind: MarketDataFailureKind

  constructor(kind: MarketDataFailureKind, message: string) {
    super(message)
    this.kind = kind
    this.name = 'MarketDataFailure'
  }
}

export async function marketDataResponseError(response: Response): Promise<MarketDataFailure> {
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    return new MarketDataFailure('backend', `Bu sembol için yeterli veri yok (backend geçersiz yanıt döndürdü; HTTP ${response.status}).`)
  }
  const detail = typeof payload === 'object' && payload !== null && 'detail' in payload && typeof payload.detail === 'string' ? payload.detail.trim() : ''
  const code = typeof payload === 'object' && payload !== null && 'code' in payload ? payload.code : undefined
  if (code === 'PREVIEW_DATA_UNAVAILABLE') return new MarketDataFailure('preview', 'Önizleme: bu sembol için örnek veri yok')
  if (response.status === 401 || response.status === 403) return new MarketDataFailure('session', `Veri okunamadı (${detail || 'oturum veya erişim izni gerekli'}; HTTP ${response.status}).`)
  if (response.status === 422 && /yeterli.*mum|mum.*yeterli|insufficient.*candles|not enough.*candles/i.test(detail)) {
    return new MarketDataFailure('history', `Bu sembol için yeterli veri yok (${detail}).`)
  }
  if ((response.status === 400 || response.status === 404) && /invalid symbol|sembol.*aktif değil|symbol.*inactive/i.test(detail)) {
    return new MarketDataFailure('inactive', `Bu sembol için yeterli veri yok (borsada aktif sembol bulunamadı; ${detail}).`)
  }
  return new MarketDataFailure('backend', `Bu sembol için yeterli veri yok (${detail || 'backend veriyi sağlayamadı'}; HTTP ${response.status}).`)
}

export function marketDataFailure(error: unknown): MarketDataFailure {
  if (error instanceof MarketDataFailure) return error
  if (error instanceof Error && error.name === 'TimeoutError') return new MarketDataFailure('frontend', 'Bu sembol için yeterli veri yok (veri isteği zaman aşımına uğradı).')
  return new MarketDataFailure('backend', `Bu sembol için yeterli veri yok (${error instanceof Error ? error.message : 'veri isteği başarısız'}).`)
}
