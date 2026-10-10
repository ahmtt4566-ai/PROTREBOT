import {useRef, useState} from 'react'
import {createRoot} from 'react-dom/client'
import KaisGreeting from '../../src/KaisGreeting'

function Fixture() {
  const anchor = useRef<HTMLButtonElement>(null)
  const [mounted, setMounted] = useState(true)
  const [open, setOpen] = useState(false)
  const [openRequest, setOpenRequest] = useState(0)
  const [route, setRoute] = useState('/')
  return <main>
    <button onClick={() => setMounted(value => !value)}>Toggle greeting</button>
    <select aria-label="Page route" value={route} onChange={event => {
      history.replaceState(null, '', event.target.value)
      setRoute(event.target.value)
    }}>
      {['/', '/login', '/register', '/forgot-password', '/reset-password', '/verify-email'].map(path => <option key={path}>{path}</option>)}
    </select>
    <button ref={anchor} aria-label="Kais AI" onClick={() => setOpenRequest(value => value + 1)}
      style={{position: 'fixed', top: 20, right: 20, width: 58, height: 58}}>Eye anchor</button>
    {open && <div role="dialog" aria-label="Fixture chat"><button onClick={() => setOpen(false)}>Close chat</button></div>}
    {mounted && <KaisGreeting enabled chatOpen={open} anchorRef={anchor} openRequest={openRequest} onOpen={() => setOpen(true)}/>}
  </main>
}

const root = document.getElementById('root')
if (!root) throw new Error('Kais greeting test root is missing')
createRoot(root).render(<Fixture/>)
