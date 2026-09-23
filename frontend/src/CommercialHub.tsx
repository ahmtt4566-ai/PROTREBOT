import { type CSSProperties, useEffect, useMemo, useState } from 'react'
import { Activity, BadgeDollarSign, BookOpenCheck, Bot, Building2, Calculator, CheckCircle2, Cpu, Crown, Download, ExternalLink, FileClock, Gauge, KeyRound, LockKeyhole, LogOut, Power, RefreshCw, Settings, ShieldCheck, ShoppingBag, Sparkles, UserCheck, UserPlus, Users, UserX, WalletCards, XCircle } from 'lucide-react'
import CommerceCenter from './CommerceCenter'
import ExecutionCenter from './ExecutionCenter'
import { API_BASE } from './api'

const API = `${API_BASE}/v22`
const SESSION_KEY = 'protrebot-v25-session'
const V24_SESSION_KEY = 'protrebot-v24-session'
const V23_SESSION_KEY = 'protrebot-v23-session'
const LEGACY_SESSION_KEY = 'protrebot-v22-session'
const REMEMBER_KEY = 'protrebot-v25-remember'

type Plan = {name:string;monthly_usd:number;days:number;agents:number;bots:number;features:string[]}
type PublicInfo = {version:string;edition:string;setup_required:boolean;auth_available?:boolean;plans:Record<string,Plan>;billing:{provider:string;live:boolean};security:Record<string,boolean>;account_storage?:string;message:string}
type User = {id:string;email:string;display_name:string;role:'OWNER'|'CUSTOMER';active:boolean;created_at:string}
type License = {id:string;user_id:string;plan:string;status:string;starts_at:string;expires_at:string;source:string;demo_only:boolean}
type Session = {user:User;license:License|null;demo_only:boolean}
type Agent = {id:string;user_id:string;device_name:string;status:string;last_seen_at:string;app_version:string;mode:string}
type Audit = {id:string;kind:string;message:string;actor:string;subject?:string;created_at:string}
type Overview = {users:number;active_users:number;licenses:number;active_licenses:number;agents:number;online_agents:number;monthly_demo_revenue_usd:number;customers:(User & {license:License|null})[];agents_list:Agent[];audit:Audit[];billing_live:boolean;demo_only:boolean}
type Gate = {key:string;label:string;passed:boolean;detail:string}
type Readiness = {version:string;stage:string;score:number;passed:number;total:number;gates:Gate[];production_ready:boolean;closed_beta_candidate?:boolean;demo_only:boolean;next_step:string;release_evidence?:Record<string,{status:string;note:string;updated_at?:string|null}>}
type Operations = {version:string;demo_connector:{configured:boolean;connected:boolean;armed:boolean;armed_until:string|null;last_error?:string|null};demo_account:{positions:number;open_orders:number;open_algo_orders:number;available_balance?:number|null;wallet_balance?:number|null;one_way:boolean};automation:{demo_enabled:boolean;demo_cycles:number;demo_last_decision?:string|null;paper_enabled:boolean;paper_cycles:number};paper:{balance:number;positions:number;pending_orders:number;closed_trades:number;emergency_brake:boolean};services:Record<string,string>;recent_demo_events:{kind:string;message:string;created_at:string}[];real_orders_enabled:boolean;testnet_orders_enabled:boolean;withdrawals_supported:boolean;demo_only:boolean}
type FeeResult = {direction:string;gross_move_pct:number;gross_usdt:number;fee_usdt:number;slippage_usdt:number;funding_usdt:number;total_cost_usdt:number;minimum_required_usdt:number;net_usdt:number;net_return_pct:number;break_even_move_pct:number;approved:boolean;decision:string;reason:string;demo_only:boolean}
type GridResult = {grid_step:number;grid_step_pct:number;capital_per_grid_usdt:number;maker_share_pct:number;effective_fee_bps:number;gross_cycle_usdt:number;fee_cycle_usdt:number;slippage_cycle_usdt:number;funding_cycle_usdt:number;net_cycle_usdt:number;minimum_cycle_net_usdt:number;approved:boolean;decision:string;demo_only:boolean}
type Tab = 'home'|'execution'|'commerce'|'customers'|'license'|'operations'|'fees'|'audit'|'readiness'
type NavigateTarget = 'v20-demo'|'v20-limit'|'v20-autopilot'|'risk-command'|'live-health'|'records-journal'|'automation-grid'

const money = (value?:number) => value === undefined ? '—' : value.toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:4})
const date = (value?:string) => value ? new Date(value).toLocaleString('tr-TR',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}) : '—'

function detailMessage(detail:unknown):string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map(item => typeof item === 'object' && item && 'msg' in item ? String(item.msg) : String(item)).join(' · ')
  if (detail && typeof detail === 'object' && 'message' in detail) return String((detail as {message:unknown}).message)
  return 'İşlem tamamlanamadı; alanları ve API bağlantısını kontrol edin.'
}

