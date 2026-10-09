export const BROWSER_REQUEST_TIMEOUT_MS = 15_000

export class BrowserRequestTimeoutError extends Error {
  constructor(message = 'Sunucu yanıt vermedi. Bağlantınızı kontrol edip yeniden deneyin.') {
    super(message)
    this.name = 'BrowserRequestTimeoutError'
  }
}

export async function withRequestDeadline<T>(
  operation:(signal:AbortSignal) => Promise<T>,
  options:{signal?:AbortSignal;timeoutMs?:number;message?:string} = {},
):Promise<T> {
  const timeoutMs = options.timeoutMs ?? BROWSER_REQUEST_TIMEOUT_MS
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new RangeError('Request deadline must be positive and finite.')
  if (options.signal?.aborted) throw options.signal.reason
  const controller = new AbortController()
  let cancel:(reason:unknown) => void = () => {}
  const cancelled = new Promise<never>((_,reject) => { cancel = reject })
  const abort = (reason:unknown) => { controller.abort(reason); cancel(reason) }
  const onAbort = () => abort(options.signal?.reason)
  options.signal?.addEventListener('abort',onAbort,{once:true})
  const timer = setTimeout(() => abort(new BrowserRequestTimeoutError(options.message)),timeoutMs)
  try {
    return await Promise.race([Promise.resolve().then(() => operation(controller.signal)),cancelled])
  } finally {
    clearTimeout(timer)
    options.signal?.removeEventListener('abort',onAbort)
  }

}

export function loadWorkspaceWithDeadline<T>(load:() => Promise<T>):Promise<T> {
  return withRequestDeadline(() => load(),{message:'Ekran dosyaları zamanında yüklenemedi. Paneli yeniden yükleyin.'})
}
