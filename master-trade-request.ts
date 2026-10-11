let marketRetryAt = 0
const marketFailureRetryAt = new Map<string, number>()

export async function fetchPublicMarketData(input: string, init: RequestInit = {}, timeoutMs?: number): Promise<Response> {
  const path = new URL(input, 'http://localhost').pathname
  if ((init.method && init.method !== 'GET') || !/^\/api\/(?:markets|klines\/[^/]+|analysis\/[^/]+|analysis-universe)$/.test(path)) {
    throw new Error('Public piyasa veri istemcisi yalnızca salt-okunur piyasa yollarını kabul eder.')
  }
  const remaining = Math.ceil((Math.max(marketRetryAt, marketFailureRetryAt.get(input) ?? 0) - Date.now()) / 1000)
  if (remaining > 0) throw new Error(`Binance piyasa verisi beklemede; yeniden denemeden önce ${remaining} saniye bekleyin.`)
  try {
    const response = await fetchWithTimeout(input, init, timeoutMs)
    if ([418, 429].includes(response.status) || response.status === 503 && response.headers.has('Retry-After')) {
      const header = response.headers.get('Retry-After')
      const seconds = header && /^\d+$/.test(header) ? Number(header) : header ? (Date.parse(header) - Date.now()) / 1000 : 30
      const delay = Number.isFinite(seconds) && seconds > 0 ? seconds : 30
      marketRetryAt = Math.max(marketRetryAt, Date.now() + delay * 1000)
    } else if (response.status === 503) marketFailureRetryAt.set(input, Date.now() + 30000)
    else if (response.ok) marketFailureRetryAt.delete(input)
    return response
  } catch (error) {
    if (!(error instanceof Error && error.name === 'AbortError')) marketFailureRetryAt.set(input, Date.now() + 15000)
    throw error
  }
}

export async function fetchWithTimeout(input:RequestInfo|URL, init:RequestInit = {}, timeoutMs = 15000):Promise<Response> {
  const controller = new AbortController()
  const parentAbort = () => controller.abort(init.signal?.reason)
  if (init.signal?.aborted) parentAbort()
  else init.signal?.addEventListener('abort', parentAbort, {once:true})
  const timer = window.setTimeout(() => controller.abort(new DOMException(
    'İstek zaman aşımına uğradı. İşlemi tekrar göndermeden önce backend durumunu doğrulayın.',
    'TimeoutError',
  )), timeoutMs)
  try {
    controller.signal.throwIfAborted()
    const response = await fetch(input, {...init, signal:controller.signal})
    await response.clone().arrayBuffer()
    return response
  } finally {
    window.clearTimeout(timer)
    init.signal?.removeEventListener('abort', parentAbort)
  }
}
