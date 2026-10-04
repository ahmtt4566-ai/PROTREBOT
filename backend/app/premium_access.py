"""Membership enforcement and fail-closed public trading projections."""
from __future__ import annotations

from typing import Any

PRIVATE_KEYS = frozenset({
    "entry", "entry_price", "entryPrice", "entry_preview", "entryPreview", "entry_quality",
    "stop", "stop_loss", "stopLoss", "initial_stop_loss", "sl", "tp1", "tp2", "tp3",
    "target", "targets", "target_price", "take_profit", "takeProfit", "trigger_price",
    "triggerPrice", "limit_price", "support", "resistance", "risk_reward", "riskReward",
    "risk_pct", "potential_tp3_pct", "estimated_stop_loss_usdt", "stop_distance_pct",
    "reason", "reasons", "whyWait", "why_wait", "waitingFor", "waiting_for",
    "explanation", "long_case", "short_case", "longCase", "shortCase",
    "trigger_monitor", "triggerMonitor", "conditions", "invalidation", "detail",
    "plans", "events", "external_trades", "open_orders", "open_algo_orders",
})

PUBLIC_TRADE_KEYS = frozenset({
    "results", "items", "data", "history", "analysis", "signal", "scanner", "account",
    "positions", "settings", "policy", "auto", "credentials", "vault", "connections",
    "LIVE", "TESTNET", "stream", "readiness", "gates", "emergency", "daily",
    "authorization", "consent", "safety", "markets", "candidates",
    "id", "symbol", "symbols", "display", "direction", "side", "trend", "momentum",
    "price", "currentPrice", "current_price", "mark_price", "markPrice", "change",
    "volume", "volume_ratio", "volume_change_pct", "volatility_pct", "rsi", "macd",
    "macd_signal", "macd_hist", "atr", "ema20", "ema50", "ema200", "confidence",
    "smart_score", "opportunity_score", "final_decision_score", "mtf_direction",
    "mtf_alignment", "mtf_timeframes", "timeframes", "timeframe", "interval",
    "anomaly", "kind", "label", "strength", "time", "open", "high", "low", "close",
    "timestamp", "created_at", "updated_at", "cached", "count", "limit",
    "status", "connected", "configured", "active", "ready", "enabled", "passed",
    "key", "read_only", "real_trading_locked", "armed", "armed_until",
    "live_auto_trade", "execution_state", "reconciliation_required", "recovery_ready",
    "policy_acknowledged", "last_scan", "last_scan_at", "last_skip_reason",
    "last_cycle_stage", "scanned_symbol_count", "candidate_count", "candidate_symbols",
    "selected_symbols", "selected_symbols_count", "session_until", "expires_at",
    "valid", "scope_match", "fingerprint", "tested_at", "last_test_ok",
    "wallet_balance", "available_balance", "margin_balance", "unrealized_pnl",
    "realized_pnl", "remaining_loss_budget", "entries", "hedge_mode", "leverage",
    "quantity", "positionAmt", "positionSide", "margin_type", "marginType",
    "total_trades", "wins", "losses", "win_rate", "total_profit", "total_loss",
    "net_profit", "average_trade", "best_trade", "worst_trade", "profit_factor",
    "average_win", "average_loss", "losing_streak", "max_drawdown", "history_quality",
    "allowed_symbols", "risk_per_trade_pct", "max_positions", "max_exposure_usdt",
    "min_confidence", "min_margin_usdt", "max_margin_usdt", "max_leverage",
    "max_daily_loss_usdt", "max_trades_per_day", "timeframe", "strategy",
    "market_data", "live_account", "risk", "exposure", "protection", "protection_state",
    "last_verified", "secrets_returned_to_browser", "connection_test_creates_orders",
    "live_orders_require_v25_gates", "contractType", "quoteAsset", "score",
})


def public_projection(payload: Any, *, allowlist: bool = False) -> Any:
    if isinstance(payload, list):
        return [public_projection(row, allowlist=allowlist) for row in payload]
    if isinstance(payload, dict):
        return {
            key: public_projection(value, allowlist=allowlist)
            for key, value in payload.items()
            if key not in PRIVATE_KEYS and (not allowlist or key in PUBLIC_TRADE_KEYS)
        }
    return payload


def requires_premium(path: str, method: str) -> bool:
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    if path.startswith(("/api/v25/", "/api/exchange-connections/")):
        return True
    if path.startswith("/api/grid/plan/clear/"):
        return True
    if path in {
        "/api/binance-demo/connect", "/api/binance-demo/arm", "/api/binance-demo/order",
        "/api/binance-demo/order/test", "/api/v21/auto/start", "/api/v21/scanner/scan",
        "/api/v21/smoke-test", "/api/v21/drill", "/api/grid/engine/start",
        "/api/grid/plan/save", "/api/grid/engine/recenter", "/api/paper/open",
        "/api/paper/limit", "/api/paper/bot/start", "/api/v7/orchestrator/start",
        "/api/v9/paper/order", "/api/v10/evolution/start", "/api/v11/risk/start",
        "/api/v9/twin/start", "/api/v21/settings", "/api/grid/engine/profile",
        "/api/paper/bot/training/toggle",
    }:
        return True
    return path.startswith(("/api/paper/demo/", "/api/v20/profile/"))
