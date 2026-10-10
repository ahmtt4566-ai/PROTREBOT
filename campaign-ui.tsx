import {ModBadge, ModCard} from './moderator-ui'
import {audienceLabels, campaignStatusLabels, type Campaign, type CampaignPreview, type CampaignStatus} from './campaign-model'
import {moderatorDate} from './moderator-model'

export function CampaignBadge({status}: {status: CampaignStatus}) {
  return <ModBadge tone={status === 'failed' ? 'warning' : status === 'sent' ? 'positive' : 'neutral'}>{campaignStatusLabels[status]}</ModBadge>
}
export function CampaignPreviewView({preview}: {preview: CampaignPreview}) {
  return <ModCard title="E-posta önizlemesi"><div className="mod-stack">
    <strong>{preview.subject}</strong><p className="mod-secondary">{audienceLabels[preview.audience]} · {preview.recipient_count} alıcı · {preview.opt_out_count} hesap duyurulardan çıkmış; atlanacak.</p>
    {preview.warnings.map(warning => <p key={warning} className="mod-warning" role="alert">{warning}</p>)}
    {preview.approximate_schedule && <p className="mod-secondary">Zamanlama yaklaşık: servis uyuduğunda gönderim gecikebilir.</p>}
    {!preview.delivery_enabled && <p className="mod-warning">Duyuru gönderimi kapalı. Kayıtlar kuyrukta bekler; gönderildi sayılmaz.</p>}
    <p className="mod-secondary">Altbilgideki çıkış bağlantısı her alıcıya özel imzalanır. Önizlemede kendi bağlantınız gösterilir.</p>
    <pre className="mod-campaign-text">{preview.text}</pre>
  </div></ModCard>
}
export function CampaignReport({campaign}: {campaign: Campaign}) {
  return <ModCard title="Gönderim raporu"><CampaignBadge status={campaign.status}/><dl className="mod-facts">
    <div><dt>Alıcı</dt><dd>{campaign.recipient_count}</dd></div><div><dt>Gönderildi</dt><dd>{campaign.sent_count}</dd></div>
    <div><dt>Bekliyor</dt><dd>{campaign.queued_count}</dd></div><div><dt>Başarısız</dt><dd>{campaign.failed_count}</dd></div>
    <div><dt>Atlandı</dt><dd>{campaign.skipped_count}</dd></div><div><dt>Alıcı grubu</dt><dd>{audienceLabels[campaign.audience]}</dd></div>
    <div><dt>Başlangıç</dt><dd>{moderatorDate(campaign.started_at)}</dd></div><div><dt>Bitiş</dt><dd>{moderatorDate(campaign.finished_at)}</dd></div>
  </dl><p className="mod-secondary">Gönderildi: sağlayıcının kabul ettiği iletiler. Teslim/bounce ve şikâyet takibi bu aşamada yok.</p></ModCard>
}
export function CampaignConfirmation({preview, busy, onConfirm, onCancel}: {preview: CampaignPreview; busy: boolean; onConfirm: () => void; onCancel: () => void}) {
  const dialog = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const previous = document.activeElement
    dialog.current?.focus()
    return () => {if (previous instanceof HTMLElement && previous.isConnected) previous.focus()}
  }, [])
  return <div ref={dialog} tabIndex={-1} className="mod-campaign-confirm" role="dialog" aria-modal="true" aria-labelledby="mod-send-title" onKeyDown={event => {
    if (event.key === 'Escape' && !busy) {event.preventDefault(); onCancel()}
    if (event.key === 'Tab') {
      const buttons = dialog.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')
      if (!buttons?.length) {event.preventDefault(); return}
      const first = buttons[0], last = buttons[buttons.length - 1]
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {event.preventDefault(); last.focus()}
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) {event.preventDefault(); first.focus()}
    }
  }}>
    <ModCard title="Gönderimi onayla"><h3 id="mod-send-title">{preview.recipient_count} alıcı için bilgilendirme duyurusu</h3>
      <p>{preview.opt_out_count} hesap tercihi nedeniyle atlanacak. Bu işlem reklam/kampanya gönderimi değildir.</p>
      {preview.warnings.map(warning => <p key={warning} className="mod-warning">{warning}</p>)}
      <div className="mod-actions"><button disabled={busy} onClick={onConfirm}>{busy ? 'İşlem sürüyor...' : 'Alıcı sayısını onayla ve gönder'}</button>
        <button disabled={busy} onClick={onCancel}>Vazgeç</button></div>
    </ModCard>
  </div>
}
import {useEffect, useRef} from 'react'
