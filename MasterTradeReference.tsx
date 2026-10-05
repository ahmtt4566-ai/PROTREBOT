import {useLayoutEffect, useRef, useState, type RefCallback} from 'react'
import {Activity, BarChart3, Cable, Camera, Check, ChevronDown, Crosshair, LockKeyhole, Power, RefreshCw, Settings2, SlidersHorizontal, UserRound, Wallet, X} from 'lucide-react'
import {type DecisionAnalysis, type TradeDecision, type TriggerMonitor} from './masterTradeDecision'
import {MasterTradeMetricTile, MasterTradeMetricVisual} from './MasterTradeLayout'
import {PremiumBoundary, useMemberAccess} from './premium-access'
import {analysisChartLabels, analysisChartRange, analysisPrice, analysisValue, autoTradePresentation, triggerPresentation, MASTER_MTF_INTERVALS} from './master-trade-presentation'
import {liveBlockerMessage, type SharedLiveStatus} from './frontend/src/LiveTradingPanel'
import {CoinIcon} from './src/components/CoinIcon'
import {MasterTradeAccordions} from './MasterTradeAccordions'
import {MasterTradeMarketWatch} from './MasterTradeMarketWatch'
import type {MarketScore} from './master-market-data'
import type {MasterMarketFeed} from './useMasterMarketQuotes'
import {TRADING_TIMEFRAMES} from './account-settings-api'
import './master-trade-reference.css'

type Candle = {time: number; open: number; high: number; low: number; close: number; volume: number}
type Props = {
  assistantSlotRef?: RefCallback<HTMLDivElement>;
  symbol: string; interval: string; query: string; onQuery: (value: string) => void;
  scores: readonly MarketScore[];
  marketFeed: MasterMarketFeed;
  onMarket: (symbol: string) => void; onInterval: (value: string) => void;
  onNavigate: (tab: 'analiz' | 'canli' | 'pozisyonlar' | 'baglanti') => void;
  onAutoTrade: () => void; onRefresh: () => void; refreshing: boolean;
  candles: Candle[]; analysis: DecisionAnalysis | null; decision: TradeDecision; trigger: TriggerMonitor;
  live: SharedLiveStatus | null; price: number | null; change?: number | null; volume?: number | null;
  levelsVisible: boolean; volumeVisible: boolean; onLevels: () => void; onVolume: () => void;
  timeline: Array<{time: string; message: string}>; error: string; dataIssue: string;
}

const numeric = (value: number | null | undefined, suffix = '', decimals = 0) => typeof value === 'number' && Number.isFinite(value)
  ? `${value.toLocaleString('en-US', {minimumFractionDigits: decimals, maximumFractionDigits: 2})}${suffix}` : '—'
const directionTone = (direction: string) => direction === 'LONG' ? 'positive' : direction === 'SHORT' ? 'negative' : 'neutral'
const candleTime = (time: number) => new Date(time < 1e12 ? time * 1000 : time).toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'})

function ReferenceBars({candles, neutral = false}: {candles: Candle[]; neutral?: boolean}) {
  if (!candles.length) return <span>—</span>
  const changes = candles.slice(-12).map(candle => Math.abs(candle.close - candle.open))
  const maximum = Math.max(...changes, Number.EPSILON)
  return <svg className="refMetricBars" data-neutral={neutral} viewBox="0 0 100 28" preserveAspectRatio="none" aria-hidden="true">{changes.map((change, index) => {
    const height = change / maximum * 25
    return <rect key={index} x={index * 8.3} y={28 - height} width="5.8" height={height}/>
  })}</svg>
}

