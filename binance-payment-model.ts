export type BinancePayment = {
  id:string; reference_code:string; amount:string; currency:'USDT'; method:'binance_manual';
  status:'created'|'pending'|'approved'|'rejected'|'expired'; transaction_number:string|null; expires_at:string;
}
export const isRecord = (value:unknown):value is Record<string,unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
export const validAmount = (value:unknown):value is string => typeof value === 'string' && /^\d+\.\d{2}$/.test(value) && Number(value) > 0
const statuses:BinancePayment['status'][] = ['created','pending','approved','rejected','expired']
function validStatus(value:unknown):value is BinancePayment['status'] {
  return statuses.some(status => status === value)
}
export function parseBinancePayment(value:unknown):BinancePayment {
  if (isRecord(value) && typeof value.id === 'string' && /^[a-f0-9]{32}$/.test(value.id)
    && typeof value.reference_code === 'string' && /^KT-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{6}$/.test(value.reference_code)
    && validAmount(value.amount) && value.currency === 'USDT' && value.method === 'binance_manual'
    && validStatus(value.status)
    && (value.transaction_number === null || typeof value.transaction_number === 'string')
    && typeof value.expires_at === 'string' && Number.isFinite(Date.parse(value.expires_at))) {
    return {
      id:value.id,reference_code:value.reference_code,amount:value.amount,currency:value.currency,method:value.method,
      status:value.status,transaction_number:value.transaction_number,expires_at:value.expires_at,
    }
  }
  throw new Error('Binance ödeme talebi doğrulanamadı.')
}
