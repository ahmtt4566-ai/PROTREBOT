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

export type TriggerLifecycle = 'WAITING' | 'ARMED' | 'TRIGGERED' | 'CONFIRMED' | 'INVALIDATED' | 'EXPIRED'
export type TriggerCondition = { key: string; label: string; passed: boolean; available: boolean; detail: string }
export type PreTradeCheck = { label: string; status: 'PASS' | 'FAIL' | 'LOCKED' | 'UNAVAILABLE'; detail: string }
export type TriggerMonitor = {
  lifecycle: TriggerLifecycle
  available: boolean
  direction: DecisionDirection
  triggerPrice: number | null
  currentPrice: number | null
  distancePct: number | null
  conditions: TriggerCondition[]
  remainingConditions: number | null
  invalidation: string[]
  waitingMessage: string
  statusMessage: string
  entryPreview: { entry: number; stopLoss: number; tp1: number; tp2: number; tp3: number; riskReward: number } | null
  preTradeChecks: PreTradeCheck[]
}

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

const unavailableTrigger = (): TriggerMonitor => ({
  lifecycle: 'WAITING', available: false, direction: 'NEUTRAL', triggerPrice: null, currentPrice: null,
  distancePct: null, conditions: [], remainingConditions: null, invalidation: ['Market data unavailable'],
  waitingMessage: 'DATA UNAVAILABLE', statusMessage: 'WAITING FOR MARKET DATA', entryPreview: null,
  preTradeChecks: [
    { label: 'Market data', status: 'UNAVAILABLE', detail: 'No current snapshot' },
    { label: 'Live trading', status: 'LOCKED', detail: 'Execution is disabled' },
  ],
})

