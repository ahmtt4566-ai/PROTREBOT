import {useRef, useState} from 'react'
import {createRoot} from 'react-dom/client'
import KaisGreeting from '../../src/KaisGreeting'

function Fixture() {
  const anchor = useRef<HTMLButtonElement>(null)
  const [mounted, setMounted] = useState(true)
  const [open, setOpen] = useState(false)
  return <main>
    <button onClick={() => setMounted(value => !value)}>Toggle greeting</button>
    <button ref={anchor} aria-label="Kais AI" aria-expanded={open} onClick={() => setOpen(true)}
      style={{position: 'fixed', top: 20, right: 20, width: 58, height: 58}}>Eye anchor</button>
    {mounted && <KaisGreeting userId="greeting-cleanup-test" enabled chatOpen={open} anchorRef={anchor} onOpen={() => setOpen(true)}/>}
  </main>
}

const root = document.getElementById('root')
if (!root) throw new Error('Kais greeting test root is missing')
createRoot(root).render(<Fixture/>)
