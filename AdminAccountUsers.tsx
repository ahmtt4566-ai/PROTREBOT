import {useCallback, useEffect, useRef, useState} from 'react'
import {Check, ChevronLeft, ChevronRight, Mail, Monitor, RefreshCw, Search, ShieldCheck, UserRound, X} from 'lucide-react'
import {accountDate, accountInitials, accountRequest, type AccountOverview, type AdminAccount, type AdminAccounts} from './account-settings-api'
import './admin-account-users.css'
import AdminModeratorAccess from './AdminModeratorAccess'
import {accountRoleLabel} from './account-role'

type Props = {search: string; onSearch: (value: string) => void; page: number; onPage: (value: number) => void; filter: string; onFilter: (value: string) => void; pageSize: number}
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'Kullanıcı bilgileri alınamadı.'

function AccountDetail({id, onClose, onChanged}: {id: string; onClose: () => void; onChanged: () => void}) {
  const [data, setData] = useState<AccountOverview | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const dialog = useRef<HTMLDialogElement>(null)
  const [confirmation, setConfirmation] = useState<'status' | 'sessions' | 'reset' | null>(null)
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const result = await accountRequest<AccountOverview>(`/admin/accounts/${encodeURIComponent(id)}`, {signal})
    if (!signal?.aborted) {setData(result); setError('')}
  }, [id])
  useEffect(() => {
    const previous = document.activeElement
    const element = dialog.current
    element?.showModal()
    const controller = new AbortController()
    void refresh(controller.signal).catch(failure => {if (!controller.signal.aborted) setError(errorMessage(failure))})
    return () => {controller.abort(); element?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus()}
  }, [refresh])
  const act = async () => {
    if (!confirmation || !data || inFlight.current) return
    inFlight.current = true; setBusy(true); setError(''); setMessage('')
    try {
      const uid = encodeURIComponent(id)
      if (confirmation === 'status') await accountRequest(`/customers/${uid}/status`, {method: 'POST', body: JSON.stringify({active: !data.user.active, reason: 'Admin kullanıcı yönetimi'})})
      else if (confirmation === 'sessions') await accountRequest(`/admin/users/${uid}/sessions/revoke`, {method: 'POST'})
      else await accountRequest(`/admin/accounts/${uid}/password-reset`, {method: 'POST', body: '{}'})
      await refresh()
      onChanged()
      setMessage(confirmation === 'reset' ? 'Parola yenileme e-postası gönderildi.' : 'Kullanıcı işlemi tamamlandı.')
      setConfirmation(null)
    } catch (failure) {setError(errorMessage(failure))}
    finally {inFlight.current = false; setBusy(false)}
  }
  return <dialog className="adminAccountDialog" ref={dialog} aria-labelledby="admin-account-title" onCancel={event => {event.preventDefault(); if (!busy) onClose()}}>
    <header><h2 id="admin-account-title">Kullanıcı detayları</h2><button aria-label="Kullanıcı detayını kapat" disabled={busy} onClick={onClose}><X/></button></header>
    {error && <p role="alert" className="adminAccountError">{error}</p>}{message && <p role="status">{message}</p>}
    {!data && !error && <p role="status">Kullanıcı bilgileri yükleniyor...</p>}
    {!data && error && <button disabled={busy} onClick={() => void refresh().catch(failure => setError(errorMessage(failure)))}>Yeniden dene</button>}
    {data && <>
      <div className="adminAccountIdentity"><i>{accountInitials(data.user.display_name)}</i><div><h3>{data.user.display_name}</h3><p>{data.user.email}</p><small>{data.user.id}</small></div><b data-tone={data.user.active ? 'positive' : 'negative'}>{data.user.active ? 'Aktif' : 'Kapalı / devre dışı'}</b></div>
      <dl className="adminAccountFacts">
        <div><dt>Hesap rolü</dt><dd>{accountRoleLabel(data.user.role)}</dd></div>
        <div><dt>Giriş yöntemleri</dt><dd>{data.user.auth_methods.join(' / ') || '—'}</dd></div>
        <div><dt>E-posta doğrulaması</dt><dd>{data.user.email_verified ? 'Doğrulandı' : 'Doğrulanmadı'}</dd></div>
        <div><dt>Bekleyen e-posta</dt><dd>{data.pending_email?.email || '—'}</dd></div>
        <div><dt>Paket</dt><dd>{data.subscription.plan}</dd></div>
        <div><dt>Abonelik durumu</dt><dd>{data.subscription.status}</dd></div>
        <div><dt>Premium erişimi</dt><dd>{data.subscription.is_premium ? 'Var' : 'Yok'}{data.user.role === 'OWNER' && ' · Yönetici yetkisi ücretli paket değildir'}</dd></div>
        <div><dt>Paket bitişi</dt><dd>{accountDate(data.subscription.expires_at)}</dd></div>
        <div><dt>Kayıt tarihi</dt><dd>{accountDate(data.user.created_at)}</dd></div>
        <div><dt>Son giriş</dt><dd>{accountDate(data.user.last_login)}</dd></div>
        <div><dt>2FA</dt><dd>{data.security.two_factor_enabled ? 'Etkin' : 'Kapalı'}</dd></div>
        <div><dt>Aktif oturumlar</dt><dd>{data.security.active_sessions}</dd></div>
        <div><dt>Son parola değişikliği</dt><dd>{accountDate(data.user.password_changed_at)}</dd></div>
      </dl>
      <h3>İşlem tercihleri</h3><dl className="adminAccountFacts"><div><dt>Bölüm</dt><dd>{data.preferences.trading_mode}</dd></div><div><dt>Zaman dilimi</dt><dd>{data.preferences.timeframe}</dd></div><div><dt>Borsa</dt><dd>{data.preferences.exchange}</dd></div><div><dt>Tercih edilen risk</dt><dd>{data.preferences.risk_per_trade}% · canlı politika değildir</dd></div><div><dt>Semboller</dt><dd>{data.preferences.symbols.join(', ') || '—'}</dd></div></dl>
      <h3>Oturumlar</h3><ul className="adminAccountRecords">{data.sessions.map(session => <li key={session.id}><Monitor/><span>{session.device} · {session.browser}</span><time>{accountDate(session.last_seen_at)}</time></li>)}</ul>{!data.sessions.length && <p>Aktif oturum yok.</p>}
      <h3>Hesap aktiviteleri</h3><ul className="adminAccountRecords">{data.activity.map(item => <li key={item.id}><span>{item.message}</span><time>{accountDate(item.created_at)}</time></li>)}</ul>{!data.activity.length && <p>Kayıtlı aktivite yok.</p>}
      <p className="adminAccountPrivacy">Şifreler, 2FA anahtarları, kurtarma kodları, oturum token'ları ve API anahtarları bu ekranda gösterilmez.</p>
      <AdminModeratorAccess id={id} role={data.user.role} onBusyChange={setBusy} onChanged={async () => {await refresh(); onChanged()}}/>
      <div className="adminAccountActions"><button disabled={busy || data.user.role === 'OWNER'} onClick={() => setConfirmation('status')}>{data.user.active ? 'Hesabı devre dışı bırak' : 'Hesabı yeniden aç'}</button><button disabled={busy} onClick={() => setConfirmation('sessions')}>Oturumları sonlandır</button><button disabled={busy || !data.security.email_delivery_available} onClick={() => setConfirmation('reset')}>Parola yenileme e-postası gönder</button></div>
      {confirmation && <section className="adminAccountConfirmation" role="group" aria-label="Yönetici işlemi onayı"><p>{confirmation === 'status' ? `Bu hesabı ${data.user.active ? 'devre dışı bırakmak' : 'yeniden açmak'} istiyor musunuz? Kayıtlar silinmez.` : confirmation === 'sessions' ? 'Bu kullanıcının tüm oturumları sonlandırılacak.' : 'Kullanıcıya gerçek parola yenileme e-postası gönderilecek.'}</p><button disabled={busy} onClick={() => void act()}>İşlemi onayla</button><button disabled={busy} onClick={() => setConfirmation(null)}>İptal</button></section>}
    </>}
  </dialog>
}

