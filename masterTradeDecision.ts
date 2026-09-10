export type DecisionDirection = 'LONG' | 'SHORT' | 'NEUTRAL'
export type DecisionStatus = 'WAIT' | 'WATCH' | 'LONG SETUP' | 'SHORT SETUP' | 'STRONG LONG' | 'STRONG SHORT' | 'NO TRADE'
export type SignalStrength = 'VERY WEAK' | 'WEAK' | 'MODERATE' | 'STRONG' | 'VERY STRONG'
export type SignalFreshness = 'FRESH' | 'RECENT' | 'AGING' | 'STALE'
export type EntryQuality = 'POOR' | 'FAIR' | 'GOOD' | 'EXCELLENT'

export type DecisionAnalysis = {
  direction?: string
  confidence?: number
  entry?: number
  stop_loss?: number
  tp1?: number
  tp2?: number
  tp3?: number
  risk_reward?: number
  trend?: string
  momentum?: string
  rsi?: number
  macd?: number
  adx?: number
  atr?: number
  volume_ratio?: number
  support?: number
  resistance?: number
  normalized_signal?: string
  radar?: { trap_score?: number; breakout_quality?: number; entry_timing?: string }
}

export type DecisionCandle = { time: number; high: number; low: number; close: number; volume: number }
export type MtfAnalysis = DecisionAnalysis & { timeframe: string }
export type MtfRow = { timeframe: string; trend: string; momentum: string; direction: DecisionDirection; available: boolean }
export type ScoreBreakdown = { analysis: number; liquidity: number; volatility: number; mtf: number; freshness: number; riskReward: number }

export type TradeDecision = {
  status: DecisionStatus
  direction: DecisionDirection
  opportunityScore: number | null
  confidenceScore: number | null
  trendScore: number | null
  momentumScore: number | null
  volumeScore: number | null
  mtfScore: number | null
  riskRewardScore: number | null
  signalStrength: SignalStrength | null
  freshness: SignalFreshness | null
  signalAgeSeconds: number | null
  entryQuality: EntryQuality | null
  riskReward: number | null
  mtfConfirmed: number | null
  mtfTotal: number
  marketRegime: string
  reasons: string[]
  whyWait: string[]
  waitingFor: string[]
  riskFlags: string[]
  longCase: string[]
  shortCase: string[]
  breakdown: ScoreBreakdown | null
  mtfRows: MtfRow[]
}

const clamp = (value: number, min = 0, max = 100) => Math.round(Math.max(min, Math.min(max, value)))
const finite = (value: number | undefined): value is number => typeof value === 'number' && Number.isFinite(value)
const labelDirection = (value: string | undefined): DecisionDirection => {
  const normalized = String(value || '').toUpperCase()
  return normalized === 'LONG' ? 'LONG' : normalized === 'SHORT' ? 'SHORT' : 'NEUTRAL'
}
const directionalWord = (direction: DecisionDirection) => direction === 'LONG' ? 'bullish' : direction === 'SHORT' ? 'bearish' : 'neutral'

function scoreStrength(score: number): SignalStrength {
  if (score >= 85) return 'VERY STRONG'
  if (score >= 70) return 'STRONG'
  if (score >= 55) return 'MODERATE'
  if (score >= 35) return 'WEAK'
  return 'VERY WEAK'
}

function freshnessFor(age: number): SignalFreshness {
  if (age <= 120) return 'FRESH'
  if (age <= 900) return 'RECENT'
  if (age <= 3600) return 'AGING'
  return 'STALE'
}

function scoreForMtf(direction: DecisionDirection, rows: MtfRow[]) {
  const available = rows.filter(row => row.available)
  if (!available.length || direction === 'NEUTRAL') return null
  return clamp((available.filter(row => row.direction === direction).length / available.length) * 100)
}

