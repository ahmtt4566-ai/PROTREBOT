import { useEffect, useState } from 'react'
import { Check, CreditCard, ExternalLink, Lock, Receipt, ShieldCheck, Sparkles, XCircle } from 'lucide-react'
import { API_BASE, userSessionToken } from './api'
import { PLAN_BY_CODE, SUBSCRIPTION_PLANS, type PlanCode } from './subscription'
import BinanceManualPayment from './BinanceManualPayment'

type SubscriptionView = {
  status: string
  plan: PlanCode | null
  master_trade_access: boolean
  trial_end?: string | null
  current_period_end?: string | null
  next_payment_amount?: number | null
  cancel_at_period_end?: boolean
  features?: string[]
  mode?: string
}
type ApiError = { detail?: string }
type PaymentMethods = { stripe_enabled: boolean; binance_enabled: boolean }

const formatDate = (value?: string | null, locale = 'en-US') => value ? new Date(value).toLocaleDateString(locale, { year: 'numeric', month: 'short', day: 'numeric' }) : 'Not available'
const daysRemaining = (value?: string | null) => value ? Math.max(0, Math.ceil((new Date(value).getTime() - Date.now()) / 86400000)) : 0
const masterFeaturesTr = ['Master Trade analiz çalışma alanı', 'Premium işlem çalışma alanı', 'Pozisyon ve risk takibi', 'Sunucuda doğrulanan MASTER MODE erişimi']

