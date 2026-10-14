import {useEffect, useRef, useState} from 'react'
import {API_BASE} from './api'

type RiskRecord = {
  risk_percent: number
  daily_loss_percent: number
  warning_level: 'NORMAL' | 'YELLOW' | 'ORANGE' | 'RED'
  daily_warning_level: 'NORMAL' | 'ORANGE' | 'RED'
  risk_acknowledged: boolean
  high_risk_confirmed: boolean
  warning_balance_usdt: number | null
}
type RiskAuditItem = {id: number; created_at: string; actor_user_id: string; before: RiskRecord; after: RiskRecord}
type RiskAuditPage = {items: RiskAuditItem[]; total: number}
const levelName = {NORMAL: 'Normal', YELLOW: 'Sarı', ORANGE: 'Turuncu', RED: 'Kırmızı'}

function validRecord(value: unknown): value is RiskRecord {
  return typeof value === 'object' && value !== null
    && 'risk_percent' in value && typeof value.risk_percent === 'number' && Number.isFinite(value.risk_percent)
    && 'daily_loss_percent' in value && typeof value.daily_loss_percent === 'number' && Number.isFinite(value.daily_loss_percent)
    && 'warning_level' in value && typeof value.warning_level === 'string' && Object.hasOwn(levelName, value.warning_level)
    && 'daily_warning_level' in value && typeof value.daily_warning_level === 'string'
    && ['NORMAL', 'ORANGE', 'RED'].includes(value.daily_warning_level)
    && 'risk_acknowledged' in value && typeof value.risk_acknowledged === 'boolean'
    && 'high_risk_confirmed' in value && typeof value.high_risk_confirmed === 'boolean'
    && 'warning_balance_usdt' in value && (value.warning_balance_usdt === null
      || typeof value.warning_balance_usdt === 'number' && Number.isFinite(value.warning_balance_usdt) && value.warning_balance_usdt > 0)
}

function validPage(value: unknown): value is RiskAuditPage {
  return typeof value === 'object' && value !== null && 'total' in value
    && typeof value.total === 'number' && Number.isSafeInteger(value.total) && value.total >= 0
    && 'items' in value && Array.isArray(value.items) && value.items.every((item: unknown) =>
      typeof item === 'object' && item !== null
      && 'id' in item && typeof item.id === 'number' && Number.isSafeInteger(item.id) && item.id > 0
      && 'created_at' in item && typeof item.created_at === 'string' && Number.isFinite(Date.parse(item.created_at))
      && 'actor_user_id' in item && typeof item.actor_user_id === 'string' && item.actor_user_id.length > 0
      && 'before' in item && validRecord(item.before) && 'after' in item && validRecord(item.after))
}

export default function AdminOriginalRisk({token}: {token: string}) {
  const [data, setData] = useState<RiskAuditPage | null>(null)
  const [offset, setOffset] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const generation = useRef(0)
  useEffect(() => {
    const id = ++generation.current
    const controller = new AbortController()
    setLoading(true)
    setError('')
    const load = async () => {
      try {
        const response = await fetch(`${API_BASE}/v22/admin/audit?action=original_live.risk_changed&limit=20&offset=${offset}`, {
          headers: {Authorization: `Bearer ${token}`}, signal: controller.signal,
        })
        if (!response.ok) throw new Error('Risk değişikliği kayıtları alınamadı; admin yetkisi ve veritabanını kontrol edin.')
        const payload: unknown = await response.json()
        if (!validPage(payload)) {
          throw new Error('Risk kayıtlarının biçimi doğrulanamadı.')
        }
        if (id === generation.current) setData(payload)
      } catch (cause: unknown) {
        if (!controller.signal.aborted && id === generation.current) setError(cause instanceof Error ? cause.message : 'Risk kayıtları okunamadı.')
      } finally {
        if (id === generation.current) setLoading(false)
      }
    }
    void load()
    return () => controller.abort()
  }, [token, offset, refreshKey])
  return <section className="adminPanelCard adminSectionCard" aria-label="Original V2 risk değişiklikleri">
    <header className="adminSectionTitle"><h2>Original V2 risk değişiklikleri</h2><button type="button" disabled={loading} onClick={() => setRefreshKey(value => value + 1)}>Yenile</button></header>
    {loading && <p role="status">Risk kayıtları yükleniyor…</p>}
    {error && <p role="alert">{error}{data && ' Önceki kayıtlar gösteriliyor; güncel liste doğrulanamadı.'}</p>}
    {!loading && !error && data?.items.length === 0 && <p>Henüz risk değişikliği yok.</p>}
    {!!data?.items.length && <div className="adminTableWrap"><table className="adminTable">
      <thead><tr><th>Zaman / kullanıcı</th><th>İşlem riski</th><th>Günlük sınır</th><th>Gösterilen uyarı</th><th>Onay / bakiye</th></tr></thead>
      <tbody>{data.items.map(item => <tr key={item.id}>
        <td>{new Date(item.created_at).toLocaleString('tr-TR')}<br/>{item.actor_user_id}</td>
        <td>%{item.before.risk_percent} → %{item.after.risk_percent}</td>
        <td>%{item.before.daily_loss_percent} → %{item.after.daily_loss_percent}</td>
        <td>İşlem: {levelName[item.after.warning_level]}<br/>Günlük: {levelName[item.after.daily_warning_level]}</td>
        <td>{item.after.high_risk_confirmed ? 'Yüksek risk kabul yazısı doğrulandı' : item.after.risk_acknowledged ? 'Bu riski anladım onayı' : 'Ek yüksek risk onayı gerekmiyor'}<br/>{item.after.warning_balance_usdt?.toLocaleString('tr-TR')} USDT</td>
      </tr>)}</tbody>
    </table></div>}
    <div className="adminPagination">
      <button type="button" disabled={loading || offset === 0} onClick={() => setOffset(value => Math.max(0, value - 20))}>Önceki</button>
      <span>{data?.total ?? '—'} kalıcı kayıt</span>
      <button type="button" disabled={loading || !data || offset + 20 >= data.total} onClick={() => setOffset(value => value + 20)}>Sonraki</button>
    </div>
  </section>
}
