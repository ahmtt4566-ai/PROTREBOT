export type BillingInterval = 'monthly'
export type PlanCode = 'TRIAL'|'MASTER_MODE'
export type Entitlements = { canAccessMasterTrade:boolean }
export type SubscriptionPlan = {
  code:PlanCode
  name:string
  description:string
  monthlyPrice:number
  features:string[]
  entitlements:Entitlements
}

export const SUBSCRIPTION_PLANS:SubscriptionPlan[] = [
  {code:'TRIAL',name:'7-Day Free Trial',description:'Try the full Master Trade workspace for seven days.',monthlyPrice:119.90,features:['Master Trade access','Payment method required','Automatic conversion after 7 days','Cancel anytime'],entitlements:{canAccessMasterTrade:true}},
  {code:'MASTER_MODE',name:'Master Mode',description:'The complete premium trading workspace for active operators.',monthlyPrice:119.90,features:['Immediate Master Trade access','Automatic monthly renewal','Premium execution workspace','Cancel anytime'],entitlements:{canAccessMasterTrade:true}},
]

export const PLAN_BY_CODE = Object.fromEntries(SUBSCRIPTION_PLANS.map(plan => [plan.code,plan])) as Record<PlanCode,SubscriptionPlan>
export const subscriptionStatus = (subscription?:{status?:string}|null) => String(subscription?.status || 'EXPIRED')
