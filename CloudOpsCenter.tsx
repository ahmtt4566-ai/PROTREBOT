import { useEffect, useMemo, useState } from 'react'
import {
  Activity, AlertTriangle, Bot, CheckCircle2, Cloud, CloudCog,
  Database, History, RefreshCw, ShieldCheck, TimerReset, WalletCards,
} from 'lucide-react'
import { API_BASE } from './api'


type Position = {
  symbol:string;direction:string;quantity:number;entry_price:number;mark_price:number;
  unrealized_pnl:number;leverage?:number;margin_type?:string;liquidation_price?:number
}
type EvidenceEvent = {
  id?:string;created_at?:string;kind?:string;symbol?:string;message?:string;
  side?:string;status?:string;realized_pnl?:number;reason?:string;source?:string
}
type Gate = {name:string;passed:boolean;value:string|number;target:string|number}
type Operations = {
  version:string;generated_at:string;
  deployment:{tier:string;always_on:boolean;database:string;uptime_seconds:number};
  testnet:{
    configured:boolean;connected:boolean;armed:boolean;
    stream:{status?:string;transport?:string;last_event?:string;last_error?:string};
    auto:{enabled?:boolean;busy?:boolean;cycles?:number;last_scan?:string;last_decision?:string;last_error?:string};
    account:{wallet_balance?:number;available_balance?:number;unrealized_pnl?:number;positions:Position[];open_orders:unknown[];open_algo_orders:unknown[]};
    daily:{entries:number;last_decision?:string;last_scan?:string};
  };
  evidence:{
    status:string;persistent:boolean;restored:boolean;count:number;last_sync?:string;last_error?:string;
    events:EvidenceEvent[];certificate:{status:string;score:number;passed_gates:number;total_gates:number;gates:Gate[]}
  };
  safety:{testnet_only:boolean;real_trading_locked:boolean;auto_resumes_after_restart:boolean;profit_guaranteed:boolean}
}

