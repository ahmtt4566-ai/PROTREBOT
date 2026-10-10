import {useEffect, useState} from 'react'
import {approvalRequest} from './approval-api'
import {parseNotificationSummary, type NotificationSummary} from './notification-model'
import {NotificationSummaryLine} from './notification-ui'

export default function AdminNotificationSummary({refresh}: {refresh: number}) {
  const [summary, setSummary] = useState<NotificationSummary | null>(null)
  const [error, setError] = useState(false)
  const [version, setVersion] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setSummary(null); setError(false)
    void approvalRequest(true, '/notifications/summary', parseNotificationSummary, controller.signal)
      .then(value => {if (!controller.signal.aborted) setSummary(value)})
      .catch(() => {if (!controller.signal.aborted) setError(true)})
    return () => controller.abort()
  }, [refresh, version])
  return <div><NotificationSummaryLine summary={summary}/>
    {error && <div className="mod-actions"><p className="mod-secondary" role="alert">Bildirim sayıları alınamadı.</p><button onClick={() => setVersion(value => value + 1)}>Bildirimleri yenile</button></div>}</div>
}
