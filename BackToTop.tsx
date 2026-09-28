import { useEffect, useState } from 'react'
import { ChevronUp } from 'lucide-react'

type ScrollTarget = Window | HTMLElement

const findScrollTarget = ():ScrollTarget => {
  let element = document.getElementById('root')
  while (element) {
    const style = window.getComputedStyle(element)
    if (/(auto|scroll|overlay)/.test(style.overflowY) && element.scrollHeight > element.clientHeight + 1) return element
    element = element.parentElement
  }
  return window
}

const getScrollTop = (target:ScrollTarget):number => target === window ? window.scrollY : target.scrollTop

export default function BackToTop() {
  const [visible,setVisible] = useState(false)

  useEffect(() => {
    const target = findScrollTarget()
    let ticking = false
    const update = () => {
      setVisible(getScrollTop(target) >= 360)
      ticking = false
    }
    const onScroll = () => {
      if (!ticking) {
        ticking = true
        window.requestAnimationFrame(update)
      }
    }

    update()
    target.addEventListener('scroll',onScroll,{passive:true})
    return () => target.removeEventListener('scroll',onScroll)
  },[])

  const scrollToTop = () => {
    const target = findScrollTarget()
    const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
    target.scrollTo({top:0,behavior})
  }

  return <button
    type="button"
    className={`globalBackToTop${visible ? ' visible' : ''}`}
    aria-label="Yukarı çık"
    aria-hidden={!visible}
    tabIndex={visible ? 0 : -1}
    onClick={scrollToTop}
  >
    <ChevronUp aria-hidden="true" />
  </button>
}
