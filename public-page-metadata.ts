export const PUBLIC_PAGE_METADATA = {
  home: {
    title: 'KaisTrade | Kripto Analiz ve İşlem Platformu',
    description: 'KaisTrade ile kripto piyasalarını izleyin, yapay zekâ analizlerini inceleyin ve demo işlemlerle stratejilerinizi değerlendirin.',
    url: 'https://kaistrade.com/',
  },
  privacy: {
    title: 'Gizlilik Politikası | KaisTrade',
    description: 'KaisTrade kişisel verilerin, API anahtarlarının ve tanılama bilgilerinin nasıl işlendiğini ve korunduğunu açıklıyor.',
    url: 'https://kaistrade.com/privacy',
  },
  terms: {
    title: 'Kullanım Şartları | KaisTrade',
    description: 'KaisTrade hizmet koşullarını, kullanıcı sorumluluklarını ve demo ile canlı işlem kullanım şartlarını inceleyin.',
    url: 'https://kaistrade.com/terms',
  },
  risk: {
    title: 'Risk Uyarısı | KaisTrade',
    description: 'Kripto işlemlerinde sermaye kaybı ve piyasa oynaklığı risklerini öğrenin. KaisTrade kâr veya getiri garantisi vermez.',
    url: 'https://kaistrade.com/risk',
  },
} as const

export type PublicPage = keyof typeof PUBLIC_PAGE_METADATA

export const PAGE_METADATA = {
  ...PUBLIC_PAGE_METADATA,
  dashboard: {title:'Piyasa Özeti | KaisTrade',description:'Kripto fiyatlarını, piyasa hareketlerini ve çalışma alanınızın güncel durumunu KaisTrade panelinden takip edin.'},
  trading: {title:'İşlem Çalışma Alanı | KaisTrade',description:'KaisTrade işlem araçlarıyla piyasa verilerini inceleyin ve emir ile pozisyon durumlarını takip edin.'},
  analyst: {title:'AI Analist | KaisTrade',description:'Kripto paralar için yapay zekâ analizleri oluşturun ve kaydedilmiş analiz sonuçlarınızı inceleyin.'},
  scanner: {title:'Piyasa Tarayıcı | KaisTrade',description:'Kripto piyasalarını tarayın; fiyat hareketlerini ve stratejinize uygun analiz fırsatlarını değerlendirin.'},
  performance: {title:'Performans Takibi | KaisTrade',description:'İşlem sonuçlarınızı ve çalışma alanınızın performans verilerini KaisTrade üzerinden değerlendirin.'},
  testnet: {title:'Demo İşlemler | KaisTrade',description:'Gerçek sermaye kullanmadan demo ortamında kripto işlem stratejilerini ve pozisyon yönetimini deneyin.'},
  ops: {title:'Operasyon Durumu | KaisTrade',description:'KaisTrade bağlantılarını, çalışma alanı durumunu ve operasyon kontrollerini görüntüleyin.'},
  live: {title:'Canlı İşlem | KaisTrade',description:'Yetkilendirilmiş canlı işlem hesabınızın bağlantı, emir ve güvenlik kontrol durumlarını takip edin.'},
  setup: {title:'Borsa Bağlantısı | KaisTrade',description:'KaisTrade borsa bağlantılarınızı yapılandırın ve demo ile canlı hesap bağlantı durumlarını inceleyin.'},
  pricing: {title:'Abonelik Planları | KaisTrade',description:'KaisTrade deneme ve MASTER MODE planlarını karşılaştırın; özellikleri ve güncel abonelik fiyatını inceleyin.'},
  billing: {title:'Abonelik ve Ödemeler | KaisTrade',description:'KaisTrade abonelik durumunuzu, erişim sürenizi ve kullanılabilir ödeme seçeneklerini görüntüleyin.'},
  'master-trade': {title:'Master Trade Analiz | KaisTrade',description:'Kripto grafiklerini, analiz sonuçlarını ve pozisyon yönetimi araçlarını Master Trade çalışma alanında inceleyin.'},
  login: {title:'Giriş Yap | KaisTrade',description:'Piyasa analizlerinize ve işlem çalışma alanınıza ulaşmak için KaisTrade hesabınıza güvenli giriş yapın.'},
  register: {title:'Hesap Oluştur | KaisTrade',description:'Kripto analizlerini ve demo işlem araçlarını keşfetmek için KaisTrade hesabınızı oluşturun.'},
  'forgot-password': {title:'Şifremi Unuttum | KaisTrade',description:'KaisTrade hesabınıza yeniden erişmek için e-posta adresinizle şifre sıfırlama bağlantısı isteyin.'},
  'reset-password': {title:'Şifre Yenileme | KaisTrade',description:'KaisTrade hesabınız için yeni ve güçlü bir şifre belirleyerek hesabınıza erişiminizi yenileyin.'},
  'verify-email': {title:'E-posta Doğrulama | KaisTrade',description:'KaisTrade hesabınızın e-posta adresini doğrulayın ve hesabınızın doğrulama durumunu kontrol edin.'},
  profile: {title:'Hesabım | KaisTrade',description:'KaisTrade hesap bilgilerinizi, e-posta doğrulamanızı ve kişisel tercihlerinizi görüntüleyin.'},
  settings: {title:'Hesap ve Güvenlik Ayarları | KaisTrade',description:'KaisTrade profilinizi, şifrenizi, iki aşamalı doğrulamanızı ve hesap güvenliği ayarlarınızı yönetin.'},
  admin: {title:'Yönetim Paneli | KaisTrade',description:'Yetkili KaisTrade yöneticileri için kullanıcı, abonelik, ödeme ve sistem durumu yönetim paneli.'},
  moderator: {title:'Moderatör Paneli | KaisTrade',description:'KaisTrade destek taleplerini ve size yetkilendirilmiş moderasyon işlemlerini tek panelden takip edin.'},
  'local-owner-setup': {title:'Yerel Yönetici Kurulumu | KaisTrade',description:'KaisTrade yerel geliştirme ortamında yönetici hesabının kurulum durumunu kontrol edin.'},
} as const

