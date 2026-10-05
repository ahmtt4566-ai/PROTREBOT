import { Children, cloneElement, isValidElement, type ReactElement, type ReactNode, useState } from 'react'
import { Activity, Cable, Check, ChevronDown, ListChecks, LockKeyhole, Minus, Power, Send, Settings2, ShieldCheck, Wallet, X } from 'lucide-react'
import { PremiumWorkspace } from './premium-access'

type LayoutElement = ReactElement<{className?: string; children?: ReactNode; title?: string; 'aria-label'?: string; 'data-status'?: string}>
type Tab = 'analiz' | 'canli' | 'pozisyonlar' | 'baglanti'
const STEPS = ['Bağlan', 'Onayla', 'Arm', 'Çalıştır']

export function MasterTradeValue({children}: {children: ReactNode}) {
  return children === '--' || children === '—' ? <span className="masterTradeSkeleton"><span className="masterTradeScreenReader">{children}</span></span> : children
}

export function masterTradeTone(value?: string | number | null): 'positive' | 'negative' | 'neutral' {
  if (typeof value === 'number') return value > 0 ? 'positive' : value < 0 ? 'negative' : 'neutral'
  if (value && /bull|long|positive|pozitif|yükseliş/i.test(value)) return 'positive'
  if (value && /bear|short|negative|negatif|düşüş/i.test(value)) return 'negative'
  return 'neutral'
}

export function MasterTradeMetricTile({label, value, tone = 'neutral', status, children}: {label: string; value: ReactNode; tone?: 'positive' | 'negative' | 'neutral'; status?: ReactNode; children?: ReactNode}) {
  return <article className="masterTradeMetricTile" data-tone={tone}>
    <small>{label}</small><strong title={typeof value === 'string' ? value : undefined}><MasterTradeValue>{value}</MasterTradeValue></strong>
    <div className="masterTradeMetricVisual">{children}</div>
    <small className="masterTradeMetricStatus" title={typeof status === 'string' ? status : undefined}>{status ?? (tone === 'positive' ? <Check aria-hidden="true"/> : tone === 'negative' ? <X aria-hidden="true"/> : <Minus aria-hidden="true"/>)}</small>
  </article>
}

export function MasterTradeMetricVisual({kind, value, volumes = []}: {kind: 'rsi' | 'macd' | 'volume' | 'confidence'; value?: number | null; volumes?: number[]}) {
  if (kind === 'volume') {
    const maximum = Math.max(...volumes, 1)
    const points = volumes.map((volume, index) => `${index * 100 / Math.max(volumes.length - 1, 1)},${28 - volume / maximum * 24}`).join(' ')
    return <svg className="masterTradeMiniSparkline" viewBox="0 0 100 32" preserveAspectRatio="none" aria-hidden="true"><polyline points={points} fill="none" vectorEffect="non-scaling-stroke" /></svg>
  }
  const width = kind === 'macd' ? Math.abs(value ?? 0) / (Math.abs(value ?? 0) + 1) * 50 : Math.max(0, Math.min(100, value ?? 0))
  return <div className={`masterTradeMiniMeter meter-${kind}`} aria-hidden="true">
    {kind === 'macd' ? <><span className="masterTradeMeterZero"/><i className={(value ?? 0) < 0 ? 'negative' : 'positive'} style={{width: `${width}%`, left: (value ?? 0) < 0 ? `${50 - width}%` : '50%'}}/></> : <i style={{width: `${width}%`}}/>}
    {kind === 'rsi' && <div className="masterTradeMeterMarks"><span style={{left: '30%'}}>30</span><span style={{left: '50%'}}>50</span><span style={{left: '70%'}}>70</span></div>}
  </div>
}

export function MasterTradeChartLabels({lines, currentPrice, toY, format, pills = false}: {lines: Array<{label: string; value: number; tone: string}>; currentPrice?: number | null; toY: (value: number) => number; format: (value: number) => string; pills?: boolean}) {
  const labels = [...lines, ...(currentPrice === null || currentPrice === undefined ? [] : [{label: '', value: currentPrice, tone: 'current'}])].sort((left, right) => toY(left.value) - toY(right.value))
  const positions: number[] = []
  labels.forEach((line, index) => { positions.push(Math.max(14, toY(line.value), index ? positions[index - 1] + 18 : 14)) })
  for (let index = positions.length - 1; index >= 0; index -= 1) positions[index] = Math.min(positions[index], index === positions.length - 1 ? 278 : positions[index + 1] - 18)
  if (pills) return <div className="masterTradeChartPills">{labels.map((line, index) => <span key={`${line.label}-${line.value}`} className={`level-${line.tone}`} style={{top: `${(positions[index] - 12) / 300 * 100}%`}} title={`${line.label} ${format(line.value)}`}>{line.label}{line.label ? ' ' : ''}{format(line.value)}</span>)}</div>
  return <g className="masterTradeChartLabels">{labels.map((line, index) => <g key={`${line.label}-${line.value}`} className={`chartLevel level-${line.tone}`} data-label-y={positions[index]}>
    <line x1="0" x2="760" y1={toY(line.value)} y2={toY(line.value)} strokeDasharray={line.tone === 'current' ? undefined : '3 4'}/>
    <path d={`M 592 ${toY(line.value)} L 604 ${positions[index]}`} className="masterTradeLabelLeader"/>
  </g>)}</g>
}

