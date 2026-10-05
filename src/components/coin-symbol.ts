import manifest from '../assets/coins/manifest.json' with {type: 'json'}
import logos from '../assets/coins/logo-manifest.json' with {type: 'json'}
import {coinBaseAsset} from '../../tools/coin-logo-symbol.mjs'

const coinNames = new Map(manifest.map(coin => [coin.symbol, coin.name]))
const generatedNames: Record<string, {name: string}> = logos.assets
export {coinBaseAsset}

export function coinDisplayName(symbol: string): string {
  const asset = coinBaseAsset(symbol)
  return asset === 'BTCDOM' ? 'Bitcoin dominance' : generatedNames[asset]?.name ?? coinNames.get(asset) ?? asset
}
