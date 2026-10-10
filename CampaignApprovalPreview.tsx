import {type ApprovalDetail} from './approval-model'
import {CampaignPreviewView} from './campaign-ui'
import {ModCard} from './moderator-ui'

export default function CampaignApprovalPreview({detail, busy, note, onNote, onApprove, onReject}: {
  detail: ApprovalDetail; busy: boolean; note: string; onNote: (value: string) => void; onApprove: () => void; onReject: () => void
}) {
  if (!detail.campaign_preview) return <ModCard title="Duyuru önizlemesi"><p role="alert">Önizleme alınamadı. Talebi yenileyin.</p></ModCard>
  return <div className="mod-stack"><CampaignPreviewView preview={detail.campaign_preview}/>
    <ModCard title="Duyuru gönderim kararı"><p>Bu işlem hesap durumunu değiştirmez; yalnız bilgilendirme e-postası kuyruğunu başlatır.</p>
      {detail.target_changed && <p className="mod-warning" role="alert">İçerik değişti. Yeniden incele; bu talep uygulanamaz.</p>}
      {detail.request.status === 'pending' && <div className="mod-support-note">
        <label>Red gerekçesi<textarea disabled={busy} maxLength={500} value={note} onChange={event => onNote(event.target.value)}/></label>
        <div className="mod-actions"><button disabled={busy || detail.target_changed} onClick={onApprove}>{busy ? 'İşlem sürüyor...' : 'Onayla'}</button>
          <button disabled={busy || !note.trim()} onClick={onReject}>Reddet</button></div>
      </div>}
    </ModCard>
  </div>
}
