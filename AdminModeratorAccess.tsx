import {useCallback, useEffect, useRef, useState} from 'react'
import {accountRequest} from './account-settings-api'
import {MODERATOR_PERMISSIONS, permissionLabels, type ModeratorPermission} from './moderator-model'

type Grants = {user_id: string; permissions: ModeratorPermission[]}

export default function AdminModeratorAccess({id, role, onChanged, onBusyChange, disabled = false}: {
  id: string; role: string; onChanged: () => Promise<void>; onBusyChange: (busy: boolean) => void; disabled?: boolean
}) {
  const [grants, setGrants] = useState<Grants | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const path = `/admin/users/${encodeURIComponent(id)}`
  const load = useCallback(async (signal?: AbortSignal) => {
    const value = await accountRequest<Grants>(`${path}/permissions`, {signal})
    if (!signal?.aborted) {setGrants(value); setError('')}
  }, [path])
  useEffect(() => {
    setGrants(null); setError('')
    if (role !== 'MODERATOR') return
    const controller = new AbortController()
    void load(controller.signal).catch(failure => {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : 'İzinler alınamadı.')
    })
    return () => controller.abort()
  }, [role, load])
  const run = async (operation: () => Promise<void>) => {
    if (inFlight.current || disabled) return
    inFlight.current = true; setBusy(true); onBusyChange(true); setError('')
    try {await operation()}
    catch (failure) {setError(failure instanceof Error ? failure.message : 'İşlem tamamlanamadı. Yeniden deneyin.')}
    finally {inFlight.current = false; setBusy(false); onBusyChange(false)}
  }
  const changeRole = () => {
    if (!window.confirm('Rol değişince kullanıcının tüm oturumları kapanır. Devam edilsin mi?')) return
    void run(async () => {
      await accountRequest(`${path}/role`, {method: 'PATCH', body: JSON.stringify({role: role === 'MODERATOR' ? 'CUSTOMER' : 'MODERATOR'})})
      await onChanged()
    })
  }
  const changePermission = (permission: ModeratorPermission, checked: boolean) => void run(async () => {
    await accountRequest(checked ? `${path}/permissions` : `${path}/permissions/${permission}`, {
      method: checked ? 'POST' : 'DELETE', ...(checked ? {body: JSON.stringify({permission})} : {}),
    })
    await load()
  })
  return <section aria-label="Moderatör rolü ve izinleri">
    <h3>Moderatör erişimi</h3>
    <div className="adminAccountActions"><button disabled={busy || disabled || role === 'OWNER'} onClick={changeRole}>{role === 'MODERATOR' ? 'Moderatörlüğü kaldır' : 'Moderatör yap'}</button></div>
    {role === 'MODERATOR' && grants && <fieldset disabled={busy || disabled}><legend>Moderatör izinleri</legend>
      {MODERATOR_PERMISSIONS.map(permission => <p key={permission}><label><input type="checkbox" checked={grants.permissions.includes(permission)} onChange={event => changePermission(permission, event.target.checked)}/> {permissionLabels[permission]}</label></p>)}
    </fieldset>}
    {role === 'MODERATOR' && !grants && !error && <p role="status">İzinler yükleniyor...</p>}
    {error && <p className="adminAccountError" role="alert">{error} {role === 'MODERATOR' && <button disabled={busy || disabled} onClick={() => void run(() => load())}>Yeniden dene</button>}</p>}
  </section>
}
