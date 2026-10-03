import {useState} from 'react'
import {createRoot} from 'react-dom/client'
import KaisEye from '../../src/KaisEye'

function Fixture() {
  const [mounted, setMounted] = useState(false)
  return <main>
    <button onClick={() => setMounted(value => !value)}>Toggle eyes</button>
    {mounted && <><button aria-label="First Kais AI"><KaisEye state="idle"/></button>
      <button aria-label="Second Kais AI"><KaisEye state="idle"/></button></>}
  </main>
}

const root = document.getElementById('root')
if (!root) throw new Error('Kais header eye test root is missing')
createRoot(root).render(<Fixture/>)