function flattenCards(children: ReactNode, prefix = ''): ReactNode[] {
  return Children.toArray(children).flatMap((child, index) => {
    if (isValidElement(child) && (child as LayoutElement).props.className?.split(' ').includes('masterTradeLiveGrid')) {
      return flattenCards((child as LayoutElement).props.children, `${prefix}${index}.`)
    }
    return [isValidElement(child) ? cloneElement(child, {key: `${prefix}${child.key ?? index}`}) : child]
  })
}

function logMessage(row: ReactNode): string | null {
  if (!isValidElement(row)) return null
  const message = Children.toArray((row as LayoutElement).props.children).find(child => isValidElement(child) && child.type === 'span') as LayoutElement | undefined
  return message && typeof message.props.children === 'string' ? message.props.children : null
}

function collapseLogRows(children: ReactNode): ReactNode[] {
  const groups: Array<{row: ReactNode; message: string | null; count: number}> = []
  Children.toArray(children).forEach(row => {
    const message = logMessage(row)
    const previous = groups[groups.length - 1]
    if (message !== null && previous?.message === message) previous.count += 1
    else groups.push({row, message, count: 1})
  })
  return groups.map(({row, message, count}) => {
    if (!isValidElement(row)) return row
    const element = row as LayoutElement
    const content = Children.toArray(element.props.children).map(child => {
      if (!isValidElement(child) || child.type !== 'span') return child
      return cloneElement(child as ReactElement<{title?: string}>, {title: message ?? undefined})
    })
    return cloneElement(element, {children: <>{content}{count > 1 && <b className="masterTradeLogCount" title={`${count} tekrar`}>×{count}</b>}</>})
  })
}

function presentLogs(node: ReactNode, inActivity = false, livePresentation = false): ReactNode {
  if (!isValidElement(node)) return node
  const element = node as LayoutElement
  const className = element.props.className ?? ''
  const text = element.props.children
  const titled = typeof text === 'string' && typeof element.type === 'string' && ['p','small','strong','b','em','span'].includes(element.type)
    ? cloneElement(element, {
      title: element.props.title ?? text,
      ...(livePresentation && /^(READY|CONNECTED|PASS|CLEAR|DISCONNECTED|NOT CONNECTED|BLOCKED|LOCKED|KİLİTLİ|PENDING|REQUIRED|UNKNOWN|WAITING|LONG|SHORT)$/.test(text) ? {'data-status': text} : {}),
    })
    : element
  const activity = inActivity || className.split(' ').some(value => value === 'masterTradeLiveActivity' || value === 'masterTradeLiveActivityRail')
  if (activity && (element.type === 'ol' || className === 'masterTradeLiveActivityRailList')) {
    return cloneElement(element, {children: collapseLogRows(element.props.children)})
  }
  if (livePresentation && element.type === 'b' && (text === '—' || text === '--')) {
    return cloneElement(titled, {children: <MasterTradeValue>{text}</MasterTradeValue>})
  }
  if (element.props.children === undefined) return titled
  return cloneElement(titled, {children: Children.map(element.props.children, child => presentLogs(child, activity, livePresentation))})
}

function cardText(card: LayoutElement, tags: string[]): ReactNode {
  const findHeading = (node: ReactNode): ReactNode => {
    for (const child of Children.toArray(node)) {
      if (!isValidElement(child)) continue
      const element = child as LayoutElement
      if (typeof element.type === 'string' && tags.includes(element.type)) return element.props.children
      const nested = findHeading(element.props.children)
      if (nested) return nested
    }
    return null
  }
  return findHeading(card.props.children)
}

function cardTitle(card: LayoutElement): ReactNode {
  return cardText(card, ['h3']) ?? card.props['aria-label']
}

function cardIcon(className: string) {
  if (className.includes('Connection')) return Cable
  if (className.includes('Confirmation') || className.includes('Safety')) return ShieldCheck
  if (className.includes('Control')) return Power
  if (className.includes('OrderGrid')) return Send
  if (className.includes('Account')) return Wallet
  if (className.includes('Setup')) return Settings2
  if (className.includes('Operations') || className.includes('Signal') || className.includes('Activity')) return Activity
  return ListChecks
}

