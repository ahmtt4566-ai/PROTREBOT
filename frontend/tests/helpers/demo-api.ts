const timestamp = '2026-10-03T00:00:00Z'

export const demoStatus = {
  version: 'V22', mode: 'DEMO', configured: false, connected: false, armed: false, armed_until: null,
  rest_host: 'https://demo-fapi.binance.com', websocket_host: 'wss://fstream.binancefuture.com',
  real_trading_locked: true, last_checked: timestamp, last_error: null, events: [],
  limits: {max_margin_usdt: 100, max_leverage: 10, max_notional_usdt: 200, max_open_positions: 5, arm_minutes: 10},
}

export const demoAccount = {
  ...demoStatus, wallet_balance: 0, available_balance: 0, margin_balance: 0, unrealized_pnl: 0,
  positions: [], open_orders: [], open_algo_orders: [], hedge_mode: false, plans: [],
}

export const demoSummary = {
  version: 'V21', mode: 'DEMO', real_trading_locked: true, last_saved: timestamp,
  settings: {
    allowed_symbols: ['BTCUSDT'], allow_long: true, allow_short: true, max_loss_per_trade: 5,
    max_margin_per_trade: 100, daily_loss_limit: 30, daily_trade_limit: 30, max_positions: 5,
    min_confidence: 70, max_volatility_pct: 7, max_correlation_pct: 80, schedule_start_hour: 0,
    schedule_end_hour: 24, scan_seconds: 60, breakeven_enabled: false, breakeven_trigger_r: 1,
    trailing_enabled: false, trailing_trigger_r: 2, trailing_distance_r: 1, notifications: false,
    fee_bps_per_side: 4, slippage_bps_per_side: 2,
  },
  auto: {enabled: false, busy: false, cycles: 0, last_scan: timestamp, last_decision: 'WAIT', last_error: null},
  scanner: {
    active: false, scan_status: 'IDLE', scan_interval_seconds: 60, coins_scanned: 0, selected_count: 0,
    eligible_count: 0, last_scan_at: timestamp, next_scan_at: null, last_error: null,
    top_candidates: [], all_candidates: [], selected_symbols: [], last_stage: 'WAIT',
  },
  stream: {status: 'DISCONNECTED', transport: 'NONE', last_event: null, last_sync: null, reconnect_count: 0, error_count: 0, last_error: null},
  daily: {date: '2026-10-03', auto_entries: 0, events: 0, realized_pnl: 0, remaining_loss_budget: 30},
  account: {wallet_balance: null, available_balance: null, unrealized_pnl: null, positions: 0, reconciled_active_positions: 0, normal_orders: 0, algo_orders: 0},
  protection: {repairs: 0, duplicate_blocks: 0}, journal: [], automation_trades: [], backtest: null,
  certificate: {version: 'V21', status: 'UNKNOWN', score: 0, passed_gates: 0, total_gates: 0, gates: [], reason: 'Read-only UI fixture', generated_at: timestamp},
}

export const demoPerformance = {
  period: 'all', total_trades: 0, wins: 0, losses: 0, win_rate: 0, total_profit: 0, total_loss: 0, net_profit: 0,
  average_trade: 0, best_trade: 0, worst_trade: 0, profit_factor: null, average_win: null, average_loss: null,
  winning_streak: 0, losing_streak: 0, equity_curve: [], history_quality: 'EMPTY', max_drawdown: 0,
  directional: {
    LONG: {trades: 0, win_rate: null, realized_pnl: null, profit_factor: null},
    SHORT: {trades: 0, win_rate: null, realized_pnl: null, profit_factor: null},
  },
  demo_only: true, read_only: true,
}

export const demoHistory = {orders: [], algo_orders: [], trades: []}

export const demoReadFixtures = {
  '/api/binance-demo/status': demoStatus,
  '/api/binance-demo/account': demoAccount,
  '/api/v21/summary': demoSummary,
  '/api/v21/settings': demoSummary.settings,
  '/api/v21/performance': demoPerformance,
  '/api/v21/journal': {items: []},
  '/api/v21/scanner/candidates': {top_candidates: [], candidates: []},
}
