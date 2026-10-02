import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { BarChart3, Check, ChevronDown, ChevronUp, Filter, RefreshCw, Search, X } from 'lucide-react'
import { API_BASE } from './api'
import { analystCopy, marketCopy } from './ui-copy'
import { AnalystCreditBadge, AnalystCreditsExhausted, useAnalystCredits } from './analyst-credit-ui'
import { useMemberAccess } from './premium-access'

type Direction = 'LONG'|'SHORT'|'BEKLE'
type Row = {symbol:string;display:string;price:number;change:number;volume:number;volume_ratio:number;volume_change_pct:number;volatility_pct:number;rsi:number;ema20:number;ema50:number;ema200:number;trend:string;direction:Direction;confidence:number;smart_score:number;opportunity_score:number;final_decision_score:number;entry?:number;stop_loss?:number;tp1?:number;tp2?:number;tp3?:number;support?:number;resistance?:number;risk_reward?:number;risk_pct?:number;potential_tp3_pct?:number;mtf_direction:string;mtf_alignment:number;mtf_timeframes:{timeframe:string;direction:string;confidence:number}[];anomaly:{kind:string;label:string;strength:number}|null}
type Consensus = {direction:string;alignment:number;timeframes:{timeframe:string;direction:string;confidence:number}[]}
type SignalHistoryItem = {id:string;symbol:string;timestamp:string;signal:Direction;score:number;entry:number;stop:number;tp1:number;tp2:number;tp3:number;timeframe:string;mtf:string;risk_reward:number;status:string}
type SignalPerformance = {available:boolean;message?:string;total_signals?:number;tp1_hit_rate?:number;tp2_hit_rate?:number;tp3_hit_rate?:number;stop_rate?:number;average_risk_reward?:number}
type ScannerAlert = {id:string;symbol:string;signal:string;score_min:number;rsi_min:number;volume_spike:boolean;price_crosses_ema20:boolean;mtf:string;active:boolean;last_triggered_at:string|null}
type FilterState = {signal:string;strength:string;trend:string;rsiMin:number;rsiMax:number;volume:string;mtf:string;volatility:string;ema20:boolean;ema20_50:boolean;ema50_200:boolean;minRR:number}
type AnalystMode = ''|'market-overview'|'best-long'|'best-short'|'why-coin'|'technical'|'decision'|'coin-detail'|'prompt'
const emptyFilters:FilterState = {signal:'ALL',strength:'ANY',trend:'ALL',rsiMin:0,rsiMax:100,volume:'ANY',mtf:'ANY',volatility:'ANY',ema20:false,ema20_50:false,ema50_200:false,minRR:0}
const intervals = ['1m','5m','15m','30m','1h','4h','1d']
const ANALYST_SCAN_LIMIT = 40
const ANALYST_SCAN_TIMEOUT_MS = 15000
const fmt = (value?:number) => value === undefined || !Number.isFinite(value) ? '—' : value.toLocaleString('tr-TR',{maximumFractionDigits:value < 10 ? 3 : 2})
const scoreLabel = (score:number) => score >= 90 ? 'Very Strong' : score >= 75 ? 'Strong' : score >= 60 ? 'Moderate' : score >= 40 ? 'Neutral' : 'Weak'
const tone = (direction:Direction) => direction === 'LONG' ? 'positive' : direction === 'SHORT' ? 'negative' : 'neutral'
const filterCount = (filters:FilterState) => Object.entries(filters).filter(([key,value]) => value !== emptyFilters[key as keyof FilterState] && value !== false).length
function Metric({label,value,tone}:{label:string;value:ReactNode;tone?:string}) { return <span className={tone}><small>{label}</small><b>{value}</b></span> }