export function buildTradeDecision(analysis: DecisionAnalysis | null, candles: DecisionCandle[], mtfAnalyses: MtfAnalysis[]): TradeDecision {
  const unavailable: TradeDecision = {
    status: 'NO TRADE', direction: 'NEUTRAL', opportunityScore: null, confidenceScore: null,
    trendScore: null, momentumScore: null, volumeScore: null, mtfScore: null, riskRewardScore: null,
    signalStrength: null, freshness: null, signalAgeSeconds: null, entryQuality: null, riskReward: null,
    mtfConfirmed: null, mtfTotal: 5, marketRegime: 'UNCLEAR', reasons: [],
    whyWait: ['Market analysis data unavailable'], waitingFor: ['Fresh market data'],
    riskFlags: ['Data unavailable'], longCase: [], shortCase: [], breakdown: null, mtfRows: [],
  }
  if (!analysis || candles.length < 2) return unavailable

  const direction = labelDirection(analysis.direction)
  const latest = candles[candles.length - 1]
  const previous = candles.slice(-21, -1)
  const previousHigh = previous.length ? Math.max(...previous.map(candle => candle.high)) : null
  const previousLow = previous.length ? Math.min(...previous.map(candle => candle.low)) : null
  const volumeRatio = finite(analysis.volume_ratio) ? analysis.volume_ratio : null
  const volatilityPct = latest.close > 0 ? ((latest.high - latest.low) / latest.close) * 100 : null
  const atrPct = finite(analysis.atr) && finite(analysis.entry) && analysis.entry > 0 ? (analysis.atr / analysis.entry) * 100 : null
  const ageSeconds = latest.time > 1_000_000_000_000 ? Math.max(0, (Date.now() - latest.time) / 1000) : Math.max(0, Date.now() / 1000 - latest.time)
  const freshness = freshnessFor(ageSeconds)
  const rawConfidence = finite(analysis.confidence) ? clamp(analysis.confidence) : null
  const riskReward = finite(analysis.risk_reward) ? analysis.risk_reward : null
  const freshnessPenalty = freshness === 'STALE' ? 0.55 : freshness === 'AGING' ? 0.8 : freshness === 'RECENT' ? 0.95 : 1
  const confidence = rawConfidence === null ? null : clamp(rawConfidence * freshnessPenalty)
  const trendScore = confidence === null ? null : clamp((/Güçlü|strong/i.test(analysis.trend || '') ? 100 : 65) * .55 + confidence * .45)
  const momentumScore = finite(analysis.rsi) && finite(analysis.macd) ? clamp((direction === 'LONG' ? analysis.rsi : direction === 'SHORT' ? 100 - analysis.rsi : 50) * .65 + (analysis.macd === 0 ? 50 : analysis.macd > 0 === (direction === 'LONG') ? 100 : 0) * .35) : null
  const volumeScore = volumeRatio === null ? null : clamp(volumeRatio / 1.5 * 100)
  const mtfRows: MtfRow[] = mtfAnalyses.map(item => ({
    timeframe: item.timeframe, trend: item.trend || '--', momentum: item.momentum || '--', direction: labelDirection(item.direction), available: Boolean(item.direction),
  }))
  const mtfScore = scoreForMtf(direction, mtfRows)
  const mtfConfirmed = mtfScore === null ? null : mtfRows.filter(row => row.available && row.direction === direction).length
  const volatilityScore = atrPct === null && volatilityPct === null ? null : clamp(100 - Math.max(0, (atrPct ?? volatilityPct ?? 0) - 3) * 25)
  const freshnessScore = clamp(100 - ageSeconds / 7200 * 100)
  const riskRewardScore = riskReward === null ? null : clamp(riskReward / 3 * 100)
  const breakdown = confidence === null || volumeScore === null || volatilityScore === null || mtfScore === null || riskRewardScore === null
    ? null
    : { analysis: confidence, liquidity: volumeScore, volatility: volatilityScore, mtf: mtfScore, freshness: freshnessScore, riskReward: riskRewardScore }
  const opportunityScore = breakdown ? clamp(breakdown.analysis * .45 + breakdown.liquidity * .15 + breakdown.volatility * .10 + breakdown.mtf * .15 + breakdown.freshness * .10 + breakdown.riskReward * .05) : null
  const distanceToResistance = finite(analysis.resistance) && latest.close > 0 ? Math.abs(analysis.resistance - latest.close) / latest.close * 100 : null
  const distanceToSupport = finite(analysis.support) && latest.close > 0 ? Math.abs(latest.close - analysis.support) / latest.close * 100 : null
  const nearbyLevel = direction === 'LONG' ? distanceToResistance !== null && distanceToResistance < Math.max(0.25, (atrPct || 0) * 1.2) : direction === 'SHORT' ? distanceToSupport !== null && distanceToSupport < Math.max(0.25, (atrPct || 0) * 1.2) : false
  const riskFlags: string[] = []
  if (volatilityPct !== null && volatilityPct > 3) riskFlags.push('High volatility')
  if (nearbyLevel) riskFlags.push(direction === 'LONG' ? 'Resistance nearby' : 'Support nearby')
  if (volumeRatio !== null && volumeRatio < 1) riskFlags.push('Weak volume')
  if (mtfScore !== null && mtfScore < 60) riskFlags.push('MTF conflict')
  if (freshness === 'AGING' || freshness === 'STALE') riskFlags.push('Stale signal')
  if (riskReward !== null && riskReward < 1.5) riskFlags.push('Risk/Reward below minimum')
  const longCase = [labelDirection(analysis.direction) === 'LONG' ? 'Trend aligned' : 'Trend not aligned', finite(analysis.macd) && analysis.macd > 0 ? 'MACD bullish' : 'MACD not bullish', volumeRatio !== null && volumeRatio >= 1 ? 'Volume confirmed' : 'Volume not confirmed', mtfScore !== null && mtfScore >= 60 ? 'MTF supportive' : 'MTF not confirmed']
  const shortCase = [labelDirection(analysis.direction) === 'SHORT' ? 'Trend aligned' : 'Trend not aligned', finite(analysis.macd) && analysis.macd < 0 ? 'MACD bearish' : 'MACD not bearish', volumeRatio !== null && volumeRatio >= 1 ? 'Volume confirmed' : 'Volume not confirmed', mtfScore !== null && mtfScore >= 60 ? 'MTF supportive' : 'MTF not confirmed']
  const reasons: string[] = []
  if (analysis.trend) reasons.push(`${analysis.trend} trend`)
  if (analysis.momentum) reasons.push(`${analysis.momentum} momentum`)
  if (volumeRatio !== null && volumeRatio >= 1) reasons.push('Volume increasing')
  if (finite(analysis.macd) && ((direction === 'LONG' && analysis.macd > 0) || (direction === 'SHORT' && analysis.macd < 0))) reasons.push(`MACD ${direction === 'LONG' ? 'bullish' : 'bearish'}`)
  if (finite(analysis.rsi) && ((direction === 'LONG' && analysis.rsi >= 50 && analysis.rsi <= 70) || (direction === 'SHORT' && analysis.rsi >= 30 && analysis.rsi <= 50))) reasons.push('RSI healthy')
  if (mtfConfirmed !== null) reasons.push(`MTF confirmation ${mtfConfirmed}/${mtfRows.filter(row => row.available).length}`)
  const whyWait = [...riskFlags]
  if (direction === 'NEUTRAL') whyWait.unshift('No clear directional structure')
  if (mtfScore !== null && mtfScore < 60) whyWait.push('MTF confirmation missing')
  if (volumeRatio !== null && volumeRatio < 1) whyWait.push('Volume insufficient')
  const waitingFor: string[] = []
  if (direction === 'NEUTRAL' || mtfScore !== null && mtfScore < 60) waitingFor.push('Directional confirmation')
  if (volumeRatio !== null && volumeRatio < 1) waitingFor.push('Volume expansion')
  if (nearbyLevel) waitingFor.push(direction === 'LONG' ? 'Resistance breakout' : 'Support breakdown')
  if (!waitingFor.length && direction !== 'NEUTRAL') waitingFor.push('Fresh confirmation candle')
  const blocked = direction === 'NEUTRAL' || !opportunityScore || riskFlags.includes('Risk/Reward below minimum') || riskFlags.includes('Stale signal')
  const strong = !blocked && opportunityScore >= 80 && (confidence ?? 0) >= 75 && (mtfScore ?? 0) >= 60 && riskFlags.length === 0
  const setup = !blocked && opportunityScore >= 60 && (mtfScore ?? 0) >= 40
  const status: DecisionStatus = blocked ? (direction === 'NEUTRAL' ? 'NO TRADE' : 'WAIT') : strong ? direction === 'LONG' ? 'STRONG LONG' : 'STRONG SHORT' : setup ? direction === 'LONG' ? 'LONG SETUP' : 'SHORT SETUP' : 'WATCH'
  const entryQuality: EntryQuality | null = opportunityScore === null ? null : opportunityScore >= 82 && !nearbyLevel ? 'EXCELLENT' : opportunityScore >= 65 ? 'GOOD' : opportunityScore >= 45 ? 'FAIR' : 'POOR'
  const marketRegime = direction === 'NEUTRAL' ? 'UNCLEAR' : previousHigh !== null && latest.close > previousHigh ? 'BREAKOUT' : previousLow !== null && latest.close < previousLow ? 'BREAKDOWN' : /Güçlü yükseliş|strong bullish/i.test(analysis.trend || '') ? 'TRENDING BULLISH' : /Güçlü düşüş|strong bearish/i.test(analysis.trend || '') ? 'TRENDING BEARISH' : volatilityPct !== null && volatilityPct > 3 ? 'HIGH VOLATILITY' : 'RANGING'
  return {
    status, direction, opportunityScore, confidenceScore: confidence, trendScore, momentumScore, volumeScore, mtfScore, riskRewardScore,
    signalStrength: opportunityScore === null ? null : scoreStrength(opportunityScore), freshness, signalAgeSeconds: Math.round(ageSeconds), entryQuality,
    riskReward, mtfConfirmed, mtfTotal: mtfRows.filter(row => row.available).length || 5, marketRegime, reasons: reasons.slice(0, 6), whyWait: [...new Set(whyWait)].slice(0, 5), waitingFor: [...new Set(waitingFor)].slice(0, 4), riskFlags,
    longCase, shortCase, breakdown, mtfRows,
  }
}
