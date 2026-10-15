import {API_BASE} from './api'
import {fetchWithTimeout} from '../../master-trade-request'
import type {SharedConnectionStatus, SharedLiveStatus} from './LiveTradingPanel'

const normalizeCredentialFingerprint = (value: string | null | undefined) => (value || '').trim().toUpperCase().replace(/^SHA256:/, '')

export function liveCredentialStatusMatches(status: SharedLiveStatus, connections: SharedConnectionStatus | null): boolean {
  const connection = connections?.connections?.LIVE
  if (connection?.active !== true) return status.credentials?.configured !== true && status.connected !== true
  if (status.credentials?.configured !== true) return false
  const saved = normalizeCredentialFingerprint(connection.fingerprint)
  return Boolean(saved && saved === normalizeCredentialFingerprint(status.credentials?.fingerprint))
}

export async function readLiveStatusResponses(onConnections?: (connections: SharedConnectionStatus) => void): Promise<[Response, Response]> {
  for (let attempt = 0; ; attempt++) {
    try {
      const connection = await fetchWithTimeout(`${API_BASE}/exchange-connections/status`, {cache: 'no-store'})
      if (!connection.ok) {
        if (attempt === 0 && [502, 503, 504].includes(connection.status)) {
          await new Promise(resolve => setTimeout(resolve, 350))
          continue
        }
        const payload: unknown = await connection.json()
        const detail = payload && typeof payload === 'object' && 'detail' in payload && typeof payload.detail === 'string'
          ? payload.detail : `Kayıtlı anahtar durumu okunamadı (HTTP ${connection.status}).`
        throw new Error(detail)
      }
      const connections = await connection.clone().json() as SharedConnectionStatus
      if (!connections || typeof connections.connections?.LIVE?.configured !== 'boolean') {
        throw new Error('Kayıtlı anahtar durum yanıtı eksik veya geçersiz; LIVE doğrulanamadı.')
      }
      onConnections?.(connections)
      const status = await fetchWithTimeout(`${API_BASE}/v25/status`, {cache: 'no-store'})
      const payload = status.ok ? await status.clone().json() as SharedLiveStatus : null
      const wallet = payload?.account?.wallet_balance
      const pending = status.ok && (!payload || !liveCredentialStatusMatches(payload, connections)
        || connections.connections?.LIVE?.configured === false
        || (connections.connections?.LIVE?.active === true
          && (payload.connected !== true || typeof wallet !== 'number' || !Number.isFinite(wallet))))
      if (attempt === 0 && (pending || [502, 503, 504].includes(status.status))) {
        await new Promise(resolve => setTimeout(resolve, 350))
        continue
      }
      return [status, connection]
    } catch (error) {
      if (attempt === 0 && (error instanceof TypeError || error instanceof Error && error.name === 'BrowserRequestTimeoutError')) {
        await new Promise(resolve => setTimeout(resolve, 350))
        continue
      }
      if (error instanceof TypeError) throw new Error('Sunucuya ulaşılamadı; kayıtlı anahtar ve Binance bakiyesi doğrulanamadı.')
      if (error instanceof SyntaxError) throw new Error('Sunucu geçerli bir durum yanıtı döndürmedi; LIVE doğrulanamadı.')
      throw error
    }
  }
}
