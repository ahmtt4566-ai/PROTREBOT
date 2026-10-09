import React from 'react'
import ReactDOM from 'react-dom/client'
import TestnetFirstApp from './TestnetFirstApp'
import AppErrorBoundary from './AppErrorBoundary'
import WebAccessGate from './WebAccessGate'
import AuthGate from './AuthGate'
import EmailVerificationRoute from '../../EmailVerificationRoute'
import PublicPolicyPage from '../../PublicPolicyPage'
import { publicPolicyForPath } from '../../compliance-content'
import { installAuthorizedFetch } from './api'
import { MemberAccessProvider } from '../../premium-access'
import './style.css'
import './binance-demo.css'
import './execution-v25.css'
import './web-access.css'
import './testnet-first.css'
import './mobile-dashboard.css'
import './cloud-ops-v27.css'
import './exchange-connections.css'
import './dark-dashboard.css'

installAuthorizedFetch()
const ownerPreview = import.meta.env.VITE_OWNER_PREVIEW === 'true'
const application = <AuthGate><MemberAccessProvider><TestnetFirstApp/></MemberAccessProvider></AuthGate>
const gatedApplication = ownerPreview ? <WebAccessGate>{application}</WebAccessGate> : application
const publicPolicy = publicPolicyForPath(window.location.pathname)
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><AppErrorBoundary><EmailVerificationRoute>{publicPolicy ? <PublicPolicyPage policy={publicPolicy}/> : gatedApplication}</EmailVerificationRoute></AppErrorBoundary></React.StrictMode>)
