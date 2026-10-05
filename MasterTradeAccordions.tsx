import {useId, useRef, useState, type KeyboardEvent, type ReactNode} from 'react'
import {Activity, AlertTriangle, BarChart3, Check, ChevronDown, Crosshair, History, ListChecks, LockKeyhole, Minus, ShieldCheck, X, type LucideIcon} from 'lucide-react'
import type {DecisionAnalysis, PreTradeCheck, TradeDecision, TriggerMonitor} from './masterTradeDecision'
import {analysisPrice, triggerPresentation} from './master-trade-presentation'
import './master-trade-accordions.css'

type Tone = 'positive' | 'negative' | 'warning' | 'neutral'
type Props = {
  decision: TradeDecision; trigger: TriggerMonitor; analysis: DecisionAnalysis | null;
  timeline: ReadonlyArray<{time: string; message: string}>;
}
const scoreLabels: Record<string, string> = {analysis: 'Analysis', liquidity: 'Liquidity', volatility: 'Volatility', mtf: 'MTF', freshness: 'Freshness', riskReward: 'Risk/Reward'}
const scoreTone = (value: number): Tone => value >= 75 ? 'positive' : value >= 40 ? 'warning' : 'negative'
const scoreWidth = (value: number) => Math.max(0, Math.min(100, value))
const valueText = (value: number) => value.toLocaleString('en-US', {maximumFractionDigits: 2})
const checkTone = (status: PreTradeCheck['status']): Tone => status === 'PASS' ? 'positive' : status === 'FAIL' ? 'negative' : status === 'LOCKED' ? 'warning' : 'neutral'
const positiveCases = new Set(['Trend aligned', 'MACD bullish', 'MACD bearish', 'Volume confirmed', 'MTF supportive'])
const negativeCases = new Set(['Trend not aligned', 'MACD not bullish', 'MACD not bearish', 'Volume not confirmed', 'MTF not confirmed'])

function EmptyContent() {
  return <div className="refAdvisoryEmpty" role="img" aria-label="Veri yok"><span/><span/><span/></div>
}

function DetailText({text}: {text: string}) {
  return text === '—' || text === '--' || !text.trim()
    ? <span className="refInlinePlaceholder" aria-label="Değer yok"/>
    : <small className="refCheckDetail" title={text}>{text}</small>
}

function StatusChip({status}: {status: PreTradeCheck['status']}) {
  return <span className="refStatusChip" data-tone={checkTone(status)}>{status === 'LOCKED' && <LockKeyhole aria-hidden="true"/>}{status}</span>
}

function Accordion({group, title, Icon, summary, chip, tone = 'neutral', children}: {
  group: string; title: string; Icon: LucideIcon; summary?: string; chip?: string; tone?: Tone; children: ReactNode;
}) {
  const id = useId()
  const [expanded, setExpanded] = useState(false)
  return <details className="refAdvisoryAccordion" name={group} data-tone={tone} onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary aria-expanded={expanded} aria-controls={id}>
      <span className="refAccordionIcon"><Icon aria-hidden="true"/></span>
      <span className="refAccordionCopy"><span>{title}</span>{summary && <small title={summary}>{summary}</small>}</span>
      {chip && <span className="refStatusChip" data-tone={tone}>{chip}</span>}
      <ChevronDown className="refAccordionChevron" aria-hidden="true"/>
    </summary>
    <div id={id} className="refAdvisoryBody">{children}</div>
  </details>
}

