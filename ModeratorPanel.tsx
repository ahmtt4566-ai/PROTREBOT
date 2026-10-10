import {useEffect, useRef, useState} from 'react'
import {Activity, ChevronLeft, ChevronRight, CreditCard, Home, LogOut, Menu, MessageCircle, ShieldCheck, UserRound, Users, Wallet} from 'lucide-react'
import {accountRoleLabel} from './account-role'
import {moderatorRequest} from './moderator-api'
import {ModBadge, ModCard, ModEmpty, ModError, ModResourceView, ModSearch} from './moderator-ui'
import {
  moderatorSections, parseCustomerPage, parseCustomerPayments, parseCustomerProfile, parseCustomerSubscription,
  parseModeratorMe, permissionLabels, visibleModeratorSections,
  type CustomerPage, type CustomerResource, type ModeratorMe, type ModeratorPermission, type ModeratorSection,
} from './moderator-model'
import './admin.css'
import './moderator-panel.css'
import ModeratorSupport, {SupportOverview} from './ModeratorSupport'

const icons = {overview: Home, customers: Users, subscriptions: Wallet, payments: CreditCard, support: MessageCircle, activity: Activity, approvals: ShieldCheck}
type Navigate = (section: ModeratorSection, userId?: string) => void

function CustomerResourcePanel({kind, initialUserId, onBack}: {kind: CustomerResource['kind']; initialUserId: string; onBack?: () => void}) {
  const [input, setInput] = useState(initialUserId)
  const [query, setQuery] = useState(initialUserId ? {userId: initialUserId, version: 0} : null)
  const [resource, setResource] = useState<CustomerResource | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (!query) return
    const controller = new AbortController()
    setBusy(true); setError(null); setResource(null)
    const path = `/customers/${encodeURIComponent(query.userId)}`
    const load = async (): Promise<CustomerResource> => {
      if (kind === 'profile') return {kind, data: await moderatorRequest(path, parseCustomerProfile, controller.signal)}
      if (kind === 'subscription') return {kind, data: await moderatorRequest(`${path}/subscription`, parseCustomerSubscription, controller.signal)}
      return {kind, data: await moderatorRequest(`${path}/payments`, parseCustomerPayments, controller.signal)}
    }
    void load().then(value => {if (!controller.signal.aborted) setResource(value)})
      .catch(failure => {if (!controller.signal.aborted) setError(failure)})
      .finally(() => {if (!controller.signal.aborted) setBusy(false)})
    return () => controller.abort()
  }, [kind, query])
  return <div className="mod-stack">
    {onBack ? <button className="mod-back" onClick={onBack}><ChevronLeft aria-hidden="true"/>Listeye dön</button> :
      <ModSearch value={input} onChange={setInput} busy={busy} onSubmit={event => {event.preventDefault(); setQuery({userId: input.trim(), version: (query?.version ?? 0) + 1})}}/>}
    {busy && <p className="mod-secondary" role="status">Bilgiler yükleniyor...</p>}
    {error !== null && <ModError error={error} onRetry={() => {if (query) setQuery({...query, version: query.version + 1})}}/>}
    {!query && <ModCard title="Müşteri seçimi"><ModEmpty text="Görüntülemek için bir kullanıcı numarası yazın."/></ModCard>}
    {resource && <ModResourceView resource={resource}/>}
  </div>
}

function Customers({permissions, navigate, initialUserId = ''}: {permissions: ModeratorPermission[]; navigate: Navigate; initialUserId?: string}) {
  const [input, setInput] = useState('')
  const [query, setQuery] = useState({search: '', offset: 0, version: 0})
  const [page, setPage] = useState<CustomerPage | null>(null)
  const [selected, setSelected] = useState(initialUserId)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(true)
  useEffect(() => {
    if (selected) return
    const controller = new AbortController()
    setBusy(true); setError(null); setPage(null)
    const params = new URLSearchParams({limit: '25', offset: String(query.offset)})
    if (query.search) params.set('search', query.search)
    void moderatorRequest(`/customers?${params}`, parseCustomerPage, controller.signal)
      .then(value => {if (!controller.signal.aborted) setPage(value)})
      .catch(failure => {if (!controller.signal.aborted) setError(failure)})
      .finally(() => {if (!controller.signal.aborted) setBusy(false)})
    return () => controller.abort()
  }, [query, selected])
  if (selected) return <div className="mod-stack"><CustomerResourcePanel kind="profile" initialUserId={selected} onBack={() => setSelected('')}/>
    <div className="mod-actions">{permissions.includes('subscriptions.view') && <button onClick={() => navigate('subscriptions', selected)}>Aboneliği görüntüle</button>}
      {permissions.includes('payments.view') && <button onClick={() => navigate('payments', selected)}>Ödemeleri görüntüle</button>}</div>
  </div>
  return <div className="mod-stack">
    <ModSearch customerSearch value={input} onChange={setInput} busy={busy} onSubmit={event => {event.preventDefault(); setQuery({search: input.trim(), offset: 0, version: query.version + 1})}}/>
    {busy && <p className="mod-secondary" role="status">Müşteriler yükleniyor...</p>}
    {error !== null && <ModError error={error} onRetry={() => setQuery({...query, version: query.version + 1})}/>}
    {page && <><p className="mod-secondary" role="status">{page.total} müşteri bulundu</p>
      {!page.items.length ? <ModCard title="Arama sonuçları"><ModEmpty/></ModCard> :
        <ul className="mod-results">{page.items.map(customer => <li key={customer.user_id}>
          <button className="mod-customer-row" onClick={() => setSelected(customer.user_id)}>
            <UserRound aria-hidden="true"/><span><strong>{customer.email_masked}</strong><small>{customer.user_id}</small></span>
            <ModBadge tone={customer.active ? 'positive' : 'neutral'}>{customer.active ? 'Aktif' : 'Devre dışı'}</ModBadge><ChevronRight aria-hidden="true"/>
          </button>
        </li>)}</ul>}
      <div className="mod-pagination"><button disabled={busy || query.offset === 0} onClick={() => setQuery({...query, offset: Math.max(0, query.offset - page.limit)})}><ChevronLeft aria-hidden="true"/>Önceki</button>
        <button disabled={busy || page.offset + page.limit >= page.total} onClick={() => setQuery({...query, offset: query.offset + page.limit})}>Sonraki<ChevronRight aria-hidden="true"/></button></div>
    </>}
  </div>
}

