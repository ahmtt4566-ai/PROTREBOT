import {useState} from 'react'
import {createRoot} from 'react-dom/client'
import PublicPolicyPage from '../../../PublicPolicyPage'
import type {PublicPolicy} from '../../../compliance-content'

function MetadataNavigation() {
  const [policy, setPolicy] = useState<PublicPolicy | null>(null)
  return <>
    <nav aria-label="Metadata test navigation">
      {(['privacy', 'terms', 'risk'] as const).map(page =>
        <button key={page} onClick={() => setPolicy(page)}>{page}</button>)}
      <button onClick={() => setPolicy(null)}>home</button>
    </nav>
    {policy && <PublicPolicyPage policy={policy}/>}
  </>
}

createRoot(document.getElementById('root')!).render(<MetadataNavigation/>)
