import { useEffect } from 'react'
import ComplianceContent from './ComplianceContent'
import { PUBLIC_POLICIES, type PublicPolicy } from './compliance-content'
import './public-policy.css'

export default function PublicPolicyPage({policy}:{policy:PublicPolicy}) {
  const title = PUBLIC_POLICIES[policy].title
  useEffect(() => { document.title = `${title} | KaisTrade` }, [title])

  return <main className="publicPolicyPage">
    <header><a href="/" aria-label="KaisTrade ana sayfa"><img src="/kaistrade-logo.png" alt="KaisTrade"/></a></header>
    <nav aria-label="Politika sayfaları">
      <a href="/privacy" aria-current={policy === 'privacy' ? 'page' : undefined}>Gizlilik Politikası</a>
      <a href="/terms" aria-current={policy === 'terms' ? 'page' : undefined}>Kullanım Şartları</a>
      <a href="/risk" aria-current={policy === 'risk' ? 'page' : undefined}>Risk Uyarısı</a>
    </nav>
    <article><h1>{title}</h1><ComplianceContent policy={policy}/></article>
    <footer><a href="/">Ana sayfaya dön</a></footer>
  </main>
}