export function applyPageMetadataForPath(pathname:string, indexingPath = pathname):void {
  const privatePage = /^\/(?:admin|moderator|profile|settings|dashboard|master-trade|billing|login|register|forgot-password|reset-password|verify-email|local-owner-setup|trading|analyst|scanner|performance|testnet|ops|live|setup)(?:\/|$)/.test(indexingPath.split('?')[0])
  const robots = document.head.querySelector('meta[name="robots"][data-kaistrade-private]')
  if (privatePage) {
    const element = robots ?? document.createElement('meta')
    element.setAttribute('name','robots')
    element.setAttribute('content','noindex, nofollow')
    element.setAttribute('data-kaistrade-private','true')
    if (!robots) document.head.appendChild(element)
  } else {
    robots?.remove()
  }
  const path = pathname.split('?')[0].replace(/\/$/, '')
  const page = path === '' ? 'home' : path.split('/')[1]
  if (!(page in PAGE_METADATA)) return
  const key = page as keyof typeof PAGE_METADATA
  if (key in PUBLIC_PAGE_METADATA) {
    applyPublicPageMetadata(key as PublicPage)
    return
  }
  const metadata = PAGE_METADATA[key]
  document.title = metadata.title
  for (const [selector, value] of [
    ['meta[name="description"]',metadata.description],
    ['meta[property="og:title"]',metadata.title],
    ['meta[property="og:description"]',metadata.description],
    ['meta[name="twitter:title"]',metadata.title],
    ['meta[name="twitter:description"]',metadata.description],
  ]) {
    const element = document.head.querySelector(selector)
    if (!element) throw new Error(`Missing page metadata: ${selector}`)
    element.setAttribute('content',value)
  }
}

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
