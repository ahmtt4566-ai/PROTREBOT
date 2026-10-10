import {type FormEvent, type ReactNode} from 'react'
import {AlertCircle, Inbox, Search} from 'lucide-react'
import {ModeratorRequestError, moderatorDate, moderatorErrorMessage, subscriptionLabels, type CustomerResource} from './moderator-model'

export function ModCard({title, children}: {title: string; children: ReactNode}) {
  return <section className="mod-card"><h2>{title}</h2>{children}</section>
}
export function ModBadge({children, tone = 'neutral'}: {children: ReactNode; tone?: 'neutral' | 'positive' | 'warning'}) {
  return <span className={`mod-badge mod-badge-${tone}`}>{children}</span>
}
export function ModEmpty({text = 'Henüz veri yok'}: {text?: string}) {
  return <div className="mod-empty"><Inbox aria-hidden="true"/><p>{text}</p></div>
}
export function ModError({error, onRetry}: {error: unknown; onRetry?: () => void}) {
  const known = error instanceof ModeratorRequestError ? error : null
  return <div className="mod-error" role="alert"><AlertCircle aria-hidden="true"/>
    <div><p>{known ? known.message : moderatorErrorMessage(503)}</p>
      {known?.code === 'mfa_required' ? <a href="/settings">Profil ve güvenliğe git</a> :
        known?.status === 401 ? <a href="/login">Giriş yap</a> : onRetry && <button onClick={onRetry}>Yeniden dene</button>}
    </div>
  </div>
}
export function ModSearch({value, onChange, onSubmit, busy, customerSearch = false}: {
  value: string; onChange: (value: string) => void; onSubmit: (event: FormEvent) => void; busy: boolean; customerSearch?: boolean
}) {
  return <form className="mod-search" onSubmit={onSubmit}>
    <label htmlFor="mod-search-input">{customerSearch ? 'Müşteri ara' : 'Kullanıcı numarası'}</label>
    <div><Search aria-hidden="true"/><input id="mod-search-input" value={value} onChange={event => onChange(event.target.value)} autoComplete="off" spellCheck={false} aria-describedby="mod-search-hint" required={!customerSearch}/><button disabled={busy} type="submit">{busy ? 'Yükleniyor...' : customerSearch ? 'Ara' : 'Görüntüle'}</button></div>
    <p id="mod-search-hint">{customerSearch ? 'Müşterinin tam e-postası veya kullanıcı numarası. Kısmi e-posta ile arama yapılamaz.' : 'Müşterinin kullanıcı numarasını yazın.'}</p>
  </form>
}
export function ModResourceView({resource}: {resource: CustomerResource}) {
  const {kind, data} = resource
  if (kind === 'profile') return <ModCard title="Müşteri profili">
    <p className="mod-primary">{data.email_masked}</p><p className="mod-secondary">{data.user_id}</p>
    <div className="mod-badges"><ModBadge tone={data.active ? 'positive' : 'neutral'}>{data.active ? 'Aktif' : 'Devre dışı'}</ModBadge>
      <ModBadge>{data.email_verified ? 'E-posta doğrulandı' : 'E-posta doğrulanmadı'}</ModBadge>
      <ModBadge>{data.mfa_enabled ? 'MFA açık' : 'MFA kapalı'}</ModBadge></div>
    <dl className="mod-facts"><div><dt>Rol</dt><dd>Müşteri</dd></div><div><dt>Kayıt tarihi</dt><dd>{moderatorDate(data.created_at)}</dd></div></dl>
  </ModCard>
  if (kind === 'subscription') return <ModCard title="Abonelik özeti">
    <p className="mod-secondary">{data.user_id}</p>
    {data.plan === null && data.subscription_status === null ? <ModEmpty/> : <>
      <p className="mod-primary">{data.plan === 'TRIAL' ? 'Deneme' : data.plan === 'MASTER_MODE' ? 'Master Mode' : 'veri yok'}</p>
      <div className="mod-badges"><ModBadge tone={data.subscription_status === 'ACTIVE' ? 'positive' : 'neutral'}>{data.subscription_status ? subscriptionLabels[data.subscription_status] : 'veri yok'}</ModBadge>
        {data.cancel_at_period_end === true && <ModBadge tone="warning">İptal bekliyor</ModBadge>}</div>
    </>}
    <dl className="mod-facts"><div><dt>Dönem bitişi</dt><dd>{moderatorDate(data.current_period_end)}</dd></div>
      <div><dt>İptal bekliyor mu?</dt><dd>{data.cancel_at_period_end === null ? 'veri yok' : data.cancel_at_period_end ? 'Evet' : 'Hayır'}</dd></div></dl>
  </ModCard>
  return <ModCard title="Ödeme özeti"><p className="mod-secondary">{data.user_id}</p>
    {data.payment_status === 'veri yok' ? <ModEmpty/> : <ModBadge tone={data.payment_status === 'FAILED' ? 'warning' : 'positive'}>{data.payment_status === 'FAILED' ? 'Ödeme başarısız' : 'Ödendi'}</ModBadge>}
    <dl className="mod-facts"><div><dt>Son başarısız ödeme tarihi</dt><dd>{moderatorDate(data.last_failed_payment_at)}</dd></div></dl>
    <p className="mod-secondary">Yalnızca veritabanında kayıtlı durum gösterilir.</p>
  </ModCard>
}
