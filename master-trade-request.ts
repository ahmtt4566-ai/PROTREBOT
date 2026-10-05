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
