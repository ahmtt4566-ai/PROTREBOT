import React from 'react'
import ReactDOM from 'react-dom/client'
import TestnetFirstApp from './TestnetFirstApp'
import AppErrorBoundary from './AppErrorBoundary'
import WebAccessGate from './WebAccessGate'
import AuthGate from './AuthGate'
import CopyProtection from './CopyProtection'
import { installAuthorizedFetch } from './api'
import './terminal-theme.css'
import './subscription.css'
import './coin-analysis.css'

installAuthorizedFetch()
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><CopyProtection/><AppErrorBoundary><WebAccessGate><AuthGate><TestnetFirstApp/></AuthGate></WebAccessGate></AppErrorBoundary></React.StrictMode>)
