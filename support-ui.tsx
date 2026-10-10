import {type FormEvent} from 'react'
import {ModBadge, ModCard, ModEmpty} from './moderator-ui'
import {moderatorDate} from './moderator-model'
import {CASE_STATUSES, priorityLabels, statusLabels, supportCanEdit, type CaseStatus, type SupportDetail, type SupportPriority, type SupportSummary} from './support-model'

export function SupportStatusBadge({status}: {status: CaseStatus}) {
  return <ModBadge tone={status === 'RESOLVED' ? 'positive' : status === 'WAITING' ? 'warning' : 'neutral'}>{statusLabels[status]}</ModBadge>
}
export function SupportPriorityBadge({priority}: {priority: SupportPriority}) {
  return <ModBadge tone={priority === 'HIGH' ? 'warning' : 'neutral'}>{priorityLabels[priority]}</ModBadge>
}
export function SupportSummaryCards({summary}: {summary: SupportSummary | null}) {
  return <div className="mod-support-summary">
    {[['Açık talepler', summary?.open_cases], ['Atanmamış', summary?.unassigned], ['Bana atanmış', summary?.assigned_to_me]].map(([label, count]) =>
      <ModCard key={label} title={String(label)}><p className="mod-primary">{count ?? 'veri yok'}</p></ModCard>)}
  </div>
}
export function SupportDetailCards({detail, manage, busy, note, status, onNoteChange, onStatusChange, onTake, onRelease, onSaveStatus, onAddNote, onCustomer}: {
  detail: SupportDetail; manage: boolean; busy: boolean; note: string; status: CaseStatus;
  onNoteChange: (value: string) => void; onStatusChange: (value: CaseStatus) => void;
  onTake: () => void; onRelease: () => void; onSaveStatus: () => void; onAddNote: (event: FormEvent) => void; onCustomer?: () => void
}) {
  const editable = supportCanEdit(detail, manage)
  return <div className="mod-stack">
    <ModCard title="Müşteri mesajı"><p className="mod-primary">{detail.subject}</p>
      <div className="mod-badges"><SupportStatusBadge status={detail.case_status}/><SupportPriorityBadge priority={detail.priority}/></div>
      <p className="mod-support-text">{detail.message || 'veri yok'}</p>
      <p className="mod-secondary">{moderatorDate(detail.created_at)}</p>
      {detail.legacy_response_note && <><h3>Eski sistemdeki cevap notu</h3><p className="mod-support-text">{detail.legacy_response_note}</p></>}
    </ModCard>
    <ModCard title="Müşteri özeti">
      <p className="mod-primary">{detail.customer.email_masked}</p><p className="mod-secondary">{detail.customer.user_id}</p>
      <div className="mod-badges"><ModBadge>{detail.customer.active ? 'Aktif' : 'Devre dışı'}</ModBadge>
        <ModBadge>{detail.customer.email_verified ? 'E-posta doğrulandı' : 'E-posta doğrulanmadı'}</ModBadge><ModBadge>{detail.customer.mfa_enabled ? 'MFA açık' : 'MFA kapalı'}</ModBadge></div>
      {onCustomer && <button className="mod-refresh" onClick={onCustomer}>Müşteri profilini görüntüle</button>}
    </ModCard>
    <ModCard title="Ekip yönetimi">
      <p className="mod-secondary">{detail.assignee_user_id === null ? 'Atanmamış' : detail.assigned_to_me ? 'Bu talep size atanmış.' : `Atanan kullanıcı: ${detail.assignee_user_id}`}</p>
      {!editable && <p className="mod-secondary">Salt okunur: düzenleme için yönetme izni ve talebin size atanması gerekir.</p>}
      <div className="mod-actions">{manage && detail.assignee_user_id === null && <button disabled={busy} onClick={onTake}>Üstlen</button>}
        {editable && <button disabled={busy} onClick={onRelease}>Bırak</button>}</div>
      <fieldset className="mod-support-status" disabled={!editable || busy}>
        <legend>Talep durumu</legend><label htmlFor="mod-case-status">Yeni durum</label>
        <div><select id="mod-case-status" value={status} onChange={event => {const next = CASE_STATUSES.find(value => value === event.target.value); if (next) onStatusChange(next)}}>
          {CASE_STATUSES.map(value => <option key={value} value={value}>{statusLabels[value]}</option>)}</select>
          <button onClick={onSaveStatus} disabled={!editable || busy || status === detail.case_status}>Durumu kaydet</button></div>
      </fieldset>
    </ModCard>
    <ModCard title="Ekip notları">
      {detail.notes.length ? <ol className="mod-support-timeline">{detail.notes.map(item => <li key={item.id}>
        <p className="mod-support-text">{item.body || 'Hesap silme işlemi kapsamında temizlendi.'}</p>
        <small>{item.author_user_id} · {moderatorDate(item.created_at)}</small>
      </li>)}</ol> : <ModEmpty text="Henüz ekip notu yok"/>}
      <form className="mod-support-note" onSubmit={onAddNote}>
        <label htmlFor="mod-team-note">Ekip içi not</label><textarea id="mod-team-note" value={note} disabled={!editable || busy} maxLength={2000} rows={4} onChange={event => onNoteChange(event.target.value)} required aria-describedby="mod-note-hint"/>
        <p id="mod-note-hint" className="mod-secondary">En fazla 2000 karakter. Anahtar, token veya kart bilgisi eklemeyin. Notlar sonradan düzenlenemez.</p>
        <button type="submit" disabled={!editable || busy || !note.trim()}>Not ekle</button>
      </form>
    </ModCard>
  </div>
}
