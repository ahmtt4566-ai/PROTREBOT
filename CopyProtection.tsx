import { useEffect } from 'react'

const editableTarget = (target: EventTarget | null): boolean => {
  const element = target instanceof HTMLElement ? target : null
  return Boolean(element?.matches('input, textarea, select, option, [contenteditable="true"]') || element?.closest('input, textarea, select, option, [contenteditable="true"]'))
}

export default function CopyProtection() {
  useEffect(() => {
    const onContextMenu = (event: MouseEvent) => {
      if (!editableTarget(event.target)) event.preventDefault()
    }
    const onSelectStart = (event: Event) => {
      if (!editableTarget(event.target)) event.preventDefault()
    }
    const onCopyOrCut = (event: ClipboardEvent) => {
      if (!editableTarget(event.target)) event.preventDefault()
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (editableTarget(event.target)) return
      if ((event.ctrlKey || event.metaKey) && ['a', 'c', 'x'].includes(event.key.toLowerCase())) event.preventDefault()
    }
    document.addEventListener('contextmenu', onContextMenu)
    document.addEventListener('selectstart', onSelectStart)
    document.addEventListener('copy', onCopyOrCut)
    document.addEventListener('cut', onCopyOrCut)
    document.addEventListener('keydown', onKeyDown)
    document.body.classList.add('site-copy-protected')
    return () => {
      document.removeEventListener('contextmenu', onContextMenu)
      document.removeEventListener('selectstart', onSelectStart)
      document.removeEventListener('copy', onCopyOrCut)
      document.removeEventListener('cut', onCopyOrCut)
      document.removeEventListener('keydown', onKeyDown)
      document.body.classList.remove('site-copy-protected')
    }
  }, [])

  return null
}
