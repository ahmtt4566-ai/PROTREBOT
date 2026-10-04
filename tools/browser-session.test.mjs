import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'
import {runInNewContext} from 'node:vm'
import {stripTypeScriptTypes} from 'node:module'

function storage() {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
}

function browser(mode = 'production') {
  const source = readFileSync(new URL('../api.ts', import.meta.url), 'utf8')
    .replace(/^import .*kais-chat-storage'.*$/m, '')
    .replaceAll('import.meta.env', `(${JSON.stringify({PROD: mode === 'production', DEV: false, MODE: mode})})`)
  const calls = []
  const exports = {}
  const localStorage = storage()
  const sessionStorage = storage()
  const window = {
    location: {href: 'https://app.example/'},
    dispatchEvent() {},
    fetch: async (input, init) => {
      calls.push({input, init})
      return {ok: true, json: async () => ({authorized: true})}
    },
  }
  const code = stripTypeScriptTypes(source).replace(/\bexport (?=(?:async )?function|const)/g, '') +
    '\nObject.assign(exports, {API_BASE, USER_SESSION_KEY, userSessionToken, saveUserSessionToken, ownerAccessToken, saveOwnerAccessToken, clearOwnerAccessToken, verifyOwnerAccess, installAuthorizedFetch})'
  runInNewContext(code, {
    exports, window, localStorage, sessionStorage, URL, Request, Headers, Event, atob, console,
    clearKaisChatHistory() {}, clearLegacyChatHistory() {},
  })
  return {api: exports, calls, window, localStorage, sessionStorage}
}

test('Production uses same-origin API and removes legacy bearer persistence', () => {
  const {api, localStorage, sessionStorage} = browser()
  assert.equal(api.API_BASE, '/api')
  localStorage.setItem(api.USER_SESSION_KEY, 'synthetic-legacy-session-not-live')
  sessionStorage.setItem(api.USER_SESSION_KEY, 'synthetic-old-tab-session-not-live')
  assert.equal(api.userSessionToken(), '')
  assert.equal(localStorage.getItem(api.USER_SESSION_KEY), null)
  assert.equal(sessionStorage.getItem(api.USER_SESSION_KEY), null)
})

test('Only public cookie session metadata can be remembered in production', () => {
  const {api, localStorage} = browser()
  assert.throws(() => api.saveUserSessionToken('synthetic-native-bearer-not-live', true), /güvenli tarayıcı/)
  api.saveUserSessionToken('cookie-session:member-a', true)
  assert.equal(api.userSessionToken(), 'cookie-session:member-a')
  assert.equal(localStorage.getItem(api.USER_SESSION_KEY), 'cookie-session:member-a')
})

test('Cookie hints never become Bearer, vault session, or owner authentication headers', async () => {
  const {api, window, calls} = browser()
  api.saveUserSessionToken('cookie-session:member-a', false)
  api.saveOwnerAccessToken('synthetic-owner-access-not-live')
  api.installAuthorizedFetch()
  await window.fetch('/api/v25/policy', {
    method: 'POST', headers: {
      Authorization: 'Bearer cookie-session:member-a',
      'X-ProTreBot-Session': 'cookie-session:member-a',
      'X-ProTreBot-Owner': 'cookie-owner',
    },
  })
  const {headers, credentials} = calls[0].init
  for (const name of ['Authorization', 'X-ProTreBot-Session', 'X-ProTreBot-Owner']) assert.equal(headers.has(name), false)
  assert.equal(headers.get('X-Requested-With'), 'XMLHttpRequest')
  assert.equal(credentials, 'include')
})

test('Non-API fetches do not receive session credentials or browser auth headers', async () => {
  const {api, window, calls} = browser()
  api.saveUserSessionToken('cookie-session:member-a', false)
  api.installAuthorizedFetch()
  await window.fetch('https://market.example/ticker')
  assert.equal(calls[0].init.credentials, undefined)
  assert.equal(calls[0].init.headers.has('Authorization'), false)
  assert.equal(calls[0].init.headers.has('X-Requested-With'), false)
})

test('Owner cookie metadata contains no owner secret and is not sent as an access code', async () => {
  const {api, sessionStorage, calls} = browser()
  api.saveOwnerAccessToken('synthetic-owner-access-not-live')
  assert.equal(sessionStorage.getItem('protrebot.web.owner-access'), 'cookie-owner')
  await api.verifyOwnerAccess(api.ownerAccessToken())
  assert.equal(calls[0].init.credentials, 'include')
  assert.equal(calls[0].init.headers['X-ProTreBot-Owner'], undefined)
})

test('Owner logout requests server-side cookie deletion before resolving', async () => {
  const {api, calls, sessionStorage} = browser()
  api.saveOwnerAccessToken('synthetic-owner-access-not-live')
  await api.clearOwnerAccessToken()
  assert.equal(sessionStorage.getItem('protrebot.web.owner-access'), null)
  assert.equal(calls[0].input, '/api/web/access/logout')
  assert.equal(calls[0].init.method, 'POST')
  assert.equal(calls[0].init.headers['X-Requested-With'], 'XMLHttpRequest')
})

test('Explicit test-mode fixtures stay restricted to the test build', () => {
  const {api} = browser('test')
  api.saveUserSessionToken('synthetic-playwright-member-fixture', true)
  assert.equal(api.userSessionToken(), 'synthetic-playwright-member-fixture')
})

test('Vercel proxies API before SPA fallback and rejects executable inline scripts', () => {
  const config = JSON.parse(readFileSync(new URL('../vercel.json', import.meta.url), 'utf8'))
  assert.equal(config.rewrites[0].source, '/api/:path*')
  assert.equal(config.rewrites.at(-1).destination, '/index.html')
  const headers = new Map(config.headers[0].headers.map(row => [row.key, row.value]))
  const policy = headers.get('Content-Security-Policy')
  assert.match(policy, /script-src 'self';/)
  assert.match(policy, /frame-ancestors 'none'/)
  assert.equal(policy.includes('unsafe-eval'), false)
  assert.ok(headers.has('Strict-Transport-Security'))
})