function ReferenceChart({candles, symbol, interval, analysis, trigger, price, levelsVisible, volumeVisible, dataIssue, refreshing, premium}: Pick<Props, 'candles' | 'symbol' | 'interval' | 'analysis' | 'trigger' | 'price' | 'levelsVisible' | 'volumeVisible' | 'dataIssue' | 'refreshing'> & {premium: boolean}) {
  const [hover, setHover] = useState<number | null>(null)
  const canvas = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({width: 900, height: 360})
  useLayoutEffect(() => {
    const element = canvas.current
    if (!element) return
    const measure = () => {
      const {width, height} = element.getBoundingClientRect()
      if (width > 0 && height > 0) setSize(previous => previous.width === width && previous.height === height ? previous : {width, height})
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const plotWidth = Math.max(30, size.width - 176)
  const plotHeight = Math.max(1, size.height - 26)
  const priceHeight = plotHeight * .79
  const visible = candles.slice(-Math.min(80, Math.max(1, Math.floor((plotWidth - 20) / 7))))
  const levels = [
    {label: 'TP3', value: analysis?.tp3, tone: 'tp'}, {label: 'TP2', value: analysis?.tp2, tone: 'tp'},
    {label: 'TP1', value: analysis?.tp1, tone: 'tp'}, {label: 'RESISTANCE', value: analysis?.resistance, tone: 'resistance'},
    {label: 'TRIGGER', value: trigger.triggerPrice, tone: 'trigger'}, {label: 'ENTRY', value: analysis?.entry, tone: 'entry'},
    {label: 'SUPPORT', value: analysis?.support, tone: 'support'}, {label: 'SL', value: analysis?.stop_loss, tone: 'stop'},
  ].filter((line): line is {label: string; value: number; tone: string} => typeof line.value === 'number' && Number.isFinite(line.value))
  const range = analysisChartRange(visible)
  const labels = range ? analysisChartLabels([
    ...(premium && levelsVisible ? levels : []),
    ...(typeof price === 'number' && Number.isFinite(price) ? [{label: '', value: price, tone: 'current'}] : []),
  ], range, size.height, priceHeight) : []
  const y = (value: number) => range ? (range.high - value) / (range.high - range.low) * priceHeight : 0
  const x = (index: number) => 10 + (index + .5) / visible.length * (plotWidth - 20)
  const bodyWidth = Math.max(5, Math.min(9, (plotWidth - 20) / visible.length * .7))
  const maximumVolume = Math.max(...visible.map(candle => candle.volume), 1)
  const selected = hover === null ? visible[visible.length - 1] : visible[Math.min(hover, visible.length - 1)]
  return <>
    <div className="refOhlc"><b>{symbol} · {interval}</b>{selected ? <><span>O {analysisPrice(selected.open)}</span><span>H {analysisPrice(selected.high)}</span><span>L {analysisPrice(selected.low)}</span><span>C {analysisPrice(selected.close)}</span></> : <span>—</span>}</div>
    <div className="refChartCanvas" ref={canvas}>
      {!range || dataIssue ? <div className="refChartEmpty" role="status"><div><BarChart3 aria-hidden="true"/><strong>{refreshing ? 'Veriler yükleniyor...' : 'Analiz yok'}</strong><p>{dataIssue || (refreshing ? `${symbol} mumları ve analizi alınıyor.` : 'Bu sembol için yeterli veri yok')}</p></div></div> : <>
        <svg viewBox={`0 0 ${size.width} ${size.height}`} preserveAspectRatio="none" data-axis-low={range.low} data-axis-high={range.high} data-price-height={priceHeight} data-plot-height={plotHeight} aria-label={`${symbol} ${interval} candlestick chart`} onMouseLeave={() => setHover(null)} onMouseMove={event => {
          const box = event.currentTarget.getBoundingClientRect()
          const index = Math.floor((event.clientX - box.left - 10) / (plotWidth - 20) * visible.length)
          setHover(Math.max(0, Math.min(visible.length - 1, index)))
        }}>
          <g className="refChartGrid">{Array.from({length: 7}, (_, index) => {
            const tickY = 11 + index / 6 * (priceHeight - 22)
            return <g key={index}><line x1="0" x2={plotWidth} y1={tickY} y2={tickY}/>{!labels.some(label => Math.abs(label.y - tickY) <= 21) && <text className="refPriceTick" data-price-y={tickY} x={plotWidth + 10} y={tickY} dominantBaseline="middle">{analysisPrice(range.high - tickY / priceHeight * (range.high - range.low))}</text>}</g>
          })}{Array.from({length: 6}, (_, index) => <line key={`v${index}`} x1={index * plotWidth / 5} x2={index * plotWidth / 5} y1="0" y2={plotHeight}/>)}</g>
          {visible.map((candle, index) => <g key={candle.time} className={`refCandle ${candle.close >= candle.open ? 'up' : 'down'}`}><line x1={x(index)} x2={x(index)} y1={y(candle.high)} y2={y(candle.low)} vectorEffect="non-scaling-stroke"/><rect x={x(index) - bodyWidth / 2} y={y(Math.max(candle.open, candle.close))} width={bodyWidth} height={Math.max(1, Math.abs(y(candle.open) - y(candle.close)))}/>{volumeVisible && <rect className="refVolume" x={x(index) - bodyWidth / 2} y={plotHeight - candle.volume / maximumVolume * plotHeight * .15} width={bodyWidth} height={candle.volume / maximumVolume * plotHeight * .15}/>}</g>)}
          <g className="masterTradeChartLabels">{labels.filter(label => label.edge === 'inside').map(label => <g key={`${label.label}-${label.value}`} data-level={label.label} className={`chartLevel level-${label.tone}`}><line x1="0" x2={plotWidth} y1={label.priceY} y2={label.priceY} strokeDasharray={label.tone === 'current' ? undefined : '3 4'}/>{Math.abs(label.y - label.priceY) > 1 && <path d={`M ${plotWidth + 2} ${label.priceY} L ${plotWidth + 8} ${label.y}`} className="masterTradeLabelLeader"/>}</g>)}</g>
          <g className="refTimeAxis">{Array.from({length: 6}, (_, index) => {const candle = visible[Math.round(index / 5 * (visible.length - 1))]; return <text key={index} x={10 + index / 5 * (plotWidth - 20)} y={size.height - 6} textAnchor={index === 0 ? 'start' : index === 5 ? 'end' : 'middle'}>{candleTime(candle.time)}</text>})}</g>
          {hover !== null && <line className="refCrosshair" x1={x(Math.min(hover, visible.length - 1))} x2={x(Math.min(hover, visible.length - 1))} y1="0" y2={plotHeight}/>}
        </svg>
        <div className="masterTradeChartPills">{labels.map(label => <span key={`${label.label}-${label.value}`} className={`level-${label.tone}`} data-edge={label.edge} data-price-y={label.priceY} data-label-y={label.y} style={{top: label.y - 11}} title={`${label.label} ${analysisPrice(label.value)}`}>{label.edge === 'above' ? '↑ ' : label.edge === 'below' ? '↓ ' : ''}{label.label}{label.label ? ' ' : ''}{analysisPrice(label.value)}</span>)}</div>
      </>}
    </div>
  </>
}

export default function MasterTradeReference(props: Props) {
  const {premium, ready, userId, openUpgrade} = useMemberAccess()
  const [notice, setNotice] = useState('')
  const {decision, trigger, analysis, live} = props
  const available = Boolean(analysis && props.candles.length >= 2)
  const locked = live?.real_trading_locked !== false
  const recovery = Boolean(live?.emergency?.active || live?.reconciliation_required || live?.recovery_error || live?.execution_state === 'UNKNOWN')
  const gatesReady = Boolean(premium && live?.connected && !locked && live.armed && live.authorization?.valid === true && live.consent?.active === true && live.policy_acknowledged === true && live.readiness?.ready === true && live.recovery_ready === true && !recovery && live.readiness.gates?.length && live.readiness.gates.every(gate => gate.passed === true))
  const enabled = live?.live_auto_trade === true && gatesReady
  const blocker = !premium ? 'Auto Trade için Premium üyelik gerekli.' : liveBlockerMessage(live, Boolean(live?.armed && !locked))
  const autoText = !live ? '—' : live.live_auto_trade && !enabled ? locked ? 'SCANNING ONLY · LOCKED' : 'ON · GATES BLOCKED' : autoTradePresentation('LIVE', live.live_auto_trade).label
  const autoStatus = live && !gatesReady && !live.live_auto_trade ? `${autoText} · Kilitli` : autoText
  const triggerStatus = triggerPresentation(trigger.lifecycle, trigger.available)
  const explainLock = () => {if (!premium) openUpgrade('Auto Trade'); else setNotice(blocker === 'No LIVE blocker.' ? 'Mevcut Canlı İşlem güvenlik ve onay akışını tamamlayın.' : blocker)}
  const lockedOrder = () => {if (!premium) openUpgrade('Manuel emir'); else setNotice('Analiz emir göndermez. Emir hazırlığı için mevcut Canlı İşlem akışını kullanın.')}
  const reasons = available ? [...new Set([...decision.reasons, ...(decision.direction === 'LONG' ? decision.longCase : decision.direction === 'SHORT' ? decision.shortCase : [])])].slice(0, 4) : []
  const action = !available ? 'Analiz yok' : ['WAIT', 'WATCH', 'NO TRADE'].includes(decision.status) ? decision.status === 'NO TRADE' ? 'BEKLE / NO TRADE' : 'BEKLE' : decision.direction === 'LONG' ? 'AL' : decision.direction === 'SHORT' ? 'SAT' : 'BEKLE'
  const tabs = [{id: 'analiz', label: 'Analiz', Icon: BarChart3}, {id: 'canli', label: 'Canlı İşlem', Icon: Activity}, {id: 'pozisyonlar', label: 'Pozisyonlar', Icon: Wallet}, {id: 'baglanti', label: 'Bağlantı', Icon: Cable}] as const
  return <section className="masterReferenceAnalysis" data-layout-tab="analiz">
    <header className="refHeader masterTradeTerminalHeader">
      <a className="refBrand" href="/" aria-label="Kaistrade ana sayfa"><Activity/><span>kais<b>trade</b></span></a>
      <nav role="tablist" aria-label="Master Trade sections">{tabs.map(({id, label, Icon}) => <button key={id} id={`masterTradeTab-${id}`} type="button" role="tab" aria-selected={id === 'analiz'} aria-controls={`masterTradePanel-${id}`} onClick={() => props.onNavigate(id)}><Icon/>{label}</button>)}</nav>
      <span className="refConnection" data-connected={live?.connected === true}><i/>{live?.connected ? 'CONNECTED' : 'DISCONNECTED'}{locked && <><LockKeyhole/> LOCKED</>}</span>
      <UserRound className="refUser" aria-label="Kullanıcı"/>{props.assistantSlotRef && <div className="assistantMasterSlot" ref={props.assistantSlotRef}/>}
    </header>
    <main className="refWorkspace masterAnalysis" role="tabpanel" aria-labelledby="masterTradeTab-analiz" id="masterTradePanel-analiz">
      <MasterTradeMarketWatch symbol={props.symbol} query={props.query} onQuery={props.onQuery} onMarket={props.onMarket} scores={props.scores} feed={props.marketFeed}/>
      <section className="refMetrics" aria-label="Primary trade decision">
        <article className="refCard"><small>MARKET PRICE</small><strong>{analysisPrice(props.price)} <em data-tone={typeof props.change === 'number' && props.change < 0 ? 'negative' : 'positive'}>{typeof props.change === 'number' ? `${props.change >= 0 ? '+' : ''}${numeric(props.change, '%')}` : '—'}</em></strong><span>{props.symbol} · <span aria-label="24 saat hacim" title={`${numeric(props.volume, ' USDT', 2)} · 24h`}>{typeof props.volume === 'number' ? `${props.volume.toLocaleString('en-US', {notation: 'compact', maximumFractionDigits: 2})} USDT` : '—'}</span></span></article>
        <article className="refCard"><small>SİNYAL / DURUM</small><strong className="refSignal" data-tone={directionTone(decision.direction)}>{available ? decision.status : 'Analiz yok'}</strong><span>{analysis?.trend ?? '—'}</span></article>
        <article className="refCard"><small>MTF / YÖN</small><strong><b className="refPill" data-tone={directionTone(decision.direction)}>{available ? decision.direction : '—'}</b></strong><span>{decision.mtfScore === null ? '—' : `${decision.mtfConfirmed} / ${decision.mtfTotal} teyit`}</span></article>
        <article className="refCard"><small>GÜVEN</small><strong><b className="refPill">{numeric(decision.confidenceScore, '%')}</b></strong><span>{decision.signalStrength ?? '—'}</span></article>
        <article className="refCard"><small>RİSK / ÖDÜL</small><strong>{decision.riskReward === null ? '—' : `1 : ${numeric(decision.riskReward, '', 2)}`}</strong><span>{decision.entryQuality ?? '—'}</span></article>
      </section>
      <section className="refCenter">
        <section className="refCard refChart">
          <div className="refToolbar" aria-label="Market chart controls"><span title="Mum grafiği"><BarChart3/><ChevronDown/></span><select aria-label="Zaman dilimi" value={props.interval} onChange={event => props.onInterval(event.target.value)}>{TRADING_TIMEFRAMES.map(range => <option key={range}>{range}</option>)}</select>
            <details className="refIndicators"><summary><SlidersHorizontal/> Göstergeler <ChevronDown/></summary><div><button type="button" aria-pressed={props.levelsVisible} onClick={props.onLevels}>LEVELS</button><button type="button" aria-pressed={props.volumeVisible} onClick={props.onVolume}>VOLUME</button></div></details>
            <div className="refDrawingTools"><button disabled aria-label="Çizim araçları mevcut değil"><Crosshair/></button><button disabled aria-label="Grafik ayarları mevcut değil"><Settings2/></button><button disabled aria-label="Grafik ekran görüntüsü mevcut değil"><Camera/></button></div>
            <div className="refOrderTools">{['LİMİT', 'PİYASA'].map(label => <span className="refLockedAction" key={label} onClick={lockedOrder}><button type="button" disabled><LockKeyhole/>{label}</button><button type="button" className="refLockInfo" aria-label={`${label} neden kilitli?`} onClick={event => {event.stopPropagation(); lockedOrder()}}><LockKeyhole/></button></span>)}<button type="button" disabled={props.refreshing} onClick={props.onRefresh}><RefreshCw/>YENİLE</button></div>
          </div>
          {props.error && <p className="refError" role="alert">{props.error}</p>}
          <ReferenceChart {...props} premium={premium}/>
        </section>
        <div className="refIndicatorGrid" aria-label="Market indicators">
          <MasterTradeMetricTile label="TREND" value={analysis?.trend ?? '—'} tone={directionTone(decision.direction)}><ReferenceBars candles={props.candles}/></MasterTradeMetricTile>
          <MasterTradeMetricTile label="MOMENTUM" value={analysis?.momentum ?? '—'} tone={analysis?.momentum === 'POSITIVE' ? 'positive' : analysis?.momentum === 'NEGATIVE' ? 'negative' : 'neutral'}><ReferenceBars candles={props.candles} neutral/></MasterTradeMetricTile>
          <MasterTradeMetricTile label="RSI" value={numeric(analysis?.rsi, '', 2)}><MasterTradeMetricVisual kind="rsi" value={analysis?.rsi}/></MasterTradeMetricTile>
          <MasterTradeMetricTile label="MACD" value={analysisValue(analysis?.macd)} tone={typeof analysis?.macd === 'number' ? analysis.macd >= 0 ? 'positive' : 'negative' : 'neutral'}><MasterTradeMetricVisual kind="macd" value={analysis?.macd}/></MasterTradeMetricTile>
          <MasterTradeMetricTile label="VOLUME" value={numeric(analysis?.volume_ratio, 'x')}><MasterTradeMetricVisual kind="volume" volumes={props.candles.slice(-14).map(candle => candle.volume)}/></MasterTradeMetricTile>
          <MasterTradeMetricTile label="CONFIDENCE" value={numeric(decision.confidenceScore, '%')} tone={decision.confidenceScore === null ? 'neutral' : 'positive'}><MasterTradeMetricVisual kind="confidence" value={decision.confidenceScore}/></MasterTradeMetricTile>
          <MasterTradeMetricTile label="OPPORTUNITY" value={numeric(decision.opportunityScore)}><MasterTradeMetricVisual kind="confidence" value={decision.opportunityScore}/></MasterTradeMetricTile>
        </div>
        <div className="refMtfStrip"><span>MTF CONFIRMATION {decision.mtfScore === null ? '—' : `${decision.mtfConfirmed} / ${decision.mtfTotal}`}</span><div className="refMtfPills">{MASTER_MTF_INTERVALS.map(timeframe => {const row = decision.mtfRows.find(row => row.timeframe === timeframe); return <span key={timeframe} data-tone={directionTone(row?.direction ?? 'NEUTRAL')}>{timeframe} <b>{row?.available ? row.direction : '—'}</b></span>})}</div></div>
      </section>
      <aside className="refRight" aria-label="Analysis decision panel">
        <section className="refCard refReport" aria-label="Trade decision analysis">
          <h2>AI ANALİZ RAPORU <small>{props.symbol}</small></h2>
          <div className="refReportSymbol"><CoinIcon symbol={props.symbol} size={40}/><div><strong>{props.symbol}</strong><span>{numeric(decision.confidenceScore, '%')} güven</span></div><b className="refDecisionBadge" data-tone={action === 'AL' ? 'positive' : action === 'SAT' ? 'negative' : 'neutral'}>{action}</b></div>
          <p className="refDecisionSummary">{available ? `${decision.status} · ${decision.marketRegime}` : '—'}</p>
          <PremiumBoundary label="Why this decision" compact><ul className="refReasons">{reasons.length ? reasons.map(reason => <li key={reason}>{/not |unavailable|missing|failed/i.test(reason) ? <X className="negative"/> : <Check className="positive"/>}<span>{reason}</span></li>) : <li>—</li>}</ul></PremiumBoundary>
          <div className="refTargets" data-locked={!premium}><h3>OLASI HEDEFLER</h3>{[['TP1', analysis?.tp1], ['TP2', analysis?.tp2], ['TP3', analysis?.tp3], ['Stop Loss', analysis?.stop_loss]].map(([label, value]) => <div key={label} className={label === 'Stop Loss' ? 'refStop' : ''}><span>{label}</span><strong aria-hidden={!premium}>{premium && typeof value === 'number' ? analysisPrice(value) : '—'}</strong></div>)}{!premium && <button type="button" disabled={!ready || !userId} onClick={() => openUpgrade('Giriş / SL / TP seviyeleri')} aria-label="Olası hedefler · Premium"><LockKeyhole/></button>}</div>
        </section>
        <section className="refCard refAutoTrade" data-enabled={enabled}><header><Power/><h2>AUTO TRADE</h2><button type="button" role="switch" aria-label="Auto Trade" aria-checked={enabled} disabled={!gatesReady} className="refToggle" onClick={props.onAutoTrade}><span/></button>{!gatesReady && <button type="button" className="refLockInfo" aria-label="Auto Trade neden kilitli?" onClick={explainLock}><LockKeyhole/></button>}<button type="button" className="refAutoSettings" aria-label="Auto Trade ayarlarına git" onClick={props.onAutoTrade}><Settings2/></button></header><p>{autoStatus}</p><small>Ayrı güvenlik kapıları gerekir</small><button type="button" className="refAutoLink" onClick={props.onAutoTrade}>{autoTradePresentation('LIVE', live?.live_auto_trade).action}</button></section>
        {notice && <div className="refNotice" role="status">{notice}<button type="button" aria-label="Bilgiyi kapat" onClick={() => setNotice('')}><X/></button></div>}
        <section className="refCard refTrigger" aria-label="Trigger monitor">
          <h2>TRIGGER MONITOR <span className="refSnapshotChip">{trigger.available ? 'LIVE SNAPSHOT' : '—'}</span></h2>
          <span className="refTriggerStatus" data-tone={triggerStatus.tone}>{triggerStatus.label}</span>
          <div className="refTriggerGrid">
            <div><small>CURRENT</small><strong>{analysisPrice(trigger.currentPrice)}</strong></div>
            <div><small>{trigger.direction === 'SHORT' ? 'SHORT TRIGGER BELOW' : 'LONG TRIGGER ABOVE'}</small><strong>{analysisPrice(trigger.triggerPrice)}</strong></div>
            <div><small>DISTANCE (%)</small><strong>{numeric(trigger.distancePct, '%', 2)}</strong></div>
            <div><small>STATUS</small><strong>{trigger.available ? trigger.lifecycle : '—'}</strong></div>
          </div>
          <div className="refTriggerEvents"><span data-active={trigger.available && ['TRIGGERED', 'CONFIRMED'].includes(trigger.lifecycle)}>Triggered <b>{trigger.available && ['TRIGGERED', 'CONFIRMED'].includes(trigger.lifecycle) ? trigger.lifecycle : '—'}</b></span><span data-active={trigger.available && trigger.lifecycle === 'EXPIRED'}>Expired <b>{trigger.available && trigger.lifecycle === 'EXPIRED' ? 'EXPIRED' : '—'}</b></span></div>
          <PremiumBoundary label="Trigger monitor" compact>
            <MasterTradeAccordions decision={decision} trigger={trigger} analysis={analysis} timeline={props.timeline}/>
          </PremiumBoundary>
          <p className="refSafety">Final Decision does not send orders or grant Auto Trade eligibility.</p>
        </section>
      </aside>
    </main>
  </section>
}
