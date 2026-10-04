import { PUBLIC_POLICIES, type PublicPolicy } from './compliance-content'

export default function ComplianceContent({policy}:{policy:PublicPolicy}) {
  return <div lang="en" style={{display:'grid',gap:'0.8rem',color:'#e2e8f0',lineHeight:1.6}}>
    {PUBLIC_POLICIES[policy].paragraphs.map(paragraph => <p key={paragraph}>{paragraph}</p>)}
  </div>
}