export default function CoinAnalysisCenter({interval,onIntervalChange,chart}:{interval:string;onIntervalChange:(value:string)=>void;chart:(symbol:string,interval:string,showLevels:boolean,showEma:boolean)=>ReactNode}) {
  const {premium, userId, openUpgrade} = useMemberAccess()
  const credits = useAnalystCredits()
  const [detail, setDetail] = useState<{userId: string | null; row: Row; timeframe: string; expiresAt: number; cached: boolean} | null>(null)
  const [purchasing, setPurchasing] = useState(false)
  const analysisController = useRef<AbortController | null>(null)
  const [rows,setRows] = useState<Row[]>([])
  const [selected,setSelectedSymbol] = useState('BTCUSDT')
  const [query,setQuery] = useState('')
  const [debouncedQuery,setDebouncedQuery] = useState('')
  const [filters,setFilters] = useState<FilterState>(emptyFilters)
  const [draftFilters,setDraftFilters] = useState<FilterState>(emptyFilters)
  const [filterOpen,setFilterOpen] = useState(false)
  const [sort,setSort] = useState<keyof Row>('final_decision_score')
  const [ascending,setAscending] = useState(false)
  const [loading,setLoading] = useState(false)
  const loadController = useRef<AbortController|null>(null)
  const resultRef = useRef<HTMLElement>(null)
  const [error,setError] = useState('')
  const [autoScan,setAutoScan] = useState(true)
  const [scanMessage,setScanMessage] = useState('Tarama bekleniyor')
  const [showLevels,setShowLevels] = useState(true)
  const [showEma,setShowEma] = useState(true)
  const [detailsOpen,setDetailsOpen] = useState(false)
  const [history,setHistory] = useState<SignalHistoryItem[]>([])
  const [performance,setPerformance] = useState<SignalPerformance|null>(null)
  const [alerts,setAlerts] = useState<ScannerAlert[]>([])
  const [alertOpen,setAlertOpen] = useState(false)
  const [paperOpen,setPaperOpen] = useState(false)
  const [paperBusy,setPaperBusy] = useState(false)
  const [historyExpanded,setHistoryExpanded] = useState(false)
  const [analystOpen,setAnalystOpen] = useState(false)
  const [storedAnalystAnswer,setAnalystAnswer] = useState('')
  const [pendingAction, setPendingAction] = useState<{label: string; question: string; symbol: string; timeframe: string} | null>(null)
  const [analystSelection,setAnalystSelection] = useState('')
  const [analystMode,setAnalystMode] = useState<AnalystMode>('')
  const [analystLoading,setAnalystLoading] = useState(false)
  const [analystError,setAnalystError] = useState('')
  const [selectorPulse,setSelectorPulse] = useState(false)
  const analystTimer = useRef<number|null>(null)
  const [selectorQuery,setSelectorQuery] = useState('')
  const [selectorDirection,setSelectorDirection] = useState('ALL')
  const [alertDraft,setAlertDraft] = useState({signal:'ANY',score_min:85,rsi_min:0,volume_spike:false,price_crosses_ema20:false,mtf:'ANY'})

  const load = async () => {
    loadController.current?.abort()
    const controller = new AbortController()
    loadController.current = controller
    let timedOut = false
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort() }, ANALYST_SCAN_TIMEOUT_MS)
    setLoading(true); setError(''); setScanMessage('Taranıyor…')
    try {
      const response = await fetch(`${API_BASE}/analysis-universe?interval=${interval}&limit=${ANALYST_SCAN_LIMIT}`,{signal:controller.signal})
      const payload = await response.json() as {results?:Row[];detail?:string}
      if (!response.ok) throw new Error(payload.detail || 'Market data temporarily unavailable.')
      if (controller.signal.aborted) return
      setAnalystSelection(''); setAnalystAnswer('')
      setRows(payload.results || []); setError(''); setScanMessage(`${payload.results?.length || 0} coin analiz edildi`)
      if (payload.results?.length && !payload.results.some(row => row.symbol === selected)) setSelectedSymbol(payload.results[0].symbol)
    } catch (reason) {
      if (timedOut) {
        setRows([]); setError('Market data request timed out. Please retry.'); setScanMessage('Zaman aşımı')
      } else if (!controller.signal.aborted) {
        setRows([]); setError(reason instanceof Error ? reason.message : 'Market data temporarily unavailable.'); setScanMessage('Tarama başarısız')
      }
    } finally {
      window.clearTimeout(timeout)
      if (loadController.current === controller) { loadController.current = null; setLoading(false) }
    }
  }
  useEffect(() => { if (!autoScan) return; void load(); const timer = window.setInterval(() => void load(),60000); return () => { window.clearInterval(timer); loadController.current?.abort() } },[interval,autoScan])
  useEffect(() => { const timer = window.setTimeout(() => setDebouncedQuery(query.trim().toUpperCase()),250); return () => window.clearTimeout(timer) },[query])
  const matches = (row:Row) => {
    const signalMatch = filters.signal === 'ALL' || row.direction === filters.signal
    const strengthMatch = filters.strength === 'ANY' || (filters.strength === 'MODERATE' && row.smart_score >= 60) || (filters.strength === 'STRONG' && row.smart_score >= 75) || (filters.strength === 'VERY_STRONG' && row.smart_score >= 90)
    const trendMatch = filters.trend === 'ALL' || (filters.trend === 'BULLISH' && row.trend.includes('yükseliş')) || (filters.trend === 'BEARISH' && row.trend.includes('düşüş')) || (filters.trend === 'NEUTRAL' && row.trend === 'Karışık')
    const mtfMatch = filters.mtf === 'ANY' || (filters.mtf === 'BULLISH_3' && row.mtf_timeframes.filter(item => item.direction === 'LONG').length >= 3) || (filters.mtf === 'BULLISH_4' && row.mtf_timeframes.filter(item => item.direction === 'LONG').length === 4) || (filters.mtf === 'BEARISH_3' && row.mtf_timeframes.filter(item => item.direction === 'SHORT').length >= 3) || (filters.mtf === 'BEARISH_4' && row.mtf_timeframes.filter(item => item.direction === 'SHORT').length === 4)
    const volatilityMatch = filters.volatility === 'ANY' || (filters.volatility === 'LOW' && row.volatility_pct < 1) || (filters.volatility === 'NORMAL' && row.volatility_pct >= 1 && row.volatility_pct < 3) || (filters.volatility === 'HIGH' && row.volatility_pct >= 3)
    return signalMatch && strengthMatch && trendMatch && mtfMatch && volatilityMatch && row.rsi >= filters.rsiMin && row.rsi <= filters.rsiMax && (filters.volume === 'ANY' || (filters.volume === 'INCREASING' && row.volume_ratio >= 1.05) || (filters.volume === 'STRONG' && row.volume_ratio >= 1.25)) && (!filters.ema20 || row.price > row.ema20) && (!filters.ema20_50 || row.ema20 > row.ema50) && (!filters.ema50_200 || row.ema50 > row.ema200) && (filters.minRR === 0 || row.risk_reward !== undefined && row.risk_reward >= filters.minRR)
  }
  const rankRows = (left:Row,right:Row) => right.final_decision_score - left.final_decision_score || right.confidence - left.confidence || right.smart_score - left.smart_score
  const visible = useMemo(() => rows.filter(row => row.display.toUpperCase().includes(debouncedQuery) && matches(row)).sort((left,right) => {
    if (sort === 'final_decision_score') return rankRows(left,right) * (ascending ? -1 : 1)
    const leftValue = left[sort], rightValue = right[sort]
    if (leftValue === undefined || leftValue === null) return rightValue === undefined || rightValue === null ? 0 : 1
    if (rightValue === undefined || rightValue === null) return -1
    return (leftValue < rightValue ? -1 : leftValue > rightValue ? 1 : 0) * (ascending ? 1 : -1)
  }),[rows,debouncedQuery,filters,sort,ascending])
  const authorizedDetail = !credits.exhausted && detail?.userId === userId && detail?.row.symbol === selected && detail.timeframe === interval && detail.expiresAt > credits.now ? detail : null
  const active = authorizedDetail?.row || rows.find(row => row.symbol === selected) || visible[0]
  const analystAnswer = premium || authorizedDetail ? storedAnalystAnswer : ''
  const activeSymbol = active?.symbol || ''
  const consensus = active ? {direction:active.mtf_direction,alignment:active.mtf_alignment,timeframes:active.mtf_timeframes} : null
  useEffect(() => {
    setAnalystSelection('')
    setAnalystAnswer('')
  },[activeSymbol])
  useEffect(() => {
    if (rows.length && analystLoading && !purchasing && analystTimer.current === null) setAnalystLoading(false)
  },[rows.length,analystLoading,purchasing])
  useEffect(() => {
    analysisController.current?.abort()
    setDetail(null); setAnalystAnswer(''); setAnalystMode(''); setPendingAction(null); setPurchasing(false); setAnalystLoading(false)
  }, [interval, userId])
  useEffect(() => () => {
    analysisController.current?.abort()
    if (analystTimer.current !== null) window.clearTimeout(analystTimer.current)
  }, [])
  useEffect(() => {
    if (!activeSymbol) return
    const controller = new AbortController()
    const request = (path:string) => fetch(`${API_BASE}${path}`,{signal:controller.signal})
    Promise.all([
      request(`/signal-history/${activeSymbol}`).then(response => response.ok ? response.json() as Promise<{history?:SignalHistoryItem[]}> : {history:[]}),
      request(`/signal-performance/${activeSymbol}`).then(response => response.ok ? response.json() as Promise<SignalPerformance> : null),
      request('/scanner-alerts').then(response => response.ok ? response.json() as Promise<{alerts?:ScannerAlert[]}> : {alerts:[]}),
    ]).then(([historyPayload,performancePayload,alertPayload]) => {
      if (controller.signal.aborted) return
      setHistory(historyPayload.history || [])
      setPerformance(performancePayload)
      setAlerts((alertPayload.alerts || []).filter(item => item.symbol === activeSymbol))
    }).catch(reason => { if (reason?.name !== 'AbortError' && !controller.signal.aborted) { setHistory([]); setPerformance(null); setAlerts([]) } })
    return () => controller.abort()
  },[activeSymbol])
  const topLong = useMemo(() => rows.filter(row => row.direction === 'LONG').sort(rankRows).slice(0,5),[rows])
  const topShort = useMemo(() => rows.filter(row => row.direction === 'SHORT').sort(rankRows).slice(0,5),[rows])
  const anomalies = useMemo(() => rows.filter(row => row.anomaly).sort((left,right) => (right.anomaly?.strength || 0) - (left.anomaly?.strength || 0)).slice(0,5),[rows])
  const featured = useMemo(() => [...rows].filter(row => row.direction !== 'BEKLE').sort(rankRows).slice(0,5),[rows])
  const summary = useMemo(() => ({long:rows.filter(row => row.direction === 'LONG').length,short:rows.filter(row => row.direction === 'SHORT').length,watch:rows.filter(row => row.direction === 'BEKLE').length}),[rows])
  const hasMarketData = rows.length > 0
  const selectorRows = useMemo(() => rows.filter(row => row.display.toUpperCase().includes(selectorQuery.trim().toUpperCase()) && (selectorDirection === 'ALL' || (selectorDirection === 'WATCH' ? row.direction === 'BEKLE' : row.direction === selectorDirection))),[rows,selectorQuery,selectorDirection])
  const selectorSignal = (direction:Direction) => direction === 'BEKLE' ? 'WATCH' : direction
  const chooseSort = (key:keyof Row) => { if (sort === key) setAscending(value => !value); else { setSort(key); setAscending(false) } }
  const sortIcon = (key:keyof Row) => sort === key ? ascending ? <ChevronUp/> : <ChevronDown/> : null
  const applyFilters = () => { setFilters(draftFilters); setFilterOpen(false) }
  const clearFilters = () => { setDraftFilters(emptyFilters); setFilters(emptyFilters) }
  const removeFilter = (key:keyof FilterState) => { const next = {...filters,[key]:emptyFilters[key]}; setFilters(next); setDraftFilters(next) }
  const filterNames:Record<string,string> = {signal:'Signal',strength:'Strength',trend:'Trend',rsiMin:'RSI',volume:'Volume',mtf:'MTF',volatility:'Volatility',ema20:'Price > EMA20',ema20_50:'EMA20 > EMA50',ema50_200:'EMA50 > EMA200',minRR:'R/R'}
  const reasons = active ? [active.ema20 > active.ema50 ? 'EMA20, EMA50’nin üzerinde' : 'EMA20, EMA50’nin altında',active.ema50 > active.ema200 ? 'EMA50, EMA200’ün üzerinde' : 'EMA50, EMA200’ün altında',`RSI ${fmt(active.rsi)} ile teknik momentum ölçülüyor`,`Hacim son ortalamaya göre ${active.volume_ratio >= 1 ? '+' : ''}${fmt((active.volume_ratio - 1) * 100)}%`,`Smart Score ${fmt(active.smart_score)} · ${scoreLabel(active.smart_score)}`,`Risk/Reward ${fmt(active.risk_reward)}`] : []
  const timeframeSummary = consensus ? `${consensus.timeframes.filter(item => item.direction === 'LONG').length}/${consensus.timeframes.length} bullish` : 'Ölçülüyor'
  const analystPrompts = useMemo(() => { const symbol = active?.symbol.replace(/USDT$/,'') || 'seçili coin'; return [`${symbol} neden hareket ediyor?`,`${symbol} trendi güçleniyor mu?`,`${symbol} desteği nerede?`,`${symbol} direnci nerede?`,`${symbol} momentumu nasıl?`,`${symbol} hacmi ne gösteriyor?`,`${symbol} riski nedir?`,`${symbol} neden ${active?.direction || 'LONG/SHORT'}?`] },[active?.direction,active?.symbol])
  const answerAnalyst = (question:string) => { if (!active) { setAnalystAnswer('Güncel piyasa verisiyle yanıt oluşturulamıyor.'); return }; const normalized = question.toLowerCase(); const volume = `${active.volume_ratio >= 1 ? '+' : ''}${fmt((active.volume_ratio - 1) * 100)}% ortalamaya göre`; const emaAligned = active.direction === 'LONG' ? active.ema20 > active.ema50 && active.ema50 > active.ema200 : active.direction === 'SHORT' ? active.ema20 < active.ema50 && active.ema50 < active.ema200 : false; let answer = `${active.display} şu anda ${active.direction}; Teknik Sinyal Skoru ${fmt(active.smart_score)}/100, yapı ${active.trend.toLowerCase()}, RSI ${fmt(active.rsi)}, MTF ${active.mtf_alignment} ve R/R ${fmt(active.risk_reward)}.`; if (normalized.includes('hareket')) answer = `${active.display}, ${active.direction} baskısıyla hareket ediyor: yapı ${active.trend.toLowerCase()}, RSI ${fmt(active.rsi)} ve hacim ${volume}.`; else if (normalized.includes('güçlen')) answer = `${active.display} trendi EMA yapısında ${emaAligned ? 'uyumlu' : 'tam uyumlu değil'}; MTF uyumu ${active.mtf_alignment} ve sinyal ${active.direction}.`; else if (normalized.includes('deste')) answer = `${active.display} desteği ${fmt(active.support)}, direnci ${fmt(active.resistance)} ve güncel fiyatı ${fmt(active.price)}.`; else if (normalized.includes('direnc')) answer = `${active.display} direnci ${fmt(active.resistance)}, desteği ${fmt(active.support)} ve güncel fiyatı ${fmt(active.price)}.`; else if (normalized.includes('risk')) answer = `${active.display} stop seviyesine göre ${fmt(active.risk_pct)}% model riski taşıyor; R/R ${fmt(active.risk_reward)} ve sinyal ${active.direction}.`; else if (normalized.includes('momentum')) answer = `${active.display} momentumu RSI ${fmt(active.rsi)} ile ${active.rsi >= 50 ? 'yukarı' : 'aşağı'} yönlü; güncel sinyal ${active.direction}.`; else if (normalized.includes('hacim')) answer = `${active.display} hacmi hareketi ${active.volume_ratio >= 1 ? 'doğruluyor' : 'doğrulamıyor'}: ${volume}.`; else if (normalized.includes('neden') && normalized.includes('long')) answer = `${active.display} LONG çünkü ${active.trend.toLowerCase()} yapı, ${active.mtf_alignment} MTF uyumu, RSI ${fmt(active.rsi)} ve R/R ${fmt(active.risk_reward)} sinyali destekliyor.`; else if (normalized.includes('neden') && normalized.includes('short')) answer = `${active.display} SHORT çünkü ${active.trend.toLowerCase()} yapı, ${active.mtf_alignment} MTF uyumu, RSI ${fmt(active.rsi)} ve R/R ${fmt(active.risk_reward)} sinyali destekliyor.`; setAnalystAnswer(answer) }
  const renderAnalyst = (label:string, question:string) => {
    const mode:AnalystMode = label === 'Market Overview' ? 'market-overview' : label === 'Best LONG' ? 'best-long' : label === 'Best SHORT' ? 'best-short' : label === 'Why This Coin' ? 'why-coin' : ['Trend Analysis','Momentum','Volume Intelligence','EMA Structure','RSI / MACD','Support / Resistance','Volatility'].includes(label) ? 'technical' : ['Market Regime','Entry Analysis','Exit Analysis','Risk / R:R','Signal Confidence'].includes(label) ? 'decision' : 'prompt'
    const needsCoin = mode === 'technical' || mode === 'decision' || mode === 'why-coin' || mode === 'prompt'
    setAnalystSelection(label)
    setAnalystMode(mode)
    setAnalystError('')
    setAnalystAnswer('')
    if (needsCoin && !active) {
      setAnalystError(analystCopy.selectCoin)
      setSelectorPulse(true)
      window.setTimeout(() => setSelectorPulse(false), 1200)
      resultRef.current?.scrollIntoView({behavior:'smooth',block:'nearest'})
      return
    }
    if (loading || !rows.length) {
      setAnalystLoading(true)
      resultRef.current?.scrollIntoView({behavior:'smooth',block:'nearest'})
      return
    }
    setAnalystLoading(true)
    if (analystTimer.current !== null) window.clearTimeout(analystTimer.current)
    analystTimer.current = window.setTimeout(() => {
      setAnalystLoading(false)
      if (mode === 'why-coin' || mode === 'prompt') answerAnalyst(question)
      resultRef.current?.scrollIntoView({behavior:'smooth',block:'nearest'})
    }, 220)
  }
  const requestAnalysis = async (symbol: string): Promise<Row | null> => {
    analysisController.current?.abort()
    const controller = new AbortController()
    analysisController.current = controller
    if (analystTimer.current !== null) { window.clearTimeout(analystTimer.current); analystTimer.current = null }
    setPendingAction(null); setPurchasing(true); setAnalystLoading(true); setAnalystError(''); setAnalystAnswer('')
    try {
      const result = await credits.purchase<Row>(symbol, interval, controller.signal)
      if (controller.signal.aborted) return null
      if (result.result.symbol !== symbol) throw new Error('Analiz yanıtındaki sembol istekle eşleşmiyor.')
      setDetail({userId, row: result.result, timeframe: interval, expiresAt: Date.parse(result.cacheExpiresAt), cached: result.cached})
      return result.result
    } catch (reason: unknown) {
      if (!controller.signal.aborted) setAnalystError(reason instanceof Error ? reason.message : 'Analiz açılamadı.')
      return null
    } finally {
      if (analysisController.current === controller) { setPurchasing(false); setAnalystLoading(false) }
    }
  }
  const setSelected = (symbol: string) => { setSelectedSymbol(symbol); void requestAnalysis(symbol) }
  const runAnalyst = async (label: string, question: string) => {
    if (!active) { renderAnalyst(label, question); return }
    const row = await requestAnalysis(active.symbol)
    if (row) setPendingAction({label, question, symbol: row.symbol, timeframe: interval})
  }
  useEffect(() => {
    if (!pendingAction) return
    if (credits.exhausted) { setPendingAction(null); return }
    if (authorizedDetail?.row.symbol === pendingAction.symbol && interval === pendingAction.timeframe) {
      renderAnalyst(pendingAction.label, pendingAction.question)
      setPendingAction(null)
    }
  }, [pendingAction, authorizedDetail?.row, credits.exhausted, interval])
  const selectAnalystCoin = (symbol:string) => {
    setSelected(symbol)
    setAnalystSelection('')
    setAnalystMode('coin-detail')
    setAnalystError('')
    resultRef.current?.scrollIntoView({behavior:'smooth',block:'nearest'})
  }
  const whySignal = active ? [{label:'Trend alignment',ok:active.direction === 'LONG' ? active.ema20 > active.ema50 && active.ema50 > active.ema200 : active.direction === 'SHORT' ? active.ema20 < active.ema50 && active.ema50 < active.ema200 : false},{label:'RSI confirmation',ok:active.direction === 'LONG' ? active.rsi > 50 : active.direction === 'SHORT' ? active.rsi < 50 : false},{label:'Multi-timeframe support',ok:active.mtf_direction === active.direction},{label:`Risk/Reward ${fmt(active.risk_reward)}`,ok:active.risk_reward !== undefined && active.risk_reward >= 2}].filter(item => item.ok) : []
  const createAlert = async () => { try { const response = await fetch(`${API_BASE}/scanner-alerts`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...alertDraft,symbol:selected})}); if (!response.ok) throw new Error('Alarm oluşturulamadı'); const item = await response.json() as ScannerAlert; setAlerts(current => [item,...current]); setAlertOpen(false) } catch { setError('Alarm oluşturulamadı; tekrar deneyin.') } }
  const openPaperTrade = async () => { if (!premium) { openUpgrade('Paper işlem'); return }; if (!active) return; setPaperBusy(true); try { const direction = active.direction === 'SHORT' ? 'SHORT' : 'LONG'; const response = await fetch(`${API_BASE}/paper/open`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:active.symbol,direction,amount:100,stop_loss:active.stop_loss,take_profit:active.tp1,tp2:active.tp2,tp3:active.tp3,source:'MANUAL',signal_confidence:active.smart_score})}); if (!response.ok) throw new Error('Paper işlem açılamadı'); setPaperOpen(false); setAnalystAnswer('Paper position opened in the existing virtual wallet.') } catch (reason) { setError(reason instanceof Error ? reason.message : 'Paper işlem açılamadı.') } finally { setPaperBusy(false) } }
  const risingRows = useMemo(() => [...rows].sort((left,right) => right.change - left.change).slice(0,3),[rows])
  const fallingRows = useMemo(() => [...rows].sort((left,right) => left.change - right.change).slice(0,3),[rows])
  const marketDirection = summary.long > summary.short ? analystCopy.rising : summary.short > summary.long ? analystCopy.falling : analystCopy.balanced
  const technicalRows: Array<[string,string]> = active ? [
    [analystCopy.trend, active.trend],
    [analystCopy.momentum, `RSI ${fmt(active.rsi)} · ${active.rsi >= 50 ? 'yukarı' : 'aşağı'} yönlü`],
    [analystCopy.volume, `${active.volume_ratio >= 1 ? '+' : ''}${fmt((active.volume_ratio - 1) * 100)}% ortalamaya göre`],
    [analystCopy.ema, `${fmt(active.ema20)} / ${fmt(active.ema50)} / ${fmt(active.ema200)}`],
    [analystCopy.oscillator, `RSI ${fmt(active.rsi)} · MTF ${fmt(active.mtf_alignment)}`],
    [analystCopy.supportResistance, `${fmt(active.support)} / ${fmt(active.resistance)}`],
    [analystCopy.volatility, `%${fmt(active.volatility_pct)}`],
  ] : []
  const decisionRows: Array<[string,string]> = active ? [
    [analystCopy.regime, active.trend],
    [analystCopy.entry, fmt(active.entry)],
    [analystCopy.exit, `TP1 ${fmt(active.tp1)} · TP2 ${fmt(active.tp2)} · TP3 ${fmt(active.tp3)}`],
    [analystCopy.risk, `R/R ${fmt(active.risk_reward)} · -%${fmt(active.risk_pct)}`],
    [analystCopy.signalConfidence, `%${fmt(active.confidence)} · skor ${fmt(active.smart_score)}`],
  ] : []
  const resultRows = (items:Array<[string,string]>) => <div className="analystInsightRows">{items.map(([label,value]) => <div className="analystInsightRow" key={label}><span>{label}</span><b>{value}</b></div>)}</div>
  const resultOpportunityRows = (items:Row[], empty:string) => items.length ? <div className="analystOpportunityList">{items.map((row,index) => <div className="analystOpportunityRow" key={row.symbol}><span className="analystOpportunityRank">{index + 1}</span><div><strong>{row.display}</strong><small>{row.direction} · RSI {fmt(row.rsi)}</small></div><span><b>{fmt(row.smart_score)}</b><small>{analystCopy.score}</small></span><span><b>{fmt(row.price)}</b><small>{analystCopy.price}</small></span><span><b>{fmt(row.risk_reward)}</b><small>{analystCopy.riskReward}</small></span><button type="button" onClick={() => selectAnalystCoin(row.symbol)}>{analystCopy.detail}</button></div>)}</div> : <p className="analystEmpty">{empty}</p>
  const analystCompactSnapshot = active ? <div className="analystCompactSnapshot"><div><small>SIGNAL</small><b className={tone(active.direction)}>{active.direction}</b></div><div><small>CONFIDENCE</small><b>{fmt(active.confidence)}%</b></div><div><small>TREND</small><b>{active.trend}</b></div><div><small>MOMENTUM</small><b>RSI {fmt(active.rsi)}</b></div><div><small>MTF</small><b>{fmt(active.mtf_alignment)}</b></div><div><small>R/R</small><b>{fmt(active.risk_reward)}</b></div></div> : null
  const renderAnalystResult = () => credits.exhausted && credits.budget ? <AnalystCreditsExhausted budget={credits.budget} now={credits.now}/> : <section className={`analystResultWorkspace${selectorPulse ? ' selectorPulse' : ''}`} aria-live="polite" ref={resultRef}>
    <header><div><span>{analystCopy.analysisResult}</span><h3>{analystCopy.resultTitle}</h3></div>{active && <strong className={tone(active.direction)}>{active.display}</strong>}</header>
    {analystCompactSnapshot}
    {analystLoading ? <div className="analystInlineLoading"><i/><span>{analystCopy.loading}</span></div> : error ? <div className="analystDataError" role="alert"><strong>{analystCopy.dataUnavailable}</strong><p>{error}</p><button type="button" onClick={() => void load()}>{analystCopy.retry}</button></div> : analystError ? <div className="analystSelectionError" role="status"><strong>{analystError}</strong><button type="button" onClick={() => { setSelectorPulse(true); window.setTimeout(() => setSelectorPulse(false),1200) }}>{analystCopy.selector}</button></div> : !analystMode ? <p className="analystEmpty">{analystCopy.resultEmpty}</p> : analystMode === 'market-overview' ? <><div className="analystOverviewLead"><strong>{marketDirection}</strong><span>{summary.long} LONG · {summary.short} SHORT · {summary.watch} BEKLE</span></div>{resultRows([[analystCopy.opportunities, `${summary.long} LONG`],[analystCopy.declining, `${summary.short} SHORT`],[analystCopy.marketTrend, marketDirection]])}<div className="analystMoverGrid"><div><strong>{analystCopy.highest}</strong>{risingRows.map(row => <button type="button" key={row.symbol} onClick={() => selectAnalystCoin(row.symbol)}><span>{row.display}</span><b>+{fmt(row.change)}%</b></button>)}</div><div><strong>{analystCopy.lowest}</strong>{fallingRows.map(row => <button type="button" key={row.symbol} onClick={() => selectAnalystCoin(row.symbol)}><span>{row.display}</span><b>{fmt(row.change)}%</b></button>)}</div></div></> : analystMode === 'best-long' ? resultOpportunityRows(topLong.slice(0,3), analystCopy.noLong) : analystMode === 'best-short' ? resultOpportunityRows(topShort.slice(0,3), analystCopy.noShort) : analystMode === 'technical' ? <>{<div className="analystSelectedHeading"><strong>{active?.display || analystCopy.selectCoin}</strong><span>{analystCopy.technical}</span></div>}{resultRows(technicalRows)}</> : analystMode === 'decision' ? <>{<div className="analystSelectedHeading"><strong>{active?.display || analystCopy.selectCoin}</strong><span>{analystCopy.decision}</span></div>}{resultRows(decisionRows)}</> : <>{active && <div className="analystSelectedHeading"><strong>{active.display}</strong><span>{analystCopy.signalReason}</span></div>}{analystAnswer && <p className="analystResponseText">{analystAnswer}</p>}{active && resultRows([[analystCopy.trend,active.trend],[analystCopy.signalConfidence,`%${fmt(active.confidence)}`],[analystCopy.riskReward,fmt(active.risk_reward)]])}</>}
  </section>

  return <section className="coinAnalysisCenter">
    <section className="analystWorkspace" aria-label={analystCopy.ariaLabel}>
      <header className="analystWorkspaceHeader">
        <div>
          <span className="analystEyebrow">{analystCopy.eyebrow}</span>
          <h2>{analystCopy.title}</h2>
          <p>{analystCopy.description}</p>
        </div>
        <div className="analystHeaderControls">
          <AnalystCreditBadge budget={credits.budget} now={credits.now}/>
          {authorizedDetail?.cached && <small className="analystCached">Ücretsiz (son 15 dk)</small>}
          <div className="analystWorkspaceContext"><small>{analystCopy.selectedCoin}</small><strong>{active?.display || analystCopy.waiting}</strong></div>
          <label className="analystAutoScan"><input type="checkbox" checked={autoScan} onChange={event => setAutoScan(event.target.checked)}/><span><i className={loading ? 'scanDot' : autoScan ? 'liveDot' : 'idleDot'}/>{loading ? 'SCANNING' : autoScan ? 'AUTO SCAN' : 'PAUSED'}</span><small>{loading ? 'Market data updating' : autoScan ? `Every 60s · ${scanMessage}` : 'Manual scan only'}</small></label>
        </div>
      </header>
      {credits.error && <div className="analystCreditError" role="alert"><span>{credits.error}</span><button type="button" onClick={() => void credits.refresh()}>Tekrar dene</button></div>}
      <div className="analystCommandGrid">
        <section className="analystCommandGroup analystQuickGroup">
          <header><span>01</span><div><strong>{analystCopy.groups.quick}</strong><small>Güncel piyasa görünümü</small></div></header>
          <div className="analystCommandItems">
            <button type="button" className={analystSelection === 'Market Overview' ? 'active' : ''} aria-pressed={analystSelection === 'Market Overview'} onClick={() => runAnalyst('Market Overview',"Piyasa nasıl görünüyor?")}><b>{analystCopy.marketOverview}</b><small>Genişlik ve piyasa bağlamı</small></button>
            <button type="button" className={analystSelection === 'Best LONG' ? 'active' : ''} aria-pressed={analystSelection === 'Best LONG'} onClick={() => runAnalyst('Best LONG','En iyi LONG')}><b>{analystCopy.bestLong}</b><small>En güçlü LONG kurulumu</small></button>
            <button type="button" className={analystSelection === 'Best SHORT' ? 'active' : ''} aria-pressed={analystSelection === 'Best SHORT'} onClick={() => runAnalyst('Best SHORT','En iyi SHORT')}><b>{analystCopy.bestShort}</b><small>En güçlü SHORT kurulumu</small></button>
            <button type="button" className={analystSelection === 'Why This Coin' ? 'active' : ''} aria-pressed={analystSelection === 'Why This Coin'} onClick={() => runAnalyst('Why This Coin',`${active?.display || 'Bu coin'} neden?`)}><b>{analystCopy.whyCoin}</b><small>Seçili sinyali açıkla</small></button>
          </div>
        </section>
        <section className="analystCommandGroup">
          <header><span>02</span><div><strong>{analystCopy.groups.technical}</strong><small>Yapı, teyit ve baskıyı inceleyin</small></div></header>
          <div className="analystCommandItems">
            {['Trend Analysis','Momentum','Volume Intelligence','EMA Structure','RSI / MACD','Support / Resistance','Volatility'].map(label => <button type="button" key={label} className={analystSelection === label ? 'active' : ''} aria-pressed={analystSelection === label} onClick={() => runAnalyst(label,label)}><b>{analystCopy.labels[label]}</b><small>{label === 'Trend Analysis' ? 'Yön ve uyum yapısı' : label === 'Momentum' ? 'RSI ve momentum teyidi' : label === 'Volume Intelligence' ? 'Ortalama hacme göre katılım' : label === 'EMA Structure' ? 'EMA20, EMA50 ve EMA200 uyumu' : label === 'RSI / MACD' ? 'Güncel osilatör bağlamı' : label === 'Support / Resistance' ? 'Güncel yapısal fiyat seviyeleri' : 'Fiyat hareketi ve bant baskısı'}</small></button>)}
          </div>
        </section>
        <section className="analystCommandGroup analystDecisionGroup">
          <header><span>03</span><div><strong>{analystCopy.groups.decision}</strong><small>Veriyi risk odaklı görünüme dönüştürün</small></div></header>
          <div className="analystCommandItems">
            {['Market Regime','Entry Analysis','Exit Analysis','Risk / R:R','Signal Confidence'].map(label => <button type="button" key={label} className={analystSelection === label ? 'active' : ''} aria-pressed={analystSelection === label} onClick={() => runAnalyst(label,label)}><b>{analystCopy.labels[label]}</b><small>{label === 'Market Regime' ? 'Trend, bant veya karma koşullar' : label === 'Entry Analysis' ? 'Güncel giriş ve geçersizlik bağlamı' : label === 'Exit Analysis' ? 'Hedefler ve koruma seviyeleri' : label === 'Risk / R:R' ? 'Aşağı yön ve getiri yapısı' : 'Tarama görünümünden güven seviyesi'}</small></button>)}
          </div>
        </section>
      </div>
      {renderAnalystResult()}
      <div className="analystLowerGrid">
        <section className="analystCoinSelector" aria-label={analystCopy.selector}>
        <header><div><span>{analystCopy.selector}</span><strong>{rows.length ? `${selectorRows.length} / ${rows.length} piyasa` : analystCopy.scannerWaiting}</strong></div><small>Güncel tarama görünümünden seçim yapın</small></header>
        <div className="analystSelectorToolbar"><label><Search/><input value={selectorQuery} onChange={event => setSelectorQuery(event.target.value)} placeholder={analystCopy.searchCoin} aria-label="Tarama coinlerini ara"/></label><div className="analystSelectorFilters" role="group" aria-label="Tarama coinlerini filtrele">{[['ALL',analystCopy.all],['LONG','LONG'],['SHORT','SHORT'],['WATCH',analystCopy.watch]].map(([value,label]) => <button type="button" key={value} className={selectorDirection === value ? 'active' : ''} aria-pressed={selectorDirection === value} onClick={() => setSelectorDirection(value)}>{label}</button>)}</div></div>
        {loading ? <div className="analystSelectorSkeleton" aria-label="Tarama verileri yükleniyor"><i/><i/><i/><i/></div> : error ? <p className="analystSelectorEmpty">{analystCopy.dataUnavailable}</p> : <div className="analystSelectorList">{selectorRows.map(row => <button type="button" key={row.symbol} className={row.symbol === selected ? 'selected' : ''} onClick={() => selectAnalystCoin(row.symbol)} aria-pressed={row.symbol === selected}><span className="analystSelectorIdentity"><b>{row.symbol}</b><small>{row.display}</small></span><span className={`analystSelectorSignal ${tone(row.direction)}`}><strong>{selectorSignal(row.direction)}</strong><small>{fmt(row.confidence)}% {analystCopy.confidence}</small></span><span className="analystSelectorSnapshot"><b>{fmt(row.price)}</b><small>{row.trend}</small></span></button>)}{!selectorRows.length && <p className="analystSelectorEmpty">{analystCopy.noMatch}</p>}</div>}
        </section>
        <div className="analystDetailColumn">
          <section className="analystLegacyResultWorkspace" aria-live="polite">
            <header><div><span>{analystCopy.analysisResult}</span><h3>{active?.display || analystCopy.waiting}</h3></div><strong className={active ? tone(active.direction) : 'neutral'}>{active?.direction || analystCopy.waitingResult}</strong></header>
            {loading ? <div className="analystLoadingState" aria-label={analystCopy.loading}><i/><i/><i/><i/></div> : error ? <div className="analystDataError" role="alert"><strong>{analystCopy.dataUnavailable}</strong><p>{analystCopy.dataUnavailableHint}</p><button type="button" onClick={() => void load()}>{analystCopy.retry}</button></div> : active ? <>
              <div className="analystDetailHero"><strong className={tone(active.direction)}>{active.direction}</strong><span>{fmt(active.confidence)}% {analystCopy.confidence}</span></div>
              <div className="analystMetricCards">
                <span><small>{analystCopy.regime}</small><b>{active.trend}</b></span>
                <span><small>{analystCopy.entry}</small><b>{fmt(active.entry)}</b></span>
                <span><small>{analystCopy.exit}</small><b>TP1 {fmt(active.tp1)} · TP2 {fmt(active.tp2)} · TP3 {fmt(active.tp3)}</b></span>
                <span><small>{analystCopy.risk}</small><b>R/R {fmt(active.risk_reward)} · -{fmt(active.risk_pct)}%</b></span>
                <span><small>{analystCopy.signalConfidence}</small><b>{fmt(active.confidence)}%</b></span>
                <span><small>{analystCopy.mtfAlignment}</small><b>{fmt(active.mtf_alignment)}</b></span>
                <span><small>{analystCopy.momentum}</small><b>RSI {fmt(active.rsi)}</b></span>
                <span><small>{analystCopy.volume}</small><b>{active.volume_ratio >= 1 ? '+' : ''}{fmt((active.volume_ratio - 1) * 100)}%</b></span>
              </div>
              <div className="analystWhyResult"><span>{analystCopy.whyResult}</span><p>{analystAnswer || `${active.display} şu anda ${active.direction}; güncel tarama görünümünde güven seviyesi ${fmt(active.confidence)}%. İşlem öncesi yapı ve risk metriklerini inceleyin.`}</p></div>
            </> : <p className="analystEmpty">{analystCopy.resultEmpty}</p>}
          </section>
          <section className="analystAskBar">
        <div><span>{analystCopy.ask}</span><small>{analystCopy.askHint}</small></div>
        <div className="analystPromptChips">{analystPrompts.map(prompt => <button type="button" className={analystSelection === prompt ? 'active' : ''} aria-pressed={analystSelection === prompt} key={prompt} onClick={() => runAnalyst(prompt,prompt)}>{prompt}</button>)}</div>
          </section>
        </div>
      </div>
    </section>
    <header className="coinAnalysisHeader"><div><span><BarChart3/> {marketCopy.scanner}</span><h2>Coin Analiz Merkezi</h2><p>{marketCopy.subtitle}</p></div><div className="coinHeaderActions"><div className="coinHeaderStatus"><strong><i className={autoScan ? 'liveDot' : 'idleDot'}/>{autoScan ? marketCopy.live : marketCopy.paused}</strong><small>{rows.length} {marketCopy.assets}</small><small>{marketCopy.lastScan} · {scanMessage}</small></div><button type="button" onClick={() => void load()} disabled={loading}><RefreshCw className={loading ? 'spin' : ''}/>{loading ? marketCopy.scanning : marketCopy.runScan}</button></div></header>
    <div className="coinAnalysisToolbar"><label><Search/><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search coin…"/>{query && <button type="button" className="searchClear" aria-label="Aramayı temizle" onClick={() => setQuery('')}><X/></button>}</label><button className="filterTrigger" type="button" onClick={() => { setDraftFilters(filters); setFilterOpen(true) }}><Filter/>FİLTRELER{filterCount(filters) ? ` · ${filterCount(filters)}` : ''}</button></div>
    <div className="activeFilterChips">{Object.entries(filters).filter(([key,value]) => value !== emptyFilters[key as keyof FilterState] && value !== false).map(([key,value]) => <button type="button" key={key} onClick={() => removeFilter(key as keyof FilterState)}>{filterNames[key]}{key === 'minRR' ? ` > ${value}` : `: ${value}`} <X/></button>)}</div>
    <div className="marketPulse"><span className="marketPulseLabel">{marketCopy.overview}</span><div className="marketPulseCards"><article><b>{hasMarketData ? rows.length : '—'}</b><small>{marketCopy.assets}</small></article><article className="positive"><b>{hasMarketData ? summary.long : '—'}</b><small>LONG</small></article><article className="negative"><b>{hasMarketData ? summary.short : '—'}</b><small>SHORT</small></article><article className="neutral"><b>{hasMarketData ? summary.watch : '—'}</b><small>{analystCopy.watch}</small></article><article className={hasMarketData ? (summary.long >= summary.short ? 'regimeCard positive' : 'regimeCard negative') : 'regimeCard neutral'}><b>{hasMarketData ? (summary.long >= summary.short ? 'YÜKSELİŞ' : 'DÜŞÜŞ') : '—'}</b><small>PİYASA GENİŞLİĞİ</small></article></div></div>
    <div className="coinIntervals">{intervals.map(item => <button type="button" key={item} className={interval === item ? 'active' : ''} onClick={() => onIntervalChange(item)}>{item.toUpperCase()}</button>)}</div>
    {error && <div className="coinAnalysisError">{error}<button type="button" onClick={() => void load()}>RETRY</button></div>}
    <section className="topOpportunityGrid"><article><header><span>LONG SETUPS</span><h3>TOP LONG</h3></header>{topLong.length ? topLong.map(row => <button type="button" key={row.symbol} className={row.symbol === selected ? 'selectedRow' : ''} onClick={() => { setSelected(row.symbol); setDetailsOpen(true) }}><b>{row.display}</b><span className="rowBadges"><strong className="positive">LONG</strong><em>{fmt(row.smart_score)}</em></span><small>{fmt(row.price)} · RSI {fmt(row.rsi)} · R/R {fmt(row.risk_reward)} · MTF {row.mtf_direction}</small></button>) : <p className="emptyHint">{loading ? 'Scanning opportunities…' : 'No LONG setups yet.'}</p>}</article><article><header><span>SHORT SETUPS</span><h3>TOP SHORT</h3></header>{topShort.length ? topShort.map(row => <button type="button" key={row.symbol} className={row.symbol === selected ? 'selectedRow' : ''} onClick={() => { setSelected(row.symbol); setDetailsOpen(true) }}><b>{row.display}</b><span className="rowBadges"><strong className="negative">SHORT</strong><em>{fmt(row.smart_score)}</em></span><small>{fmt(row.price)} · RSI {fmt(row.rsi)} · R/R {fmt(row.risk_reward)} · MTF {row.mtf_direction}</small></button>) : <p className="emptyHint">{loading ? 'Scanning opportunities…' : 'No SHORT setups yet.'}</p>}</article></section>
    <section className="marketSignalsRow"><article className="anomalyPanel"><header><div><span>MARKET EVENTS</span><h3>Anomalies</h3></div><small>{anomalies.length ? `${anomalies.length} önemli olay` : 'No anomalies detected.'}</small></header>{anomalies.map(row => <button type="button" key={row.symbol} onClick={() => { setSelected(row.symbol); setDetailsOpen(true) }}><b>{row.display}</b><span>{row.anomaly?.label}</span><em>{row.anomaly?.strength}</em></button>)}</article><article className="heatmapPanel"><header><div><span>MARKET OVERVIEW</span><h3>Market Heatmap</h3></div><small>Volume-weighted scanner view</small></header><div className="heatmapGrid">{rows.map(row => <button type="button" key={row.symbol} className={tone(row.direction)} onClick={() => { setSelected(row.symbol); setDetailsOpen(true) }}><b>{row.display.replace('/USDT','')}</b><small>{row.change >= 0 ? '+' : ''}{fmt(row.change)}%</small></button>)}</div></article></section>
    <section className="featuredOpportunities"><header><div><span>HIGH-CONVICTION TECHNICAL SETUPS</span><h3>Öne Çıkan Fırsatlar</h3></div><small>Technical Signal Score · not a probability</small></header><div className="featuredList">{featured.map(row => <button type="button" key={row.symbol} className="featuredCard" onClick={() => { setSelected(row.symbol); setDetailsOpen(true) }}>
      <div className="featuredCardTop"><b className="featuredCardSymbol">{row.display}</b><strong className={`featuredCardBadge ${tone(row.direction)}`}>{row.direction}</strong></div>
      <div className="featuredCardScore"><b>{fmt(row.smart_score)}</b><small>/100</small></div>
      <div className="featuredCardMetrics">
        <span><small>Fiyat</small><b>{fmt(row.price)}<em className={row.change >= 0 ? 'positive' : 'negative'}>{row.change >= 0 ? ' +' : ' '}{fmt(row.change)}%</em></b></span>
        <span><small>RSI</small><b>{fmt(row.rsi)}</b></span>
        <span><small>Hacim</small><b>{row.volume_ratio >= 1 ? '↑' : '↓'} {fmt(Math.abs(row.volume_ratio - 1) * 100)}%</b></span>
      </div>
      <div className="featuredCardChips"><span className="featuredChip">R/R {fmt(row.risk_reward)}</span><span className="featuredChip">{row.trend}</span></div>
      <span className="featuredCardCta">Detayları gör</span>
    </button>)}</div></section>
    <div className="coinAnalysisLayout"><div className="coinTablePanel"><div className="coinTableMeta"><div><span>COINLER</span><b>{visible.length} sonuç</b></div><small>Smart Score DESC · tıklayarak detay aç</small></div><div className="coinTableScroll"><table><thead><tr>{[['symbol','COIN'],['price','PRICE'],['change','24H'],['direction','SIGNAL'],['smart_score','SCORE'],['rsi','RSI'],['trend','TREND'],['volume','VOLUME'],['risk_reward','R/R']].map(([key,label]) => <th key={key}><button type="button" onClick={() => chooseSort(key as keyof Row)}>{label}{sortIcon(key as keyof Row)}</button></th>)}</tr></thead><tbody>{visible.map(row => <tr key={row.symbol} className={row.symbol === selected ? 'selected' : ''} onClick={() => { setSelected(row.symbol); setDetailsOpen(true) }}><td><b>{row.display}</b></td><td>{fmt(row.price)}</td><td className={row.change >= 0 ? 'positive' : 'negative'}>{row.change >= 0 ? '+' : ''}{fmt(row.change)}%</td><td className={tone(row.direction)}><strong>{row.direction}</strong></td><td><strong className="scoreValue">{fmt(row.smart_score)}</strong></td><td>{fmt(row.rsi)}</td><td>{row.trend}</td><td>{fmt(row.volume)}</td><td>{fmt(row.risk_reward)}</td></tr>)}</tbody></table>{!loading && !visible.length && <div className="coinEmpty">{rows.length ? 'No matching coins found.' : 'Loading market data…'}</div>}{loading && <div className="coinEmpty">Scanning opportunities…</div>}</div></div>
      <aside className={`coinDetailPanel${detailsOpen ? ' mobileOpen' : ''}`}>{active ? <><div className="coinDetailTitle"><div><span>SEÇİLEN COIN · TECHNICAL VIEW</span><h3>{active.display}</h3><b>{fmt(active.price)} <em className={active.change >= 0 ? 'positive' : 'negative'}>{active.change >= 0 ? '+' : ''}{fmt(active.change)}%</em></b></div><div><strong className={tone(active.direction)}>{active.direction}</strong><button className="detailClose" type="button" onClick={() => setDetailsOpen(false)} aria-label="Detay panelini kapat"><X/></button></div></div><div className="marketContextRow"><span>MARKET CONTEXT</span><strong className={active.trend.includes('yükseliş') ? 'positive' : active.trend.includes('düş') ? 'negative' : 'neutral'}>{active.trend}</strong></div><div className="detailScore"><b>{fmt(active.smart_score)}<small>/100</small></b><span>Technical Signal Score · {scoreLabel(active.smart_score)}<em>not a probability</em></span></div><div className="coinDetailMetrics"><Metric label="RSI" value={fmt(active.rsi)}/><Metric label="TREND" value={active.trend}/><Metric label="R/R" value={fmt(active.risk_reward)}/><Metric label="VOLUME" value={`${active.volume_ratio >= 1 ? '+' : ''}${fmt((active.volume_ratio - 1) * 100)}%`}/><Metric label="RISK" value={`-${fmt(active.risk_pct)}%`}/><Metric label="POTENTIAL TP3" value={`+${fmt(active.potential_tp3_pct)}%`}/><Metric label="SUPPORT / RESISTANCE" value={`${fmt(active.support)} / ${fmt(active.resistance)}`}/></div><div className="tradePlan"><span className="tradePlanHeading">TRADE PLAN</span><div className="tradePlanRow entryTone"><span>ENTRY</span><b>{fmt(active.entry)}</b></div><div className="tradePlanRow stopTone"><span>STOP</span><b>{fmt(active.stop_loss)}</b></div><div className="tradePlanRow tpTone"><span>TP1</span><b>{fmt(active.tp1)}</b></div><div className="tradePlanRow tpTone"><span>TP2</span><b>{fmt(active.tp2)}</b></div><div className="tradePlanRow tpTone"><span>TP3</span><b>{fmt(active.tp3)}</b></div></div><div className="detailToggles"><label><input type="checkbox" checked={showLevels} onChange={event => setShowLevels(event.target.checked)}/><span>LEVELS</span></label><label><input type="checkbox" checked={showEma} onChange={event => setShowEma(event.target.checked)}/><span>EMA LINES</span></label></div><div className="coinDetailChart">{chart(active.symbol,interval,showLevels,showEma)}</div><div className="mtfRow"><b title="MTF = Multi-Timeframe trend agreement">MTF TREND</b>{(consensus?.timeframes || []).map(item => <span key={item.timeframe} className={tone(item.direction as Direction)}>{item.timeframe.toUpperCase()} {item.direction === 'LONG' ? '●' : item.direction === 'SHORT' ? '●' : '○'}</span>)}<em>{timeframeSummary}</em></div><section className="whySignal"><header><b>Bu sinyal neden oluştu?</b><small>Gerçek market verisinden türetildi</small></header><ul>{reasons.slice(0,6).map(reason => <li key={reason}><Check/>{reason}</li>)}</ul><button type="button" onClick={() => setDetailsOpen(true)}>Detayları göster</button></section></> : <div className="coinEmpty">Coin seçildiğinde detay burada açılır.</div>}</aside></div>
    <section className="coinToolsGrid"><article className="coinHistoryPanel"><header className="coinToolsHeader"><h3>Sinyal geçmişi</h3><p className="coinToolsSubtitle">Son sinyaller · {performance?.available ? `${performance.total_signals} sonuç` : 'Gerçek kayıtlar'}</p></header>{history.length ? <><div className="historyList">{(historyExpanded ? history : history.slice(0,5)).map(item => <div className="historyItem" key={item.id}><b className={`historyDirection ${tone(item.signal)}`}>{item.signal}</b><span className="historySymbol">{item.symbol.replace('USDT','/USDT')} · {fmt(item.entry)}</span><strong className="historyScore">Skor {fmt(item.score)}</strong><em className="historyStatus">{item.status}</em></div>)}</div>{history.length > 5 && <button type="button" className="historyShowAll" onClick={() => setHistoryExpanded(value => !value)}>{historyExpanded ? 'Daha az göster' : `Tümünü gör (${history.length})`}</button>}{performance?.available ? <div className="performanceStrip"><span>Total {performance.total_signals}</span><span>TP1 {performance.tp1_hit_rate}%</span><span>TP2 {performance.tp2_hit_rate}%</span><span>STOP {performance.stop_rate}%</span><span>R/R {performance.average_risk_reward}</span></div> : <small className="notEnough">Not enough historical data</small>}</> : <div className="coinEmpty">No historical signals yet.</div>}</article><article className="coinPaperPanel"><h3>Sanal cüzdan</h3><b className="paperBalance">—</b><p>Gerçek emir gönderilmez, paper pozisyon mevcut fiyatla takip edilir.</p><button type="button" className="paperTradeCta" onClick={() => setPaperOpen(true)} disabled={!active}>Paper trade</button></article></section>
    <div className="coinMobileCards">{visible.map(row => <button type="button" key={row.symbol} className={row.symbol === selected ? 'selectedRow' : ''} onClick={() => { setSelected(row.symbol); setDetailsOpen(true) }}><div><b>{row.display}</b><strong className={tone(row.direction)}>{row.direction}</strong></div><b className="mobileScore">{fmt(row.smart_score)}</b><div><span>{fmt(row.price)}</span><em className={row.change >= 0 ? 'positive' : 'negative'}>{row.change >= 0 ? '+' : ''}{fmt(row.change)}%</em></div><div><span>RSI {fmt(row.rsi)}</span><span>Volume {row.volume_ratio >= 1 ? '↑' : '↓'}{fmt(Math.abs(row.volume_ratio - 1) * 100)}%</span><span>R/R {fmt(row.risk_reward)}</span><span>{row.trend}</span></div><small>DETAYLARI GÖR</small></button>)}{!visible.length && <div className="coinEmpty">{loading ? 'Scanning opportunities…' : rows.length ? 'No matching coins found.' : 'Loading market data…'}</div>}</div>
    {alertOpen && <div className="featureBackdrop" role="presentation" onClick={event => { if (event.target === event.currentTarget) setAlertOpen(false) }}><div className="featureDrawer" role="dialog" aria-modal="true" aria-label="Create alert"><header><div><span>ALERT SYSTEM</span><h3>Create Alert · {active?.display}</h3></div><button type="button" aria-label="Alarm panelini kapat" onClick={() => setAlertOpen(false)}><X/></button></header><label>Signal<select value={alertDraft.signal} onChange={event => setAlertDraft({...alertDraft,signal:event.target.value})}><option value="ANY">ANY</option><option value="LONG">LONG</option><option value="SHORT">SHORT</option></select></label><label>Smart Score ≥ <input type="number" min="0" max="100" value={alertDraft.score_min} onChange={event => setAlertDraft({...alertDraft,score_min:Number(event.target.value)})}/></label><label>RSI ≥ <input type="number" min="0" max="100" value={alertDraft.rsi_min} onChange={event => setAlertDraft({...alertDraft,rsi_min:Number(event.target.value)})}/></label><label><input type="checkbox" checked={alertDraft.volume_spike} onChange={event => setAlertDraft({...alertDraft,volume_spike:event.target.checked})}/> Volume spike</label><label><input type="checkbox" checked={alertDraft.price_crosses_ema20} onChange={event => setAlertDraft({...alertDraft,price_crosses_ema20:event.target.checked})}/> Price above EMA20</label><label>MTF<select value={alertDraft.mtf} onChange={event => setAlertDraft({...alertDraft,mtf:event.target.value})}><option value="ANY">Any</option><option value="BULLISH_3">3/4 bullish</option><option value="BULLISH_4">4/4 bullish</option><option value="BEARISH_3">3/4 bearish</option><option value="BEARISH_4">4/4 bearish</option></select></label><button className="applyFeature" type="button" onClick={() => void createAlert()}>CREATE ALERT</button></div></div>}
    {analystOpen && <div className="featureBackdrop" role="presentation" onClick={event => { if (event.target === event.currentTarget) setAnalystOpen(false) }}><div className="featureDrawer analystDrawer" role="dialog" aria-modal="true" aria-label="ProTreBot Analyst"><header><div><span>PROTREBOT ANALYST</span><h3>Market Intelligence</h3></div><button type="button" aria-label="Analyst panelini kapat" onClick={() => setAnalystOpen(false)}><X/></button></header><p className="contextNote">Analyst yalnızca mevcut scanner, MTF, RSI, EMA, volume ve R/R verisini kullanır.</p><div className="analystChips">{analystPrompts.map(prompt => <button type="button" className={analystSelection === prompt ? 'active' : ''} aria-pressed={analystSelection === prompt} key={prompt} onClick={() => runAnalyst(prompt,prompt)}>{prompt}</button>)}</div>{analystAnswer && active ? <div className="analystResult"><div className="analystResultMain"><div className="analystCoinHead"><b>{active.display}</b><strong className={tone(active.direction)}>{active.direction}</strong></div><p className="analystResponseText">{analystAnswer}</p>{whySignal.length > 0 && <div className="whySignalBlock"><span className="whySignalTitle">WHY THIS SIGNAL</span><ul className="whySignalList">{whySignal.map(item => <li key={item.label}><Check/>{item.label}</li>)}</ul></div>}</div><aside className="signalSummary"><span className="signalSummaryTitle">SIGNAL SUMMARY</span><div className="signalSummaryRow"><span>Direction</span><b className={tone(active.direction)}>{active.direction}</b></div><div className="signalSummaryRow"><span>Signal Score</span><b>{fmt(active.smart_score)}</b></div><div className="signalSummaryRow"><span>RSI</span><b>{fmt(active.rsi)}</b></div><div className="signalSummaryRow"><span>MTF</span><b>{fmt(active.mtf_alignment)}</b></div><div className="signalSummaryRow"><span>R/R</span><b>{fmt(active.risk_reward)}</b></div></aside></div> : <p className="analystEmpty">No market analysis available yet. Select a question to get started.</p>}</div></div>}
    {paperOpen && active && <div className="featureBackdrop" role="presentation" onClick={event => { if (event.target === event.currentTarget) setPaperOpen(false) }}><div className="featureDrawer" role="dialog" aria-modal="true" aria-label="Paper Trade"><header><div><span>PAPER ONLY</span><h3>{active.display} Paper Trade</h3></div><button type="button" aria-label="Paper trade panelini kapat" onClick={() => setPaperOpen(false)}><X/></button></header><div className="paperPlanGrid"><Metric label="SIGNAL" value={active.direction}/><Metric label="ENTRY" value={fmt(active.entry)}/><Metric label="STOP" value={fmt(active.stop_loss)}/><Metric label="TP1" value={fmt(active.tp1)}/><Metric label="TP2" value={fmt(active.tp2)}/><Metric label="TP3" value={fmt(active.tp3)}/></div><button className="applyFeature" type="button" disabled={paperBusy} onClick={() => void openPaperTrade()}>{paperBusy ? 'OPENING…' : 'OPEN PAPER POSITION'}</button></div></div>}
    {filterOpen && <div className="filterBackdrop" role="presentation" onClick={event => { if (event.target === event.currentTarget) setFilterOpen(false) }}><div className="filterDrawer" role="dialog" aria-modal="true" aria-label="Coin filtreleri"><header><div><span>SMART FILTERS</span><h3>Filtreler</h3></div><button type="button" onClick={() => setFilterOpen(false)} aria-label="Filtreleri kapat"><X/></button></header><div className="filterGroup"><b>SIGNAL</b><div>{[['ALL','All'],['LONG','LONG'],['SHORT','SHORT'],['BEKLE','NEUTRAL']].map(([value,label]) => <button type="button" key={value} className={draftFilters.signal === value ? 'active' : ''} onClick={() => setDraftFilters({...draftFilters,signal:value})}>{label}</button>)}</div></div><div className="filterGroup"><b>STRENGTH</b><div>{[['ANY','Any'],['MODERATE','Moderate+'],['STRONG','Strong+'],['VERY_STRONG','Very Strong']].map(([value,label]) => <button type="button" key={value} className={draftFilters.strength === value ? 'active' : ''} onClick={() => setDraftFilters({...draftFilters,strength:value})}>{label}</button>)}</div></div><div className="filterGroup"><b>TREND</b><div>{[['ALL','All'],['BULLISH','Bullish'],['BEARISH','Bearish'],['NEUTRAL','Neutral']].map(([value,label]) => <button type="button" key={value} className={draftFilters.trend === value ? 'active' : ''} onClick={() => setDraftFilters({...draftFilters,trend:value})}>{label}</button>)}</div></div><div className="filterGroup"><b>RSI · {draftFilters.rsiMin}–{draftFilters.rsiMax}</b><div className="rangeRow"><input type="range" min="0" max="100" value={draftFilters.rsiMin} onChange={event => setDraftFilters({...draftFilters,rsiMin:Math.min(Number(event.target.value),draftFilters.rsiMax)})}/><input type="range" min="0" max="100" value={draftFilters.rsiMax} onChange={event => setDraftFilters({...draftFilters,rsiMax:Math.max(Number(event.target.value),draftFilters.rsiMin)})}/></div></div><div className="filterGroup"><b>VOLUME</b><div>{[['ANY','Any'],['INCREASING','Increasing'],['STRONG','Strong increase']].map(([value,label]) => <button type="button" key={value} className={draftFilters.volume === value ? 'active' : ''} onClick={() => setDraftFilters({...draftFilters,volume:value})}>{label}</button>)}</div></div><div className="filterChecks"><label><input type="checkbox" checked={draftFilters.ema20} onChange={event => setDraftFilters({...draftFilters,ema20:event.target.checked})}/> Price above EMA20</label><label><input type="checkbox" checked={draftFilters.ema20_50} onChange={event => setDraftFilters({...draftFilters,ema20_50:event.target.checked})}/> EMA20 &gt; EMA50</label><label><input type="checkbox" checked={draftFilters.ema50_200} onChange={event => setDraftFilters({...draftFilters,ema50_200:event.target.checked})}/> EMA50 &gt; EMA200</label></div><label className="rrField"><b>Minimum R/R</b><input type="number" min="0" step="0.1" value={draftFilters.minRR} onChange={event => setDraftFilters({...draftFilters,minRR:Number(event.target.value)})}/></label><footer><button type="button" onClick={clearFilters}>TEMİZLE</button><button type="button" className="applyFilters" onClick={applyFilters}>FİLTRELERİ UYGULA</button></footer></div></div>}
  </section>
}
