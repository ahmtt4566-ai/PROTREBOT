import {API_BASE} from './api'
import {withRequestDeadline} from './browser-request'
import {moderatorApiRequest} from './moderator-api'
import {ModeratorRequestError} from './moderator-model'

export async function approvalRequest<T>(owner: boolean, path: string, parse: (value: unknown) => T, signal: AbortSignal, options: RequestInit = {}): Promise<T> {
  if (!owner) return moderatorApiRequest(`/approvals${path}`, parse, signal, options)
  return withRequestDeadline(async transportSignal => {
    const headers = new Headers(options.headers)
    headers.set('X-Requested-With', 'XMLHttpRequest')
    if (options.body) headers.set('Content-Type', 'application/json')
    const response = await fetch(`${API_BASE}/v22/admin/approvals${path}`, {...options, headers, credentials: 'same-origin', signal: transportSignal})
    if (!response.ok) throw new ModeratorRequestError(response.status)
    return parse(await response.json())
  }, {signal, timeoutMs: 20000})
}
