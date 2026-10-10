import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'

test('User notification panel preserves existing severity types and exposes critical status as text', () => {
  const source = readFileSync(new URL('../TestnetFirstApp.tsx', import.meta.url), 'utf8')
  assert.match(source, /severity:'success'\|'warning'\|'error'\|'critical'\|'info'/)
  assert.match(source, /item\.severity === 'critical' && <strong className="v26NotificationCritical">KRİTİK<\/strong>/)
  assert.match(source, /className=\{`v26NotificationItem \$\{item\.severity\}/)
})

test('Critical icon and label have dedicated styles without changing other severities', () => {
  const css = readFileSync(new URL('../terminal-theme.css', import.meta.url), 'utf8')
  for (const severity of ['warning', 'error', 'info', 'critical']) {
    assert.ok(css.includes(`.v26NotificationItem.${severity} > i`))
  }
  assert.match(css, /\.v26App \.v26NotificationCritical \{[^}]*color: var\(--terminal-negative/)
})
