import {useId} from 'react'
import type {KaisEyeProps} from '../../KaisEye'
import {assistantCopy} from '../../ui-copy'
import {useHeaderKaisEye} from './useHeaderKaisEye'
import '../../kais-eye.css'
import './kais-eye.css'

type HeaderKaisEyeProps = Pick<KaisEyeProps, 'size' | 'state' | 'unreadBadge'>

export default function KaisEye({size = 56, state: requestedState, unreadBadge = false}: HeaderKaisEyeProps) {
  const id = `kais-header-eye-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const {eyeRef, gazeRef, state, motion, transitions, blinking, engaged, slowBlinking, attention, reaction} = useHeaderKaisEye(requestedState)
  if (!Number.isFinite(size) || size <= 0) throw new RangeError('KaisEye size must be a positive finite number')
  const closed = state === 'private'
  const dimmed = state === 'error' || state === 'off'
  const upper = dimmed ? 'M4 48Q48 32 92 48' : 'M4 48Q48 14 92 48'
  const lower = dimmed ? 'M4 48Q48 64 92 48' : 'M4 48Q48 82 92 48'
  const aperture = `${upper}${dimmed ? 'Q48 64 4 48Z' : 'Q48 82 4 48Z'}`
  const language = document.documentElement.lang.toLowerCase().startsWith('en') ? 'en' : 'tr'
  const rays = size <= 36 ? 12 : 24

  return <svg ref={eyeRef} className={`kaisEye kaisHeaderEye${motion ? ' kaisEyeMotion' : ''}${transitions ? ' kaisEyeTransitions' : ''}${blinking ? ' kaisEyeBlink' : ''}${engaged ? ' kaisEyeEngaged' : ''}${slowBlinking ? ' kaisEyeSlowBlink' : ''}${attention === 'drowsy' ? ' kaisEyeDrowsy' : ''}${reaction === 'error' ? ' kaisEyeSurprised' : ''}${reaction === 'unread' ? ' kaisEyeReactUnread' : ''}`}
    width={size} height={size} style={{width: size, height: size}} viewBox="0 0 96 96" fill="none"
    aria-hidden="true" aria-label={assistantCopy[language].eyeStates[state]} focusable="false" data-state={state}
    data-unread={unreadBadge} data-look-x="0" data-look-y="0" data-attention={attention} data-reaction={reaction}
    data-detail={size <= 24 ? 'compact' : size <= 36 ? 'medium' : 'full'}>
    <defs>
      <radialGradient id={`${id}-sclera`} cx=".5" cy=".5" r=".5">
        <stop className="cyberEyeScleraStop" stopOpacity=".9"/>
        <stop offset=".65" className="cyberEyeScleraStop" stopOpacity=".5"/>
        <stop offset="1" className="cyberEyeScleraStop" stopOpacity="0"/>
      </radialGradient>
      <radialGradient id={`${id}-iris`} cx=".5" cy=".5" r=".5">
        <stop offset=".4" className="cyberEyeInkStop"/>
        <stop offset=".55" className="kaisEyeAccentStop"/>
        <stop offset=".68" className="cyberEyeLightStop"/>
        <stop offset=".86" className="kaisEyeAccentStop"/>
        <stop offset="1" className="cyberEyeInkStop"/>
      </radialGradient>
      <clipPath id={`${id}-aperture`}><path d={aperture}/></clipPath>
      <filter id={`${id}-glow`} x="-20%" y="-50%" width="140%" height="200%" colorInterpolationFilters="sRGB">
        <feGaussianBlur stdDeviation="1.4"/>
      </filter>
    </defs>
    <g className="kaisEyeOpening">
      <g className="kaisEyeRingGlow cyberEyeGlow" filter={`url(#${id}-glow)`} data-layer="eyelid-glow">
        <path d={upper}/><path d={lower}/>
      </g>
      <g className="kaisEyeInterior" clipPath={`url(#${id}-aperture)`} data-layer="eye-interior">
        <g className="kaisEyeSclera" data-layer="sclera"><path d={aperture} fill={`url(#${id}-sclera)`}/></g>
        <g ref={gazeRef} className="kaisEyeGaze" data-layer="gaze">
          <g className="cyberEyeIris" data-layer="iris">
            <circle cx="48" cy="48" r="18" fill={`url(#${id}-iris)`}/>
            <circle className="cyberEyeIrisGlow" cx="48" cy="48" r="18.5" filter={`url(#${id}-glow)`}/>
            <g className="kaisEyeRotor cyberEyeCircuit">
              <circle cx="48" cy="48" r="17.5"/>
              <circle cx="48" cy="48" r="14" strokeDasharray="3 2"/>
              {Array.from({length: rays}, (_, index) => <path key={index}
                d={index % 3 ? 'M48 31v4' : 'M48 30v7'} transform={`rotate(${index * 360 / rays} 48 48)`}/>)}
              {size > 24 && [0, 1, 2, 3, 4, 5].map(index => <rect key={index} x="46.8" y="32" width="2.4" height="2.4"
                transform={`rotate(${index * 60 + 15} 48 48)`}/>)}
            </g>
          </g>
          <g className="cyberEyePupil" data-layer="pupil"><circle cx="48" cy="48" r="10"/></g>
          <g className="kaisEyePulse cyberEyeReflection" data-layer="reflection">
            <path d="M51 36.5h4v4h-4z"/><circle cx="43" cy="41" r="1.2"/>
          </g>
        </g>
      </g>
      <g className="kaisEyeLids" data-layer="eyelids">
        <g className="kaisEyeUpperLid" data-layer="upper-lid"><path d={upper}/></g>
        <g className="kaisEyeLowerLid" data-layer="lower-lid"><path d={lower}/></g>
      </g>
    </g>
    <g className="kaisEyePrivacy" data-layer="privacy" visibility={closed ? 'visible' : 'hidden'}>
      <rect x="73" y="63" width="14" height="12" rx="3"/>
      <path d="M76 63v-3a4 4 0 0 1 8 0v3M80 68v3"/>
    </g>
    <g className="kaisEyeError" data-layer="error" visibility={state === 'error' ? 'visible' : 'hidden'}>
      <circle cx="80" cy="70" r="7"/><path d="M80 66v4m0 3v.5"/>
    </g>
    <g className="kaisEyeUnread" data-layer="unread" visibility={unreadBadge ? 'visible' : 'hidden'}>
      <circle cx="84" cy="25" r="4"/>
    </g>
  </svg>
}
