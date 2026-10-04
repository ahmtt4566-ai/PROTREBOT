export const PUBLIC_POLICIES = {
  privacy: {
    title: 'Gizlilik Politikası',
    paragraphs: [
      'We do not store raw exchange secrets in browser storage. API keys and credentials are handled through the secure backend vault or server-side environment when available.',
      'Diagnostic and operational metadata may be retained for monitoring, integrity, and support purposes. Sensitive values are minimized and access is restricted to authorized operational workflows.',
      'Users remain responsible for safeguarding their own credentials and for reviewing any legal privacy obligations applicable to their region and use case.',
    ],
  },
  terms: {
    title: 'Kullanım Şartları',
    paragraphs: [
      'Use of this platform is governed by the applicable service agreement, risk acknowledgment, and product terms provided by the operator. The software is provided as a workflow and analytics environment.',
      'Testnet or demo features are not a substitute for regulated financial advice or live market execution. Users must confirm that their use case complies with local rules and account restrictions.',
      'Any live trading activation requires separate authorization, security validation, and explicit user acknowledgment of the associated execution risk.',
    ],
  },
  risk: {
    title: 'Risk Uyarısı',
    paragraphs: [
      'This is a research and demo-first operating workspace. It is not a guarantee of profit and it does not promise financial returns.',
      'Market data, signal quality, order logic, and execution status can change rapidly. The platform uses fail-closed security gates by default. Live orders are never activated automatically and only proceed after explicit validation and safety checks.',
      'Users must understand that market exposure carries risk, including potential loss of capital. This platform is designed for education, simulation, risk review, and controlled testnet workflows unless a separate live trading authorization is explicitly completed.',
    ],
  },
} as const

export type PublicPolicy = keyof typeof PUBLIC_POLICIES

export function publicPolicyForPath(pathname:string):PublicPolicy|null {
  switch (pathname.replace(/\/$/, '')) {
    case '/privacy': return 'privacy'
    case '/terms': return 'terms'
    case '/risk': return 'risk'
    default: return null
  }
}
