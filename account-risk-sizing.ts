export function preferredRiskMargin(input: {wallet: number; available: number; entry: number; stop: number; leverage: number; percent: number}): {margin: number | null; reason: string} {
  if (![input.wallet, input.available, input.entry, input.stop, input.leverage, input.percent].every(value => Number.isFinite(value) && value > 0)) return {margin: null, reason: 'Güncel bakiye, giriş, stop, kaldıraç ve risk tercihi gerekli.'}
  if (input.percent < 0.1 || input.percent > 1 || input.leverage > 125) return {margin: null, reason: 'Risk veya kaldıraç tercihi desteklenen aralığın dışında.'}
  const distance = Math.abs(input.entry - input.stop) / input.entry
  if (distance <= 0 || distance >= 1) return {margin: null, reason: 'Geçerli bir stop mesafesi gerekli.'}
  const margin = Math.floor((input.wallet * input.percent / 100 / distance / input.leverage) * 100) / 100
  if (!Number.isFinite(margin) || margin < 5) return {margin: null, reason: 'Hesaplanan marj mevcut minimumun altında.'}
  if (margin > input.available) return {margin: null, reason: 'Hesaplanan marj kullanılabilir bakiyeyi aşıyor; risk veya stop tercihini kontrol edin.'}
  return {margin, reason: 'Tahmini marj. Ücret, kayma ve canlı risk sınırları mevcut emir öncesi kontrollerde doğrulanır.'}
}
