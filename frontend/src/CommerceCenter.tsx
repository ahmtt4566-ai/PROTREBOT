import { useEffect, useMemo, useState } from 'react'
import { BadgeDollarSign, Building2, CheckCircle2, CircleDashed, LifeBuoy, ReceiptText, Rocket, Save, Send, ShieldCheck, ShoppingBag, UserPlus } from 'lucide-react'
import { API_BASE } from './api'

const API = `${API_BASE}/v22/commerce`

type Plan = {name:string;monthly_usd:number;days:number;agents:number;bots:number;features:string[]}
type Business = {brand_name:string;legal_name:string;support_email:string;website_url:string;currency:'USD'|'EUR'|'TRY'|'USDT';trial_days:number;terms_version:string;country:string;payment_provider:string;checkout_live:boolean;card_data_collected:boolean}
type Lead = {id:string;name:string;email:string;company:string;interested_plan:string;note:string;status:string;created_at:string}
type DemoInvoice = {id:string;customer_name:string;plan:string;plan_name:string;months:number;subtotal:number;discount:number;tax:number;total:number;currency:string;payment_status:string;created_at:string}
type Ticket = {id:string;subject:string;message:string;priority:string;status:string;created_at:string;customer?:{display_name:string;email:string}}
type Checklist = {key:string;label:string;passed:boolean;detail:string}
type CommerceOverview = {business:Business;plans:Record<string,Plan>;leads:Lead[];invoices:DemoInvoice[];support_tickets:Ticket[];funnel:Record<string,number>;open_tickets:number;launch_score:number;launch_checklist:Checklist[];checkout_live:boolean;payment_provider:string;collects_card_data:boolean;demo_only:boolean}
type CustomerHome = {license:{plan:string;expires_at:string}|null;entitlements:{agents:number;bots:number;features:string[]};latest_acceptance?:{terms_version:string;accepted_at:string}|null;support_tickets:Ticket[];business:Business;checkout_live:boolean;demo_only:boolean}
type SubTab = 'launch'|'leads'|'billing'|'support'

const money = (value:number,currency='USD') => `${value.toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2})} ${currency}`
const date = (value:string) => new Date(value).toLocaleString('tr-TR',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'})

function messageFrom(body:unknown):string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as {detail:unknown}).detail
    if (typeof detail === 'string') return detail
  }
  return 'İşlem tamamlanamadı.'
}

