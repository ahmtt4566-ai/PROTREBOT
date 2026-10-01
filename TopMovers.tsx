import type { TopMover } from './use-top-movers'

type Props = {movers:TopMover[]}

function formatPrice(mover:TopMover) {
  return mover.symbol === 'USDTTRY' ? `₺${mover.price.toFixed(4)}` : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:4}).format(mover.price)
}

function FlameIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12.2 21c4.1 0 7.3-2.7 7.3-6.6 0-3.1-1.8-5.5-4.8-8.5.1 2.4-.7 3.5-1.8 4.2.2-3.3-1.4-6.1-4.1-8.1.2 3.5-3.6 5.7-3.6 10.1 0 4.9 3 8.9 7 8.9Zm0-2.2c-2.4 0-4.3-2.2-4.3-5.1 0-1.6.6-2.9 1.7-4.2.5 1.3 1.5 2.3 3.1 3 .8-1 1.3-2 1.3-3.4 1.7 2.1 2.8 3.7 2.8 5.5 0 2.5-1.9 4.2-4.6 4.2Z"/></svg>
}

export default function TopMovers({movers}:Props) {
  return <section className="topMovers" aria-label="En çok yükselenler"><header><h2><FlameIcon/> En çok yükselenler</h2><span>Son 24 saat</span></header><div className="topMoversScroller">{movers.length ? movers.map(mover => <article className="topMoverChip" key={mover.symbol}><b>{mover.symbol}</b><em>▲ {mover.changePercent.toFixed(2)}%</em><small>{formatPrice(mover)}</small></article>) : Array.from({length:5},(_,index) => <i className="topMoverSkeleton" key={index}/>)}</div></section>
}