function MasterPlanPreview({disabledLabel}: {disabledLabel?: string}) {
  const plan = PLAN_BY_CODE.MASTER_MODE
  return <section className="subscriptionPlans" aria-label="MASTER MODE planı">
    <article className="subscriptionPlan subscriptionPlanFeatured">
      <header><span>MASTER MODE</span><h2>MASTER MODE</h2><p>Analiz ve işlem yönetimi için premium çalışma alanı.</p></header>
      <div className="subscriptionPrice"><strong>{plan.monthlyPrice.toFixed(2)} USD</strong><span>/ ay</span></div>
      <ul>{masterFeaturesTr.map(feature => <li key={feature}><Check/>{feature}</li>)}</ul>
      <small className="subscriptionTerms">Bu kart erişim açmaz veya ücret almaz. Ödeme yöntemi kullanılabilir olduğunda başvurabilirsiniz.</small>
      {disabledLabel && <button type="button" disabled>{disabledLabel}</button>}
    </article>
  </section>
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = userSessionToken()
  const headers = new Headers(options.headers)
  headers.set('Authorization', `Bearer ${token}`)
  if (options.body) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_BASE}/v22${path}`, { ...options, headers })
  const payload = await response.json().catch(() => {
    throw new Error(`The billing service returned an invalid response (${response.status}).`)
  }) as T & ApiError
  if (!response.ok) throw new Error(payload?.detail || 'The billing request could not be completed.')
  return payload
}

export default function SubscriptionCenter({ mode, onNavigate }: { mode: 'pricing' | 'billing'; onNavigate: (target: 'pricing' | 'billing' | 'live' | 'master-trade') => void }) {
  const [subscription, setSubscription] = useState<SubscriptionView | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [loadError, setLoadError] = useState('')
  const [subscriptionLoading, setSubscriptionLoading] = useState(true)
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [paymentMethods, setPaymentMethods] = useState<PaymentMethods | null>(null)
  const [methodsError, setMethodsError] = useState('')
  const authenticated = Boolean(userSessionToken())
  useEffect(() => {
    const controller = new AbortController()
    setMethodsError('')
    void request<PaymentMethods>('/subscription/config', {signal: controller.signal}).then(current => {
      if (!current || typeof current.stripe_enabled !== 'boolean' || typeof current.binance_enabled !== 'boolean') {
        throw new Error('Ödeme yöntemleri doğrulanamadı.')
      }
      if (!controller.signal.aborted) setPaymentMethods(current)
    }).catch(exception => {
      if (!controller.signal.aborted) {
        setPaymentMethods(null)
        console.warn('Billing method configuration failed:', exception instanceof Error ? exception.message : 'Unknown error')
        setMethodsError('Ödeme yöntemleri yüklenemedi.')
      }
    })
    return () => controller.abort()
  }, [authenticated, loadAttempt])

  useEffect(() => {
    const refresh = () => setLoadAttempt(value => value + 1)
    window.addEventListener('protrebot-access-refresh', refresh)
    return () => window.removeEventListener('protrebot-access-refresh', refresh)
  }, [])

  useEffect(() => {
    if (!authenticated) { setSubscriptionLoading(false); return }
    const controller = new AbortController()
    setSubscriptionLoading(true)
    setSubscription(null)
    setLoadError('')
    void request<SubscriptionView>('/subscription', {signal: controller.signal}).then(current => {
      if (!current || typeof current.status !== 'string' || typeof current.master_trade_access !== 'boolean') {
        throw new Error('The billing service returned invalid subscription information.')
      }
      if (!controller.signal.aborted) setSubscription(current)
    }).catch(exception => {
      if (!controller.signal.aborted) setLoadError(exception instanceof Error ? exception.message : 'Subscription information could not be loaded.')
    }).finally(() => {
      if (!controller.signal.aborted) setSubscriptionLoading(false)
    })
    return () => controller.abort()
  }, [authenticated, loadAttempt, mode])

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
      window.dispatchEvent(new Event('protrebot-access-refresh'))
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

  const status = subscription?.status || 'UNKNOWN'
  const hasAccess = Boolean(subscription?.master_trade_access)
  const trialing = status === 'TRIALING'
  const pastDue = status === 'PAST_DUE'
  const binancePaid = subscription?.mode === 'BINANCE_MANUAL'
  const stripeEnabled = paymentMethods?.stripe_enabled === true
  const binanceEnabled = paymentMethods?.binance_enabled === true
  const paymentFeedback = methodsError ? <p className="subscriptionFeedback subscriptionFeedbackError" role="alert">{methodsError}{' '}<button type="button" onClick={() => setLoadAttempt(value => value + 1)}>Yeniden dene</button></p>
    : !paymentMethods ? <p role="status">Ödeme yöntemleri yükleniyor…</p>
    : null
  const unavailableLabel = methodsError ? 'Ödeme bilgileri yüklenemedi' : !paymentMethods ? 'Ödeme yöntemleri yükleniyor…'
    : !binanceEnabled ? 'Ödeme yakında açılacak' : undefined
  const endDate = formatDate(subscription?.current_period_end, binancePaid ? 'tr-TR' : 'en-US')
  const activePlan = subscription?.plan ? SUBSCRIPTION_PLANS.find(plan => plan.code === subscription.plan) : null
  const nextPayment = subscription?.next_payment_amount != null ? `$${subscription.next_payment_amount.toFixed(2)}` : 'Not available'
  const statusLabel = subscriptionLoading ? 'Loading' : loadError ? 'Unavailable' : trialing ? 'Free trial' : subscription ? status.replace('_', ' ') : 'Not available'
  const emptyPlanTitle = subscription ? 'No active plan' : subscriptionLoading ? 'Loading plan' : 'Plan unavailable'
  const accessLabel = hasAccess ? 'Enabled' : subscription ? 'Closed' : 'Not available'
  const loadFeedback = loadError && <p className="subscriptionFeedback subscriptionFeedbackError" role="alert">{loadError}{' '}<button type="button" onClick={() => setLoadAttempt(attempt => attempt + 1)} disabled={subscriptionLoading}>Retry subscription</button></p>

  if (mode === 'billing' && !stripeEnabled) {
    const trStatus: Record<string, string> = {ACTIVE:'Aktif', TRIALING:'Deneme dönemi', PAST_DUE:'Ödeme gecikti', UNPAID:'Ödenmedi', CANCELLED:'İptal edildi', EXPIRED:'Süresi doldu'}
    const expiry = subscription?.current_period_end || subscription?.trial_end
    return <main className="subscriptionPage subscriptionBilling">
      <header className="subscriptionPageHeader"><div><span>ABONELİK</span><h1>Abonelik yönetimi</h1><p>MASTER MODE planınızı, erişiminizi ve ödeme seçeneklerini buradan takip edin.</p></div><button onClick={() => onNavigate('pricing')}><Sparkles/>PLANLARI GÖR</button></header>
      <section className={`subscriptionStatusCard subscriptionStatus-${status.toLowerCase()}`}>
        <div className="subscriptionPlanHeading"><div><small>MEVCUT PLAN</small><h2>{subscription?.plan ? 'MASTER MODE' : 'Aktif plan yok'}</h2></div><span className="subscriptionStatusBadge">{subscriptionLoading ? 'Yükleniyor' : loadError ? 'Bilgi alınamadı' : trStatus[status] || '—'}</span></div>
        <div className="subscriptionStatusTop">
          <div><small>MASTER MODE ERİŞİMİ</small><strong>{subscription ? hasAccess ? 'Açık' : 'Kapalı' : '—'}</strong></div>
          <div><small>BİTİŞ TARİHİ</small><strong>{expiry ? formatDate(expiry, 'tr-TR') : '—'}</strong></div>
          <div><small>ÖDEME YÖNTEMİ</small><strong>{binancePaid ? 'Binance ile ödendi' : '—'}</strong></div>
          <div><small>ERİŞİM DÖNEMİ</small><strong>{binancePaid ? '30 gün · elle yenileme' : '—'}</strong></div>
        </div>
        {binancePaid && <div className="subscriptionCallout"><b>Binance ile ödendi</b><span>MASTER MODE erişiminiz {formatDate(expiry,'tr-TR')} tarihinde sona erer. Otomatik yenileme veya otomatik ücretlendirme yoktur.</span></div>}
        {hasAccess && <button className="subscriptionSecondaryAction" onClick={() => onNavigate('master-trade')}><ShieldCheck/>MASTER TRADE’E GİT</button>}
      </section>
      <MasterPlanPreview disabledLabel={unavailableLabel}/>
      {authenticated && binanceEnabled && <BinanceManualPayment/>}
      {paymentFeedback}
      {loadError && <p className="subscriptionFeedback subscriptionFeedbackError" role="alert">Abonelik bilgileri yüklenemedi. <button type="button" onClick={() => setLoadAttempt(value => value + 1)}>Yeniden dene</button></p>}
    </main>
  }

  if (mode === 'billing') return <main className="subscriptionPage subscriptionBilling">
    <header className="subscriptionPageHeader"><div><span>BILLING</span><h1>Subscription control center</h1><p>{binancePaid || !stripeEnabled ? 'MASTER MODE erişiminizi ve bitiş tarihinizi buradan takip edin.' : 'Review your plan, Master Trade access, renewal timing, and Stripe-managed billing from one place.'}</p></div><button onClick={() => onNavigate('pricing')}><Sparkles />VIEW PLANS</button></header>
    <section className={`subscriptionStatusCard subscriptionStatus-${status.toLowerCase()}`}>
      <div className="subscriptionPlanHeading"><div className="subscriptionPlanHeadingMain"><div className="subscriptionPlanIcon"><Lock /></div><div className="subscriptionPlanHeadingText"><small>CURRENT PLAN</small><h2>{activePlan?.name || emptyPlanTitle}</h2><p>{activePlan?.description || (subscription ? 'Choose a plan to unlock Master Trade access.' : 'Subscription information is not available yet.')}</p></div></div><span className="subscriptionStatusBadge">{statusLabel}</span></div>
      <div className="subscriptionStatusTop"><div><small>MASTER TRADE ACCESS</small><strong>{accessLabel}</strong></div><div><small>{binancePaid ? 'BİTİŞ TARİHİ' : 'NEXT BILLING / RENEWAL'}</small><strong>{trialing ? `${daysRemaining(subscription?.trial_end)} days` : endDate}</strong></div><div><small>{binancePaid ? 'ERİŞİM DÖNEMİ' : 'BILLING INTERVAL'}</small><strong>{binancePaid ? '30 gün · elle yenileme' : subscription?.plan ? 'Monthly' : 'Not available'}</strong></div><div><small>{binancePaid ? 'ÖDEME YÖNTEMİ' : 'NEXT PAYMENT'}</small><strong>{binancePaid ? 'Binance ile ödendi' : nextPayment}</strong></div></div>
      {binancePaid && <div className="subscriptionCallout"><b>Binance ile ödendi</b><span>MASTER MODE erişiminiz {endDate} tarihinde sona erer. Otomatik ücret alınmaz; yenileme elle yapılır.</span></div>}
      {trialing && <div className="subscriptionCallout"><b>Your free trial ends in {daysRemaining(subscription?.trial_end)} days.</b>{stripeEnabled && <span>After the trial, Stripe will automatically charge $119.90/month unless you cancel.</span>}</div>}
      {pastDue && <div className="subscriptionCallout subscriptionCalloutWarning"><b>Payment method update required</b><span>Your Master Trade access remains temporarily available during the payment retry period.</span></div>}
      {status === 'UNPAID' && <div className="subscriptionCallout subscriptionCalloutDanger"><b>Master Trade access is closed</b><span>Your subscription payment could not be recovered. Update your payment method to reactivate it.</span></div>}
      {!binancePaid && subscription?.cancel_at_period_end && <div className="subscriptionCallout"><b>Cancellation scheduled</b><span>Your subscription remains active until {formatDate(subscription.current_period_end || subscription.trial_end)}.</span></div>}
      <div className="subscriptionFeatureList">{(subscription?.features || ['Secure Stripe billing', 'Account access remains server-authorized', 'No card details are stored by the platform']).filter(feature => stripeEnabled || !/stripe/i.test(feature)).map(feature => <span key={feature}><Check />{feature}</span>)}</div>
    </section>
    <div className="subscriptionBillingGrid">
      <section className="subscriptionPanel subscriptionPlanDetails"><header><div><small>PLAN DETAILS</small><h2>{activePlan?.name || (subscription ? 'Subscription options' : emptyPlanTitle)}</h2></div>{activePlan && <span className="subscriptionCurrentBadge">CURRENT PLAN</span>}</header><p>{activePlan?.description || (subscription ? 'No subscription plan is currently active.' : 'Subscription information is not available yet.')}</p>{activePlan?.monthlyPrice != null && subscription?.next_payment_amount != null && <div className="subscriptionPlanPrice"><strong>${subscription.next_payment_amount.toFixed(2)}</strong><span>/ month</span></div>}<div className="subscriptionDetailList"><div><small>MASTER TRADE</small><strong>{hasAccess ? 'Included' : subscription ? 'Not enabled' : 'Not available'}</strong></div><div><small>RENEWAL</small><strong>{formatDate(subscription?.current_period_end || subscription?.trial_end)}</strong></div></div><ul>{(subscription?.features || activePlan?.features || []).filter(feature => stripeEnabled || !/stripe/i.test(feature)).map(feature => <li key={feature}><Check />{feature}</li>)}</ul></section>
      {binancePaid ? <section className="subscriptionPanel subscriptionBillingPanel"><header><div><small>ÖDEME VE YENİLEME</small><h2>Binance ile ödendi</h2></div><CreditCard/></header><p>Ödemeniz yönetici tarafından doğrulandı. Bitiş tarihi: {endDate}.</p><p>Otomatik yenileme veya otomatik ücretlendirme yoktur.</p><button type="button" onClick={() => setLoadAttempt(value => value + 1)} disabled={subscriptionLoading}>Abonelik bilgisini yenile</button></section> :
      stripeEnabled && <section className="subscriptionPanel subscriptionBillingPanel"><header><div><small>BILLING & PAYMENT</small><h2>Manage billing</h2></div><CreditCard /></header><div className="subscriptionPaymentPlaceholder"><CreditCard /><div><strong>Payment method</strong><span>Payment details are managed securely by Stripe.</span></div></div><div className="subscriptionActionList">{(pastDue || status === 'UNPAID') && <button onClick={openPortal} disabled={busy}><CreditCard />UPDATE PAYMENT METHOD</button>}{authenticated && subscription?.plan && <button onClick={openPortal} disabled={busy}><ExternalLink />MANAGE BILLING PORTAL</button>}{hasAccess && !subscription?.cancel_at_period_end && <button onClick={() => void cancel(false)} disabled={busy}><XCircle />CANCEL AT PERIOD END</button>}{hasAccess && trialing && <button className="subscriptionQuietAction" onClick={() => void cancel(true)} disabled={busy}>CANCEL NOW</button>}<button onClick={() => onNavigate('pricing')}><Sparkles />VIEW PLANS</button></div></section>}
    </div>
    {authenticated && binanceEnabled && <BinanceManualPayment/>}
    {paymentFeedback}
    <div className="subscriptionLowerGrid"><section className="subscriptionPanel subscriptionStatusPanel"><header><div><small>SUBSCRIPTION STATUS</small><h2>Access overview</h2></div><span className="subscriptionStatusBadge">{statusLabel}</span></header><div className="subscriptionStatusRows"><div><span>Subscription status</span><strong className={`subscriptionStatusPill ${hasAccess ? 'isPositive' : 'isNegative'}`}>{statusLabel}</strong></div><div><span>{binancePaid ? 'Bitiş tarihi' : 'Renewal'}</span><strong>{formatDate(subscription?.current_period_end || subscription?.trial_end)}</strong></div><div><span>Master Trade access</span><strong className={`subscriptionStatusPill ${hasAccess ? 'isPositive' : 'isNegative'}`}>{accessLabel}</strong></div></div>{hasAccess && <button className="subscriptionSecondaryAction" onClick={() => onNavigate('master-trade')}><ShieldCheck />GO TO MASTER TRADE</button>}</section>{!binancePaid && stripeEnabled && <section className="subscriptionPanel subscriptionHistoryPanel"><header><div><small>BILLING HISTORY</small><h2>Recent activity</h2></div><span>Stripe-managed</span></header><div className="subscriptionEmptyState"><div className="subscriptionEmptyIcon"><Receipt /></div><strong>Billing history is managed in Stripe</strong><span>Invoices and payment events are not loaded here. Use the billing portal to view available records.</span></div></section>}</div>
    {loadFeedback}{notice && <p className="subscriptionFeedback">{notice}</p>}{error && <p className="subscriptionFeedback subscriptionFeedbackError">{error}</p>}
  </main>

  return <main className="subscriptionPage subscriptionPricing">
    <header className="subscriptionPageHeader"><div><span>MASTER TRADE</span><h1>{stripeEnabled ? 'Choose your access level.' : 'MASTER MODE ile çalışma alanını aç.'}</h1><p>{stripeEnabled ? 'Secure Stripe billing, immediate entitlement updates, and clear renewal terms for every subscription.' : 'Plan özelliklerini inceleyin ve kullanılabilir ödeme yöntemini seçin.'}</p></div><button onClick={() => onNavigate('billing')}><CreditCard />{stripeEnabled ? 'BILLING' : 'ABONELİK'}</button></header>
    {!stripeEnabled && <MasterPlanPreview disabledLabel={unavailableLabel}/>}
    {stripeEnabled && <section className="subscriptionPlans">{SUBSCRIPTION_PLANS.map(plan => <article className={`subscriptionPlan ${plan.code === 'MASTER_MODE' ? 'subscriptionPlanFeatured' : ''}`} key={plan.code}>
      <header><span>{plan.code === 'TRIAL' ? 'LIMITED-TIME ACCESS' : 'FULL ACCESS'}</span><h2>{plan.name}</h2><p>{plan.description}</p></header>
      <div className="subscriptionPrice"><strong><small>$</small>{plan.code === 'TRIAL' ? '0' : '119.90'}</strong><span>{plan.code === 'TRIAL' ? 'today' : '/month'}</span></div>
      {plan.code === 'TRIAL' ? <div className="subscriptionTrialTerms"><b>7 days free</b><span>Then $119.90/month</span></div> : <div className="subscriptionTrialTerms"><b>Immediate access</b><span>Renews automatically every month</span></div>}
      <ul>{plan.features.map(feature => <li key={feature}><Check />{feature}</li>)}</ul>
      {plan.code === 'TRIAL' && <small className="subscriptionTerms">Payment method required. No charge today. If you do not cancel, Stripe automatically charges $119.90/month after the 7-day trial.</small>}
      <button onClick={() => void startCheckout(plan.code)} disabled={busy}>{plan.code === 'TRIAL' ? 'START FREE TRIAL' : 'START MASTER MODE'}<ExternalLink /></button>
      {plan.code === 'MASTER_MODE' && authenticated && binanceEnabled && <BinanceManualPayment/>}
    </article>)}</section>}
    {!stripeEnabled && authenticated && binanceEnabled && <BinanceManualPayment/>}
    {stripeEnabled && <div className="subscriptionSecurityNote"><ShieldCheck /><span><b>SECURE STRIPE BILLING</b><small>Stripe collects and stores payment details. The platform never stores card numbers or security codes.</small></span></div>}
    {paymentFeedback}
    {stripeEnabled ? loadFeedback : loadError && <p className="subscriptionFeedback subscriptionFeedbackError" role="alert">Abonelik bilgileri yüklenemedi. <button type="button" onClick={() => setLoadAttempt(value => value + 1)}>Yeniden dene</button></p>}{notice && <p className="subscriptionFeedback">{notice}</p>}{error && <p className="subscriptionFeedback subscriptionFeedbackError">{error}</p>}
  </main>
}