function CaseTabs({decision}: {decision: TradeDecision}) {
  const id = useId()
  const [side, setSide] = useState<'LONG' | 'SHORT'>(decision.direction === 'SHORT' ? 'SHORT' : 'LONG')
  const longButton = useRef<HTMLButtonElement>(null)
  const shortButton = useRef<HTMLButtonElement>(null)
  const move = (event: KeyboardEvent<HTMLButtonElement>) => {
    const next = event.key === 'Home' ? 'LONG' : event.key === 'End' ? 'SHORT' : ['ArrowLeft', 'ArrowRight'].includes(event.key) ? side === 'LONG' ? 'SHORT' : 'LONG' : null
    if (!next) return
    event.preventDefault()
    setSide(next)
    ;(next === 'LONG' ? longButton : shortButton).current?.focus()
  }
  return <>
    <div className="refCaseTabs" role="tablist" aria-label="Long / Short case">
      {(['LONG', 'SHORT'] as const).map(direction => <button type="button" key={direction} ref={direction === 'LONG' ? longButton : shortButton} id={`${id}-${direction}`} role="tab" aria-selected={side === direction} aria-controls={`${id}-${direction}-panel`} tabIndex={side === direction ? 0 : -1} data-tone={direction === 'LONG' ? 'positive' : 'negative'} onClick={() => setSide(direction)} onKeyDown={move}>{direction}</button>)}
    </div>
    {(['LONG', 'SHORT'] as const).map(direction => <div key={direction} className="refCasePanel" role="tabpanel" id={`${id}-${direction}-panel`} aria-labelledby={`${id}-${direction}`} hidden={side !== direction}>
      {(direction === 'LONG' ? decision.longCase : decision.shortCase).length ? <div className="refCaseItems">{(direction === 'LONG' ? decision.longCase : decision.shortCase).map((text, index) => {
        const tone = positiveCases.has(text) ? 'positive' : negativeCases.has(text) ? 'negative' : 'neutral'
        const Icon = tone === 'positive' ? Check : tone === 'negative' ? X : Minus
        return <div className="refCaseItem" key={`${index}-${text}`} data-tone={tone}><span className="refConditionIcon"><Icon aria-hidden="true"/></span><span>{text}</span></div>
      })}</div> : <EmptyContent/>}
    </div>)}
    {decision.whyWait.length > 0 && <section className="refCaseRisks"><h3>Riskler</h3><div>{decision.whyWait.map((text, index) => <span key={`${index}-${text}`}><AlertTriangle aria-hidden="true"/>{text}</span>)}</div></section>}
  </>
}

