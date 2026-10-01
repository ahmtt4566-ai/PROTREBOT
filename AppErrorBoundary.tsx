import { Component, type ErrorInfo, type ReactNode } from 'react'
import { RefreshCw, ShieldCheck } from 'lucide-react'
import { reportClientError } from './api'

type Props = { children:ReactNode }
type State = { failed:boolean; message:string }

export default class AppErrorBoundary extends Component<Props,State> {
  state:State = {failed:false,message:''}

  static getDerivedStateFromError(error:unknown):State {
    return {
      failed:true,
      message:error instanceof Error ? error.message : 'Beklenmeyen bir ekran hatası oluştu.',
    }
  }

  componentDidCatch(error:unknown, info:ErrorInfo) {
    console.error('ProTreBot güvenli ekran koruması', error, info.componentStack)
    reportClientError(error, {componentStack:info.componentStack})
  }

  private recover = () => {
    this.setState({failed:false,message:''})
    window.location.reload()
  }

  render() {
    if (!this.state.failed) return this.props.children
    return <main className="appRecoveryShell" role="alert">
      <section className="appRecoveryCard">
        <span className="appRecoveryMode"><i aria-hidden="true" />DEMO MODE</span>
        <span className="appRecoveryIcon" aria-hidden="true"><ShieldCheck size={34} strokeWidth={1.7} /></span>
        <small className="appRecoveryEyebrow">SECURE RECOVERY</small>
        <h1>Panel geçici olarak yüklenemedi</h1>
        <p>Demo işlem masası verileri şu anda yüklenemedi. Mevcut işlem kaydı korunuyor.</p>
        <p className="appRecoveryHint">Yenilemeyi deneyebilir veya birkaç saniye sonra tekrar açabilirsiniz.</p>
        <button className="appRecoveryAction" type="button" onClick={this.recover}>
          <RefreshCw size={16} aria-hidden="true" />
          Paneli yeniden yükle
        </button>
        <details className="appRecoveryDetails">
          <summary>Teknik ayrıntılar</summary>
          <pre>{this.state.message || 'Veri yeniden istenecek.'}</pre>
        </details>
      </section>
    </main>
  }
}