export function buildTriggerMonitor(
  decision: TradeDecision,
  analysis: DecisionAnalysis | null,
  candles: DecisionCandle[],
  previousLifecycle: TriggerLifecycle | null = null,
  currentPrice?: number,
): TriggerMonitor {
  if (!analysis || candles.length < 2 || decision.direction === 'NEUTRAL') return unavailableTrigger()
  const latest = candles[candles.length - 1]
  const price = finite(currentPrice) && currentPrice > 0 ? currentPrice : latest.close
  const direction = decision.direction
  const triggerPrice = direction === 'LONG' ? (finite(analysis.resistance) ? analysis.resistance : null) : (finite(analysis.support) ? analysis.support : null)
  const volumePassed = finite(analysis.volume_ratio) && analysis.volume_ratio >= 1
  const momentumPassed = finite(analysis.rsi) && finite(analysis.macd) && (direction === 'LONG' ? analysis.rsi >= 50 && analysis.rsi <= 72 && analysis.macd > 0 : analysis.rsi >= 28 && analysis.rsi <= 50 && analysis.macd < 0)
  const trendPassed = labelDirection(analysis.direction) === direction
  const mtfPassed = decision.mtfScore !== null && decision.mtfScore >= 60
  const rrPassed = decision.riskReward !== null && decision.riskReward >= 1.5
  const freshnessPassed = decision.freshness === 'FRESH' || decision.freshness === 'RECENT'
  const breakoutPassed = triggerPrice !== null && (direction === 'LONG' ? price >= triggerPrice : price <= triggerPrice)
  const conditions: TriggerCondition[] = [
    { key: 'trend', label: `${decision.direction === 'LONG' ? '15m' : '15m'} Trend`, passed: trendPassed, available: Boolean(analysis.trend), detail: analysis.trend || '--' },
    { key: 'momentum', label: 'Momentum', passed: momentumPassed, available: finite(analysis.rsi) && finite(analysis.macd), detail: analysis.momentum || '--' },
    { key: 'volume', label: 'Volume', passed: volumePassed, available: finite(analysis.volume_ratio), detail: finite(analysis.volume_ratio) ? `${analysis.volume_ratio.toFixed(2)}x` : '--' },
    { key: 'breakout', label: direction === 'LONG' ? 'Breakout' : 'Breakdown', passed: breakoutPassed, available: triggerPrice !== null, detail: triggerPrice === null ? '--' : `Trigger ${triggerPrice}` },
    { key: 'mtf', label: 'MTF', passed: mtfPassed, available: decision.mtfScore !== null, detail: decision.mtfConfirmed === null ? '--' : `${decision.mtfConfirmed}/${decision.mtfTotal}` },
    { key: 'riskReward', label: 'R/R', passed: rrPassed, available: decision.riskReward !== null, detail: decision.riskReward === null ? '--' : `1:${decision.riskReward.toFixed(2)}` },
    { key: 'freshness', label: 'Freshness', passed: freshnessPassed, available: decision.freshness !== null, detail: decision.freshness || '--' },
  ]
  const unavailableCount = conditions.filter(condition => !condition.available).length
  const failedCount = conditions.filter(condition => condition.available && !condition.passed).length
  const allPassed = unavailableCount === 0 && failedCount === 0
  const invalidation: string[] = direction === 'LONG'
    ? ['Support lost', 'MTF conflict', 'Volume deterioration', 'Signal stale']
    : ['Resistance reclaimed', 'Bullish MTF reversal', 'Volume deterioration', 'Signal stale']
  const invalidated = previousLifecycle !== null && ['ARMED', 'TRIGGERED', 'CONFIRMED'].includes(previousLifecycle)
    && ((direction === 'LONG' && finite(analysis.support) && price < analysis.support) || (direction === 'SHORT' && finite(analysis.resistance) && price > analysis.resistance) || (decision.mtfScore !== null && decision.mtfScore < 40))
  const expired = decision.freshness === 'STALE'
  const canArm = failedCount === 1 && unavailableCount === 0 && trendPassed && momentumPassed && mtfPassed && rrPassed && freshnessPassed
  const lifecycle: TriggerLifecycle = expired ? 'EXPIRED' : invalidated ? 'INVALIDATED' : allPassed ? 'CONFIRMED' : breakoutPassed ? 'TRIGGERED' : canArm ? 'ARMED' : 'WAITING'
  const distancePct = triggerPrice !== null && price > 0 ? Math.abs(triggerPrice - price) / price * 100 : null
  const waitingMessage = lifecycle === 'CONFIRMED' ? `${direction} TRIGGER REACHED` : triggerPrice === null ? 'WAITING — trigger price unavailable' : `${lifecycle === 'ARMED' ? 'ARMED' : 'WAITING'} — ${distancePct === null ? '--' : `${distancePct.toFixed(2)}%`} TO ${direction} TRIGGER`
  const preTradeChecks: PreTradeCheck[] = [
    { label: 'Market data', status: 'PASS', detail: 'Current snapshot available' },
    { label: 'Signal freshness', status: decision.freshness === null ? 'UNAVAILABLE' : freshnessPassed ? 'PASS' : 'FAIL', detail: decision.freshness || '--' },
    { label: 'MTF confirmation', status: decision.mtfScore === null ? 'UNAVAILABLE' : mtfPassed ? 'PASS' : 'FAIL', detail: decision.mtfScore === null ? '--' : `${decision.mtfConfirmed}/${decision.mtfTotal}` },
    { label: 'Liquidity', status: finite(analysis.volume_ratio) ? (volumePassed ? 'PASS' : 'FAIL') : 'UNAVAILABLE', detail: finite(analysis.volume_ratio) ? `${analysis.volume_ratio.toFixed(2)}x volume` : '--' },
    { label: 'Risk/Reward', status: decision.riskReward === null ? 'UNAVAILABLE' : rrPassed ? 'PASS' : 'FAIL', detail: decision.riskReward === null ? '--' : `1:${decision.riskReward.toFixed(2)}` },
    { label: 'Stop Loss', status: finite(analysis.stop_loss) ? 'PASS' : 'UNAVAILABLE', detail: finite(analysis.stop_loss) ? String(analysis.stop_loss) : '--' },
    { label: 'Position limit', status: 'UNAVAILABLE', detail: 'Existing account gate decides' },
    { label: 'Duplicate symbol', status: 'UNAVAILABLE', detail: 'Existing account gate decides' },
    { label: 'Daily loss', status: 'UNAVAILABLE', detail: 'Existing risk gate decides' },
    { label: 'Live trading', status: 'LOCKED', detail: 'Live execution is fail-closed' },
  ]
  return {
    lifecycle, available: true, direction, triggerPrice, currentPrice: price, distancePct,
    conditions, remainingConditions: unavailableCount + failedCount, invalidation, waitingMessage,
    statusMessage: lifecycle === 'CONFIRMED' ? 'SETUP CONFIRMED — NO ORDER SENT' : lifecycle,
    entryPreview: allPassed && finite(analysis.entry) && finite(analysis.stop_loss) && finite(analysis.tp1) && finite(analysis.tp2) && finite(analysis.tp3) && decision.riskReward !== null
      ? { entry: analysis.entry, stopLoss: analysis.stop_loss, tp1: analysis.tp1, tp2: analysis.tp2, tp3: analysis.tp3, riskReward: decision.riskReward } : null,
    preTradeChecks,
  }
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
