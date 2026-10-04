import {expect, type Page} from '@playwright/test'
import {demoAccount, demoHistory, demoReadFixtures, demoSummary} from './demo-api'

type ApiCall = {path: string; body: Record<string, unknown>}
type MockState = {
  userId: string; remaining: number; chatStatus: number; chatBody: Record<string, unknown>;
  confirmStatus: number; confirmations: ApiCall[]; chats: ApiCall[]; requests: string[];
  network: boolean; hold: Promise<void> | null; historyLimit: number; confirmNetwork: boolean;
  secretMinimum: number;
  proactiveEnabled: boolean; proactiveMessage: Record<string, unknown> | null;
  preferenceStatus: number; checkInStatus: number; preferenceSaves: ApiCall[]; checkIns: ApiCall[];
  profileStatus: number; sessionStatus: number; ownerStatus: number;
}

export const approval = {action: 'get_analysis', symbol: 'BTCUSDT', timeframe: '1h', cost: 7, analysis_cost: 7, confirmation_token: 'signed-approval-ui-test'}
export const checkInMessage = {id: 'owned-status-check-in', reply: '1 açık pozisyon. PnL: 12,30.\nKoruma doğrulandı.\nVeri 4 saniye önce alındı.',
  language: 'tr', sources: ['get_my_positions', 'get_protection_status'], fetched_at: '2026-10-03T00:00:00Z', stale: false}

