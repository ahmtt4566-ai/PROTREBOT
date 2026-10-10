export const PUBLIC_PAGE_METADATA = {
  home: {
    title: 'KaisTrade | AI-Powered Crypto Trading Platform',
    description: "Explore KaisTrade's AI-powered crypto analysis, market scanning and trading tools, with demo trading and risk controls for informed market decisions.",
    url: 'https://kaistrade.com/',
  },
  privacy: {
    title: 'Gizlilik Politikası | KaisTrade',
    description: 'KaisTrade gizlilik politikası: API anahtarlarının güvenli işlenmesi, tanılama ve operasyon verilerinin saklanması ve yetkili erişim ilkeleri.',
    url: 'https://kaistrade.com/privacy',
  },
  terms: {
    title: 'Kullanım Şartları | KaisTrade',
    description: 'KaisTrade kullanım şartları: hizmet koşulları, kullanıcı sorumlulukları, demo ve testnet kapsamı ve canlı işlem için ayrı yetkilendirme gereklilikleri.',
    url: 'https://kaistrade.com/terms',
  },
  risk: {
    title: 'Risk Uyarısı | KaisTrade',
    description: 'KaisTrade risk uyarısı: kripto piyasalarında sermaye kaybı riski, değişken piyasa ve işlem koşulları, demo kapsamı ve canlı işlem güvenlik kontrolleri.',
    url: 'https://kaistrade.com/risk',
  },
} as const

export type PublicPage = keyof typeof PUBLIC_PAGE_METADATA

export function publicPageForPath(pathname: string): PublicPage | null {
  const path = pathname.split('?')[0].replace(/\/$/, '')
  if (path === '') return 'home'
  if (path === '/privacy' || path === '/terms' || path === '/risk') return path.slice(1) as Exclude<PublicPage, 'home'>
  return null
}

export function publicPageSchema(page: PublicPage) {
  const metadata = PUBLIC_PAGE_METADATA[page]
  return page === 'home'
    ? {'@context': 'https://schema.org', '@type': 'WebSite', name: 'KaisTrade', alternateName: 'Kais Trade', url: metadata.url}
    : {'@context': 'https://schema.org', '@type': 'WebPage', name: metadata.title, description: metadata.description, url: metadata.url,
      isPartOf: {'@type': 'WebSite', name: 'KaisTrade', alternateName: 'Kais Trade', url: PUBLIC_PAGE_METADATA.home.url}}
}

export function publicPageHeadValues(page: PublicPage) {
  const {title, description, url} = PUBLIC_PAGE_METADATA[page]
  return [
    ['meta[name="description"]', 'content', description],
    ['meta[property="og:title"]', 'content', title],
    ['meta[property="og:description"]', 'content', description],
    ['meta[property="og:url"]', 'content', url],
    ['meta[name="twitter:title"]', 'content', title],
    ['meta[name="twitter:description"]', 'content', description],
    ['link[rel="canonical"]', 'href', url],
  ] as const
}

export function applyPublicPageMetadata(page: PublicPage): void {
  document.title = PUBLIC_PAGE_METADATA[page].title
  for (const [selector, attribute, value] of publicPageHeadValues(page)) {
    const element = document.head.querySelector(selector)
    if (!element) throw new Error(`Missing public page metadata: ${selector}`)
    element.setAttribute(attribute, value)
  }
  const schema = document.head.querySelector('script[type="application/ld+json"]')
  if (!schema) throw new Error('Missing public page structured data')
  schema.textContent = JSON.stringify(publicPageSchema(page))
}
