import {type FormEvent} from 'react'
import {ModBadge, ModCard, ModEmpty} from './moderator-ui'
import {accountRoleLabel} from './account-role'
import {moderatorDate} from './moderator-model'
import {actionLabels, approvalErrorMessage, approvalLabels, canRetryAgents, type Approval, type ApprovalDetail, type ApprovalStatus} from './approval-model'

export function ApprovalBadge({status, needsReview = false}: {status: ApprovalStatus; needsReview?: boolean}) {
  return <ModBadge tone={needsReview || ['stale', 'failed', 'expired'].includes(status) ? 'warning' : status === 'executed' ? 'positive' : 'neutral'}>
    {needsReview ? 'Kontrol gerekli' : approvalLabels[status]}
  </ModBadge>
}
export function ApprovalError({error, onRefresh}: {error: unknown; onRefresh: () => void}) {
  return <div className="mod-error" role="alert"><div><p>{approvalErrorMessage(error)}</p><button onClick={onRefresh}>Yenile</button></div></div>
}
export function ApprovalCards({items, owner = false, busy = false, onCancel, onSelect}: {
  items: Approval[]; owner?: boolean; busy?: boolean; onCancel?: (id: string) => void; onSelect?: (id: string) => void
}) {
  if (!items.length) return <ModCard title="Onay talepleri"><ModEmpty text="Henüz onay talebi yok."/></ModCard>
  return <ul className="mod-results">{items.map(row => <li key={row.id}><ModCard title={actionLabels[row.action_type]}>
    <div className="mod-badges"><ApprovalBadge status={row.status} needsReview={row.needs_review}/></div>
    <p className="mod-secondary">{row.action_type === 'campaign.send' ? `Duyuru: ${row.payload.campaign_id}` : `Müşteri: ${row.target_user_id}`}</p>
    {owner && <p className="mod-secondary">Talep eden: {row.requester_user_id}</p>}
    <p className="mod-support-text">{row.reason || 'Gerekçe hesap silme nedeniyle temizlendi.'}</p>
    {row.decision_note && <p className="mod-support-text">Karar notu: {row.decision_note}</p>}
    <p className="mod-secondary">Oluşturma: {moderatorDate(row.created_at)} · Son tarih: {moderatorDate(row.expires_at)}</p>
    {row.decided_at && <p className="mod-secondary">Karar: {moderatorDate(row.decided_at)}</p>}
    {row.executed_at && <p className="mod-secondary">Uygulama: {moderatorDate(row.executed_at)}</p>}
    <div className="mod-actions">{owner ? <button disabled={busy} onClick={() => onSelect?.(row.id)}>İncele</button> :
      row.status === 'pending' && <button disabled={busy} onClick={() => onCancel?.(row.id)}>Talebi iptal et</button>}</div>
  </ModCard></li>)}</ul>
}
export function ApprovalReasonForm({reason, busy, onReason, onSubmit}: {
  reason: string; busy: boolean; onReason: (value: string) => void; onSubmit: (event: FormEvent) => void
}) {
  return <form className="mod-support-note" onSubmit={onSubmit}>
    <label htmlFor="mod-approval-reason">Gerekçe</label>
    <textarea id="mod-approval-reason" minLength={10} maxLength={500} required disabled={busy} value={reason} onChange={event => onReason(event.target.value)} rows={4}/>
    <p className="mod-secondary">10–500 karakter. Talep oluşturmak hesap durumunu değiştirmez; OWNER onayı gerekir.</p>
    <button disabled={busy || reason.trim().length < 10}>{busy ? 'Gönderiliyor...' : 'Onay talebi oluştur'}</button>
  </form>
}
export function ApprovalOwnerDetail({detail, busy, note, onNote, onApprove, onReject, onRetry}: {
  detail: ApprovalDetail; busy: boolean; note: string; onNote: (value: string) => void;
  onApprove: () => void; onReject: () => void; onRetry: () => void
}) {
  const row = detail.request
  if (row.action_type === 'campaign.send') return null
  const current = detail.current_target
  return <ModCard title="Talep incelemesi">
    <h3>{actionLabels[row.action_type]}</h3><ApprovalBadge status={row.status} needsReview={row.needs_review}/>
    <p className="mod-secondary">Müşteri: {row.target_user_id} · Talep eden: {row.requester_user_id}</p>
    <p className="mod-support-text">{row.reason || 'Gerekçe hesap silme nedeniyle temizlendi.'}</p>
    <dl className="mod-facts"><div><dt>Talep anındaki durum</dt><dd>{row.target_snapshot.active ? 'Aktif' : 'Pasif'} · {accountRoleLabel(row.target_snapshot.role)} · Oturum sürümü {row.target_snapshot.auth_version}</dd></div>
      <div><dt>Güncel durum</dt><dd>{current ? `${current.active ? 'Aktif' : 'Pasif'} · ${accountRoleLabel(current.role)} · Oturum sürümü ${current.auth_version}` : 'veri yok'}</dd></div></dl>
    {detail.target_changed && <p className="mod-secondary" role="status">{row.status === 'pending' || row.status === 'stale' ?
      'Hesap durumu değişti. Bu talep uygulanamaz; yeni inceleme gerekir.' : 'Güncel hesap durumu talep anındaki durumdan farklı.'}</p>}
    {detail.active_subscription && <p className="mod-secondary" role="status">Bu müşterinin aktif aboneliği var. Bu işlem aboneliği değiştirmez veya ödemeyi durdurmaz.</p>}
    {row.decision_note && <p className="mod-support-text">Karar notu: {row.decision_note}</p>}
    {row.status === 'failed' && <p className="mod-secondary">{canRetryAgents(row) ? 'Hesap pasifleştirildi, oturumlar geçersiz. Ajan iptali henüz doğrulanamadı.' :
      row.result_code === 'protected_positions' ? 'Açık işlem, bağlantı veya otomasyon nedeniyle hesap değiştirilmedi.' : 'İşlem güvenli şekilde tamamlanamadı; kontrol gerekli.'}</p>}
    {row.status === 'executing' && <p className="mod-secondary">Kontrol gerekli. Bu talep otomatik veya tekrar onayla yeniden uygulanmaz.</p>}
    {row.status === 'pending' && <div className="mod-support-note">
      <label htmlFor="mod-approval-decision">Red gerekçesi</label>
      <textarea id="mod-approval-decision" disabled={busy} maxLength={500} value={note} onChange={event => onNote(event.target.value)} rows={3}/>
      <div className="mod-actions"><button disabled={busy || detail.target_changed} onClick={onApprove}>{busy ? 'İşlem sürüyor...' : 'Onayla'}</button>
        <button disabled={busy || !note.trim()} onClick={onReject}>Reddet</button></div>
    </div>}
    {canRetryAgents(row) && <div className="mod-actions"><button disabled={busy} onClick={onRetry}>Yalnız ajan iptalini yeniden dene</button></div>}
  </ModCard>
}