export default function ModeratorPanel({onLogout}: {onLogout: () => void}) {
  const [me, setMe] = useState<ModeratorMe | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [refresh, setRefresh] = useState(0)
  const [section, setSection] = useState<ModeratorSection>('overview')
  const [userId, setUserId] = useState('')
  const [menuOpen, setMenuOpen] = useState(false)
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    const controller = new AbortController()
    setMe(null); setError(null)
    void moderatorRequest('/me', parseModeratorMe, controller.signal)
      .then(value => {if (!controller.signal.aborted) {setMe(value); setSection('overview'); setUserId('')}})
      .catch(failure => {if (!controller.signal.aborted) setError(failure)})
    return () => controller.abort()
  }, [refresh])
  useEffect(() => {heading.current?.focus()}, [section])
  const sections = visibleModeratorSections(me?.permissions ?? [])
  const title = moderatorSections.find(item => item.id === section)?.label ?? 'Genel Bakış'
  const navigate: Navigate = (next, selected = '') => {setSection(next); setUserId(selected); setMenuOpen(false)}
  return <div className="mod-shell">
    <a className="mod-skip" href="#mod-main">İçeriğe geç</a>
    <aside className="mod-sidebar">
      <div className="mod-brand"><strong>KaisTrade</strong><span>Moderatör paneli</span></div>
      <button className="mod-menu-toggle" aria-expanded={menuOpen} aria-controls="mod-navigation" onClick={() => setMenuOpen(value => !value)}><Menu aria-hidden="true"/>Menü</button>
      <nav id="mod-navigation" aria-label="Moderatör menüsü" className={menuOpen ? 'mod-navigation mod-navigation-open' : 'mod-navigation'}>
        {sections.map(item => {const Icon = icons[item.id]; return <button key={item.id} aria-current={section === item.id ? 'page' : undefined} onClick={() => navigate(item.id)}><Icon aria-hidden="true"/>{item.label}</button>})}
      </nav>
      <div className="mod-account-links"><a href="/settings"><UserRound aria-hidden="true"/>Profil ve güvenlik</a><button onClick={onLogout}><LogOut aria-hidden="true"/>Çıkış</button></div>
    </aside>
    <main className="mod-main" id="mod-main" tabIndex={-1}>
      <header className="mod-header"><div><p className="mod-secondary">Moderatör çalışma alanı</p><h1 ref={heading} tabIndex={-1}>{title}</h1></div>{me && <ModBadge>{accountRoleLabel(me.role)}</ModBadge>}</header>
      {error !== null ? <ModError error={error} onRetry={() => setRefresh(value => value + 1)}/> : !me ? <p role="status" className="mod-secondary">Erişim bilgileri yükleniyor...</p> :
        section === 'overview' ? <div className="mod-stack">
          {me.permissions.includes('support.view') && <SupportOverview/>}
          <ModCard title="Erişim bilgileriniz"><div className="mod-badges"><ModBadge>{accountRoleLabel(me.role)}</ModBadge><ModBadge>Müşteri bilgileri salt okunur</ModBadge></div>
            <h3>İzinleriniz</h3>{me.permissions.length ? <ul className="mod-permissions">{me.permissions.map(permission => <li key={permission}><ShieldCheck aria-hidden="true"/>{permissionLabels[permission]}</li>)}</ul> : <ModEmpty text="Henüz bir izin tanımlanmamış. Yöneticinizden erişim isteyin."/>}
            <button className="mod-refresh" onClick={() => setRefresh(value => value + 1)}>İzinleri yenile</button>
          </ModCard><ModCard title="Diğer bölümler yakında"><p className="mod-secondary">Etkinlik ve onay talepleri sonraki aşamalarda kullanıma açılacak.</p></ModCard>
        </div> : section === 'customers' ? <Customers key={`${section}:${userId}`} initialUserId={userId} permissions={me.permissions} navigate={navigate}/> :
          section === 'support' ? <ModeratorSupport manage={me.permissions.includes('support.manage')} onCustomer={me.permissions.includes('customers.view') ? id => navigate('customers', id) : undefined}/> :
          section === 'subscriptions' || section === 'payments' ? <CustomerResourcePanel key={`${section}:${userId}`} kind={section === 'subscriptions' ? 'subscription' : 'payments'} initialUserId={userId}/> :
            <ModCard title="Yakında"><p className="mod-secondary">{title} bu aşamada kullanıma açık değil.</p></ModCard>}
    </main>
  </div>
}