export default function CommerceCenter({token,role,plans,onNotice}:{token:string;role:'OWNER'|'MODERATOR'|'CUSTOMER';plans:Record<string,Plan>;onNotice:(text:string,kind:'ok'|'warn'|'error')=>void}) {
  const [tab,setTab] = useState<SubTab>('launch')
  const [busy,setBusy] = useState(false)
  const [overview,setOverview] = useState<CommerceOverview|null>(null)
  const [customerHome,setCustomerHome] = useState<CustomerHome|null>(null)
  const [business,setBusiness] = useState<Business|null>(null)
  const [lead,setLead] = useState({name:'',email:'',company:'',interested_plan:'TRIAL',note:''})
  const [invoice,setInvoice] = useState({plan:'PRO',months:'1',discount_pct:'0',tax_pct:'0',customer_name:'Demo Müşteri'})
  const [invoiceResult,setInvoiceResult] = useState<DemoInvoice|null>(null)
  const [ticket,setTicket] = useState({subject:'',message:'',priority:'NORMAL'})

  const call = async <T,>(path:string,options:RequestInit={}):Promise<T> => {
    const headers = new Headers(options.headers)
    headers.set('Authorization',`Bearer ${token}`)
    if (options.body) headers.set('Content-Type','application/json')
    const response = await fetch(`${API}${path}`,{...options,headers})
    let body:unknown = null
    try { body = await response.json() } catch { body = null }
    if (!response.ok) throw new Error(messageFrom(body))
    return body as T
  }

  const refresh = async () => {
    setBusy(true)
    try {
      if (role === 'OWNER') {
        const data = await call<CommerceOverview>('/overview')
        setOverview(data);setBusiness(data.business)
      } else {
        const data = await call<CustomerHome>('/customer-home')
        setCustomerHome(data);setBusiness(data.business)
      }
    } catch (error) {onNotice(error instanceof Error ? error.message : 'Ticari merkez yüklenemedi.','error')}
    finally {setBusy(false)}
  }

  useEffect(() => { void refresh() },[token,role])

  const saveSettings = async () => {
    if (!business || role !== 'OWNER') return
    setBusy(true)
    try {
      await call('/settings',{method:'PUT',body:JSON.stringify(business)})
      await refresh();onNotice('Marka ve müşteri kurulum ayarları kaydedildi. Canlı ödeme kapalı kaldı.','ok')
    } catch (error) {onNotice(error instanceof Error ? error.message : 'Ayarlar kaydedilemedi.','error')}
    finally {setBusy(false)}
  }

  const createLead = async () => {
    setBusy(true)
    try {
      await call('/leads',{method:'POST',body:JSON.stringify(lead)})
      setLead({name:'',email:'',company:'',interested_plan:'TRIAL',note:''})
      await refresh();onNotice('Demo satış adayı eklendi.','ok')
    } catch (error) {onNotice(error instanceof Error ? error.message : 'Satış adayı eklenemedi.','error')}
    finally {setBusy(false)}
  }

  const advanceLead = async (row:Lead) => {
    const flow = ['NEW','CONTACTED','TRIAL','WON']
    const next = flow[Math.min(flow.length - 1,Math.max(0,flow.indexOf(row.status) + 1))]
    setBusy(true)
    try {
      await call(`/leads/${row.id}/status`,{method:'PUT',body:JSON.stringify({status:next,note:'V24 Satış Merkezi'})})
      await refresh();onNotice(`${row.name} adayı ${next} aşamasına taşındı.`,'ok')
    } catch (error) {onNotice(error instanceof Error ? error.message : 'Aşama güncellenemedi.','error')}
    finally {setBusy(false)}
  }

  const previewInvoice = async () => {
    setBusy(true)
    try {
      const result = await call<DemoInvoice>('/invoice-preview',{method:'POST',body:JSON.stringify({...invoice,months:Number(invoice.months),discount_pct:Number(invoice.discount_pct),tax_pct:Number(invoice.tax_pct)})})
      setInvoiceResult(result);if (role === 'OWNER') await refresh();onNotice('Tahsilatsız Demo teklif/fatura ön izlemesi hazır.','ok')
    } catch (error) {onNotice(error instanceof Error ? error.message : 'Ön izleme oluşturulamadı.','error')}
    finally {setBusy(false)}
  }

  const openTicket = async () => {
    setBusy(true)
    try {
      await call('/support',{method:'POST',body:JSON.stringify(ticket)})
      setTicket({subject:'',message:'',priority:'NORMAL'});await refresh();onNotice('Destek kaydı açıldı.','ok')
    } catch (error) {onNotice(error instanceof Error ? error.message : 'Destek kaydı açılamadı.','error')}
    finally {setBusy(false)}
  }

  const resolveTicket = async (row:Ticket) => {
    if (role !== 'OWNER') return
    setBusy(true)
    try {
      await call(`/support/${row.id}`,{method:'PUT',body:JSON.stringify({status:'RESOLVED',response_note:'V24 Satış Merkezi üzerinden çözüldü.'})})
      await refresh();onNotice(`${row.subject} destek kaydı çözüldü olarak işaretlendi.`,'ok')
    } catch (error) {onNotice(error instanceof Error ? error.message : 'Destek kaydı güncellenemedi.','error')}
    finally {setBusy(false)}
  }

  const acceptDemoTerms = async () => {
    if (!business) return
    setBusy(true)
    try {
      await call('/acceptance',{method:'POST',body:JSON.stringify({terms_version:business.terms_version,risk_acknowledged:true,demo_only_acknowledged:true})})
      await refresh();onNotice('Demo/Paper kapsamı ve risk bildirimi kaydedildi.','ok')
    } catch (error) {onNotice(error instanceof Error ? error.message : 'Kabul kaydedilemedi.','error')}
    finally {setBusy(false)}
  }

  const activePlans = useMemo(() => Object.entries(overview?.plans || plans),[overview,plans])
  const tickets = overview?.support_tickets || customerHome?.support_tickets || []

  if (!business) return <div className="commerceLoading"><CircleDashed className={busy ? 'spin' : ''}/><b>V24 TİCARİ MERKEZ HAZIRLANIYOR</b></div>

  return <div className="commerceV24">
    <header className="commerceBanner"><div><span><Rocket/></span><div><small>V24 · COMMERCIAL COMPLETE</small><h2>{business.brand_name} Satış & Müşteri Merkezi</h2><p>Tek paket: marka, müşteri adayı, lisans, Demo fiyat teklifi, onboarding ve destek.</p></div></div><aside><b><ShieldCheck/> GÜVENLİ LAUNCH LAB</b><span>Canlı tahsilat ve gerçek emir kilitli</span></aside></header>
    <nav className="commerceSubTabs">{([['launch','Kurulum',Building2],['leads','Satış Hunisi',UserPlus],['billing','Paket & Teklif',ReceiptText],['support','Destek',LifeBuoy]] as const).map(([key,label,Icon]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}><Icon/>{label}</button>)}</nav>

    {tab === 'launch' && <section className="commerceGrid launchGrid">
      <article className="commerceCard launchScore"><div className="scoreRing" style={{'--launch-score':`${overview?.launch_score ?? 25}%`} as React.CSSProperties}><b>%{overview?.launch_score ?? 25}</b><span>YAYIN HAZIRLIĞI</span></div><h3>{role === 'OWNER' ? 'Ticari kurulum kontrolü' : 'Müşteri hesabım'}</h3><p>Bu oran yalnızca yerel hazırlığı gösterir; hukuk ve bağımsız güvenlik onayı değildir.</p></article>
      {role === 'OWNER' ? <article className="commerceCard businessForm"><div className="commerceHead"><div><small>MARKA & İŞLETME</small><h3>Müşterinin göreceği bilgiler</h3></div><Building2/></div><div className="commerceFields"><label>Marka adı<input value={business.brand_name} onChange={e => setBusiness({...business,brand_name:e.target.value})}/></label><label>Şirket / unvan<input value={business.legal_name} onChange={e => setBusiness({...business,legal_name:e.target.value})}/></label><label>Destek e-postası<input type="email" value={business.support_email} onChange={e => setBusiness({...business,support_email:e.target.value})}/></label><label>Web adresi<input placeholder="https://..." value={business.website_url} onChange={e => setBusiness({...business,website_url:e.target.value})}/></label><label>Para birimi<select value={business.currency} onChange={e => setBusiness({...business,currency:e.target.value as Business['currency']})}><option>USD</option><option>EUR</option><option>TRY</option><option>USDT</option></select></label><label>Deneme süresi<input type="number" min="1" max="90" value={business.trial_days} onChange={e => setBusiness({...business,trial_days:Number(e.target.value)})}/></label></div><button className="commercePrimary" onClick={saveSettings} disabled={busy}><Save/> AYARLARI KAYDET</button></article> : <article className="commerceCard customerLicense"><div className="commerceHead"><div><small>LİSANSIM</small><h3>{customerHome?.license?.plan || 'Lisans bekliyor'}</h3></div><ShieldCheck/></div><b>{customerHome?.entitlements.agents ?? 0} ajan · {customerHome?.entitlements.bots ?? 0} bot</b><ul>{customerHome?.entitlements.features.map(item => <li key={item}><CheckCircle2/>{item}</li>)}</ul><button className="commercePrimary" onClick={acceptDemoTerms} disabled={busy}><ShieldCheck/> DEMO KOŞULLARINI KABUL ET</button></article>}
      <article className="commerceCard checklist"><div className="commerceHead"><div><small>YAYIN KAPILARI</small><h3>Hazır olanlar ve kalanlar</h3></div><Rocket/></div>{(overview?.launch_checklist || []).map(item => <div key={item.key} className={item.passed ? 'passed' : 'pending'}>{item.passed ? <CheckCircle2/> : <CircleDashed/>}<span><b>{item.label}</b><small>{item.detail}</small></span></div>)}{role !== 'OWNER' && <><div className="passed"><CheckCircle2/><span><b>Demo/Paper güvenlik sınırı</b><small>Gerçek para ve canlı ödeme yok.</small></span></div><div className="pending"><CircleDashed/><span><b>Canlı sağlayıcılar</b><small>Yönetici onayı ve harici kurulum bekliyor.</small></span></div></>}</article>
    </section>}

    {tab === 'leads' && <section className="commerceGrid salesGrid">
      {role === 'OWNER' ? <><article className="commerceCard leadForm"><div className="commerceHead"><div><small>SATIŞ ADAYI</small><h3>Yeni Demo müşteri adayı</h3></div><UserPlus/></div><div className="commerceFields"><label>Ad / kişi<input value={lead.name} onChange={e => setLead({...lead,name:e.target.value})}/></label><label>E-posta<input type="email" value={lead.email} onChange={e => setLead({...lead,email:e.target.value})}/></label><label>Şirket<input value={lead.company} onChange={e => setLead({...lead,company:e.target.value})}/></label><label>İlgilendiği paket<select value={lead.interested_plan} onChange={e => setLead({...lead,interested_plan:e.target.value})}>{activePlans.map(([code,plan]) => <option key={code} value={code}>{plan.name}</option>)}</select></label></div><label className="wideLabel">Not<textarea value={lead.note} onChange={e => setLead({...lead,note:e.target.value})}/></label><button className="commercePrimary" onClick={createLead} disabled={busy}><UserPlus/> ADAYI EKLE</button></article><article className="commerceCard funnel"><div className="commerceHead"><div><small>YEREL DEMO CRM</small><h3>Satış hunisi</h3></div><ShoppingBag/></div><div className="funnelMetrics">{Object.entries(overview?.funnel || {}).map(([key,value]) => <span key={key}><b>{value}</b><small>{key}</small></span>)}</div><div className="leadRows">{overview?.leads.length ? overview.leads.map(row => <article key={row.id}><div><b>{row.name}</b><small>{row.company || row.email}</small></div><em>{row.interested_plan}</em><span>{row.status}</span><button onClick={() => advanceLead(row)} disabled={busy || row.status === 'WON'}>SONRAKİ AŞAMA</button></article>) : <p>Henüz Demo satış adayı yok.</p>}</div></article></> : <article className="commerceCard lockedCommerce"><ShoppingBag/><h3>Satış hunisi yönetici alanıdır</h3><p>Müşteri olarak lisans, koşullar ve destek alanlarını kullanabilirsin.</p></article>}
    </section>}

    {tab === 'billing' && <section className="commerceGrid billingGrid"><article className="commerceCard invoiceForm"><div className="commerceHead"><div><small>TAHSİLATSIZ ÖN İZLEME</small><h3>Paket fiyatı ve teklif</h3></div><ReceiptText/></div><div className="commerceFields"><label>Paket<select value={invoice.plan} onChange={e => setInvoice({...invoice,plan:e.target.value})}>{activePlans.map(([code,plan]) => <option key={code} value={code}>{plan.name} · {money(plan.monthly_usd,business.currency)}</option>)}</select></label><label>Ay<input type="number" min="1" max="24" value={invoice.months} onChange={e => setInvoice({...invoice,months:e.target.value})}/></label><label>İndirim %<input type="number" min="0" max="100" value={invoice.discount_pct} onChange={e => setInvoice({...invoice,discount_pct:e.target.value})}/></label><label>Vergi ön izlemesi %<input type="number" min="0" max="100" value={invoice.tax_pct} onChange={e => setInvoice({...invoice,tax_pct:e.target.value})}/></label><label>Müşteri adı<input value={invoice.customer_name} onChange={e => setInvoice({...invoice,customer_name:e.target.value})}/></label></div><button className="commercePrimary" onClick={previewInvoice} disabled={busy}><BadgeDollarSign/> DEMO TEKLİF OLUŞTUR</button><p className="commerceSafetyNote">Kart bilgisi istemez, para çekmez, fatura numarası üretmez. Sadece fiyat matematiğini sınar.</p></article><article className="commerceCard invoicePreview"><div className="commerceHead"><div><small>DEMO BELGE</small><h3>{invoiceResult ? invoiceResult.plan_name : 'Ön izleme bekliyor'}</h3></div><ReceiptText/></div>{invoiceResult ? <><span>Müşteri <b>{invoiceResult.customer_name}</b></span><span>Ara toplam <b>{money(invoiceResult.subtotal,invoiceResult.currency)}</b></span><span>İndirim <b>-{money(invoiceResult.discount,invoiceResult.currency)}</b></span><span>Vergi ön izlemesi <b>{money(invoiceResult.tax,invoiceResult.currency)}</b></span><strong>{money(invoiceResult.total,invoiceResult.currency)}</strong><em>DEMO_PREVIEW · TAHSİLAT YOK</em></> : <div className="commerceBlank"><ReceiptText/><p>Soldaki alanlardan bir Demo teklif üret.</p></div>}</article><article className="commerceCard providerLock"><ShieldCheck/><h3>Ödeme sağlayıcısı bağlı değil</h3><p>V24, fiyat ve abonelik yaşam döngüsünü hazırlar. Gerçek ödeme kurulumu; şirket hesabı, sözleşmeler, webhook imzası, vergi/fatura ve bağımsız güvenlik kontrolünden sonra birlikte yapılır.</p><b>NOT_CONFIGURED · CHECKOUT OFF</b></article></section>}

    {tab === 'support' && <section className="commerceGrid supportGrid"><article className="commerceCard ticketForm"><div className="commerceHead"><div><small>MÜŞTERİ DESTEK</small><h3>Yeni destek kaydı</h3></div><LifeBuoy/></div><label>Konu<input value={ticket.subject} onChange={e => setTicket({...ticket,subject:e.target.value})}/></label><label>Öncelik<select value={ticket.priority} onChange={e => setTicket({...ticket,priority:e.target.value})}><option value="LOW">Düşük</option><option value="NORMAL">Normal</option><option value="HIGH">Yüksek</option></select></label><label>Mesaj<textarea value={ticket.message} onChange={e => setTicket({...ticket,message:e.target.value})}/></label><button className="commercePrimary" onClick={openTicket} disabled={busy}><Send/> DESTEK KAYDI AÇ</button></article><article className="commerceCard ticketRows"><div className="commerceHead"><div><small>TAKİP MERKEZİ</small><h3>{tickets.length} destek kaydı</h3></div><LifeBuoy/></div>{tickets.length ? tickets.map(row => <article key={row.id}><span className={`ticketPriority ${row.priority.toLowerCase()}`}>{row.priority}</span><div><b>{row.subject}</b><small>{row.customer?.display_name || 'Hesabım'} · {date(row.created_at)}</small></div><em>{row.status}</em>{role === 'OWNER' && !['RESOLVED','CLOSED'].includes(row.status) && <button className="ticketResolve" onClick={() => resolveTicket(row)} disabled={busy}><CheckCircle2/> ÇÖZÜLDÜ</button>}</article>) : <div className="commerceBlank"><LifeBuoy/><p>Henüz destek kaydı yok.</p></div>}</article></section>}
  </div>
}
