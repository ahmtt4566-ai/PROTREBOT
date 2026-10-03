import {useId} from 'react'
import {assistantCopy} from './ui-copy'
import {useKaisEye} from './useKaisEye'
import './kais-eye.css'

export type KaisEyeState = 'idle' | 'thinking' | 'private' | 'error' | 'off'
export type KaisEyeProps = {
  size?: number
  state: KaisEyeState
  lookAt?: {x: number; y: number}
  unreadBadge?: boolean
  'aria-label'?: string
}

export default function KaisEye({size = 56, state: requestedState, lookAt, unreadBadge = false, 'aria-label': label}: KaisEyeProps) {
  const id = `kais-eye-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const {eyeRef, gazeRef, state, motion, transitions, blinking, engaged, slowBlinking, attention, reaction} = useKaisEye(requestedState, lookAt)
  if (!Number.isFinite(size) || size <= 0) throw new RangeError('KaisEye size must be a positive finite number')
  if (lookAt && (!Number.isFinite(lookAt.x) || !Number.isFinite(lookAt.y))) throw new RangeError('KaisEye lookAt coordinates must be finite')
  const closed = state === 'private'
  const dimmed = state === 'error' || state === 'off'
  const upper = dimmed ? 'M12 30Q32 24 52 30' : 'M12 30Q32 10 52 30'
  const lower = dimmed ? 'M12 30Q32 42 52 30' : 'M12 30Q32 50 52 30'
  const aperture = dimmed ? 'M12 30Q32 24 52 30Q32 42 12 30Z' : 'M12 30Q32 10 52 30Q32 50 12 30Z'
  const language = document.documentElement.lang.toLowerCase().startsWith('en') ? 'en' : 'tr'
  const accessibleLabel = label && state === requestedState ? label
    : `${assistantCopy[language].eyeStates[state]}${unreadBadge ? language === 'en' ? ' · unread notification' : ' · okunmamış bildirim' : ''}`

  return <svg ref={eyeRef} className={`kaisEye${motion ? ' kaisEyeMotion' : ''}${transitions ? ' kaisEyeTransitions' : ''}${blinking ? ' kaisEyeBlink' : ''}${engaged ? ' kaisEyeEngaged' : ''}${slowBlinking ? ' kaisEyeSlowBlink' : ''}${attention === 'drowsy' ? ' kaisEyeDrowsy' : ''}${reaction === 'error' ? ' kaisEyeSurprised' : ''}${reaction === 'unread' ? ' kaisEyeReactUnread' : ''}`}
    width={size} height={size} style={{width: size, height: size}} viewBox="0 0 64 64" fill="none"
    role="img" aria-label={accessibleLabel} focusable="false" data-state={state}
    data-unread={unreadBadge} data-look-x="0" data-look-y="0" data-attention={attention} data-reaction={reaction}
    data-detail={size <= 24 ? 'compact' : size <= 36 ? 'medium' : 'full'}>
    <title>{accessibleLabel}</title>
    <defs>
      <linearGradient id={`${id}-sclera`} x1="12" y1="20" x2="52" y2="40" gradientUnits="userSpaceOnUse">
        <stop className="kaisEyeLightStop"/>
        <stop offset="1" className="kaisEyeAccentStop"/>
      </linearGradient>
      <radialGradient id={`${id}-iris`} cx=".4" cy=".3" r=".7">
        <stop className="kaisEyeLightStop"/>
        <stop offset=".5" className="kaisEyeAccentStop"/>
        <stop offset="1" className="kaisEyeInkStop"/>
      </radialGradient>
      <clipPath id={`${id}-aperture`}><path d={aperture}/></clipPath>
    </defs>
    <g className="kaisEyeRing" data-layer="ring">
      <circle className="kaisEyeRingGlow" cx="32" cy="30" r="25"/>
      <path className="kaisEyeTail" d="M14 47 10 58 25 53"/>
      <g className="kaisEyeRotor">
        <circle className="kaisEyeRingSegments" cx="32" cy="30" r="25" strokeDasharray={size <= 24 ? '32 7' : '18 4 8 4'}/>
        <g className="kaisEyeDetail"><circle cx="32" cy="30" r="21.5" strokeDasharray="2 6"/></g>
      </g>
    </g>
    <g className="kaisEyeOpening">
    <g className="kaisEyeInterior" clipPath={`url(#${id}-aperture)`} data-layer="eye-interior">
      <g className="kaisEyeSclera" data-layer="sclera"><path d={aperture} fill={`url(#${id}-sclera)`}/></g>
      <g ref={gazeRef} className="kaisEyeGaze" data-layer="gaze">
        <g className="kaisEyeIris" data-layer="iris">
          <circle cx="32" cy="30" r="11" fill={`url(#${id}-iris)`}/>
          <circle className="kaisEyeDetail" cx="32" cy="30" r="9.5" strokeDasharray="1 2"/>
        </g>
        <g className="kaisEyePupil" data-layer="pupil"><circle cx="32" cy="30" r="7.5"/></g>
        <g className="kaisEyePulse" data-layer="pulse"><path d={size <= 24 ? 'M27 30h2l2-3 2 6 2-3h2' : 'M26 30h3l2-4 2 8 2-4h3'}/></g>
        <g className="kaisEyeReflection" data-layer="reflection">
          <path d="M25 23q3-3 6-2" strokeWidth="2" strokeLinecap="round"/>
          <circle className="kaisEyeDetail" cx="38" cy="35" r="1"/>
        </g>
      </g>
    </g>
    <g className="kaisEyeLids" data-layer="eyelids">
      <g className="kaisEyeUpperLid" data-layer="upper-lid"><path d={upper}/></g>
      <g className="kaisEyeLowerLid" data-layer="lower-lid"><path d={lower}/></g>
    </g>
    </g>
    <g className="kaisEyeThinking" data-layer="thinking" visibility={state === 'thinking' ? 'visible' : 'hidden'}>
      <circle cx="27" cy="12" r="1.5"/><circle cx="32" cy="10" r="1.5"/><circle cx="37" cy="12" r="1.5"/>
    </g>
    <g className="kaisEyePrivacy" data-layer="privacy" visibility={closed ? 'visible' : 'hidden'}>
      <rect x="40" y="43" width="14" height="12" rx="3"/>
      <path d="M43 43v-3a4 4 0 0 1 8 0v3M47 48v3"/>
    </g>
    <g className="kaisEyeError" data-layer="error" visibility={state === 'error' ? 'visible' : 'hidden'}>
      <circle cx="48" cy="48" r="7"/><path d="M48 44v4m0 3v.5"/>
    </g>
    <g className="kaisEyeUnread" data-layer="unread" visibility={unreadBadge ? 'visible' : 'hidden'}>
      <circle cx="54" cy="9" r="5"/>
    </g>
  </svg>
}
