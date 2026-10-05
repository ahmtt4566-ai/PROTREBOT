import {useCallback, useEffect, useRef, useState} from 'react'
import {accountRequest, TRADING_TIMEFRAMES, type TradingPreferences} from './account-settings-api'

function readPreferences(value: unknown): Partial<TradingPreferences> {
  if (value === null || value === undefined) return {}
  if (typeof value !== 'object' || Array.isArray(value)) throw new Error('Kaydedilmiş işlem tercihleri geçersiz.')
  const row = value as Record<string, unknown>
  const result: Partial<TradingPreferences> = {}
  if (row.trading_mode !== undefined) {
    if (row.trading_mode !== 'MANUAL' && row.trading_mode !== 'AUTO') throw new Error('Kaydedilmiş işlem bölümü geçersiz.')
    result.trading_mode = row.trading_mode
  }
  if (row.timeframe !== undefined) {
    if (typeof row.timeframe !== 'string' || !TRADING_TIMEFRAMES.some(value => value === row.timeframe)) throw new Error('Kaydedilmiş zaman dilimi geçersiz.')
    result.timeframe = row.timeframe
  }
  if (row.risk_per_trade !== undefined) {
    if (typeof row.risk_per_trade !== 'number' || !Number.isFinite(row.risk_per_trade) || row.risk_per_trade < 0.1 || row.risk_per_trade > 1) throw new Error('Kaydedilmiş risk tercihi geçersiz.')
    result.risk_per_trade = row.risk_per_trade
  }
  if (row.exchange !== undefined) {
    if (row.exchange !== 'BINANCE') throw new Error('Kaydedilmiş borsa desteklenmiyor.')
    result.exchange = row.exchange
  }
  if (row.symbols !== undefined) {
    if (!Array.isArray(row.symbols) || !row.symbols.every(symbol => typeof symbol === 'string' && /^[A-Z0-9]{1,30}USDT$/.test(symbol))) throw new Error('Kaydedilmiş semboller geçersiz.')
    result.symbols = row.symbols
  }
  return result
}

export function useTradingPreferences(enabled = true) {
  const [preferences, setPreferences] = useState<Partial<TradingPreferences> | null>(null)
  const [error, setError] = useState('')
  const touched = useRef(false)
  const untouched = useCallback(() => !touched.current, [])
  useEffect(() => {
    if (!enabled) return
    let controller = new AbortController()
    const interact = () => {touched.current = true}
    document.addEventListener('pointerdown', interact, true)
    document.addEventListener('keydown', interact, true)
    const load = async () => {
      controller.abort()
      controller = new AbortController()
      const current = controller
      try {
        const result = await accountRequest<{profile?: {preferences?: unknown} | null}>('/profile', {signal: current.signal})
        const next = readPreferences(result.profile?.preferences)
        if (!current.signal.aborted) {setPreferences(next); setError('')}
      } catch (failure) {
        if (!current.signal.aborted) {setPreferences(null); setError(failure instanceof Error ? failure.message : 'İşlem tercihleri alınamadı.')}
      }
    }
    void load()
    const refresh = () => {void load()}
    window.addEventListener('protrebot-preferences-refresh', refresh)
    return () => {controller.abort(); document.removeEventListener('pointerdown', interact, true); document.removeEventListener('keydown', interact, true); window.removeEventListener('protrebot-preferences-refresh', refresh)}
  }, [enabled])
  return {preferences, error, untouched}
}