const money = (value?:number) => value == null ? '—' : value.toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:4})
const date = (value?:string) => value ? new Date(value).toLocaleString('tr-TR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—'
const duration = (seconds:number) => {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor(seconds % 86400 / 3600)
  const minutes = Math.floor(seconds % 3600 / 60)
  return days ? `${days}g ${hours}s` : hours ? `${hours}s ${minutes}dk` : `${minutes}dk`
}

async function detail(response:Response):Promise<string> {
  const payload = await response.json().catch(() => null) as {detail?:unknown}|null
  if (typeof payload?.detail === 'string') return payload.detail
  return `Sunucu ${response.status} yanıtı verdi.`
}

export default function CloudOpsCenter() {
  const [data,setData] = useState<Operations|null>(null)
  const [error,setError] = useState('')
  const [busy,setBusy] = useState(false)
  const [updated,setUpdated] = useState('—')

  const refresh = async (silent=false) => {
    if (!silent) setBusy(true)
    try {
      const response = await fetch(`${API_BASE}/v27/operations`)
      if (!response.ok) throw new Error(await detail(response))
      setData(await response.json() as Operations)
      setError('')
      setUpdated(new Date().toLocaleTimeString('tr-TR'))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Operasyon merkezi yüklenemedi.')
    } finally {if (!silent) setBusy(false)}
  }

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(true),5000)
    return () => window.clearInterval(timer)
  },[])

  const sync = async () => {
    setBusy(true)
    try {
      const response = await fetch(`${API_BASE}/v27/evidence/sync`,{method:'POST'})
      if (!response.ok) throw new Error(await detail(response))
      setData(await response.json() as Operations)
      setError('')
      setUpdated(new Date().toLocaleTimeString('tr-TR'))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Kalıcı kayıt eşitlenemedi.')
    } finally {setBusy(false)}
  }

  const positions = data?.testnet.account.positions || []
  const certificate = data?.evidence.certificate
  const pendingGate = useMemo(() => certificate?.gates.find(gate => !gate.passed),[certificate])

  if (!data) return <div className="v27Loading"><CloudCog className="spin"/><b>OPERASYON MERKEZİ HAZIRLANIYOR</b><span>Sunucu bağlantısı bekleniyor.</span><button onClick={() => refresh()}>YENİDEN DENE</button></div>

  return <section className="v27Ops">
    <header className="v27OpsHero">
      <div className="v27OpsTitle"><span><Cloud/></span><div><small>OPERASYON VE KANIT MERKEZİ</small><h2>Bulut Operasyon ve Kanıt Merkezi</h2><p>Botun ne yaptığını, neden beklediğini ve Testnet kayıtlarının kalıcı olup olmadığını tek ekranda izleyin.</p></div></div>
      <div className="v27OpsActions"><div><i/><span>SON CANLI YENİLEME</span><b>{updated}</b></div><button onClick={() => refresh()} disabled={busy}><RefreshCw className={busy ? 'spin' : ''}/>YENİLE</button><button className="sync" onClick={sync} disabled={busy || !data.evidence.persistent}><Database/>KANITI ŞİMDİ KAYDET</button></div>
    </header>

    {error && <div className="v27Error"><AlertTriangle/><b>Sunucu bağlantısı bekleniyor</b><span>Veriler güncellenemiyor.</span></div>}
    {!data.deployment.always_on && <div className="v27SleepNotice"><TimerReset/><div><b>ÖNİZLEME SUNUCUSU UYUYABİLİR</b><span>{data.deployment.tier} katmanında servis boşta durabilir; Testnet otomasyonunun 7/24 taraması için sürekli çalışan sunucu gerekir.</span></div><em>İŞLEM DEĞİL · ALTYAPI UYARISI</em></div>}

    <div className="v27Pulse">
      <article className={data.testnet.configured ? 'ok' : 'wait'}><ShieldCheck/><small>DEMO ANAHTARI</small><b>{data.testnet.configured ? 'RENDER KASASINDA' : 'ANAHTAR BEKLİYOR'}</b><span>Değerler bu ekranda ve veritabanında tutulmaz.</span></article>
      <article className={data.testnet.connected ? 'ok' : 'wait'}><Activity/><small>BINANCE TESTNET</small><b>{data.testnet.connected ? 'BAĞLI' : 'BAĞLANTI BEKLİYOR'}</b><span>{data.testnet.stream.last_error || `${data.testnet.stream.status || 'BEKLEMEDE'} · ${data.testnet.stream.transport || '—'}`}</span></article>
      <article className={data.testnet.auto.enabled ? 'hot' : 'wait'}><Bot/><small>OTONOM TARAMA</small><b>{data.testnet.auto.enabled ? 'ÇALIŞIYOR' : 'KAPALI'}</b><span>{data.testnet.auto.cycles || 0} tur · {date(data.testnet.auto.last_scan)}</span></article>
      <article className={data.evidence.persistent ? 'ok' : 'wait'}><Database/><small>KALICI KANIT</small><b>{data.evidence.status}</b><span>{data.evidence.count} olay · {date(data.evidence.last_sync)}</span></article>
      <article className={positions.length ? 'hot' : 'ok'}><WalletCards/><small>AÇIK TESTNET POZİSYONU</small><b>{positions.length} ADET</b><span>{data.testnet.account.open_orders.length} normal · {data.testnet.account.open_algo_orders.length} koruma emri</span></article>
      <article className="locked"><ShieldCheck/><small>GERÇEK PARA</small><b>KİLİTLİ</b><span>Bu operasyon ekranı gerçek emir açmaz.</span></article>
    </div>

    <div className="v27Metrics">
      <article><small>TESTNET CÜZDAN</small><b>{money(data.testnet.account.wallet_balance)} <em>USDT</em></b><span>Kullanılabilir {money(data.testnet.account.available_balance)}</span></article>
      <article><small>AÇIK PnL</small><b className={(data.testnet.account.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{(data.testnet.account.unrealized_pnl || 0) >= 0 ? '+' : ''}{money(data.testnet.account.unrealized_pnl)} <em>USDT</em></b><span>Gerçekleşmemiş Testnet sonucu</span></article>
      <article><small>BUGÜNKÜ OTONOM GİRİŞ</small><b>{data.testnet.daily.entries}</b><span>Son tarama {date(data.testnet.daily.last_scan)}</span></article>
      <article><small>SUNUCU ÇALIŞMA SÜRESİ</small><b>{duration(data.deployment.uptime_seconds)}</b><span>Yeniden başlatmada sayaç sıfırlanır</span></article>
      <article><small>DEMO KANIT SKORU</small><b>%{certificate?.score || 0}</b><span>{certificate?.passed_gates || 0}/{certificate?.total_gates || 0} kapı geçti</span></article>
    </div>

    <div className="v27Grid">
      <section className="v27Panel v27Decision">
        <header><div><small>BOT NEDEN İŞLEM AÇTI / AÇMADI?</small><h3>Canlı Karar Monitörü</h3></div><span className={data.testnet.auto.enabled ? 'running' : ''}><i/>{data.testnet.auto.enabled ? 'TARIYOR' : 'ONAY BEKLİYOR'}</span></header>
        <div className="v27DecisionBody"><Bot/><div><small>SON KARAR</small><b>{data.testnet.auto.last_decision || 'Henüz karar yok.'}</b><span>{data.testnet.auto.last_error ? `Son hata: ${data.testnet.auto.last_error}` : 'Her yeni giriş güven, volatilite, korelasyon, günlük kayıp ve pozisyon limitinden geçer.'}</span></div></div>
        <footer><span><i/>Emir kilidi: <b>{data.testnet.armed ? 'AÇIK' : 'KAPALI'}</b></span><span><i/>Otomasyon yeniden başlatmada: <b>KAPALI BAŞLAR</b></span><span><i/>Kâr garantisi: <b>YOK</b></span></footer>
      </section>

      <section className="v27Panel v27Certificate">
        <header><div><small>30 GÜN / 100 KAPANIŞ / TATBİKAT</small><h3>Testnet Kanıt Sertifikası</h3></div><strong>%{certificate?.score || 0}</strong></header>
        <div className="v27Progress"><i style={{width:`${certificate?.score || 0}%`}}/></div>
        <div className="v27Gates">{certificate?.gates.map(gate => <article key={gate.name} className={gate.passed ? 'passed' : ''}>{gate.passed ? <CheckCircle2/> : <History/>}<div><b>{gate.name}</b><span>{String(gate.value)} / hedef {String(gate.target)}</span></div></article>)}</div>
        <footer>{pendingGate ? <><AlertTriangle/><span>Sıradaki kapı: <b>{pendingGate.name}</b></span></> : <><CheckCircle2/><span>Bütün Testnet kanıt kapıları geçti. Bu yine de kâr veya canlı para uygunluğu garantisi değildir.</span></>}</footer>
      </section>
    </div>

    <section className="v27Panel v27Positions">
      <header><div><small>BINANCE FUTURES DEMO · CANLI EŞLEŞTİRME</small><h3>Açık Pozisyon ve Koruma Haritası</h3></div><span>{positions.length} AÇIK</span></header>
      {positions.length ? <div className="v27PositionTable"><div className="head"><span>PARİTE / YÖN</span><span>GİRİŞ</span><span>MARK</span><span>MİKTAR</span><span>KALDIRAÇ</span><span>LİKİDASYON</span><span>AÇIK PnL</span></div>{positions.map(position => <article key={position.symbol}><b>{position.symbol}<em className={position.direction === 'SHORT' ? 'short' : ''}>{position.direction}</em></b><span>{money(position.entry_price)}</span><span>{money(position.mark_price)}</span><span>{money(position.quantity)}</span><span>{position.leverage || '—'}x · {(position.margin_type || '—').toUpperCase()}</span><span>{money(position.liquidation_price)}</span><strong className={position.unrealized_pnl >= 0 ? 'positive' : 'negative'}>{position.unrealized_pnl >= 0 ? '+' : ''}{money(position.unrealized_pnl)} USDT</strong></article>)}</div> : <div className="v27Empty"><WalletCards/><b>Açık Testnet pozisyonu yok</b><span>Testnet Komuta bölümünde Demo kilidini açıp otomasyonu başlattığınızda pozisyonlar burada görünür.</span></div>}
    </section>

    <section className="v27Panel v27Ledger">
      <header><div><small>POSTGRESQL + CANLI TESTNET GÜNLÜĞÜ</small><h3>İşlem ve Karar Kanıt Defteri</h3></div><span>{data.evidence.count} KALICI OLAY</span></header>
      <div className="v27Events">{data.evidence.events.length ? data.evidence.events.slice(0,30).map((event,index) => <article key={event.id || `${event.created_at}-${index}`}><i/><div><b>{event.kind || 'OLAY'}{event.symbol ? ` · ${event.symbol}` : ''}</b><span>{event.message || event.reason || 'Testnet olayı kaydedildi.'}</span></div><em>{event.source || event.status || 'SYSTEM'}</em><time>{date(event.created_at)}</time></article>) : <div className="v27Empty"><History/><b>Henüz kanıt olayı yok</b><span>Bağlantı, tarama, emir ve koruma olayları burada oluşacak.</span></div>}</div>
    </section>
  </section>
}

