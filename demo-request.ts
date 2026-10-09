import {withRequestDeadline} from './browser-request'
import {buildDemoSavePayload} from './api'

type RecordValue = Record<string,unknown>
type ErrorMessage = (detail:unknown) => string

const record = (value:unknown):value is RecordValue => Boolean(value && typeof value === 'object' && !Array.isArray(value))
const finite = (value:unknown):value is number => typeof value === 'number' && Number.isFinite(value)
const records = (value:unknown):value is RecordValue[] => Array.isArray(value) && value.every(record)

export function validDemoLimits(value:unknown):boolean {
  return record(value) && ['max_margin_usdt','max_leverage','max_notional_usdt','max_open_positions','arm_minutes']
    .every(key => finite(value[key]) && value[key] > 0)
}

function validStatus(value:RecordValue):boolean {
  return ['configured','connected','armed'].every(key => typeof value[key] === 'boolean')
    && value.real_trading_locked === true
    && (value.armed_until === null || typeof value.armed_until === 'string')
}

function validAccount(value:RecordValue):boolean {
  return validStatus(value) && ['positions','open_orders','open_algo_orders','plans'].every(key => records(value[key]))
    && (value.limits === undefined || validDemoLimits(value.limits))
    && (!value.configured || ['wallet_balance','available_balance','margin_balance','unrealized_pnl'].every(key => finite(value[key])))
    && (value.positions as RecordValue[]).every(position => typeof position.symbol === 'string'
      && ['quantity','entry_price','mark_price','liquidation_price','unrealized_pnl'].every(key => finite(position[key])))
}

function validSummary(value:RecordValue):boolean {
  if (!['settings','auto','scanner','stream','daily','account','protection','certificate'].every(key => record(value[key]))) return false
  const settings = value.settings as RecordValue
  const scanner = value.scanner as RecordValue
  const auto = value.auto as RecordValue
  return Array.isArray(settings.allowed_symbols) && settings.allowed_symbols.every(symbol => typeof symbol === 'string')
    && typeof auto.enabled === 'boolean' && typeof auto.last_decision === 'string' && finite(auto.cycles)
    && finite((value.daily as RecordValue).remaining_loss_budget)
    && records(scanner.all_candidates)
    && records(scanner.top_candidates) && scanner.top_candidates.every(candidate => typeof candidate.symbol === 'string'
      && typeof candidate.direction === 'string' && finite(candidate.score)
      && (candidate.volatility_pct === undefined || finite(candidate.volatility_pct)))
    && records(value.journal) && value.journal.every(item => typeof item.created_at === 'string' && typeof item.message === 'string')
    && records(value.automation_trades)
}

export function checkDemoResponse(payload:unknown,kind:'status'|'account'|'summary'):asserts payload is RecordValue {
  const valid = record(payload) && (kind === 'status' ? validStatus(payload) && validDemoLimits(payload.limits)
    : kind === 'account' ? validAccount(payload) : validSummary(payload))
  if (!valid) {
    const label = kind === 'status' ? 'durum' : kind === 'account' ? 'hesap' : 'merkez'
    throw new Error(`Demo ${label} yanıtı eksik veya geçersiz. İşlem izinleri değiştirilmedi; verileri yeniden getirin.`)
  }
}

export async function demoRequest<T>(base:string,path:string,options:RequestInit|undefined,errorMessage:ErrorMessage):Promise<T> {
  return withRequestDeadline(async signal => {
    const response = await fetch(`${base}${path}`,{...options,signal})
    let payload:unknown
    try { payload = await response.json() }
    catch (error) {
      if (error instanceof SyntaxError) throw new Error('Demo sunucusundan geçerli JSON yanıtı alınamadı. Verileri yeniden getirin.')
      throw error
    }

    if (!response.ok) throw new Error(errorMessage(record(payload) ? payload.detail : undefined))
    if (path === '/status' || path === '/account' || path === '/summary') checkDemoResponse(payload,path.slice(1) as 'status'|'account'|'summary')
    return payload as T
  },{signal:options?.signal ?? undefined,timeoutMs:30_000,message:'Demo sunucusu zamanında yanıt vermedi. Sonucu yenileyin; istek otomatik tekrarlanmadı.'})
}

export async function prepareDemoTrading<T>(apiBase:string,credentials:{apiKey:string;secretKey:string}|null,errorMessage:ErrorMessage):Promise<T> {
  const post = <R,>(base:string,path:string,body?:unknown) => demoRequest<R>(base,path,{
    method:'POST',headers:{'Content-Type':'application/json'},...(body === undefined ? {} : {body:JSON.stringify(body)}),
  },errorMessage)
  const connections = `${apiBase}/exchange-connections`
  const tested = await post<{ok:boolean}>(connections,'/test',{
    mode:'TESTNET',...(credentials ? {api_key:credentials.apiKey,secret_key:credentials.secretKey} : {}),
  })
  if (tested?.ok !== true) throw new Error('Demo bağlantısı doğrulanamadı. İşlem başlatılmadı.')
  if (credentials) await post(connections,'/save',buildDemoSavePayload(credentials.apiKey,credentials.secretKey))
  await post(connections,'/activate',{mode:'TESTNET',confirmation:'TESTNET BAĞLANTIYI AÇ'})
  await post(`${apiBase}/binance-demo`,'/connect')
  const armed = await post<unknown>(`${apiBase}/binance-demo`,'/arm',{confirmation:'DEMO'})
  checkDemoResponse(armed,'status')
  if (!armed.configured || !armed.connected || !armed.armed) throw new Error('Demo bağlantısı veya süreli emir izni doğrulanamadı. İşlem başlatılmadı.')
  return armed as T
}

export async function startDemoAutomation<S,A>(apiBase:string,credentials:{apiKey:string;secretKey:string}|null,
  strategyId:'kais-original-v2-demo-v1'|undefined,flags:{enabled:boolean;send_orders:boolean},errorMessage:ErrorMessage):Promise<{status:S;summary:A}> {
  if ((strategyId || flags.enabled) && (!flags.enabled || !flags.send_orders)) {
    throw new Error('Kais Original Demo emirleri sunucu ayarında kapalı. Profil ve emir izni açılmadan otomasyon başlatılmadı.')
  }
  const status = await prepareDemoTrading<S>(apiBase,credentials,errorMessage)
  const summary = await demoRequest<unknown>(`${apiBase}/v21`,'/auto/start',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      confirmation:'DEMO OTOMATİK',...(strategyId ? {strategy_id:strategyId} : {}),
    }),
  },errorMessage)
  checkDemoResponse(summary,'summary')
  const auto = summary.auto as RecordValue
  if (!auto.enabled || auto.last_error) throw new Error(typeof auto.last_error === 'string' ? auto.last_error
    : typeof auto.last_decision === 'string' ? auto.last_decision : 'Demo otomasyonu başlatılamadı.')
  return {status,summary:summary as A}
}