export default function CommercialHub({active,onNavigate}:{active:boolean;onNavigate?:(target:NavigateTarget)=>void}) {
  const [info,setInfo] = useState<PublicInfo|null>(null)
  const [token,setToken] = useState(() => localStorage.getItem(SESSION_KEY) || sessionStorage.getItem(SESSION_KEY) || localStorage.getItem(V24_SESSION_KEY) || sessionStorage.getItem(V24_SESSION_KEY) || localStorage.getItem(V23_SESSION_KEY) || sessionStorage.getItem(V23_SESSION_KEY) || sessionStorage.getItem(LEGACY_SESSION_KEY) || '')
  const [remember,setRemember] = useState(() => localStorage.getItem(REMEMBER_KEY) !== '0')
  const [session,setSession] = useState<Session|null>(null)
  const [overview,setOverview] = useState<Overview|null>(null)
  const [readiness,setReadiness] = useState<Readiness|null>(null)
  const [operations,setOperations] = useState<Operations|null>(null)
  const [tab,setTab] = useState<Tab>('home')
  const [busy,setBusy] = useState(false)
  const [notice,setNotice] = useState('V25 Live Guard & Commercial katmanı hazırlanıyor…')
  const [noticeKind,setNoticeKind] = useState<'ok'|'warn'|'error'>('warn')
  const [owner,setOwner] = useState({display_name:'',email:'',password:''})
  const [login,setLogin] = useState({email:'',password:''})
  const [customer,setCustomer] = useState({display_name:'',email:'',password:'',plan:'TRIAL',days:'14'})
  const [pairCode,setPairCode] = useState('')
  const [passwordForm,setPasswordForm] = useState({current_password:'',new_password:''})
  const [fee,setFee] = useState({entry:'65000',target:'65650',notional_usdt:'1000',direction:'LONG',fee_bps_per_side:'4',slippage_bps_per_side:'2',funding_bps:'0',minimum_net_usdt:'0.25',minimum_net_pct:'0.05'})
  const [feeResult,setFeeResult] = useState<FeeResult|null>(null)
  const [grid,setGrid] = useState({lower:'62000',upper:'68000',grid_count:'20',capital_usdt:'1000',maker_share_pct:'80',maker_fee_bps:'2',taker_fee_bps:'5',slippage_bps_per_side:'1',funding_bps:'0',minimum_cycle_net_usdt:'0.05'})
  const [gridResult,setGridResult] = useState<GridResult|null>(null)

  const request = async <T,>(path:string,options:RequestInit={}):Promise<T> => {
    const headers = new Headers(options.headers)
    if (options.body) headers.set('Content-Type','application/json')
    if (token) headers.set('Authorization',`Bearer ${token}`)
    const response = await fetch(`${API}${path}`,{...options,headers})
    let body:unknown = null
    try { body = await response.json() } catch { body = null }
    if (!response.ok) throw new Error(detailMessage(body && typeof body === 'object' && 'detail' in body ? (body as {detail:unknown}).detail : body))
    return body as T
  }

  const saveToken = (value:string,persistent=remember) => {
    setToken(value)
    localStorage.removeItem(SESSION_KEY)
    sessionStorage.removeItem(SESSION_KEY)
    localStorage.removeItem(V24_SESSION_KEY)
    sessionStorage.removeItem(V24_SESSION_KEY)
    localStorage.removeItem(V23_SESSION_KEY)
    sessionStorage.removeItem(V23_SESSION_KEY)
    sessionStorage.removeItem(LEGACY_SESSION_KEY)
    if (value) {
      if (persistent) localStorage.setItem(SESSION_KEY,value)
      else sessionStorage.setItem(SESSION_KEY,value)
    }
  }

  const refresh = async (quiet=false) => {
    try {
      const publicInfo = await request<PublicInfo>('/public')
      setInfo(publicInfo)
      if (!token) return
      const current = await request<Session>('/session')
      setSession(current)
      const [ready,operationState] = await Promise.all([
        request<Readiness>('/readiness'),request<Operations>('/operations'),
      ])
      setReadiness(ready);setOperations(operationState)
      if (current.user.role === 'OWNER') setOverview(await request<Overview>('/admin/overview'))
      if (!quiet) {setNotice('Üyelik, lisans ve güvenlik durumu yenilendi.');setNoticeKind('ok')}
    } catch (error) {
      if (token) {saveToken('');setSession(null);setOverview(null);setReadiness(null);setOperations(null)}
      setNotice(error instanceof Error ? error.message : 'V25 API bağlantısı kurulamadı.');setNoticeKind('error')
    }
  }

  useEffect(() => {
    if (!active) return
    void refresh(true)
    const timer = window.setInterval(() => void refresh(true),15000)
    return () => window.clearInterval(timer)
  },[active,token])

  const authenticate = async (mode:'bootstrap'|'login') => {
    setBusy(true)
    try {
      const persistSession = mode === 'bootstrap' ? true : remember
      const payload = {...(mode === 'bootstrap' ? owner : login),remember:persistSession}
      const result = await request<{token:string;user:User;license:License}>(mode === 'bootstrap' ? '/bootstrap' : '/auth/login',{method:'POST',body:JSON.stringify(payload)})
      localStorage.setItem(REMEMBER_KEY,persistSession ? '1' : '0')
      saveToken(result.token,persistSession);setSession({user:result.user,license:result.license,demo_only:true})
      setNotice(mode === 'bootstrap' ? 'V25 sahibi oluşturuldu; yönetim merkezi açıldı.' : 'Güvenli oturum açıldı.');setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Oturum açılamadı.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const logout = async () => {
    try { if (token) await request('/auth/logout',{method:'POST'}) } catch {}
    saveToken('');setSession(null);setOverview(null);setReadiness(null);setOperations(null)
  }

  const createCustomer = async () => {
    setBusy(true)
    try {
      await request('/customers',{method:'POST',body:JSON.stringify({...customer,days:Number(customer.days)})})
      setCustomer({display_name:'',email:'',password:'',plan:'TRIAL',days:'14'})
      setOverview(await request<Overview>('/admin/overview'))
      setNotice('Test müşterisi ve Demo lisansı oluşturuldu. Canlı ödeme alınmadı.');setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Müşteri oluşturulamadı.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const createPairCode = async () => {
    setBusy(true)
    try {
      const result = await request<{code:string}>('/agent/pair-code',{method:'POST'})
      setPairCode(result.code);setNotice('10 dakikalık tek kullanımlık ajan kodu hazır.');setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Eşleştirme kodu üretilemedi.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const reloadOwnerData = async () => {
    const [nextOverview,nextReadiness,nextOperations] = await Promise.all([
      request<Overview>('/admin/overview'),request<Readiness>('/readiness'),request<Operations>('/operations'),
    ])
    setOverview(nextOverview);setReadiness(nextReadiness);setOperations(nextOperations)
  }

  const toggleCustomer = async (row:User) => {
    if (row.role === 'OWNER') return
    const next = !row.active
    if (!window.confirm(`${row.display_name} hesabı ${next ? 'etkinleştirilsin' : 'askıya alınsın'} mı?`)) return
    setBusy(true)
    try {
      await request(`/customers/${row.id}/status`,{method:'POST',body:JSON.stringify({active:next,reason:'Commercial Control Center'})})
      await reloadOwnerData();setNotice(`Müşteri ${next ? 'etkinleştirildi' : 'askıya alındı'}; açık oturumları yenilendi.`);setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Müşteri durumu değiştirilemedi.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const renewLicense = async (row:User & {license:License|null}) => {
    const plan = (window.prompt('Paket kodu: TRIAL, STARTER, PRO veya ELITE',row.license?.plan || 'TRIAL') || '').trim().toUpperCase()
    if (!plan || !info?.plans[plan]) return
    const days = Number(window.prompt('Lisans süresi (gün):','30'))
    if (!Number.isFinite(days) || days < 1 || days > 730) {setNotice('Lisans süresi 1–730 gün olmalı.');setNoticeKind('warn');return}
    setBusy(true)
    try {
      await request('/subscriptions/activate-demo',{method:'POST',body:JSON.stringify({user_id:row.id,plan,days})})
      await reloadOwnerData();setNotice(`${row.display_name} için ${plan} Demo lisansı etkinleştirildi.`);setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Lisans yenilenemedi.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const revokeLicense = async (row:User & {license:License|null}) => {
    if (!row.license || row.role === 'OWNER' || !window.confirm(`${row.display_name} lisansı ve bağlı ajanları iptal edilsin mi?`)) return
    setBusy(true)
    try {
      await request(`/licenses/${row.license.id}/revoke`,{method:'POST',body:JSON.stringify({confirmation:'LİSANS İPTAL',reason:'Commercial Control Center'})})
      await reloadOwnerData();setNotice('Lisans ve bağlı ajan erişimleri iptal edildi.');setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Lisans iptal edilemedi.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const revokeAgent = async (agent:Agent) => {
    if (agent.status !== 'ACTIVE' || !window.confirm(`${agent.device_name} cihaz erişimi kaldırılsın mı?`)) return
    setBusy(true)
    try {
      await request(`/agents/${agent.id}/revoke`,{method:'POST',body:JSON.stringify({confirmation:'AJAN İPTAL',reason:'Commercial Control Center'})})
      await reloadOwnerData();setNotice('Ajan erişimi kaldırıldı; eski belirteç artık kabul edilmeyecek.');setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Ajan iptal edilemedi.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const changePassword = async () => {
    setBusy(true)
    try {
      await request('/auth/change-password',{method:'POST',body:JSON.stringify(passwordForm)})
      saveToken('');setSession(null);setPasswordForm({current_password:'',new_password:''});setNotice('Parola değişti. Yeniden giriş yapın.');setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Parola değiştirilemedi.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const recordEvidence = async (key:'backup'|'support') => {
    const note = window.prompt(key === 'backup' ? 'Yedekleme tatbikatı notu:' : 'Destek süreci notu:',key === 'backup' ? 'YEDEKLE.bat çalıştırıldı ve geri yükleme dosyası kontrol edildi.' : 'Destek e-postası, yanıt süresi ve olay kayıt süreci belirlendi.')
    if (!note) return
    setBusy(true)
    try {
      await request(`/release-evidence/${key}`,{method:'PUT',body:JSON.stringify({status:'RECORDED',note})})
      await reloadOwnerData();setNotice('Yayın kanıtı kaydedildi; bu kayıt bağımsız denetim yerine geçmez.');setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Kanıt kaydedilemedi.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const exportSafeReport = () => {
    const payload = {generated_at:new Date().toISOString(),version:info?.version,overview,readiness,operations,note:'API anahtarı ve parola içermez. Demo/Paper raporudur.'}
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}))
    const link = document.createElement('a');link.href=url;link.download=`protrebot-v25-rapor-${new Date().toISOString().slice(0,10)}.json`;link.click();URL.revokeObjectURL(url)
    setNotice('Gizli bilgi içermeyen yönetim raporu indirildi.');setNoticeKind('ok')
  }

  const editPlan = async (code:string,plan:Plan) => {
    const priceText = window.prompt(`${code} aylık fiyatı (USD):`,String(plan.monthly_usd))
    if (priceText === null) return
    const agentText = window.prompt(`${code} cihaz/ajan sınırı:`,String(plan.agents))
    if (agentText === null) return
    const botText = window.prompt(`${code} bot sınırı:`,String(plan.bots))
    if (botText === null) return
    setBusy(true)
    try {
      await request(`/plans/${code}`,{method:'PUT',body:JSON.stringify({monthly_usd:Number(priceText.replace(',','.')),agents:Number(agentText),bots:Number(botText)})})
      setInfo(await request<PublicInfo>('/public'))
      setNotice(`${code} fiyatı ve lisans sınırları güncellendi; canlı tahsilat hâlâ kapalı.`);setNoticeKind('ok')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Paket güncellenemedi.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const calculateFees = async () => {
    setBusy(true)
    try {
      const payload = Object.fromEntries(Object.entries(fee).map(([key,value]) => [key,key === 'direction' ? value : Number(value)]))
      const result = await request<FeeResult>('/fee-guard',{method:'POST',body:JSON.stringify(payload)})
      setFeeResult(result);setNotice(result.reason);setNoticeKind(result.approved ? 'ok' : 'warn')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Net kâr hesabı yapılamadı.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const calculateGrid = async () => {
    setBusy(true)
    try {
      const payload = Object.fromEntries(Object.entries(grid).map(([key,value]) => [key,Number(value)]))
      const result = await request<GridResult>('/grid-guard',{method:'POST',body:JSON.stringify(payload)})
      setGridResult(result);setNotice(result.decision);setNoticeKind(result.approved ? 'ok' : 'warn')
    } catch (error) {setNotice(error instanceof Error ? error.message : 'Grid maliyet hesabı yapılamadı.');setNoticeKind('error')}
    finally {setBusy(false)}
  }

  const planRows = useMemo(() => Object.entries(info?.plans || {}),[info])
  if (!active) return null

  if (!info) return <section className="commercialLoading"><RefreshCw className="spin"/><b>V25 LIVE GUARD BAĞLANIYOR</b><span>Üyelik, lisans, risk ve canlı yürütme kasası kontrol ediliyor…</span></section>

  if (info.auth_available === false) return <section className="commercialLoading"><RefreshCw className="spin"/><b>KALICI HESAP VERİTABANI BEKLENİYOR</b><span>PostgreSQL bağlantısı doğrulanmadan kayıt veya giriş ekranı açılmaz.</span></section>

  if (!session) return <section className="commercialAuth">
    <div className="commercialAuthHero"><span className="commercialEdition">V25 LIVE GUARD</span><h2>Robotunu yönetilebilir ve kontrollü bir ürüne dönüştüren merkez</h2><p>Müşterinin parası borsasında kalır. Canlı API anahtarı yalnızca kullanıcının Windows kasasında; canlı girişler varsayılan kilitlidir.</p><div><span><ShieldCheck/> Fail-closed yürütme</span><span><LockKeyhole/> Anahtar merkezde tutulmaz</span><span><BadgeDollarSign/> Kâr garantisi yok</span></div></div>
    <div className="commercialAuthCard">
      <div className="commercialAuthTitle"><Crown/><div><small>{info.setup_required ? 'İLK KURULUM' : 'GÜVENLİ GİRİŞ'}</small><h3>{info.setup_required ? 'V25 sahibini oluştur' : 'Yönetim merkezine gir'}</h3></div></div>
      {info.setup_required ? <>
        <div className="commercialAuthMode"><b>TEK SEFERLİK KAYIT</b><span>Bu hesap sonraki açılışlarda ve güncellemelerde korunur.</span></div>
        <label>Ad / işletme adı<input autoComplete="organization" value={owner.display_name} onChange={e => setOwner({...owner,display_name:e.target.value})} placeholder="ProTreBot Studio"/></label>
        <label>E-posta<input autoComplete="email" type="email" value={owner.email} onChange={e => setOwner({...owner,email:e.target.value})} placeholder="yonetici@ornek.com"/></label>
        <label>Parola · en az 10 karakter<input autoComplete="new-password" type="password" value={owner.password} onChange={e => setOwner({...owner,password:e.target.value})} onKeyDown={e => {if (e.key === 'Enter') void authenticate('bootstrap')}}/></label>
        <button disabled={busy} onClick={() => authenticate('bootstrap')}><Sparkles/>{busy ? 'HAZIRLANIYOR…' : 'KAYIT OL VE GİRİŞ YAP'}</button>
      </> : <>
        <div className="commercialAuthMode ready"><b>HESAP HAZIR</b><span>Tekrar kayıt olmayın; e-posta ve parolanızla giriş yapın.</span></div>
        <label>E-posta<input autoComplete="username" type="email" value={login.email} onChange={e => setLogin({...login,email:e.target.value})}/></label>
        <label>Parola<input autoComplete="current-password" type="password" value={login.password} onChange={e => setLogin({...login,password:e.target.value})} onKeyDown={e => {if (e.key === 'Enter') void authenticate('login')}}/></label>
        <label className="commercialRemember"><input type="checkbox" checked={remember} onChange={e => setRemember(e.target.checked)}/><span><b>Bu bilgisayarda beni hatırla</b><small>İşaretlerseniz oturum 30 gün boyunca hatırlanır.</small></span></label>
        <button disabled={busy} onClick={() => authenticate('login')}><KeyRound/>{busy ? 'DOĞRULANIYOR…' : 'GİRİŞ YAP'}</button>
      </>}
      <p className={`commercialNotice ${noticeKind}`}>{notice}</p><small className="commercialLocalNote">İlk kayıt yalnızca doğrulanmış güvenli yönetici oturumunda ve tek sefer yapılabilir. Parolanı veya borsa anahtarını kimseyle paylaşma.</small>
    </div>
  </section>

  return <section className="commercialHub">
    <div className="commercialHero">
      <div className="commercialHeroTitle"><span><Building2/></span><div><small>V25 · LIVE GUARD</small><h2>Business & Robot Control Center</h2><p>Paper, Demo, lisans, canlı risk kasası ve fail-closed yürütme tek merkezde.</p></div></div>
      <div className="commercialSafety"><b><ShieldCheck/> CANLI VARSAYILAN KİLİTLİ</b><span>Yerel kasa · Demo kanıtı · süreli çift onay</span></div>
      <div className="commercialIdentity"><span>{session.user.display_name}</span><b>{session.user.role}</b><button onClick={() => void logout()}><LogOut/> Çıkış</button></div>
    </div>

    <nav className="commercialTabs">
      {([
        ['home','Genel Bakış',Gauge],['execution','V25 Canlı Kasa',Bot],['commerce','Satış Merkezi',ShoppingBag],['customers','Müşteriler',Users],['license','Lisans & Ajan',Cpu],['operations','Operasyon',Activity],['fees','Net Kâr Koruması',Calculator],['audit','Güvenlik',FileClock],['readiness','Yayın Kapısı',ShieldCheck],
      ] as const).map(([key,label,Icon]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}><Icon/><span>{label}</span></button>)}
      <button className="commercialRefresh" onClick={() => refresh()} disabled={busy}><RefreshCw className={busy ? 'spin' : ''}/></button>
    </nav>
    <div className={`commercialNoticeBar ${noticeKind}`}>{notice}</div>

    {tab === 'home' && <div className="commercialPage">
      <div className="commercialMetrics">
        <article><Users/><small>KULLANICI</small><b>{overview?.users ?? 1}</b><span>{overview?.active_users ?? 1} etkin</span></article>
        <article><KeyRound/><small>AKTİF LİSANS</small><b>{overview?.active_licenses ?? (session.license ? 1 : 0)}</b><span>{session.license?.plan ?? '—'} paket</span></article>
        <article><Cpu/><small>YEREL AJAN</small><b>{overview?.agents ?? 0}</b><span>{overview?.online_agents ?? 0} çevrimiçi</span></article>
        <article><BadgeDollarSign/><small>AYLIK DEMO MRR</small><b>${money(overview?.monthly_demo_revenue_usd ?? 0)}</b><span>tahsilat kapalı</span></article>
        <article className="commercialScore"><ShieldCheck/><small>HAZIRLIK PUANI</small><b>%{readiness?.score ?? 0}</b><span>{readiness?.stage ?? 'ölçülüyor'}</span></article>
      </div>
      <div className="commercialSplit">
        <div className="commercialPanel"><div className="commercialPanelHead"><div><small>ÜRÜN MİMARİSİ</small><h3>Anahtar müşterinin cihazında kalır</h3></div><LockKeyhole/></div><div className="commercialFlow"><span><b>1</b>Paper kanıtı</span><i>→</i><span><b>2</b>Binance Demo</span><i>→</i><span><b>3</b>Yerel DPAPI</span><i>→</i><span><b>4</b>V25 Canlı Kasa</span></div><p>Merkezi panel lisansı ve politikayı doğrular; para veya borsa anahtarı saklamaz. Fonlar kullanıcının Binance hesabında kalır.</p></div>
        <div className="commercialPanel commercialDecision"><div><small>ŞİMDİKİ AŞAMA</small><h3>Fail-closed canlı aday</h3></div><strong>ÇEKİM DESTEĞİ YOK</strong><strong>CANLI GİRİŞ VARSAYILAN KİLİTLİ</strong><strong>DEMO KANITI ZORUNLU</strong><p>Canlı Kasa sekmesi, bütün kapıları geçmeden gerçek emir göndermez.</p></div>
      </div>
      <div className="commercialPlans">{planRows.map(([code,plan],index) => <article key={code} className={index === 2 ? 'featured' : ''}><div><small>{code}</small>{index === 2 && <em>ÖNERİLEN</em>}</div><h3>{plan.name}</h3><strong>${money(plan.monthly_usd)}<small>/ay</small></strong><span>{plan.agents} ajan · {plan.bots} bot</span><ul>{plan.features.map(item => <li key={item}><CheckCircle2/>{item}</li>)}</ul>{session.user.role === 'OWNER' && <button className="planEdit" disabled={busy} onClick={() => editPlan(code,plan)}>FİYAT & SINIR DÜZENLE</button>}</article>)}</div>
    </div>}

    {tab === 'execution' && <ExecutionCenter token={token}/>} 

    {tab === 'commerce' && <CommerceCenter token={token} role={session.user.role} plans={info.plans} onNotice={(text,kind) => {setNotice(text);setNoticeKind(kind)}}/>}

    {tab === 'customers' && <div className="commercialPage commercialColumns">
      <div className="commercialPanel customerForm"><div className="commercialPanelHead"><div><small>YÖNETİCİ ARACI</small><h3>Test müşterisi oluştur</h3></div><UserPlus/></div>
        {session.user.role === 'OWNER' ? <><label>Ad / işletme<input value={customer.display_name} onChange={e => setCustomer({...customer,display_name:e.target.value})}/></label><label>E-posta<input type="email" value={customer.email} onChange={e => setCustomer({...customer,email:e.target.value})}/></label><label>Geçici parola<input type="password" value={customer.password} onChange={e => setCustomer({...customer,password:e.target.value})} placeholder="En az 10 karakter"/></label><div className="commercialInline"><label>Paket<select value={customer.plan} onChange={e => setCustomer({...customer,plan:e.target.value})}>{planRows.map(([code,plan]) => <option key={code} value={code}>{plan.name}</option>)}</select></label><label>Süre (gün)<input type="number" min="1" max="730" value={customer.days} onChange={e => setCustomer({...customer,days:e.target.value})}/></label></div><button className="commercialPrimary" disabled={busy} onClick={createCustomer}><UserPlus/> DEMO MÜŞTERİSİ EKLE</button><p>Bu işlem gerçek ödeme almaz; abonelik akışını test etmek için yerel lisans üretir.</p></> : <p>Bu alan yalnızca OWNER rolüne açıktır.</p>}
      </div>
      <div className="commercialPanel customerTable"><div className="commercialPanelHead"><div><small>MÜŞTERİ MERKEZİ</small><h3>Kullanıcılar, lisanslar ve erişim</h3></div><Users/></div><div className="commercialRows">{overview?.customers?.map(row => <article key={row.id} className={!row.active ? 'rowSuspended' : ''}><span className="commercialAvatar">{row.display_name.slice(0,2).toUpperCase()}</span><div><b>{row.display_name}</b><small>{row.email}</small></div><em>{row.active ? row.role : 'ASKIDA'}</em><span className={row.license ? 'statusOn' : 'statusOff'}>{row.license?.plan ?? 'LİSANSSIZ'}</span><time>{row.license ? date(row.license.expires_at) : '—'}</time>{session.user.role === 'OWNER' && row.role !== 'OWNER' && <div className="commercialRowActions"><button onClick={() => renewLicense(row)} title="Demo lisansı ekle veya yenile"><KeyRound/>YENİLE</button><button onClick={() => toggleCustomer(row)} title={row.active ? 'Müşteriyi askıya al' : 'Müşteriyi etkinleştir'}>{row.active ? <UserX/> : <UserCheck/>}{row.active ? 'ASKIYA AL' : 'AÇ'}</button>{row.license && <button className="danger" onClick={() => revokeLicense(row)}><Power/>LİSANS İPTAL</button>}</div>}</article>) ?? <p>Yönetici özeti yükleniyor…</p>}</div></div>
    </div>}

    {tab === 'license' && <div className="commercialPage commercialColumns">
      <div className="commercialPanel pairingPanel"><div className="commercialPanelHead"><div><small>TEK KULLANIMLIK KOD</small><h3>Sürekli güvenli yerel ajanı eşleştir</h3></div><Cpu/></div><p>Borsa anahtarı sunucuya gönderilmez. Kod yalnızca bu lisansı yerel ajanla eşleştirir ve 10 dakika sonra geçersiz olur.</p><button className="commercialPrimary" onClick={createPairCode} disabled={busy}><KeyRound/> EŞLEŞTİRME KODU ÜRET</button><div className={pairCode ? 'pairCode active' : 'pairCode'}>{pairCode || 'KOD BEKLENİYOR'}</div><ol><li>Paketteki <b>V24-AJANI-BAGLA.bat</b> dosyasını aç.</li><li>Bu kodu ve cihaz adını yaz.</li><li>Pencere açıkken ajan 45 saniyede bir lisans kalp atışı gönderir.</li></ol></div>
      <div className="commercialPanel"><div className="commercialPanelHead"><div><small>CİHAZ ENVANTERİ</small><h3>Eşleşen ve iptal edilen ajanlar</h3></div><ShieldCheck/></div><div className="agentGrid">{overview?.agents_list?.length ? overview.agents_list.map(agent => <article key={agent.id} className={agent.status !== 'ACTIVE' ? 'agentRevoked' : ''}><Cpu/><div><b>{agent.device_name}</b><small>{agent.app_version} · {agent.mode}</small></div><span className={agent.status === 'ACTIVE' ? 'statusOn' : 'statusOff'}>{agent.status}</span><time>{date(agent.last_seen_at)}</time>{agent.status === 'ACTIVE' && <button className="agentRevoke" onClick={() => revokeAgent(agent)}><Power/>ERİŞİMİ KALDIR</button>}</article>) : <div className="commercialEmpty"><Cpu/><b>Henüz ajan eşleşmedi</b><span>Yukarıdan bir kod üretip yerel ajanı bağla.</span></div>}</div><div className="commercialRule"><LockKeyhole/><p><b>Merkezi anahtar kasası yok.</b> Demo borsa anahtarı yalnızca müşterinin kendi Windows/VPS cihazında DPAPI ile şifreli tutulur.</p></div></div>
    </div>}

    {tab === 'operations' && <div className="commercialPage professionalOps">
      <div className="operationsPulse">
        <article className={operations?.demo_connector.configured ? 'ready' : 'waiting'}><WalletCards/><small>DEMO ANAHTAR</small><b>{operations?.demo_connector.configured ? 'YEREL KASADA' : 'BEKLİYOR'}</b><span>Merkeze gönderilmez</span></article>
        <article className={operations?.demo_connector.connected ? 'ready' : 'waiting'}><Activity/><small>DEMO BAĞLANTI</small><b>{operations?.demo_connector.connected ? 'BAĞLI' : 'BAĞLANTI BEKLİYOR'}</b><span>{operations?.demo_connector.armed ? '10 dk emir kilidi açık' : 'Yeni giriş kilitli'}</span></article>
        <article><Gauge/><small>DEMO POZİSYON</small><b>{operations?.demo_account.positions ?? 0} / 3</b><span>{operations?.demo_account.open_orders ?? 0} açık giriş emri</span></article>
        <article className={operations?.automation.demo_enabled ? 'ready' : 'waiting'}><Settings/><small>OTOMASYON</small><b>{operations?.automation.demo_enabled ? 'ÇALIŞIYOR' : 'ONAY BEKLİYOR'}</b><span>{operations?.automation.demo_cycles ?? 0} kontrollü tur</span></article>
        <article className="locked"><ShieldCheck/><small>GERÇEK PARA</small><b>KİLİTLİ</b><span>Çekim ve canlı emir yok</span></article>
      </div>
      <div className="operationLaunchGrid">
        {([
          ['v20-demo','Binance Demo İşlem Masası','Piyasa/limit LONG–SHORT, kaldıraç, Stop, TP1–TP3, pozisyon ve canlı günlük.',Activity],
          ['v20-limit','Limit & Pozisyon Haritası','Kendi giriş, stop, hedef ve grid kademelerini grafikte gör.',Gauge],
          ['v20-autopilot','Paper Autopilot','TP1–TP3 yaşam döngüsü ve güvenli otomatik sanal tarama.',Settings],
          ['risk-command','Otonom Risk Merkezi','Günlük kayıp, korelasyon, Monte Carlo, portföy ısısı ve acil fren.',ShieldCheck],
          ['automation-grid','Canlı Paper Grid','Komisyon sonrası sanal grid, envanter kilidi ve dijital ikiz.',WalletCards],
          ['records-journal','İşlem ve Denetim Günlüğü','Açılan, kapanan, engellenen işlemler ve net performans kayıtları.',BookOpenCheck],
        ] as const).map(([target,title,description,Icon]) => <article key={target}><span><Icon/></span><div><h3>{title}</h3><p>{description}</p></div><button onClick={() => onNavigate?.(target)}><ExternalLink/> MERKEZİ AÇ</button></article>)}
      </div>
      <div className="commercialSplit operationBottom">
        <div className="commercialPanel"><div className="commercialPanelHead"><div><small>SERVİS NABZI</small><h3>Profesyonel çalışma yığını</h3></div><Activity/></div><div className="serviceMatrix">{Object.entries(operations?.services || {}).map(([key,value]) => <span key={key}><small>{key.replace('_',' ').toUpperCase()}</small><b className={value === 'BAĞLI' || value === 'KALICI' ? 'statusOn' : 'statusOff'}>{value}</b></span>)}</div><p>API, TimescaleDB, Redis ve Paper kayıt katmanı tek sağlık merkezinden izlenir.</p></div>
        <div className="commercialPanel"><div className="commercialPanelHead"><div><small>SON DEMO OLAYLARI</small><h3>Emir motoru özeti</h3></div><FileClock/></div><div className="miniAudit">{operations?.recent_demo_events?.length ? operations.recent_demo_events.map((event,index) => <article key={`${event.created_at}-${index}`}><i/><div><b>{event.kind}</b><span>{event.message}</span></div><time>{date(event.created_at)}</time></article>) : <div className="commercialEmpty"><FileClock/><b>Henüz Demo olayı yok</b><span>Bağlantı ve emir testleri burada görünür.</span></div>}</div></div>
      </div>
    </div>}

    {tab === 'fees' && <div className="commercialPage feeColumns">
      <div className="commercialPanel"><div className="commercialPanelHead"><div><small>POZİSYON KAPISI</small><h3>Komisyon sonrası net kâr</h3></div><Calculator/></div><div className="fieldGrid"><label>Yön<select value={fee.direction} onChange={e => setFee({...fee,direction:e.target.value})}><option>LONG</option><option>SHORT</option></select></label><label>Giriş<input type="number" value={fee.entry} onChange={e => setFee({...fee,entry:e.target.value})}/></label><label>Hedef<input type="number" value={fee.target} onChange={e => setFee({...fee,target:e.target.value})}/></label><label>Notional USDT<input type="number" value={fee.notional_usdt} onChange={e => setFee({...fee,notional_usdt:e.target.value})}/></label><label>Tek yön ücret (bp)<input type="number" value={fee.fee_bps_per_side} onChange={e => setFee({...fee,fee_bps_per_side:e.target.value})}/></label><label>Tek yön kayma (bp)<input type="number" value={fee.slippage_bps_per_side} onChange={e => setFee({...fee,slippage_bps_per_side:e.target.value})}/></label><label>Fonlama (bp)<input type="number" value={fee.funding_bps} onChange={e => setFee({...fee,funding_bps:e.target.value})}/></label><label>Min. net USDT<input type="number" value={fee.minimum_net_usdt} onChange={e => setFee({...fee,minimum_net_usdt:e.target.value})}/></label></div><button className="commercialPrimary" onClick={calculateFees} disabled={busy}><Calculator/> NET SONUCU HESAPLA</button>{feeResult && <div className={feeResult.approved ? 'guardResult approved' : 'guardResult blocked'}><div><b>{feeResult.decision}</b><span>{feeResult.reason}</span></div><strong>{feeResult.net_usdt >= 0 ? '+' : ''}{money(feeResult.net_usdt)} USDT</strong><div className="guardMetrics"><span>Brüt <b>{money(feeResult.gross_usdt)}</b></span><span>Ücret <b>-{money(feeResult.fee_usdt)}</b></span><span>Kayma <b>-{money(feeResult.slippage_usdt)}</b></span><span>Fonlama <b>-{money(feeResult.funding_usdt)}</b></span><span>Başabaş hareket <b>%{money(feeResult.break_even_move_pct)}</b></span></div></div>}</div>
      <div className="commercialPanel"><div className="commercialPanelHead"><div><small>GRID KAPISI</small><h3>Kademe başına net getiri</h3></div><Gauge/></div><div className="fieldGrid"><label>Alt sınır<input type="number" value={grid.lower} onChange={e => setGrid({...grid,lower:e.target.value})}/></label><label>Üst sınır<input type="number" value={grid.upper} onChange={e => setGrid({...grid,upper:e.target.value})}/></label><label>Grid sayısı<input type="number" value={grid.grid_count} onChange={e => setGrid({...grid,grid_count:e.target.value})}/></label><label>Sermaye USDT<input type="number" value={grid.capital_usdt} onChange={e => setGrid({...grid,capital_usdt:e.target.value})}/></label><label>Maker oranı %<input type="number" value={grid.maker_share_pct} onChange={e => setGrid({...grid,maker_share_pct:e.target.value})}/></label><label>Maker ücret (bp)<input type="number" value={grid.maker_fee_bps} onChange={e => setGrid({...grid,maker_fee_bps:e.target.value})}/></label><label>Taker ücret (bp)<input type="number" value={grid.taker_fee_bps} onChange={e => setGrid({...grid,taker_fee_bps:e.target.value})}/></label><label>Min. tur neti<input type="number" value={grid.minimum_cycle_net_usdt} onChange={e => setGrid({...grid,minimum_cycle_net_usdt:e.target.value})}/></label></div><button className="commercialPrimary" onClick={calculateGrid} disabled={busy}><Gauge/> GRID MALİYETİNİ ÖLÇ</button>{gridResult && <div className={gridResult.approved ? 'guardResult approved' : 'guardResult blocked'}><div><b>{gridResult.decision}</b><span>%{money(gridResult.grid_step_pct)} kademe · %{money(gridResult.maker_share_pct)} maker varsayımı</span></div><strong>{gridResult.net_cycle_usdt >= 0 ? '+' : ''}{money(gridResult.net_cycle_usdt)} USDT/tur</strong><div className="guardMetrics"><span>Kademe sermayesi <b>{money(gridResult.capital_per_grid_usdt)}</b></span><span>Brüt tur <b>{money(gridResult.gross_cycle_usdt)}</b></span><span>Ücret <b>-{money(gridResult.fee_cycle_usdt)}</b></span><span>Kayma <b>-{money(gridResult.slippage_cycle_usdt)}</b></span></div></div>}</div>
    </div>}

    {tab === 'audit' && <div className="commercialPage auditLayout">
      <div className="commercialPanel accountSecurity"><div className="commercialPanelHead"><div><small>HESAP GÜVENLİĞİ</small><h3>Parola ve oturum kontrolü</h3></div><KeyRound/></div><label>Mevcut parola<input type="password" autoComplete="current-password" value={passwordForm.current_password} onChange={e => setPasswordForm({...passwordForm,current_password:e.target.value})}/></label><label>Yeni parola · en az 10 karakter<input type="password" autoComplete="new-password" value={passwordForm.new_password} onChange={e => setPasswordForm({...passwordForm,new_password:e.target.value})}/></label><button className="commercialPrimary" disabled={busy || passwordForm.new_password.length < 10} onClick={changePassword}><ShieldCheck/> PAROLAYI DEĞİŞTİR</button><p>Parola değiştiğinde daha önce verilmiş yönetim oturumları geçersizleşir.</p><button className="safeExport" onClick={exportSafeReport}><Download/> GİZLİ BİLGİ İÇERMEYEN RAPORU İNDİR</button></div>
      <div className="commercialPanel auditTimeline"><div className="commercialPanelHead"><div><small>DEĞİŞTİRİLEMEZ OLMAYAN YEREL KAYIT</small><h3>Son yönetim olayları</h3></div><FileClock/></div><div>{overview?.audit?.length ? overview.audit.map(item => <article key={item.id}><i/><div><b>{item.kind}</b><p>{item.message}</p><small>Aktör: {item.actor.slice(0,10)} · Konu: {item.subject?.slice(0,10) || 'SİSTEM'}</small></div><time>{date(item.created_at)}</time></article>) : <div className="commercialEmpty"><FileClock/><b>Henüz denetim kaydı yok</b></div>}</div></div>
      <div className="commercialPanel securityBoundary"><div className="commercialPanelHead"><div><small>SABİT GÜVENLİK SINIRI</small><h3>V25’in asla açmadığı kanallar</h3></div><LockKeyhole/></div><span><XCircle/> Para yatırma veya çekme</span><span><XCircle/> Merkezi API anahtarı saklama</span><span><XCircle/> Onaysız / süresiz canlı giriş</span><span><XCircle/> Kâr veya kayıpsızlık garantisi</span><p>Gerçek Futures emirleri yalnızca V25 Canlı Kasa’daki 30 gün/100 Demo kanıtı, yerel izin, risk politikası ve 24 saatlik kilit birlikte geçtiğinde açılır.</p></div>
    </div>}

    {tab === 'readiness' && <div className="commercialPage readinessLayout"><div className="readinessScore"><div style={{'--score':`${readiness?.score ?? 0}%`} as CSSProperties}><strong>%{readiness?.score ?? 0}</strong><span>ÜRÜN HAZIRLIĞI</span></div><h3>{readiness?.stage}</h3><p>{readiness?.next_step}</p><b>{readiness?.closed_beta_candidate ? 'KAPALI BETA ADAYI' : 'GENEL SATIŞA HAZIR DEĞİL'}</b><div className="evidenceActions"><button onClick={() => recordEvidence('backup')}><Download/> YEDEK TATBİKATINI KAYDET</button><button onClick={() => recordEvidence('support')}><Users/> DESTEK SÜRECİNİ KAYDET</button></div></div><div className="readinessGates">{readiness?.gates.map(gate => <article key={gate.key} className={gate.passed ? 'passed' : 'pending'}>{gate.passed ? <CheckCircle2/> : <XCircle/>}<div><b>{gate.label}</b><span>{gate.detail}</span></div><em>{gate.passed ? 'GEÇTİ' : 'BEKLİYOR'}</em></article>)}</div><div className="productionWarning"><ShieldCheck/><div><h3>Satış öncesi kırmızı çizgiler</h3><p>V25 kişisel canlı hesap için korumalı bir yürütme adayı içerir; bu, halka açık yatırım hizmeti veya müşteri fonu yönetimi izni değildir. Başkalarına satıştan önce ülkeye özel hukuk/vergi incelemesi, bağımsız pentest, TLS/KMS, ödeme sağlayıcısı ve kontrollü beta gerekir.</p></div></div></div>}
  </section>
}
