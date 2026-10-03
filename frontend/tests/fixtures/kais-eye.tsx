import {StrictMode, useRef, useState, type CSSProperties} from 'react'
import {createRoot} from 'react-dom/client'
import KaisEye, {type KaisEyeState} from '../../../KaisEye'
import {emitKaisReaction} from '../../../kais-reactions'
import {useKaisErrorReaction, useKaisWorkspaceReaction} from '../../../useKaisPageReactions'
import '../../../tokens.css'

const states: KaisEyeState[] = ['idle', 'thinking', 'private', 'error', 'off']
const sizes = [56, 36, 24]

declare global {
  interface Window { kaisReact: typeof emitKaisReaction }
}
window.kaisReact = emitKaisReaction

function BehaviorFixture() {
  const [mounted, setMounted] = useState(true)
  const [state, setState] = useState<KaisEyeState>('idle')
  const [unread, setUnread] = useState(false)
  const [workspace, setWorkspace] = useState('analyst')
  const [notice, setNotice] = useState<{kind: string} | null>(null)
  const area = useRef<HTMLElement>(null)
  useKaisWorkspaceReaction(workspace, area)
  useKaisErrorReaction(notice)
  return <section ref={area} data-theme="dark">
    <div data-testid="behavior-eye">{mounted && <KaisEye state={state} unreadBadge={unread}/>}</div>
    <button type="button" onClick={() => setMounted(value => !value)}>{mounted ? 'Unmount eye' : 'Mount eye'}</button>
    <label>Eye state <select aria-label="Eye state" value={state} onChange={event => setState(states[event.target.selectedIndex])}>
      {states.map(value => <option key={value} value={value}>{value}</option>)}
    </select></label>
    <label><input type="checkbox" checked={unread} onChange={event => setUnread(event.target.checked)}/>Unread</label>
    <label data-private="true">Private field<input aria-label="Private field" defaultValue="private fixture value"/></label>
    <label>Private password<input aria-label="Private password" type="password" data-private="true"/></label>
    <label>Normal field<input aria-label="Normal field" type="search"/></label>
    <label data-private="false">False marker<input aria-label="False marker"/></label>
    <button type="button" onClick={() => setWorkspace(value => value === 'analyst' ? 'scanner' : 'analyst')}>Change workspace</button>
    <button type="button" onClick={() => setNotice({kind: 'error'})}>Error toast</button>
    <button type="button" onClick={() => setNotice({kind: 'ok'})}>Success toast</button>
  </section>
}

const matrix = <>
  {['dark', 'light'].map(theme => <section key={theme} data-theme={theme}>
    <h1>{theme}</h1>
    <div className="eyeCases">
      {sizes.flatMap(size => states.map(state => <figure key={`${size}-${state}`} data-testid={`${theme}-${size}-${state}`}>
        <KaisEye size={size} state={state}/>
        <figcaption>{size}px · {state}</figcaption>
      </figure>))}
    </div>
  </section>)}
  <section data-theme="light" className="eyeCases">
    <figure data-testid="default"><KaisEye state="idle"/></figure>
    <figure data-testid="gaze"><KaisEye state="idle" lookAt={{x: 10, y: -10}}/></figure>
    <figure data-testid="private-gaze"><KaisEye state="private" lookAt={{x: 1, y: 1}}/></figure>
    <figure data-testid="off-gaze"><KaisEye state="off" lookAt={{x: 1, y: 1}}/></figure>
    <figure data-testid="unread"><KaisEye state="idle" unreadBadge/></figure>
    <figure data-testid="english"><KaisEye state="thinking" aria-label="Kais AI: thinking"/></figure>
    <figure data-testid="custom" style={{'--kais-accent': '#006b60', '--kais-glow': '#23cbbd'} as CSSProperties}><KaisEye state="idle"/></figure>
  </section>
</>

createRoot(document.getElementById('root')!).render(<StrictMode>{new URLSearchParams(window.location.search).has('behavior')
  ? <BehaviorFixture/> : matrix}</StrictMode>)
