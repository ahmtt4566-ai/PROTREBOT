import type {SharedLiveStatus} from './LiveTradingPanel'

type Plan = NonNullable<SharedLiveStatus['plans']>[number]
type Props = {status: SharedLiveStatus; verified: boolean; findPlan: (position: Record<string, unknown>) => Plan | undefined}
const amount = (value: unknown) => typeof value === 'number' && Number.isFinite(value)
  ? `${value.toLocaleString('tr-TR', {minimumFractionDigits: 2, maximumFractionDigits: 2})} USDT` : '—'
const display = (value: unknown) => typeof value === 'string' || typeof value === 'number' ? String(value) : '—'

export default function OriginalLiveOverview({status, verified, findPlan}: Props) {
  const positions = status.account?.positions ?? []
  const day = status.original_v2_live?.daily
  const dayCurrent = day?.day === new Date().toISOString().slice(0, 10)
  const sharedLimit = status.policy?.daily_loss_limit
  const fixedLimit = status.original_v2_live?.risk_limits?.daily_loss_limit_usdt
  const limit = dayCurrent && typeof day?.limit === 'number' && Number.isFinite(day.limit) && day.limit > 0
    && typeof fixedLimit === 'number' && Number.isFinite(fixedLimit) && fixedLimit > 0
    && typeof sharedLimit === 'number' && Number.isFinite(sharedLimit) && sharedLimit > 0
    ? Math.min(day.limit, fixedLimit, sharedLimit) : null
  const loss = dayCurrent && typeof day?.loss === 'number' && Number.isFinite(day.loss) ? day.loss : null
  return <div className="originalOverview">
    <div className="originalMetricGrid">
      <article><small>Bakiye</small><strong>{verified ? amount(status.account?.wallet_balance) : '—'}</strong></article>
      <article><small>Bugünkü K/Z · gerçekleşen</small><strong>{verified ? amount(status.daily?.realized_pnl) : '—'}</strong></article>
      <article><small>Açık pozisyon</small><strong>{verified ? `${positions.length} / ${display(status.policy?.max_positions)}` : '—'}</strong></article>
      <article><small>Günlük kayıp sınırı</small><strong>{verified && loss !== null && limit !== null ? `${amount(loss)} / ${amount(limit)}` : '—'}</strong>
        {verified && loss !== null && limit !== null && <div role="progressbar" aria-label="Günlük kayıp sınırı kullanımı" aria-valuemin={0} aria-valuemax={limit} aria-valuenow={Math.min(limit, Math.max(0, loss))}><i style={{width: `${Math.min(100, Math.max(0, loss / limit * 100))}%`}}/></div>}
      </article>
    </div>
    <div className="originalOverviewGrid">
      <section className="originalSurface" aria-label="Açık pozisyonlar"><header><h3>Açık pozisyonlar</h3><small>{verified ? 'Binance verisi' : 'Doğrulama bekleniyor'}</small></header>
        {positions.length === 0 ? <p>{verified ? 'Şu an açık pozisyon yok. Sinyal gelince stop ve hedefleri burada görünecek.' : 'Güncel pozisyonlar doğrulanamadı.'}</p> : positions.map((position, index) => {
          const plan = findPlan(position)
          const external = !plan || plan.provenance_state === 'READ_ONLY_EXTERNAL' || plan.source === 'READ_ONLY_EXTERNAL'
          const protection = verified && !external && plan?.provenance_state === 'CONFIRMED' ? plan.exchange_protection : undefined
          return <article className="originalPosition" key={`${display(position.symbol)}-${index}`}>
            <header><strong>{display(position.symbol)} <span>{display(position.direction)}</span></strong><b>{verified ? amount(position.unrealized_pnl) : '—'}</b></header>
            <div><span>Giriş <b>{display(position.entry_price)}</b></span><span>Stop <b>{display(plan?.stop_loss)}</b><small>{protection?.stop === true ? 'Binance’te ✓' : 'Doğrulanamadı'}</small></span>
              <span>TP1 (%60) <b>{display(plan?.targets?.[0])}</b><small>{protection?.tp1 === true ? 'Binance’te ✓' : 'Doğrulanamadı'}</small></span>
              <span>TP3 <b>{display(plan?.targets?.[2])}</b><small>{protection?.tp3 === true ? 'Binance’te ✓' : 'Doğrulanamadı'}</small></span></div>
            {external && <p>Dış pozisyon · bot tarafından yönetilmiyor.</p>}
            {!external && plan?.provenance_state !== 'CONFIRMED' && <p>Plan sahipliği doğrulanamadı.</p>}
          </article>
        })}
      </section>
      <aside className="originalSurface" aria-label="Son olaylar"><h3>Son olaylar</h3>
        {!verified && <p>Son görülen kayıtlar; güncel durum doğrulanamadı.</p>}
        {(status.events ?? []).slice(0, 6).map((event, index) => <p key={`${event.created_at}-${index}`}><time>{event.created_at ? new Date(event.created_at).toLocaleTimeString('tr-TR', {hour: '2-digit', minute: '2-digit'}) : '—'}</time> {event.message ?? event.kind}</p>)}
        {!status.events?.length && <p>Henüz olay yok.</p>}
      </aside>
    </div>
  </div>
}
