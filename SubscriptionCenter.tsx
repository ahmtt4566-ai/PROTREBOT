import { useEffect, useState } from 'react'
import { Check, CreditCard, ExternalLink, Lock, Receipt, ShieldCheck, Sparkles, XCircle } from 'lucide-react'
import { API_BASE, userSessionToken } from './api'
import { SUBSCRIPTION_PLANS, type PlanCode } from './subscription'

type SubscriptionView = {
  status: string
  plan: PlanCode | null
  master_trade_access: boolean
  trial_end?: string | null
  current_period_end?: string | null
  next_payment_amount?: number | null
  cancel_at_period_end?: boolean
  features?: string[]
}
type ApiError = { detail?: string }

const formatDate = (value?: string | null) => value ? new Date(value).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }) : 'Not available'
const daysRemaining = (value?: string | null) => value ? Math.max(0, Math.ceil((new Date(value).getTime() - Date.now()) / 86400000)) : 0

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = userSessionToken()
  const headers = new Headers(options.headers)
  headers.set('Authorization', `Bearer ${token}`)
  if (options.body) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_BASE}/v22${path}`, { ...options, headers })
  const payload = await response.json().catch(() => null) as T & ApiError
  if (!response.ok) throw new Error(payload?.detail || 'The billing request could not be completed.')
  return payload
}

export default function SubscriptionCenter({ mode, onNavigate }: { mode: 'pricing' | 'billing'; onNavigate: (target: 'pricing' | 'billing' | 'live') => void }) {
  const [subscription, setSubscription] = useState<SubscriptionView | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const authenticated = Boolean(userSessionToken())

  useEffect(() => {
    if (!authenticated) return
    void request<SubscriptionView>('/subscription').then(setSubscription).catch(() => setSubscription(null))
  }, [authenticated])

  const startCheckout = async (plan: PlanCode) => {
    if (!authenticated) { setNotice('Sign in before starting a subscription.'); onNavigate('live'); return }
    setBusy(true); setError(''); setNotice('')
    try {
      const payload = await request<{ checkout_url?: string }>('/subscription/checkout', { method: 'POST', body: JSON.stringify({ plan, billing_interval: 'monthly' }) })
      if (!payload.checkout_url) throw new Error('Checkout could not be created. Please try again.')
      window.location.assign(payload.checkout_url)
    } catch (exception) { setError(exception instanceof Error ? exception.message : 'Checkout could not be created. Please try again.') }
    finally { setBusy(false) }
  }

  const cancel = async (immediate: boolean) => {
    if (immediate && !window.confirm('Cancel the subscription immediately? Stripe will confirm the final access state.')) return
    setBusy(true); setError(''); setNotice('')
    try {
      const current = await request<SubscriptionView>('/subscription/cancel', { method: 'POST', body: JSON.stringify({ immediate }) })
      setSubscription(current)
      setNotice(immediate ? 'Immediate cancellation requested. Waiting for Stripe confirmation.' : 'Cancellation scheduled for the end of the current billing period.')
    } catch (exception) { setError(exception instanceof Error ? exception.message : 'The subscription could not be cancelled.') }
    finally { setBusy(false) }
  }

  const openPortal = async () => {
    setBusy(true); setError(''); setNotice('')
    try {
      const payload = await request<{ url?: string }>('/subscription/customer-portal', { method: 'POST' })
      if (!payload.url) throw new Error('Billing management is unavailable.')
      window.location.assign(payload.url)
    } catch (exception) { setError(exception instanceof Error ? exception.message : 'Billing management is unavailable.') }
    finally { setBusy(false) }
  }

  const status = subscription?.status || 'EXPIRED'
  const hasAccess = Boolean(subscription?.master_trade_access)
  const trialing = status === 'TRIALING'
  const pastDue = status === 'PAST_DUE'
  const activePlan = subscription?.plan ? SUBSCRIPTION_PLANS.find(plan => plan.code === subscription.plan) : null
  const nextPayment = subscription?.next_payment_amount != null ? `$${subscription.next_payment_amount.toFixed(2)}` : 'Not available'
  const statusLabel = trialing ? 'Free trial' : status.replace('_', ' ')

  if (mode === 'billing') return <main className="subscriptionPage subscriptionBilling">
    <header className="subscriptionPageHeader"><div><span>BILLING</span><h1>Subscription control center</h1><p>Review your plan, Master Trade access, renewal timing, and Stripe-managed billing from one place.</p></div><button onClick={() => onNavigate('pricing')}><Sparkles />VIEW PLANS</button></header>
    <section className={`subscriptionStatusCard subscriptionStatus-${status.toLowerCase()}`}>
      <div className="subscriptionPlanHeading"><div className="subscriptionPlanHeadingMain"><div className="subscriptionPlanIcon"><Lock /></div><div className="subscriptionPlanHeadingText"><small>CURRENT PLAN</small><h2>{activePlan?.name || 'No active plan'}</h2><p>{activePlan?.description || 'Choose a plan to unlock Master Trade access.'}</p></div></div><span className="subscriptionStatusBadge">{statusLabel}</span></div>
      <div className="subscriptionStatusTop"><div><small>MASTER TRADE ACCESS</small><strong>{hasAccess ? 'Enabled' : 'Closed'}</strong></div><div><small>NEXT BILLING / RENEWAL</small><strong>{trialing ? `${daysRemaining(subscription?.trial_end)} days` : formatDate(subscription?.current_period_end)}</strong></div><div><small>BILLING INTERVAL</small><strong>{subscription?.plan ? 'Monthly' : 'Not available'}</strong></div><div><small>NEXT PAYMENT</small><strong>{nextPayment}</strong></div></div>
      {trialing && <div className="subscriptionCallout"><b>Your free trial ends in {daysRemaining(subscription?.trial_end)} days.</b><span>After the trial, Stripe will automatically charge $119.90/month unless you cancel.</span></div>}
      {pastDue && <div className="subscriptionCallout subscriptionCalloutWarning"><b>Payment method update required</b><span>Your Master Trade access remains temporarily available during the payment retry period.</span></div>}
      {status === 'UNPAID' && <div className="subscriptionCallout subscriptionCalloutDanger"><b>Master Trade access is closed</b><span>Your subscription payment could not be recovered. Update your payment method to reactivate it.</span></div>}
      {subscription?.cancel_at_period_end && <div className="subscriptionCallout"><b>Cancellation scheduled</b><span>Your subscription remains active until {formatDate(subscription.current_period_end || subscription.trial_end)}.</span></div>}
      <div className="subscriptionFeatureList">{(subscription?.features || ['Secure Stripe billing', 'Account access remains server-authorized', 'No card details are stored by the platform']).map(feature => <span key={feature}><Check />{feature}</span>)}</div>
    </section>
    <div className="subscriptionBillingGrid">
      <section className="subscriptionPanel subscriptionPlanDetails"><header><div><small>PLAN DETAILS</small><h2>{activePlan?.name || 'Subscription options'}</h2></div>{activePlan && <span className="subscriptionCurrentBadge">CURRENT PLAN</span>}</header><p>{activePlan?.description || 'No subscription plan is currently active.'}</p>{activePlan?.monthlyPrice != null && subscription?.next_payment_amount != null && <div className="subscriptionPlanPrice"><strong>${subscription.next_payment_amount.toFixed(2)}</strong><span>/ month</span></div>}<div className="subscriptionDetailList"><div><small>MASTER TRADE</small><strong>{hasAccess ? 'Included' : 'Not enabled'}</strong></div><div><small>RENEWAL</small><strong>{formatDate(subscription?.current_period_end || subscription?.trial_end)}</strong></div></div><ul>{(subscription?.features || activePlan?.features || []).map(feature => <li key={feature}><Check />{feature}</li>)}</ul></section>
      <section className="subscriptionPanel subscriptionBillingPanel"><header><div><small>BILLING & PAYMENT</small><h2>Manage billing</h2></div><CreditCard /></header><div className="subscriptionPaymentPlaceholder"><CreditCard /><div><strong>Payment method</strong><span>Payment details are managed securely by Stripe.</span></div></div><div className="subscriptionActionList">{(pastDue || status === 'UNPAID') && <button onClick={openPortal} disabled={busy}><CreditCard />UPDATE PAYMENT METHOD</button>}{authenticated && subscription?.plan && <button onClick={openPortal} disabled={busy}><ExternalLink />MANAGE BILLING PORTAL</button>}{hasAccess && !subscription?.cancel_at_period_end && <button onClick={() => void cancel(false)} disabled={busy}><XCircle />CANCEL AT PERIOD END</button>}{hasAccess && trialing && <button className="subscriptionQuietAction" onClick={() => void cancel(true)} disabled={busy}>CANCEL NOW</button>}<button onClick={() => onNavigate('pricing')}><Sparkles />VIEW PLANS</button></div></section>
    </div>
    <div className="subscriptionLowerGrid"><section className="subscriptionPanel subscriptionStatusPanel"><header><div><small>SUBSCRIPTION STATUS</small><h2>Access overview</h2></div><span className="subscriptionStatusBadge">{statusLabel}</span></header><div className="subscriptionStatusRows"><div><span>Subscription status</span><strong className={`subscriptionStatusPill ${hasAccess ? 'isPositive' : 'isNegative'}`}>{statusLabel}</strong></div><div><span>Renewal</span><strong>{formatDate(subscription?.current_period_end || subscription?.trial_end)}</strong></div><div><span>Master Trade access</span><strong className={`subscriptionStatusPill ${hasAccess ? 'isPositive' : 'isNegative'}`}>{hasAccess ? 'Enabled' : 'Closed'}</strong></div></div>{hasAccess && <button className="subscriptionSecondaryAction" onClick={() => onNavigate('live')}><ShieldCheck />GO TO MASTER TRADE</button>}</section><section className="subscriptionPanel subscriptionHistoryPanel"><header><div><small>BILLING HISTORY</small><h2>Recent activity</h2></div><span>Stripe-managed</span></header><div className="subscriptionEmptyState"><div className="subscriptionEmptyIcon"><Receipt /></div><strong>No billing activity yet</strong><span>Your invoices and payment events will appear here.</span></div></section></div>
    {notice && <p className="subscriptionFeedback">{notice}</p>}{error && <p className="subscriptionFeedback subscriptionFeedbackError">{error}</p>}
  </main>

  return <main className="subscriptionPage subscriptionPricing">
    <header className="subscriptionPageHeader"><div><span>MASTER TRADE</span><h1>Choose your access level.</h1><p>Secure Stripe billing, immediate entitlement updates, and clear renewal terms for every subscription.</p></div><button onClick={() => onNavigate('billing')}><CreditCard />BILLING</button></header>
    <section className="subscriptionPlans">{SUBSCRIPTION_PLANS.map(plan => <article className={`subscriptionPlan ${plan.code === 'MASTER_MODE' ? 'subscriptionPlanFeatured' : ''}`} key={plan.code}>
      <header><span>{plan.code === 'TRIAL' ? 'LIMITED-TIME ACCESS' : 'FULL ACCESS'}</span><h2>{plan.name}</h2><p>{plan.description}</p></header>
      <div className="subscriptionPrice"><strong><small>$</small>{plan.code === 'TRIAL' ? '0' : '119.90'}</strong><span>{plan.code === 'TRIAL' ? 'today' : '/month'}</span></div>
      {plan.code === 'TRIAL' ? <div className="subscriptionTrialTerms"><b>7 days free</b><span>Then $119.90/month</span></div> : <div className="subscriptionTrialTerms"><b>Immediate access</b><span>Renews automatically every month</span></div>}
      <ul>{plan.features.map(feature => <li key={feature}><Check />{feature}</li>)}</ul>
      {plan.code === 'TRIAL' && <small className="subscriptionTerms">Payment method required. No charge today. If you do not cancel, Stripe automatically charges $119.90/month after the 7-day trial.</small>}
      <button onClick={() => void startCheckout(plan.code)} disabled={busy}>{plan.code === 'TRIAL' ? 'START FREE TRIAL' : 'START MASTER MODE'}<ExternalLink /></button>
    </article>)}</section>
    <div className="subscriptionSecurityNote"><ShieldCheck /><span><b>SECURE STRIPE BILLING</b><small>Stripe collects and stores payment details. The platform never stores card numbers or security codes.</small></span></div>
    {notice && <p className="subscriptionFeedback">{notice}</p>}{error && <p className="subscriptionFeedback subscriptionFeedbackError">{error}</p>}
  </main>
}