function groupLiveInsights(slots: ReactNode[], cards: ReactNode[], tab: Tab): ReactNode[] {
  if (tab !== 'canli') return slots
  const findCard = (className: string) => cards.findIndex(card => isValidElement(card) && (card as LayoutElement).props.className?.split(' ').includes(className))
  const scanner = findCard('masterTradeLiveOperations')
  const signal = findCard('masterTradeLiveSignal')
  if (scanner < 0 || signal < 0) return slots
  return slots.flatMap<ReactNode>((slot, index) => index === scanner
    ? [<div className="masterTradeLiveInsightsStack" key="live-insights">{slot}{slots[signal]}</div>]
    : index === signal ? [] : [slot])
}

export function MasterTradeLiveLayout({tab = 'canli', step, children, notice}: {tab?: Tab; step: number; children: LayoutElement; notice?:ReactNode}) {
  const [selectedStep, setSelectedStep] = useState<number | null>(null)
  const activeStep = selectedStep !== null && selectedStep <= step ? selectedStep : step
  const cards = flattenCards(children.props.children)
  const progression = cards.find(card => isValidElement(card) && (card as LayoutElement).props.className === 'masterTradeLiveFlow') as LayoutElement | undefined
  const progressionStates = Children.toArray(progression?.props.children).filter(child => isValidElement(child) && child.type === 'span')
  const content = <>
    <nav className="masterTradeStepper" aria-label={progression?.props['aria-label']} hidden={tab !== 'canli'}>{STEPS.map((label, index) => <button key={label} type="button" disabled={index > step} data-complete={index < step || undefined} aria-current={activeStep === index ? 'step' : undefined} onClick={() => setSelectedStep(index)}><span className="masterTradeStepIndex">{index > step ? <LockKeyhole/> : index < step ? <Check/> : index + 1}</span><span><b>{label}</b><small>{progressionStates[index]}</small></span></button>)}</nav>
    {notice}
    {groupLiveInsights(cards.map(node => {
      if (!isValidElement(node)) return node
      const card = node as LayoutElement
      const className = card.props.className ?? ''
      if (className === 'masterTradeLiveFlow') return null
      const connection = className.includes('masterTradeLiveConnectionCard')
      const confirmations = className.includes('masterTradeLiveConfirmations')
      const control = className.split(' ').includes('masterTradeLiveControl')
      const activity = className.includes('masterTradeLiveActivity')
      const safety = className.split(' ').includes('masterTradeLiveSafety')
      const positions = className.includes('masterTradeLiveAccountTable')
      if (positions && tab === 'pozisyonlar') return null
      const sectionTab = activity || safety ? 'baglanti' : positions ? 'pozisyonlar' : 'canli'
      const visible = tab === sectionTab || connection && tab === 'baglanti'
      const workflowStep = connection ? 0 : confirmations ? 1 : control ? (activeStep === 3 ? 3 : 2) : null
      const expanded = workflowStep === activeStep
      const locked = workflowStep !== null && workflowStep > step
      const presented = presentLogs(card, false, tab === 'canli')
      const Icon = cardIcon(className)
      const title = cardTitle(card)
      const summaryText = cardText(card, ['p', 'small'])
      const summary = <summary>{tab === 'canli' && <Icon aria-hidden="true"/>}<span className="masterTradeCardTitle" title={typeof title === 'string' ? title : undefined}>{title}</span>{tab === 'canli' && <small className="masterTradeCardSummary" title={typeof summaryText === 'string' ? summaryText : undefined}>{summaryText}</small>}{locked ? <LockKeyhole aria-hidden="true"/> : <ChevronDown aria-hidden="true"/>}</summary>
      const secondary = positions || className.includes('masterTradeLiveActivityRail') || tab === 'canli' && workflowStep === null && !className.includes('masterTradeLiveHeader') && !className.includes('masterTradeLiveAssistant')
      return <div className="masterTradeLiveSlot" key={card.key} hidden={!visible} data-flow-card={workflowStep ?? undefined} data-flow-expanded={workflowStep === null ? undefined : expanded}>
        {workflowStep !== null && tab === 'canli' && !expanded ? <details className="masterTradeFlowCard" inert={locked} aria-disabled={locked || undefined}>{summary}{presented}</details> : secondary ? <details className="masterTradeFlowCard">{summary}{presented}</details> : presented}
      </div>
    }), cards, tab)}
  </>
  return <PremiumWorkspace>{cloneElement(children, {children: content})}</PremiumWorkspace>
}