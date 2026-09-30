import { ArrowLeft } from 'lucide-react'

export default function BackButton({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" className="homeBackButton" onClick={onClick} aria-label="Ana sayfaya dön">
      <ArrowLeft size={18} strokeWidth={2} aria-hidden="true" />
      <span>Ana sayfa</span>
    </button>
  )
}
