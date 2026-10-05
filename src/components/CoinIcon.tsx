import {useEffect, useState, type CSSProperties} from 'react'
import {coinBaseAsset} from './coin-symbol'
import {coinAvatarColor, coinLogoCandidates} from '../../tools/coin-logo-symbol.mjs'
import catalog from '../assets/coins/logo-manifest.json' with {type: 'json'}
import overrides from '../assets/coins/logo-overrides.json' with {type: 'json'}

const loaders = import.meta.glob<string>('../assets/coins/**/*.{svg,webp}', {query: '?inline', import: 'default'})
const icons = new Map<string, string>()
const pending = new Map<string, Promise<string>>()

export function CoinIcon({symbol, size = 32}: {symbol: string; size?: number}) {
  const asset = coinBaseAsset(symbol)
  const [loaded, setLoaded] = useState<{file: string; url: string} | null>(null)
  const [failed, setFailed] = useState<ReadonlySet<string>>(new Set())
  const candidate = coinLogoCandidates(symbol, catalog.assets, overrides).find(candidate => !failed.has(candidate.file) && loaders[`../assets/coins/${candidate.file}`])
  const file = candidate?.file
  const icon = file ? icons.get(file) ?? (loaded?.file === file ? loaded.url : undefined) : undefined
  const fail = (file: string, error: unknown) => {
    console.error(`Local coin logo failed: ${file}`, error)
    setFailed(current => new Set([...current, file]))
  }
  useEffect(() => {
    let active = true
    const loader = file ? loaders[`../assets/coins/${file}`] : undefined
    if (file && !icons.has(file) && loader) {
      let promise = pending.get(file)
      if (!promise) {
        promise = loader().then(url => {icons.set(file, url); return url}).finally(() => pending.delete(file))
        pending.set(file, promise)
      }
      void promise.then(url => {if (active) setLoaded({file, url})}).catch((error: unknown) => {if (active) fail(file, error)})
    }
    return () => {active = false}
  }, [file])
  const style: CSSProperties = {width: size, height: size, flexShrink: 0, borderRadius: '50%', display: 'block'}
  if (icon && candidate) return <img key={candidate.file} className="coinIcon" data-asset={asset} data-logo-asset={candidate.logoAsset} data-logo-source={candidate.source} title={candidate.note} src={icon} loading="lazy" alt="" aria-hidden="true" width={size} height={size} style={style} onError={() => fail(candidate.file, new Error('Image decoding failed'))}/>
  return <svg className="coinIcon" data-asset={asset} data-fallback viewBox="0 0 32 32" aria-hidden="true" style={style}><circle cx="16" cy="16" r="15" fill={coinAvatarColor(symbol)}/><text x="16" y="16" dominantBaseline="central" textAnchor="middle" fill="white" fontSize="16" fontWeight="700">{Array.from(asset)[0] ?? '?'}</text></svg>
}