export async function mockAssistant(page: Page, remembered = false): Promise<MockState> {
  const state: MockState = {
    userId: 'assistant-member', remaining: 19, chatStatus: 200,
    chatBody: {reply: '**Plan**\nYanıt düz metindir.', language: 'tr', sources: ['get_plans']},
    confirmStatus: 200, confirmations: [], chats: [], requests: [], network: false, hold: null, historyLimit: 2, confirmNetwork: false, secretMinimum: 40,
    proactiveEnabled: false, proactiveMessage: null, preferenceStatus: 200, checkInStatus: 200, preferenceSaves: [], checkIns: [],
    profileStatus: 200, sessionStatus: 200, ownerStatus: 200,
  }
  await page.addInitScript(remembered => {
    if (!remembered) sessionStorage.setItem('protrebot-v25-session', 'member-ui-test-session')
    else if (!sessionStorage.getItem('assistant-remembered-test-seeded')) {
      sessionStorage.setItem('assistant-remembered-test-seeded', '1')
      localStorage.setItem('protrebot-v25-session', 'member-ui-test-session')
    }
  }, remembered)
  await page.route('**/api/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    state.requests.push(`${request.method()} ${path}`)
    const respond = (options: NonNullable<Parameters<typeof route.fulfill>[0]>) => route.fulfill({...options, headers: {
      'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*',
      'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS', ...options.headers,
    }})
    if (request.method() === 'OPTIONS') { await respond({status: 204}); return }
    const user = {id: state.userId, role: 'CUSTOMER', active: true, email_verified: true,
      email: 'member@example.test', display_name: 'UI Test Member'}
    if (['/api/v22/session', '/api/v22/profile'].includes(path)) {
      const status = !state.userId ? 401 : path === '/api/v22/profile' ? state.profileStatus : state.sessionStatus
      await respond({status, json: status === 200 ? {user, access: {canAccessMasterTrade: true, isPremium: false}} : {detail: 'Not authenticated'}})
      return
    }
    if (path === '/api/v22/auth/login') {
      await respond({json: {token: 'member-ui-test-session-next', user}})
      return
    }
    if (path === '/api/web/access/check') {
      await respond({status: state.ownerStatus, json: {authorized: state.ownerStatus === 200}})
      return
    }
    if (path === '/api/assistant/proactive/preferences') {
      if (request.method() === 'POST') {
        const body = request.postDataJSON()
        state.preferenceSaves.push({path, body})
        if (state.preferenceStatus === 200) state.proactiveEnabled = body.enabled
      }
      await respond({status: request.method() === 'POST' ? state.preferenceStatus : 200,
        json: {enabled: state.proactiveEnabled, available: true, poll_interval_seconds: 60}})
      return
    }
    if (path === '/api/assistant/proactive/check-in') {
      state.checkIns.push({path, body: request.postDataJSON()})
      await respond({status: state.checkInStatus, json: {enabled: state.proactiveEnabled, available: true, poll_interval_seconds: 60,
        message: state.proactiveEnabled ? state.proactiveMessage : null}})
      return
    }
    if (path === '/api/assistant/usage') {
      await respond({json: {remaining: state.remaining, total: 20, resetsAt: '2026-10-04T00:00:00Z', limits: {
        max_input_chars: 80, history_messages: state.historyLimit, history_message_max_chars: 12, page_context_max_chars: 8, secret_min_alphanumeric_chars: state.secretMinimum,
      }}})
      return
    }
    if (path === '/api/assistant/chat') {
      state.chats.push({path, body: request.postDataJSON()})
      if (state.hold) await state.hold
      if (state.network) { await route.abort('failed'); return }
      await respond({status: state.chatStatus, json: state.chatBody, headers: state.chatStatus === 429 ? {'Retry-After': '45'} : {}})
      return
    }
    if (path === '/api/assistant/analysis/confirm') {
      state.confirmations.push({path, body: request.postDataJSON()})
      if (state.confirmNetwork) { await route.abort('failed'); return }
      await respond({status: state.confirmStatus, json: state.confirmStatus === 200 ? {
        data: {symbol: 'BTCUSDT', timeframe: '1h', direction: 'LONG', final_decision_score: 72, confidence: 81, opportunity_score: 78, mtf_alignment: 90, data_age_seconds: 4,
          entry: 987654.321, stop_loss: 123456.789},
        fetched_at: '2026-10-03T00:00:00Z', stale: false, sources: ['get_analysis'],
      } : {detail: 'Invalid confirmation'}})
      return
    }
    const fixtures: Record<string, unknown> = {
      ...demoReadFixtures,
      '/api/v22/session': {user},
      '/api/v22/profile': {user, access: {canAccessMasterTrade: true, isPremium: false}},
      '/api/markets': [{symbol: 'BTCUSDT', display: 'BTC/USDT', price: 60000, change: 1, volume: 1000000}],
      '/api/health': {status: 'ok'},
      '/api/exchange-connections/status': {connections: {TESTNET: {configured: false, active: false}}, vault: {ready: true}},
      '/api/notifications': {items: [], unread: 0},
      '/api/v27/operations': {
        version: 'V27', generated_at: '2026-10-03T00:00:00Z',
        deployment: {tier: 'TEST', always_on: false, database: 'FIXTURE', uptime_seconds: 0},
        testnet: {configured: false, connected: false, armed: false, stream: demoSummary.stream,
          auto: demoSummary.auto, account: demoAccount, daily: {entries: 0}},
        evidence: {status: 'EMPTY', persistent: false, restored: false, count: 0,
          events: [], certificate: demoSummary.certificate},
        safety: {testnet_only: true, real_trading_locked: true, auto_resumes_after_restart: false, profit_guaranteed: false},
      },
      '/api/v25/status': {connected: false, real_trading_locked: true, execution_state: 'LOCKED', armed: false,
        live_auto_trade: false, recovery_ready: true, credentials: {configured: false}, account: {}, events: [],
        stream: {status: 'DISCONNECTED'}, readiness: {ready: false, gates: []}, policy: {allowed_symbols: ['BTCUSDT']}},
    }
    const payload = path.startsWith('/api/klines/') ? [] : path.startsWith('/api/analysis/')
      ? {symbol: 'BTCUSDT', direction: 'BEKLE', confidence: 0, entry: 60000, stop_loss: 59000, tp1: 61000, tp2: 62000, tp3: 63000,
        support: 59000, resistance: 61000, rsi: 50, adx: 20, volume_ratio: 1, trend: 'NEUTRAL', momentum: 'NEUTRAL',
        explanation: 'UI fixture', series: {ema20: [], ema50: [], ema200: []}}
      : fixtures[path] ?? (path.startsWith('/api/v21/history/') ? demoHistory : {})
    await respond({json: payload})
  })
  return state
}

export async function openChat(page: Page) {
  await page.goto('/')
  await page.getByRole('button', {name: 'Kais AI', exact: true}).click()
  const dialog = page.getByRole('dialog', {name: 'Kais AI'})
  await expect(dialog.getByRole('textbox', {name: 'Kais AI mesajın'})).toBeEnabled()
  return dialog
}
