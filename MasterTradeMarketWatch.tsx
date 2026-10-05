import {useEffect, useLayoutEffect, useMemo, useRef, useState} from 'react'
import {Search, Star} from 'lucide-react'
import {useMemberAccess} from './premium-access'
import {CoinIcon} from './src/components/CoinIcon'
import {filterMarketQuotes, marketPrice, marketWindow, type MarketFilter, type MarketScore, type MarketSort} from './master-market-data'
import type {MasterMarketFeed} from './useMasterMarketQuotes'
import './master-market-watch.css'

const filters: MarketFilter[] = ['Tümü', 'Favoriler', 'Skorlu', 'Yükselenler', 'Düşenler']
const sorts: MarketSort[] = ['Skor', '24s %', 'Hacim', 'A-Z']
const favoriteIcon = <Star aria-hidden="true"/>

type Props = {symbol: string; query: string; onQuery: (value: string) => void; onMarket: (symbol: string) => void; scores: readonly MarketScore[]; feed: MasterMarketFeed}

export function MasterTradeMarketWatch(props: Props) {
  const {userId} = useMemberAccess()
  return <MarketWatch key={userId ?? 'anonymous'} userId={userId} {...props}/>
}

function MarketWatch({userId, symbol, query, onQuery, onMarket, scores, feed}: Props & {userId: string | null}) {
  const {rows, error, stale} = feed
  const [filter, setFilter] = useState<MarketFilter>('Tümü')
  const [sort, setSort] = useState<MarketSort>('Skor')
  const [search, setSearch] = useState(query)
  const [favorites, setFavorites] = useState<Set<string>>(new Set())
  const [preferenceError, setPreferenceError] = useState('')
  const [viewport, setViewport] = useState({offset: 0, extent: 520, horizontal: false})
  const scroller = useRef<HTMLDivElement>(null)
  const focusedSymbol = useRef<string | null>(null)
  const key = userId ? `protrebot:master-market-favorites:${userId}` : null
  const scoreMap = useMemo(() => new Map(scores.filter(row => Number.isFinite(row.score)).map(row => [row.symbol, row])), [scores])
  const filtered = useMemo(() => filterMarketQuotes(rows, scoreMap, favorites, search, filter, sort), [rows, scoreMap, favorites, search, filter, sort])
  const size = viewport.horizontal ? 260 : 52
  const window = marketWindow(viewport.offset, viewport.extent, filtered.length, size)

  useEffect(() => {
    const timeout = globalThis.setTimeout(() => setSearch(query), 150)
    return () => globalThis.clearTimeout(timeout)
  }, [query])
  useEffect(() => {
    if (!key) return
    try {
      const raw = localStorage.getItem(key)
      if (raw === null) return
      const payload: unknown = JSON.parse(raw)
      if (!Array.isArray(payload) || !payload.every(item => typeof item === 'string' && /^[A-Z0-9]+USDT$/.test(item))) throw new Error('Favori kayıt biçimi geçersiz.')
      setFavorites(new Set(payload))
    } catch (error: unknown) {
      console.error('Market favorite preferences could not be read', error)
      setPreferenceError(error instanceof Error ? error.message : 'Favoriler okunamadı.')
    }
  }, [key])
  useLayoutEffect(() => {
    const element = scroller.current
    if (!element) return
    const measure = () => {
      const horizontal = matchMedia('(max-width: 1279px)').matches
      setViewport({horizontal, offset: horizontal ? element.scrollLeft : element.scrollTop, extent: horizontal ? element.clientWidth : element.clientHeight})
    }
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    measure()
    return () => observer.disconnect()
  }, [])
  useLayoutEffect(() => {
    const element = scroller.current
    if (!element) return
    const index = filtered.findIndex(row => row.symbol === symbol)
    const current = viewport.horizontal ? element.scrollLeft : element.scrollTop
    let offset = current
    if (index >= 0) {
      if (index * size < current) offset = index * size
      else if ((index + 1) * size > current + viewport.extent) offset = Math.max(0, (index + 1) * size - viewport.extent)
    } else offset = 0
    if (viewport.horizontal) element.scrollLeft = offset
    else element.scrollTop = offset
    setViewport(current => ({...current, offset}))
  }, [symbol, search, filter, sort, filtered.length, size, viewport.extent])
  useLayoutEffect(() => {
    const element = scroller.current
    const active = document.activeElement
    if (element?.contains(active) && focusedSymbol.current && active?.closest('.refMarketRow')?.getAttribute('data-symbol') !== focusedSymbol.current) element.focus({preventScroll: true})
  }, [filtered, window.start, window.end])
  const toggleFavorite = (symbol: string) => {
    if (!key) return
    const next = new Set(favorites)
    if (next.has(symbol)) next.delete(symbol)
    else next.add(symbol)
    try {
      localStorage.setItem(key, JSON.stringify([...next]))
      setFavorites(next)
      setPreferenceError('')
    } catch (error: unknown) {
      console.error('Market favorite preferences could not be saved', error)
      setPreferenceError(error instanceof Error ? error.message : 'Favoriler kaydedilemedi.')
    }
  }
  return <aside className="refMarkets refMarketUniverse" aria-label="Piyasa listesi">
    <section className="refCard refMarketSearch">
      <h2>PİYASA LİSTESİ <span>{filter} · {filtered.length}</span></h2>
      {rows.some(row => row.test) && <p className="refMarketDataNotice" role="note">OFFLINE TEST · TOKEN satırları sentetiktir, canlı sembol değildir.</p>}
      <label><Search/><input aria-label="Search markets" placeholder="Sembol veya isim..." value={query} onChange={event => onQuery(event.target.value)}/></label>
      <div className="refMarketFilters" role="group" aria-label="Piyasa filtreleri">{filters.map(item => <button type="button" key={item} aria-pressed={filter === item} onClick={() => setFilter(item)}>{item}</button>)}</div>
      <label className="refMarketSort">Sıralama<select aria-label="Piyasa sıralaması" value={sort} onChange={event => {const value = event.target.value; const next = sorts.find(item => item === value); if (next) setSort(next)}}>{sorts.map(item => <option key={item}>{item}</option>)}</select></label>
      {(error || stale) && <p className="refMarketDataNotice" role="status">{error || 'Son kayıt gösteriliyor · Piyasa verisi güncel değil.'}</p>}
      {preferenceError && <p className="refMarketDataNotice" role="alert">{preferenceError}</p>}
    </section>
    <section className="refCard refWatchlist refVirtualWatchlist">
      <h2>İZLEME LİSTESİ <span title="Seçili sembol">{symbol}</span></h2>
      <div className="refMarketViewport" ref={scroller} tabIndex={0} aria-label="Piyasa sembolleri" onFocusCapture={event => {focusedSymbol.current = event.target.closest('.refMarketRow')?.getAttribute('data-symbol') ?? null}} onScroll={event => {
        const element = event.currentTarget
        const offset = viewport.horizontal ? element.scrollLeft : element.scrollTop
        const next = marketWindow(offset, viewport.extent, filtered.length, size)
        if (next.start !== window.start && element.contains(document.activeElement) && document.activeElement?.closest('.refMarketRow')) element.focus({preventScroll: true})
        setViewport(current => ({...current, offset}))
      }}>
        {!filtered.length ? <p className="refMarketEmpty">Sonuç bulunamadı</p> : <div className="refMarketCanvas" style={viewport.horizontal ? {width: filtered.length * size, height: 52} : {height: filtered.length * size}}>
          {filtered.slice(window.start, window.end).map((row, index) => {
            const position = (index + window.start) * size
            const score = scoreMap.get(row.symbol)
            return <div key={index} className={`refMarketRow${row.symbol === symbol ? ' selected' : ''}`} data-symbol={row.symbol} style={viewport.horizontal ? {left: 0, top: 0, width: size, transform: `translateX(${position}px)`} : {top: 0, left: 0, right: 0, transform: `translateY(${position}px)`}}>
              <button type="button" className="refMarketSelect" aria-pressed={row.symbol === symbol} onClick={() => onMarket(row.symbol)}>
                <CoinIcon symbol={row.symbol} size={32}/>
                <span className="refMarketIdentity"><b title={row.symbol}>{row.symbol}</b><small title={row.display}>{row.test ? 'TEST · ' : ''}{row.display}</small><span className="refMarketScore">{score ? <><i data-tone={score.direction === 'LONG' ? 'positive' : score.direction === 'SHORT' ? 'negative' : 'neutral'}>{['LONG', 'SHORT'].includes(score.direction) ? score.direction : '—'}</i><em>{score.score.toLocaleString('en-US', {maximumFractionDigits: 1})}</em></> : '—'}</span></span>
                <span className="refMarketPrice"><strong>{marketPrice(row.price)}</strong><small data-tone={row.change !== null ? row.change >= 0 ? 'positive' : 'negative' : 'neutral'}>{row.change !== null ? `${row.change >= 0 ? '+' : ''}${row.change.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}%` : '—'}</small></span>
              </button>
              <button type="button" className="refMarketFavorite" aria-label={`${row.symbol} favori`} aria-pressed={favorites.has(row.symbol)} disabled={!userId} onClick={() => toggleFavorite(row.symbol)}>{favoriteIcon}</button>
            </div>
          })}
        </div>}
      </div>
    </section>
  </aside>
}
