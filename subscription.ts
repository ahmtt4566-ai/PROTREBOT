export type BillingInterval = 'monthly'|'annual'
export type PlanCode = 'STARTER'|'PRO'|'ELITE'
export type Entitlements = {
  canUseDemoTrading:boolean
  canUseLiveTrading:boolean
  canUseAdvancedAnalytics:boolean
  canUseBacktesting:boolean
  canUseAdvancedAI:boolean
  maxActivePositions:number
}
export type SubscriptionPlan = {
  code:PlanCode
  name:string
  description:string
  monthlyPrice:number
  annualPrice:number
  features:string[]
  entitlements:Entitlements
}

export const SUBSCRIPTION_PLANS:SubscriptionPlan[] = [
  {code:'STARTER',name:'Başlangıç',description:'Disiplinli deneme işlemleri için odaklanmış başlangıç planı.',monthlyPrice:19,annualPrice:190,features:['Deneme işlemlerine erişim','Temel piyasa analizi','Standart gösterge paneli','Temel işlem zekâsı','Sınırlı bot kullanımı','Sınırlı açık pozisyon','Standart stratejiler'],entitlements:{canUseDemoTrading:true,canUseLiveTrading:false,canUseAdvancedAnalytics:false,canUseBacktesting:false,canUseAdvancedAI:false,maxActivePositions:1}},
  {code:'PRO',name:'Profesyonel',description:'Aktif ve risk bilinciyle işlem yapanlar için eksiksiz çalışma alanı.',monthlyPrice:39,annualPrice:390,features:['Başlangıç planındaki her şey','Canlı işlem erişimi','Gelişmiş risk yönetimi','Gelişmiş piyasa zekâsı','Daha fazla açık pozisyon','İşlem otomasyonu','Gelişmiş performans analizi','Gelişmiş gösterge paneli özellikleri'],entitlements:{canUseDemoTrading:true,canUseLiveTrading:true,canUseAdvancedAnalytics:true,canUseBacktesting:true,canUseAdvancedAI:false,maxActivePositions:3}},
  {code:'ELITE',name:'Seçkin',description:'Ciddi operasyonlar için en yüksek zekâ, kontrol ve destek.',monthlyPrice:79,annualPrice:790,features:['Profesyonel planındaki her şey','En yüksek pozisyon sınırları','Gelişmiş yapay zekâ işlem zekâsı','Gelişmiş geriye dönük test','Gelişmiş piyasa analitiği','Öncelikli destek','Yeni özelliklere erken erişim','Gelişmiş risk kontrolleri'],entitlements:{canUseDemoTrading:true,canUseLiveTrading:true,canUseAdvancedAnalytics:true,canUseBacktesting:true,canUseAdvancedAI:true,maxActivePositions:5}},
]

export const PLAN_BY_CODE = Object.fromEntries(SUBSCRIPTION_PLANS.map(plan => [plan.code,plan])) as Record<PlanCode,SubscriptionPlan>
export const annualSavings = (plan:SubscriptionPlan) => plan.monthlyPrice * 12 - plan.annualPrice
export const subscriptionStatus = (subscription?:{status?:string;expires_at?:string|null;trial_end?:string|null}|null) => {
  if (!subscription) return 'FREE'
  const expiry = subscription.trial_end || subscription.expires_at
  if (expiry && new Date(expiry).getTime() <= Date.now()) return 'EXPIRED'
  return subscription.status === 'TRIAL' ? 'TRIAL' : subscription.status === 'ACTIVE' ? 'ACTIVE' : String(subscription.status || 'FREE')
}
