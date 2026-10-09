import {useCallback, useEffect, useState} from 'react'
import './original-demo-controls.css'

export const ORIGINAL_DEMO_ID = 'kais-original-v2-demo-v1'
export type DemoStrategy = 'default' | typeof ORIGINAL_DEMO_ID

type DryPlan = {id:string;symbol:string;decision:string;status:string;strategy_id:string;order_authorized:false;execution_connected:false}
type OriginalStatus = {
  strategy_id:string
  flags:{enabled:boolean;send_orders:boolean}
  profile_hash:string
  policy_hash:string
  selected_strategy_id:string|null
  dry_run_plans:DryPlan[]
}
type Request = <T>(path:string, options?:RequestInit) => Promise<T>

function validPlan(plan:DryPlan):boolean {
  return Boolean(plan && typeof plan.id === 'string' && typeof plan.symbol === 'string'
    && ['BUY','SELL','WAIT'].includes(plan.decision) && plan.status === 'DRY_RUN'
    && plan.strategy_id === ORIGINAL_DEMO_ID && plan.order_authorized === false && plan.execution_connected === false)
}

function checkedStatus(payload:OriginalStatus):OriginalStatus {
  if (payload?.strategy_id !== ORIGINAL_DEMO_ID || typeof payload.flags?.enabled !== 'boolean'
    || typeof payload.flags?.send_orders !== 'boolean' || !Array.isArray(payload.dry_run_plans)
    || typeof payload.profile_hash !== 'string' || typeof payload.policy_hash !== 'string'
    || !/^[a-f0-9]{64}$/.test(payload.profile_hash) || !/^[a-f0-9]{64}$/.test(payload.policy_hash)
    || ![null,ORIGINAL_DEMO_ID].includes(payload.selected_strategy_id)
    || !payload.dry_run_plans.every(validPlan)) {
    throw new Error('Original Demo durum yanıtı geçersiz. Başlatmadan önce yeniden deneyin.')
  }
  return payload
}

export default function OriginalDemoControls({strategy,onStrategyChange,request,armed,enabled,confirmation,busy,onRecorded}:{
  strategy:DemoStrategy;onStrategyChange:(strategy:DemoStrategy)=>void;request:Request;armed:boolean;enabled:boolean
  confirmation:string;busy:boolean;onRecorded:()=>Promise<unknown>
}) {
  const [status,setStatus] = useState<OriginalStatus|null>(null)
  const [error,setError] = useState('')
  const [running,setRunning] = useState(false)
  const [result,setResult] = useState('')
  const refresh = useCallback(async () => {
    try { const value = checkedStatus(await request<OriginalStatus>('/original-v2/status'));setStatus(value);setError('');return value }
    catch (failure) { setStatus(null);setError(failure instanceof Error ? failure.message : 'Original Demo durumu alınamadı.');return null }
  },[request])
  useEffect(() => { void refresh(); const timer = setInterval(() => { void refresh() },10000);return () => clearInterval(timer) },[refresh])
  useEffect(() => {
    if (enabled && status) onStrategyChange(status.selected_strategy_id === ORIGINAL_DEMO_ID ? ORIGINAL_DEMO_ID : 'default')
  },[enabled,status,onStrategyChange])
  const dryRun = async () => {
    setRunning(true);setError('');setResult('')
    try {
      const response = await request<{dry_run:boolean;order:unknown;orders_sent:number;plans:DryPlan[]}>('/original-v2/dry-run',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({strategy_id:ORIGINAL_DEMO_ID,confirmation:confirmation.trim()}),
      })
      if (response.dry_run !== true || response.order !== null || response.orders_sent !== 0 || !Array.isArray(response.plans)
        || response.plans.length !== 8 || !response.plans.every(validPlan) || new Set(response.plans.map(plan => plan.symbol)).size !== 8) {
        throw new Error('Dry-run yanıtı doğrulanamadı; plan/journal kaydını kontrol edin.')
      }
      await onRecorded()
      if (!await refresh()) throw new Error('Karar kaydedildi fakat güncel durum alınamadı; kayıtları yeniden getirin.')
      setResult(`${response.plans.length} karar kaydı; gönderilen emir: 0.`)
    } catch (failure) { setError(failure instanceof Error ? failure.message : 'Dry-run tamamlanamadı.') }
    finally { setRunning(false) }
  }
  return <section className="originalDemoControls" aria-label="Kais Original Demo profili">
    <label>Demo stratejisi<select aria-label="Demo stratejisi" value={strategy} disabled={enabled || busy || running}
      onChange={event => onStrategyChange(event.target.value === ORIGINAL_DEMO_ID ? ORIGINAL_DEMO_ID : 'default')}>
      <option value="default">Mevcut Demo (sunucu varsayılanı)</option>
      <option value={ORIGINAL_DEMO_ID}>Kais Original v2</option>
    </select></label>
    <dl><div><dt>Original profil anahtarı</dt><dd>{status ? status.flags.enabled ? 'Açık' : 'Kapalı' : '—'}</dd></div>
      <div><dt>Original emir izni</dt><dd>{status ? status.flags.send_orders ? 'Açık' : 'Kapalı' : '—'}</dd></div>
      <div><dt>Original yürütme modu</dt><dd>{status ? status.flags.enabled && status.flags.send_orders ? 'Demo emir yolu izinli; arm/onay gerekir' : 'Yalnız karar / dry-run' : '—'}</dd></div>
      <div><dt>DEMO arm</dt><dd>{armed ? 'Açık' : 'Kapalı'}</dd></div></dl>
    <p>Bayraklar sunucu ayarıdır; bu ekran onları değiştirmez. Anahtar açıkken sunucu varsayılanı da Original kullanır.</p>
    {strategy === ORIGINAL_DEMO_ID && <>
      <p>8 sabit sembol · 3x · risk 3 USDT · en fazla 5 pozisyon / aynı yönde 2 · UTC günde 3 giriş / zarar 10 USDT / 3 ardışık kayıp.</p>
      <p>TP1 %60 (miktar/minimum kontrolü), TP2 kapanışı yok; kalan TP3 veya başlangıç stop’u. BE/trailing yok. Legacy ayar alanları bu profili değiştirmez.</p>
      <button type="button" disabled={busy || running || enabled || !armed || !status || confirmation.trim().toUpperCase() !== 'DEMO OTOMATİK'}
        onClick={() => { void dryRun() }}>{running ? 'Karar hesaplanıyor…' : 'Emirsiz tek karar döngüsü'}</button>
      <p>Önce otomasyonu durdurun, DEMO arm açın ve ikinci onay alanına DEMO OTOMATİK yazın. Bu düğme iki bayrak açık olsa bile emir göndermez ve otomasyonu başlatmaz.</p>
      <details><summary>Plan kayıtları ve sabit politika</summary>
        <p>Strateji: {ORIGINAL_DEMO_ID}</p><p>Profil SHA-256: {status?.profile_hash || '—'}</p><p>Politika SHA-256: {status?.policy_hash || '—'}</p>
        <ul>{status?.dry_run_plans.map(plan => <li key={plan.id}>{plan.symbol} · {plan.decision} · {plan.status} · {plan.id}</li>)}</ul>
      </details>
    </>}
    {error && <p role="alert">{error} <button type="button" onClick={() => { void refresh() }}>Durumu yeniden getir</button></p>}
    {result && <p role="status">{result}</p>}
  </section>
}
