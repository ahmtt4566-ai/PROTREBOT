import {useState} from 'react'
import {createRoot} from 'react-dom/client'
import PublicPolicyPage from '../../../PublicPolicyPage'
import type {PublicPolicy} from '../../../compliance-content'
import {PAGE_METADATA,applyPageMetadataForPath} from '../../../public-page-metadata'

function MetadataNavigation() {
  const [policy, setPolicy] = useState<PublicPolicy | null>(null)
  return <>
    <nav aria-label="Metadata test navigation">
      {Object.keys(PAGE_METADATA).filter(page => !['home','privacy','terms','risk'].includes(page)).map(page =>
        <button key={page} onClick={() => applyPageMetadataForPath(`/${page}`)}>{page}</button>)}
      {(['privacy', 'terms', 'risk'] as const).map(page =>
        <button key={page} onClick={() => setPolicy(page)}>{page}</button>)}
      <button onClick={() => setPolicy(null)}>home</button>
    </nav>
    {policy && <PublicPolicyPage policy={policy}/>}
  </>
}

createRoot(document.getElementById('root')!).render(<MetadataNavigation/>)