export default function AdminAccountUsers({search, onSearch, page, onPage, filter, onFilter, pageSize}: Props) {
  const [data, setData] = useState<AdminAccounts | null>(null)
  const [status, setStatus] = useState('all')
  const [verified, setVerified] = useState('all')
  const [query, setQuery] = useState(search)
  const [debounced, setDebounced] = useState(search)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  useEffect(() => {setQuery(search)}, [search])
  useEffect(() => {const timer = setTimeout(() => setDebounced(query), 200); return () => clearTimeout(timer)}, [query])
  useEffect(() => {
    const controller = new AbortController()
    const params = new URLSearchParams({search: debounced, premium: filter === 'PREMIUM' ? 'premium' : filter === 'FREE' ? 'free' : 'all', status, verified, page: String(page), page_size: String(pageSize)})
    const load = async () => {
      try {
        const result = await accountRequest<AdminAccounts>(`/admin/accounts?${params}`, {signal: controller.signal})
        if (!controller.signal.aborted) {setData(result); setError('')}
      } catch (failure) {if (!controller.signal.aborted) setError(errorMessage(failure))}
      finally {if (!controller.signal.aborted) setLoading(false)}
    }
    setLoading(true)
    void load()
    const timer = setInterval(() => {if (!document.hidden) void load()}, 15000)
    return () => {controller.abort(); clearInterval(timer)}
  }, [debounced, filter, status, verified, page, pageSize, nonce])
  const selectStatus = (value: string) => {setStatus(value); onPage(1)}
  return <section className="adminContent adminAccountUsers" aria-label="Kullanıcı hesapları" aria-busy={loading}>
    <header><div><small>AYNI HESAP KAYITLARI</small><h2>Kullanıcılar <span>{data?.total ?? '—'}</span></h2><p>Kimlik, üyelik, güvenlik ve hesap durumu.</p></div><button disabled={loading} onClick={() => setNonce(value => value + 1)}><RefreshCw/>Yenile</button></header>
    <div className="adminAccountFilters"><label><Search/><input aria-label="Kullanıcı ara" placeholder="Ad, e-posta veya hesap kimliği" value={query} onChange={event => {setQuery(event.target.value); onSearch(event.target.value); onPage(1)}}/></label><select aria-label="Premium filtresi" value={filter} onChange={event => {onFilter(event.target.value); onPage(1)}}><option value="ALL">Tüm üyelikler</option><option value="PREMIUM">Premium erişimi var</option><option value="FREE">Premium erişimi yok</option></select><select aria-label="Hesap durumu filtresi" value={status} onChange={event => selectStatus(event.target.value)}><option value="all">Tüm hesaplar</option><option value="active">Aktif</option><option value="inactive">Kapalı / devre dışı</option></select><select aria-label="E-posta doğrulama filtresi" value={verified} onChange={event => {setVerified(event.target.value); onPage(1)}}><option value="all">Tüm e-postalar</option><option value="verified">Doğrulandı</option><option value="unverified">Doğrulanmadı</option></select></div>
    {error && <p className="adminAccountError" role="alert">{error}</p>}{loading && <p role="status">Kullanıcılar yükleniyor...</p>}
    <div className="adminAccountTableScroll"><table><thead><tr><th>Kullanıcı</th><th>E-posta</th><th>Rol / giriş</th><th>Üyelik</th><th>Hesap</th><th>Güvenlik</th><th>Tarihler</th><th>Detay</th></tr></thead><tbody>{!loading && !error && data?.users.map((user: AdminAccount) => <tr key={user.id}><td><div className="adminAccountUser"><i>{accountInitials(user.display_name)}</i><span><b>{user.display_name}</b><small>{user.id}</small></span></div></td><td><span>{user.email}</span><small>{user.email_verified ? <><Check/>Doğrulandı</> : <><Mail/>Doğrulanmadı</>}</small></td><td>{user.role === 'OWNER' ? 'Yönetici' : 'Kullanıcı'}<small>{user.auth_methods.join(' / ') || '—'}</small></td><td><b>{user.subscription.plan}</b><small>{user.subscription.is_premium ? 'Premium erişimi var' : 'Premium erişimi yok'} · {user.subscription.status}</small><small>Bitiş: {accountDate(user.subscription.expires_at)}</small></td><td><b data-tone={user.active ? 'positive' : 'negative'}>{user.active ? 'Aktif' : 'Kapalı / devre dışı'}</b>{user.closed_at && <small>Kapatılma: {accountDate(user.closed_at)}</small>}</td><td><small><ShieldCheck/>2FA {user.two_factor_enabled ? 'etkin' : 'kapalı'}</small><small>{user.active_sessions} oturum</small></td><td><small>Kayıt: {accountDate(user.created_at)}</small><small>Giriş: {accountDate(user.last_login)}</small></td><td><button onClick={() => setSelected(user.id)} aria-label={`${user.display_name} detayları`}><UserRound/>Görüntüle</button></td></tr>)}</tbody></table></div>
    {!loading && !error && data?.users.length === 0 && <p>Sonuç bulunamadı.</p>}
    <footer><span>{data ? `${data.total} kullanıcı · Sayfa ${page} / ${Math.max(1, Math.ceil(data.total / pageSize))}` : '—'}</span><button aria-label="Önceki kullanıcı sayfası" disabled={loading || page <= 1} onClick={() => onPage(page - 1)}><ChevronLeft/></button><button aria-label="Sonraki kullanıcı sayfası" disabled={loading || !data || page * pageSize >= data.total} onClick={() => onPage(page + 1)}><ChevronRight/></button></footer>
    {selected && <AccountDetail key={selected} id={selected} onClose={() => setSelected(null)} onChanged={() => setNonce(value => value + 1)}/>}
  </section>
}
