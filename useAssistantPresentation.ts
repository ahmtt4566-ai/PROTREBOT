import {useEffect, useState} from 'react'

export function useAssistantMotion() {
  const [paused, setPaused] = useState(() => document.hidden || window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  useEffect(() => {
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)')
    const update = () => setPaused(document.hidden || preference.matches)
    document.addEventListener('visibilitychange', update)
    preference.addEventListener('change', update)
    update()
    return () => {
      document.removeEventListener('visibilitychange', update)
      preference.removeEventListener('change', update)
    }
  }, [])
  return paused
}