export function MasterTradeAccordions({decision, trigger, analysis, timeline}: Props) {
  const group = useId()
  const passed = trigger.conditions.filter(condition => condition.available && condition.passed).length
  const total = trigger.conditions.length
  const checks = trigger.preTradeChecks
  const counts = {PASS: 0, FAIL: 0, UNAVAILABLE: 0, LOCKED: 0}
  for (const check of checks) counts[check.status]++
  const checkSummary = checks.length ? `${counts.PASS} PASS · ${counts.FAIL} FAIL · ${counts.UNAVAILABLE} N/A${counts.LOCKED ? ` · ${counts.LOCKED} LOCKED` : ''}` : undefined
  const checkStatus = counts.LOCKED ? 'LOCKED' : counts.FAIL ? 'FAIL' : counts.UNAVAILABLE ? 'UNAVAILABLE' : checks.length ? 'PASS' : undefined
  const events = [...timeline].sort((left, right) => Date.parse(right.time) - Date.parse(left.time))
  const status = triggerPresentation(trigger.lifecycle, trigger.available)
  return <div className="refAccordions">
    <Accordion group={group} title="Trigger conditions" Icon={Crosshair} summary={total ? `${passed}/${total} koşul` : undefined} chip={total ? `${passed}/${total}` : undefined} tone={total ? passed === total ? 'positive' : 'warning' : 'neutral'}>
      {total ? <>
        <div className="refConditionProgress" role="meter" aria-label="Sağlanan koşullar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={passed}>{trigger.conditions.map(condition => <i key={condition.key} data-tone={!condition.available ? 'neutral' : condition.passed ? 'positive' : 'negative'}/>)}</div>
        <div className="refConditionList">{trigger.conditions.map(condition => {
          const tone = !condition.available ? 'neutral' : condition.passed ? 'positive' : 'negative'
          const Icon = !condition.available ? Minus : condition.passed ? Check : X
          const detail = condition.key === 'breakout' ? analysisPrice(trigger.triggerPrice) : condition.detail
          return <div key={condition.key} className="refConditionRow" data-tone={tone}><span className="refConditionIcon"><Icon aria-hidden="true"/></span><span className="refConditionName">{condition.label}</span><span className="refConditionValue" title={detail}>{condition.available ? detail : <span className="refInlinePlaceholder" aria-label="Değer yok"/>}</span></div>
        })}</div>
      </> : <EmptyContent/>}
    </Accordion>
    <Accordion group={group} title="Invalidation" Icon={ShieldCheck} summary={trigger.invalidation.length ? `${trigger.invalidation.length} iptal koşulu` : undefined} chip={trigger.invalidation.length ? String(trigger.invalidation.length) : undefined} tone="warning">
      {trigger.invalidation.length ? <div className="refInvalidationGrid">{trigger.invalidation.map((text, index) => <div key={`${index}-${text}`}><AlertTriangle aria-hidden="true"/><span>{text}</span></div>)}</div> : <EmptyContent/>}
    </Accordion>
    <Accordion group={group} title="Decision timeline" Icon={History} summary={events[0]?.message} chip={events.length ? String(events.length) : undefined}>
      {events.length ? <ol className="refDecisionTimeline">{events.map((event, index) => <li key={`${index}-${event.time}-${event.message}`} data-latest={index === 0}><time dateTime={event.time}>{new Date(event.time).toLocaleTimeString('en-GB')}</time><span>{event.message}</span></li>)}</ol> : <EmptyContent/>}
    </Accordion>
    <Accordion group={group} title="Pre-trade check" Icon={ListChecks} summary={checkSummary} chip={checkStatus} tone={checkStatus ? checkTone(checkStatus) : 'neutral'}>
      {checks.length ? <>
        <div className="refCheckCounts"><span data-tone="positive">{counts.PASS} PASS</span><span data-tone="negative">{counts.FAIL} FAIL</span><span data-tone="neutral">{counts.UNAVAILABLE} N/A</span>{counts.LOCKED > 0 && <span data-tone="warning">{counts.LOCKED} LOCKED</span>}</div>
        <div className="refCheckList">{checks.map(check => <div className="refCheckRow" key={check.label}><div><strong>{check.label}</strong><DetailText text={check.label === 'Stop Loss' ? analysisPrice(analysis?.stop_loss) : check.detail}/></div><StatusChip status={check.status}/></div>)}</div>
      </> : <EmptyContent/>}
      <span className="refNoOrder"><ShieldCheck aria-hidden="true"/>NO ORDER SENT</span>
    </Accordion>
    <Accordion group={group} title="Why this score" Icon={BarChart3} summary={decision.opportunityScore !== null ? `${valueText(decision.opportunityScore)} / 100` : undefined} chip={decision.opportunityScore !== null ? valueText(decision.opportunityScore) : undefined} tone={decision.opportunityScore !== null ? scoreTone(decision.opportunityScore) : 'neutral'}>
      {decision.opportunityScore !== null && <div className="refScoreTotal" data-tone={scoreTone(decision.opportunityScore)}><svg viewBox="0 0 48 48" aria-hidden="true"><circle className="refScoreTrack" cx="24" cy="24" r="20"/><circle className="refScoreRing" cx="24" cy="24" r="20" pathLength="100" strokeDasharray="100" strokeDashoffset={100 - scoreWidth(decision.opportunityScore)}/></svg><div><strong>{valueText(decision.opportunityScore)}</strong><span>/100</span></div></div>}
      {decision.breakdown ? <div className="refScoreGrid">{Object.entries(decision.breakdown).map(([key, value]) => <div key={key} data-tone={scoreTone(value)}><small>{scoreLabels[key] ?? key.replace(/([a-z])([A-Z])/g, '$1 $2')}</small><strong>{valueText(value)}</strong><div className="refScoreBar" role="meter" aria-label={scoreLabels[key] ?? key} aria-valuemin={0} aria-valuemax={100} aria-valuenow={scoreWidth(value)}><i style={{width: `${scoreWidth(value)}%`}}/></div></div>)}</div> : <EmptyContent/>}
    </Accordion>
    <Accordion group={group} title="Long / Short case" Icon={Activity} summary={decision.longCase.length || decision.shortCase.length ? `${decision.direction} bias · ${status.label}` : undefined} chip={decision.longCase.length || decision.shortCase.length ? decision.direction : undefined} tone={decision.direction === 'LONG' ? 'positive' : decision.direction === 'SHORT' ? 'negative' : 'neutral'}>
      <CaseTabs key={decision.direction} decision={decision}/>
    </Accordion>
  </div>
}
