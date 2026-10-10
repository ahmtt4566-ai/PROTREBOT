import './demo-info-cards.css'

type DemoInfoProps = {
  account: {
    wallet_balance:number
    available_balance:number
    unrealized_pnl:number
    reconciliation?:{reconciled_active_positions?:number}
  }|null
  summary: {
    stream:{status:string}
    auto:{enabled:boolean}
    daily:{auto_entries:number;remaining_loss_budget:number}
    settings:{daily_trade_limit:number}
    certificate:{score:number}
  }|null
  maxPositions?:number
  format:(value:number|undefined)=>string
}

export default function DemoInfoCards({account,summary,maxPositions,format}:DemoInfoProps) {
  const pnl = account?.unrealized_pnl
  const cards = [
    {label:'Bakiye',value:account ? `${format(account.wallet_balance)} USDT` : '—'},
    {label:'Kullanılabilir',value:account ? `${format(account.available_balance)} USDT` : '—'},
    {label:'Gerçekleşmemiş K/Z',value:pnl === undefined ? '—' : `${pnl >= 0 ? '+' : ''}${format(pnl)} USDT`,tone:pnl === undefined ? '' : pnl >= 0 ? 'demoProfit' : 'demoLoss'},
    {label:'Açık Pozisyon',value:account ? `${account.reconciliation?.reconciled_active_positions ?? 0} / ${maxPositions ?? '—'}` : '—',detail:`Max ${maxPositions ?? '—'}`},
    {label:'Akış',value:summary?.stream.status || '—'},
    {label:'Otomasyon',value:summary ? summary.auto.enabled ? 'ÇALIŞIYOR' : 'KAPALI' : '—'},
    {label:'Günlük Demo',value:summary ? `${summary.daily.auto_entries} / ${summary.settings.daily_trade_limit}` : '—'},
    {label:'Risk Bütçesi',value:summary ? `${format(summary.daily.remaining_loss_budget)} USDT` : '—'},
    {label:'Demo Kanıt',value:summary ? `%${summary.certificate.score}` : '—'},
  ]

  return <section className="demoInfoGrid" aria-label="Demo hesap ve durum özeti">
    {cards.map(card => <article className="demoInfoCard" key={card.label}>
      <small>{card.label}</small>
      <b className={card.tone}>{card.value}</b>
      {card.detail && <span>{card.detail}</span>}
    </article>)}
  </section>
}
