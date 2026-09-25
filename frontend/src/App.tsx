import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { CandlestickSeries, ColorType, createChart, HistogramSeries, LineSeries } from 'lightweight-charts'
import { Activity, Bot, BrainCircuit, FlaskConical, Grid3X3, History, LayoutDashboard, Power, RadioTower, RefreshCw, Shield, ShieldCheck, TrendingUp } from 'lucide-react'
import { API_BASE } from './api'

const BinanceDemo = lazy(() => import('./BinanceDemo'))
const CommercialHub = lazy(() => import('./CommercialHub'))

type Market = { symbol:string; display:string; price:number; change:number; volume:number; direction?:string; confidence?:number; volume_ratio?:number; breakout?:boolean }
type Candle = { time:number; open:number; high:number; low:number; close:number; volume:number }
type Point = { time:number; value:number }
type Analysis = { direction:'LONG'|'SHORT'|'BEKLE'; confidence:number; entry:number; stop_loss:number; tp1:number; tp2:number; tp3:number; risk_reward:number; trend:string; momentum:string; rsi:number; macd:number; adx:number; atr:number; volume_ratio:number; support:number; resistance:number; explanation:string; ema:{ema20:number;ema50:number;ema200:number}; radar:{trap_score:number;trap_level:string;breakout_quality:number;entry_timing:string;squeeze:string;fomo_risk:string;wick_signal:string}; series:{ema20:Point[];ema50:Point[];ema200:Point[]} }
type Consensus = { direction:string; alignment:number; verdict:string; entry_permission:boolean; reason:string; timeframes:{timeframe:string;direction:string;confidence:number;trend:string;radar_level:string}[] }
type MarketGuard = { symbol:string; market_mode:string; auto_allowed:boolean; risk_score:number; reason:string; btc_direction:string; btc_adx:number; volatility_pct:number; last_candle_move_pct:number; symbol_direction:string }
type BotEvent = { kind:string; message:string; symbol?:string; created_at:string }
type PaperAutonomyCandidate = { rank:number;symbol:string;display:string;direction:'LONG'|'SHORT';confidence:number;trap_score:number;volume_ratio:number;change:number;price:number;breakout:boolean;edge_score:number;eligible:boolean;status:string }
type PaperAutonomyAllocation = { approved:boolean;status:string;reason:string;amount:number;risk_budget_usdt?:number;projected_stop_loss_usdt?:number;projected_plan_net_usdt?:number;projected_tp3_full_net_usdt?:number;minimum_projected_net_usdt?:number;stop_distance_pct?:number;tp3_distance_pct?:number;allocation_pct?:number;current_exposure_pct?:number;maximum_total_exposure_pct?:number;profile?:string;symbol?:string;display?:string;direction?:string;confidence?:number;edge_score?:number;calculated_at?:string;profit_guaranteed:boolean }
type PaperDailyReference = { realized_pnl_usdt:number;reference_usdt:number;progress_pct:number;status:string;profit_guaranteed:boolean;note:string }
type PaperAutonomy = { version:string;profile:string;universe_size:number;shortlist_size:number;risk_per_trade_pct:number;max_allocation_pct:number;max_total_exposure_pct:number;minimum_projected_net_usdt:number;maximum_positions:number;maximum_order_usdt:number;shortlist:PaperAutonomyCandidate[];last_allocation:PaperAutonomyAllocation|null;last_scan_at:string|null;current_exposure_usdt:number;current_exposure_pct:number;unrealized_pnl_usdt:number;daily_reference:PaperDailyReference;note:string;paper_only:boolean;profit_guaranteed:boolean }
type PaperBotState = { enabled:boolean;busy?:boolean;training_mode:boolean;profile?:'TEMKINLI'|'DENGELI'|'HIZLI';mode:string;scan_interval_seconds:number;last_action:string;last_check?:string|null;last_blocker?:string|null;last_candidate_count?:number;cycles:number;events:BotEvent[];autonomy?:PaperAutonomy;orders_enabled:boolean;testnet_orders_enabled:boolean }
type V20LifecycleEvent = { kind:string;price:number;quantity:number;net_pnl:number;created_at:string }
type PaperPosition = { id:number;symbol:string;direction:'LONG'|'SHORT';amount:number;quantity:number;original_amount?:number;original_quantity?:number;entry_price:number;current_price:number;stop_loss:number;take_profit:number;tp1?:number;tp2?:number;tp3?:number;partial_targets_hit?:string[];partial_realized_pnl?:number;lifecycle_events?:V20LifecycleEvent[];max_holding_minutes?:number;unrealized_pnl:number;status:string;source:'MANUAL'|'AUTO'|'DEMO';opened_at?:string;initial_stop_loss?:number;peak_price?:number;protection_status?:string;protection_level?:number;protection_updated_at?:string|null;signal_confidence?:number|null;guard_mode?:string|null;gate_status?:string|null;freshness_status?:string|null;entry_drift_atr?:number|null;regime_label?:string|null;regime_policy?:string|null;portfolio_mode?:string|null;portfolio_correlation?:number|null;adaptive_mode?:string|null;adaptive_confidence_floor?:number|null;stability_mode?:string|null;stability_samples?:number|null;liquidity_mode?:string|null;liquidity_score?:number|null;liquidity_spread_bps?:number|null;session_label?:string|null;session_mode?:string|null;session_confidence_bonus?:number|null;entry_order_type?:string;limit_order_id?:number|null;limit_price?:number|null;grid_levels?:number[];grid_lower?:number|null;grid_upper?:number|null;grid_count?:number }
type PaperLimitOrder = { id:number;symbol:string;direction:'LONG'|'SHORT';amount:number;limit_price:number;stop_loss:number;tp1:number;tp2:number;tp3:number;grid_count:number;grid_lower:number;grid_upper:number;grid_levels:number[];status:string;wait_reason?:string|null;created_at:string;expires_at:string;last_price:number;distance_pct:number;fill_price?:number;position_id?:number;paper_only:boolean;orders_enabled:boolean }
type PositionOverlay = { id:string;symbol:string;direction:'LONG'|'SHORT';label:string;entry:number;stop:number;tp1:number;tp2:number;tp3:number;gridLevels:number[] }
type PaperRisk = { status:string;auto_paused:boolean;daily_realized_pnl:number;daily_loss_limit:number;remaining_loss_budget:number;consecutive_losses:number;consecutive_loss_limit:number;cooldown_until:string|null;reason:string }
type PaperTrade = PaperPosition & { realized_pnl:number;fee:number;closed_at:string }
type PaperPerformance = { closed_count:number;wins:number;losses:number;win_rate:number;realized_pnl:number;average_pnl:number;profit_factor:number|null;best_trade:number;worst_trade:number;auto_trades:number;demo_trades:number;manual_trades:number }
type AppNotification = { kind:string;message:string;created_at:string }
type ShadowEvent = { created_at:string;symbol:string;direction:string;confidence:number;amount:number;regime:string;liquidity:string;session:string;confidence_floor:number;message:string }
type ShadowMode = { enabled:boolean;events:ShadowEvent[] }
type EmergencyBrake = { active:boolean;reason:string;source:string|null;triggered_at:string|null }
type PaperAccount = { balance:number;equity:number;available:number;used_margin:number;reserved_margin:number;unrealized_pnl:number;positions:PaperPosition[];pending_orders:PaperLimitOrder[];recent_limit_orders:PaperLimitOrder[];risk:PaperRisk;recent_trades:PaperTrade[];performance:PaperPerformance;shadow:ShadowMode;emergency_brake:EmergencyBrake;notifications:AppNotification[];grid_plans?:SavedGridPlan[];strategy_orchestrator?:V7Orchestrator }
type StrategyLab = { symbol:string;interval:string;sampled_candles:number;total_trades:number;wins:number;losses:number;timeouts:number;win_rate:number;avg_return_pct:number;net_return_pct:number;max_drawdown_pct:number;profit_factor:number|null;verdict:string;note:string;cost_assumption:string }
type SignalGate = { symbol:string;interval:string;direction:string;status:string;entry_allowed:boolean;reason:string;confidence:number;last_closed_at:number;next_close_at:number }
type SignalFreshness = { symbol:string;interval:string;direction:string;status:string;auto_allowed:boolean;reason:string;live_price:number;planned_entry:number;stop_loss:number;take_profit:number;drift_atr:number;drift_pct:number }
type MarketRegime = { symbol:string;interval:string;label:string;auto_allowed:boolean;preferred_direction:string;entry_policy:string;strength:number;atr_pct:number;range_atr:number;position_multiplier:number;reason:string }
type RegimeStability = { symbol:string;interval:string;regime_label:string;direction:string;mode:string;auto_allowed:boolean;stable_samples:number;required_samples:number;stability_score:number;reason:string }
type LiquidityShield = { symbol:string;mode:string;auto_allowed:boolean;liquidity_score:number;spread_bps:number;imbalance_pct:number;depth_usdt:number;best_bid:number|null;best_ask:number|null;reason:string }
type PortfolioGuard = { symbol:string;direction:string;open_positions:number;matched_symbol:string|null;correlation_pct:number;heat:number;mode:string;auto_allowed:boolean;reason:string }
type AdaptiveGate = { regime:string;direction:string;mode:string;auto_allowed:boolean;min_confidence:number;sample_size:number;wins:number;win_rate:number;net_pnl:number;average_pnl:number;reason:string }
type SessionItem = { key:string;label:string;trades:number;wins:number;win_rate:number;net_pnl:number;status:string;confidence_bonus:number }
type SessionIntelligence = { scope:string;sample_size:number;current_session:SessionItem;sessions:SessionItem[];weakest_session:SessionItem|null;confidence_bonus:number;auto_allowed:boolean;reason:string }
type WalkForward = { symbol:string;interval:string;positive_folds:number;total_folds:number;out_of_sample:{label:string;trades:number;win_rate:number;net_return_pct:number;max_drawdown_pct:number;verdict:string}|null;verdict:string;note:string;folds:{label:string;trades:number;win_rate:number;net_return_pct:number;max_drawdown_pct:number;verdict:string}[] }
type StressScenario = { label:string;description:string;net_return_pct:number;max_drawdown_pct:number;status:string }
type StressTest = { symbol:string;interval:string;baseline:{trades:number;net_return_pct:number;max_drawdown_pct:number;verdict:string};scenarios:StressScenario[];survived:number;total_scenarios:number;verdict:string;note:string;assumption:string }
type DecisionReview = { minutes:number;price:number;return_pct:number;status:string;observed_at:string }
type BlackboxEvent = { id:string;created_at:string;symbol:string;direction:string;confidence:number;entry_price:number;decision:string;source:string;reason:string;gates:Record<string,string|undefined>;latest_review:DecisionReview|null }
type DecisionBlackbox = { records:number;pending:number;opened:number;shadow:number;blocked:number;reviewed_rejections:number;shield_hits:number;shield_accuracy_pct:number;missed_opportunities:number;status:string;summary:string;method_note:string;events:BlackboxEvent[] }
type DecisionExplainCheck = { key:string;label:string;passed:boolean;detail:string }
type DecisionExplanation = { symbol:string;interval:string;direction:string;readiness_score:number;status:string;summary:string;next_action:string;checks:DecisionExplainCheck[];session:SessionItem|null;adaptive:AdaptiveGate|null;updated_at:string }
type DailyReport = { date:string;status:string;headline:string;today_pnl:number;closed_trades:number;open_positions:number;remaining_loss_budget:number;shadow_records:number;blackbox_records:number;blackbox_status:string;emergency_active:boolean;notifications:AppNotification[] }
type TestnetCheck = { label:string;status:string;passed:boolean }
type TestnetReadiness = { mode:string;status:string;credentials_configured:boolean;orders_enabled:boolean;reason:string;checks:TestnetCheck[] }
type V20GhostRow = { created_at:string;symbol:string;direction:string;decision:string;reason:string;review_minutes:number;counterfactual_return_pct:number;outcome:string }
type V20Ghost = { version:string;status:string;taken_trades:number;taken_pnl_usdt:number;blocked_reviewed:number;shield_saves:number;missed_opportunities:number;shield_save_rate_pct:number;counterfactual_edge_pct:number;rows:V20GhostRow[];orders_enabled:boolean;method_note:string }
type V20Gate = { key:string;label:string;passed:boolean;value:string }
type V20Certificate = { version:string;status:string;score:number;passed_gates:number;total_gates:number;gates:V20Gate[];paper_ready:boolean;testnet_candidate:boolean;testnet_ready:boolean;live_ready:boolean;orders_enabled:boolean;reason:string;generated_at:string }
type V20Module = { version:string;name:string;active:boolean;status:string }
type V20Policy = { profile:string;label:string;mode:string;minimum_confidence:number;maximum_trap_score:number;confidence_floor:number;cycle_seconds:number;amount_cap:number;universe_size:number;shortlist_size:number;risk_per_trade_pct:number;max_allocation_pct:number;max_total_exposure_pct:number;minimum_projected_net_usdt:number;orders_enabled:boolean }
type V20Command = { version:string;edition:string;bot:PaperBotState & {policy:V20Policy};ghost_twin:V20Ghost;certificate:V20Certificate;testnet:TestnetReadiness;modules:V20Module[];infrastructure:Record<string,string|null>;orders_enabled:boolean;safety_note:string }
type SystemHealth = { status:string;mode:string;api:string;database:string;redis:string;self_healing:string;paper_storage:string;paper_bot:string;grid_engine?:string;strategy_orchestrator?:string;future_lab?:string;market_twin?:string;strategy_evolution?:string;portfolio_risk?:string;testnet:string;last_checked:string|null;message:string }
type ArchiveEntry = { id:number;created_at:string;symbol:string;direction:string;confidence:number;entry_price:number|null;stop_loss:number|null;tp1:number|null;explanation:string }
type DecisionArchive = { available:boolean;entries:ArchiveEntry[];message:string }
type GridFeeAssumption = { single_side_pct:number;round_trip_pct:number;step_to_fee_multiple:number;label:string }
type GridCycleEstimate = { gross_usdt:number;fee_usdt:number;net_usdt:number;net_edge_pct:number }
type GridPlan = { symbol:string;interval:string;mode:string;direction:string;entry_reference:number;lower:number;upper:number;grid_count:number;grid_step:number;grid_step_pct:number;range_width_pct:number;levels:number[];support:number;resistance:number;safety_floor:number;safety_ceiling:number;atr:number;atr_pct:number;capital:number;capital_per_grid:number;max_planned_exposure:number;fee_assumption:GridFeeAssumption;estimated_per_cycle:GridCycleEstimate;safety_score:number;viability:string;paper_eligible:boolean;orders_enabled:boolean;regime:string;liquidity_mode:string;liquidity_score:number;spread_bps:number;reason:string;note:string;generated_at:string;cached?:boolean }
type GridSimulation = { symbol:string;interval:string;plan_mode:string;plan_viability:string;sampled_candles:number;fills:number;completed_cycles:number;gross_profit_usdt:number;fees_usdt:number;net_realized_usdt:number;unrealized_usdt:number;marked_result_usdt:number;net_return_pct:number;max_drawdown_pct:number;max_inventory_grids:number;open_grids:number;verdict:string;note:string;cached?:boolean }
type SavedGridPlan = GridPlan & { id:string;saved_at:string;active:boolean;status:string }
type SavedGridPlans = { plans:SavedGridPlan[];active_count:number;orders_enabled:boolean;message:string }
type LiveGridFill = { id:string;profile:string;kind:string;price:number;fee_usdt:number;slippage_usdt:number;cycle_pnl_usdt:number|null;created_at:string;paper_only:boolean }
type GridEngineEvent = { kind:string;profile:string;message:string;price:number|null;created_at:string }
type LiveTwinProfile = { profile:string;profile_label:string;score:number;status:string;grid_count:number;grid_step_pct:number;fills:number;completed_cycles:number;marked_result_usdt:number;net_return_pct:number;max_drawdown_pct:number;open_grids:number;inventory_limit:number;fees_usdt:number;slippage_usdt:number;evidence_ready:boolean }
type GridRuntime = { profile:string;profile_label:string;symbol:string;interval:string;mode:string;capital:number;lower:number;upper:number;last_price:number;grid_count:number;grid_step_pct:number;fill_count:number;completed_cycles:number;fees_usdt:number;slippage_usdt:number;net_realized_usdt:number;unrealized_usdt:number;marked_result_usdt:number;net_return_pct:number;max_drawdown_pct:number;open_grids:number;inventory_limit:number;inventory_used_pct:number;max_inventory_grids:number;score:number;status:string;recenter_status:string;fills:LiveGridFill[];last_tick_at:string;orders_enabled:boolean }
type GridEngine = { enabled:boolean;status:string;symbol:string|null;interval:string;capital:number;active_profile:string;recommended_profile:string;recommendation_status:string;promotion_ready:boolean;active_runtime:GridRuntime|null;profiles:LiveTwinProfile[];events:GridEngineEvent[];last_tick_at:string|null;started_at:string|null;stopped_at:string|null;last_action:string;recenter_count:number;orders_enabled:boolean;safety_note:string }
type TwinLabProfile = LiveTwinProfile & { cost_adjusted_edge_pct:number;verdict:string }
type TwinLab = { symbol:string;interval:string;winner:string;winner_score:number;score_gap:number;promotion_ready:boolean;status:string;reason:string;profiles:TwinLabProfile[];orders_enabled:boolean;note:string;cached?:boolean }
type OrderbookWall = { price:number;notional_usdt:number;strength:number;persistence:number }
type HeatLevel = { side:'ALIŞ'|'SATIŞ';price:number;notional_usdt:number;distance_pct:number;heat:number }
type OrderbookIntelligence = { symbol:string;mode:string;mid_price:number;spread_bps:number;pressure_pct:number;dominant_side:string;spoof_risk_score:number;history_samples:number;required_persistence_samples:number;bid_wall:OrderbookWall|null;ask_wall:OrderbookWall|null;heatmap:HeatLevel[];reason:string;orders_enabled:boolean;updated_at:string;cached?:boolean }
type V7StressCase = { label:string;net_return_pct:number;max_drawdown_pct:number;trades:number;status:string }
type V7ReplayProfile = { strategy:string;trades:number;wins:number;losses:number;win_rate:number;gross_result_usdt:number;costs_usdt:number;net_result_usdt:number;net_return_pct:number;max_drawdown_pct:number;profit_factor:number|null;score:number;status:string;evidence_ready:boolean;stress_survived:number;stress_total:number;certified:boolean;certification:string;ranking_score:number;stress_cases:V7StressCase[];orders_enabled:boolean }
type V7Replay = { symbol:string;interval:string;horizon:'24h'|'7d';sampled_candles:number;winner:string;leading_strategy:string;score_gap:number;promotion_ready:boolean;status:string;profiles:V7ReplayProfile[];orders_enabled:boolean;note:string;cached?:boolean }
type V7SymbolProfile = Pick<V7ReplayProfile,'strategy'|'trades'|'win_rate'|'net_result_usdt'|'net_return_pct'|'max_drawdown_pct'|'profit_factor'|'score'|'status'|'certified'|'certification'|'stress_survived'|'stress_total'|'ranking_score'>
type V7SymbolDecision = { symbol:string;strategy:string;direction:string;regime:string;confidence:number;risk_score:number;allocation_ready:boolean;price:number;adx:number;atr_pct:number;trap_score:number;volume_ratio:number;reason:string;replay_profiles:V7SymbolProfile[];orders_enabled:boolean;updated_at:string }
type V7Council = { strategy:string;status:string;score:number;trades:number;certified_symbols:number;average_return_pct:number;max_drawdown_pct:number;quarantined:boolean;orders_enabled:boolean }
type V7Allocation = { symbol:string;strategy:string;direction:string;allocation_pct:number;allocated_usdt:number;status:string;correlation_with:string|null;correlation_pct:number;reason:string }
type V7Event = { kind:string;symbol:string;strategy:string;message:string;created_at:string;paper_only:boolean }
type V7Orchestrator = { enabled:boolean;status:string;interval:string;capital:number;universe:string[];symbols:V7SymbolDecision[];allocations:V7Allocation[];allocation_summary:{allocated_usdt:number;idle_usdt:number;heat_pct:number;max_parallel_allocations?:number};strategies:V7Council[];quarantined_strategies:string[];certification_status:string;cycles:number;events:V7Event[];last_tick_at:string|null;started_at:string|null;stopped_at:string|null;last_action:string;orders_enabled:boolean;safety_note:string }
type V7WeeklyReport = { period:string;paper_trades:number;wins:number;win_rate:number;paper_pnl_usdt:number;orchestrator_cycles:number;certification_status:string;allocated_usdt:number;idle_usdt:number;quarantined_strategies:string[];headline:string;orders_enabled:boolean;note:string }
type V8CorridorPoint = { time:number;lower:number;base:number;upper:number }
type V8Forecast = { interval:string;horizon_candles:number;entry_price:number;probabilities:{YÜKSELİŞ:number;YATAY:number;DÜŞÜŞ:number};dominant_scenario:string;dominant_probability:number;confidence_status:string;terminal_mean_pct:number;uncertainty_pct:number;cost_hurdle_pct:number;points:V8CorridorPoint[];targets:{bear_case:number;base_case:number;bull_case:number};orders_enabled:boolean;note:string }
type V8CalibrationRecord = { forecast_at:number;predicted:string;outcome:string;confidence:number;realized_move_pct:number;hit:boolean }
type V8Calibration = { samples:number;hits:number;accuracy_pct:number;brier_score:number;average_confidence_pct:number;overconfidence_gap_pct:number;reliability_score:number;status:string;quarantined:boolean;records:V8CalibrationRecord[];orders_enabled:boolean;method_note:string }
type V8Chaos = { level:string;chaos_score:number;liquidity_shock_score:number;regime_shift_score:number;flash_move_score:number;spread_bps:number;spoof_risk_score:number;volatility_ratio:number;last_move_pct:number;volume_ratio:number;reasons:string[];veto_required:boolean;orders_enabled:boolean }
type V8ExecutionScenario = { action:'LONG'|'SHORT'|'BEKLE';probability:number;expected_net_pct:number;worst_case_pct:number }
type V8ExecutionTwin = { notional_usdt:number;visible_depth_usdt:number;partial_fill_pct:number;estimated_impact_bps:number;single_side_cost_pct:number;round_trip_cost_pct:number;latency_ms_assumption:number;scenarios:V8ExecutionScenario[];best_action:string;status:string;orders_enabled:boolean;note:string }
type V8ChaosScenario = { label:string;btc_move_pct:number;projected_symbol_move_pct:number;portfolio_impact_pct:number;status:string }
type V8PortfolioChaos = { symbol:string;btc_correlation_pct:number;btc_beta:number;exposure_pct:number;exposure_source:string;worst_case_pct:number;safe_allocation_pct:number;level:string;scenarios:V8ChaosScenario[];veto_required:boolean;orders_enabled:boolean;note:string }
type V8VetoGate = { key:string;label:string;passed:boolean;critical:boolean;detail:string }
type V8VetoCouncil = { candidate_action:string;final_action:string;paper_scenario_allowed:boolean;confidence:number;status:string;veto_count:number;vetoes:string[];gates:V8VetoGate[];reason:string;orders_enabled:boolean;safety_note:string }
type V8FutureLab = { symbol:string;interval:string;horizon:number;forecast:V8Forecast;calibration:V8Calibration;chaos:V8Chaos;execution_twin:V8ExecutionTwin;portfolio_chaos:V8PortfolioChaos;veto_council:V8VetoCouncil;orderbook_mode:string;generated_at:string;orders_enabled:boolean;note:string;cached?:boolean }
type V9Symbol = { symbol:string;price:number|null;bid:number|null;ask:number|null;spread_bps:number|null;quote_volume_24h:number|null;age_seconds:number|null;health:'CANLI'|'BAYAT'|'BEKLİYOR' }
type V9Drift = { status:string;drift_score:number;samples:number;recent_win_rate:number;baseline_win_rate:number;recent_return_pct:number;baseline_return_pct:number;worst_strategy:string|null;rollback_required:boolean;reason:string;orders_enabled:boolean }
type V9PnlItem = { source:string;trades:number;realized_pnl:number;unrealized_pnl:number;fees_usdt:number;net_pnl:number }
type V9Attribution = { items:V9PnlItem[];total_realized_pnl:number;total_unrealized_pnl:number;total_fees_usdt:number;net_pnl:number;orders_enabled:boolean;note:string }
type V9Event = { kind:string;message:string;symbol:string|null;created_at:string;paper_only:boolean }
type V9Fill = { id:string;created_at:string;symbol:string;side:'BUY'|'SELL';strategy:string;requested_notional:number;filled_notional:number;fill_pct:number;execution_price:number;quantity:number;fee_usdt:number;impact_bps:number;latency_ms:number;status:string;paper_only:boolean;orders_enabled:boolean }
type V9DailyReport = { date:string;status:string;ticks_captured:number;data_quality_pct:number;gap_count:number;recovered_candles:number;reconnect_count:number;database:string;drift_status:string;paper_net_pnl:number;paper_fills:number;headline:string;orders_enabled:boolean;safety_note:string }
type V9Rollback = { active:boolean;status:string;safe_profile:string;last_action:string;triggered_at:string|null }
type V9Twin = { version:string;enabled:boolean;status:string;stream_health:string;universe:string[];symbols:V9Symbol[];coverage_pct:number;ticks_captured:number;cycles:number;gap_count:number;recovered_candles:number;reconnect_count:number;error_count:number;started_at:string|null;stopped_at:string|null;last_tick_at:string|null;last_action:string;events:V9Event[];paper_fills:V9Fill[];drift:V9Drift;pnl_attribution:V9Attribution;rollback:V9Rollback;daily_report:V9DailyReport;database:string;orders_enabled:boolean;testnet_orders_enabled:boolean;safety_note:string }
type V10Genome = { id:string;generation:number;family:'GRID'|'TREND'|'KIRILIM';label:string;params:Record<string,number>;parent_id:string|null;orders_enabled:boolean }
type V10Fold = { label:string;trades:number;wins:number;losses:number;win_rate:number;net_result_usdt:number;net_return_pct:number;max_drawdown_pct:number;profit_factor:number|null;costs_usdt:number;orders_enabled:boolean }
type V10Candidate = { rank:number;genome:V10Genome;id:string;family:string;label:string;score:number;status:string;certified:boolean;trades:number;win_rate:number;net_return_pct:number;train_return_pct:number;test_return_pct:number;stress_return_pct:number;max_drawdown_pct:number;positive_folds:number;overfit_risk:number;folds:V10Fold[];explanation:string;orders_enabled:boolean;symbol?:string;regime?:string;promoted_at?:string;paper_policy?:string }
type V10Gate = { key:string;label:string;passed:boolean;detail:string }
type V10RegimeChampion = { id:string;label:string;score:number;status:string;certified:boolean;orders_enabled:boolean }|null
type V10Tournament = { symbol?:string;interval?:string;generation:number;sampled_candles:number;genome_count:number;regime:{label:string;preferred_family:string;strength:number;reason:string};leader:V10Candidate|null;champion:V10Candidate|null;challenger:V10Candidate|null;leaderboard:V10Candidate[];regime_champions:Record<'GRID'|'TREND'|'KIRILIM',V10RegimeChampion>;promotion_gates:V10Gate[];promotion_ready:boolean;promotion_status:string;next_generation:V10Genome[];explanation:string;generated_at:string;orders_enabled:boolean;testnet_orders_enabled:boolean;safety_note:string;cached?:boolean }
type V10Event = { kind:string;message:string;symbol:string|null;genome_id:string|null;created_at:string;paper_only:boolean;orders_enabled:boolean }
type V10Evolution = { version:string;enabled:boolean;busy:boolean;status:string;interval:string;capital:number;universe:string[];generation:number;cycles:number;active_champion:V10Candidate|null;previous_champion:V10Candidate|null;champions:Record<string,V10Candidate>;leaderboard:V10Candidate[];latest_tournament:V10Tournament|null;events:V10Event[];started_at:string|null;stopped_at:string|null;last_tick_at:string|null;last_action:string;promotion_gate:string;orders_enabled:boolean;testnet_orders_enabled:boolean;mode:string;safety_note:string }
type V11Allocation = { symbol:string;weight_pct:number;paper_budget_usdt:number;volatility_pct:number;average_correlation_pct:number;risk_contribution_pct:number;cluster:string;status:string;orders_enabled:boolean }
type V11MatrixPair = { left:string;right:string;correlation_pct:number;level:string }
type V11Matrix = { symbols:string[];rows:{symbol:string;values:number[]}[];pairs:V11MatrixPair[];average_abs_correlation_pct:number;max_pair:V11MatrixPair|null;orders_enabled:boolean }
type V11Cluster = { id:string;members:string[];size:number;average_correlation_pct:number;status:string;orders_enabled:boolean }
type V11MonteCarlo = { simulations:number;horizon_candles:number;capital:number;expected_return_pct:number;expected_pnl_usdt:number;var_95_pct:number;var_95_usdt:number;cvar_95_pct:number;cvar_95_usdt:number;probability_loss_pct:number;probability_drawdown_5_pct:number;ruin_probability_pct:number;worst_path_pct:number;best_path_pct:number;quantiles:Record<string,number>;distribution:{label:string;count:number;percentage:number}[];orders_enabled:boolean;method_note:string }
type V11Stress = { label:string;portfolio_impact_pct:number;loss_usdt:number;status:string;description:string;orders_enabled:boolean }
type V11Gate = { key:string;label:string;passed:boolean;detail:string }
type V11Fingerprint = { key:string;label:string;score:number }
type V11Report = { version:string;interval:string;capital:number;symbols:string[];sampled_returns:number;risk_score:number;risk_level:'YEŞİL'|'SARI'|'KIRMIZI';paper_action:string;veto_required:boolean;exposure_ratio_pct:number;invested_budget_usdt:number;cash_reserve_usdt:number;diversification_score:number;concentration_pct:number;allocations:V11Allocation[];correlation_matrix:V11Matrix;clusters:V11Cluster[];monte_carlo:V11MonteCarlo;stress_scenarios:V11Stress[];worst_scenario:V11Stress;risk_fingerprint:V11Fingerprint[];gates:V11Gate[];summary:string;generated_at:string;orders_enabled:boolean;testnet_orders_enabled:boolean;safety_note:string;cached?:boolean }
type V11Event = { kind:string;message:string;created_at:string;paper_only:boolean;orders_enabled:boolean }
type V11Intervention = { active:boolean;status:string;reason:string;triggered_at:string|null;paper_orchestrator_stopped:boolean;stopped_engines?:string[] }
type V11Risk = { version:string;enabled:boolean;busy:boolean;status:string;interval:string;capital:number;universe:string[];simulations:number;horizon_candles:number;cycles:number;latest_report:V11Report|null;approved_allocations:V11Allocation[];intervention:V11Intervention;events:V11Event[];started_at:string|null;stopped_at:string|null;last_tick_at:string|null;last_action:string;orders_enabled:boolean;testnet_orders_enabled:boolean;mode:string;safety_note:string }
type WorkspaceTab = 'dashboard'|'risk'|'strategy'|'live'|'automation'|'records'
type WorkspaceView = 'dashboard'|'v22-commercial'|'v20-demo'|'v20-limit'|'v20-autopilot'|'v20-ghost'|'v20-certification'|'risk-command'|'strategy-evolution'|'strategy-future'|'strategy-lab'|'live-health'|'live-twin'|'automation-orchestra'|'automation-grid'|'automation-plan'|'records-command'|'records-journal'|'records-archive'

const objectValue = (value:unknown):Record<string,unknown>|null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string,unknown> : null
const numberValue = (value:unknown, fallback=0):number => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}
const apiErrorMessage = (payload:unknown, fallback:string):string => {
  const row = objectValue(payload)
  const detail = row?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  const detailRow = objectValue(detail)
  if (detailRow) {
    const message = detailRow.message ?? detailRow.msg ?? detailRow.reason
    if (typeof message === 'string' && message.trim()) return message
  }
  if (Array.isArray(detail)) {
    const messages = detail.map(item => {
      const entry = objectValue(item)
      return typeof entry?.msg === 'string' ? entry.msg : typeof item === 'string' ? item : ''
    }).filter(Boolean)
    if (messages.length) return messages.join(' · ')
  }
  return fallback
}

/**
 * Render veya ağ geçici bir hata nesnesi döndürdüğünde bunu Paper hesabı gibi
 * ekrana basmayız. Eski kayıtların eksik alanlarını da güvenli varsayılanlarla
 * tamamlarız; böylece tek bozuk yanıt bütün kokpiti beyaz ekrana düşüremez.
 */
const normalizePaperAccount = (value:unknown):PaperAccount|null => {
  const row = objectValue(value)
  if (!row || !Array.isArray(row.positions)) return null

  const normalizePosition = (item:unknown):PaperPosition|null => {
    const position = objectValue(item)
    if (!position) return null
    const symbol = String(position.symbol ?? '').toUpperCase().replace(/[^A-Z0-9]/g, '')
    const direction = String(position.direction ?? '').toUpperCase()
    const entry = numberValue(position.entry_price)
    if (!symbol.endsWith('USDT') || !['LONG','SHORT'].includes(direction) || entry <= 0) return null
    const amount = Math.max(0, numberValue(position.amount, numberValue(position.original_amount)))
    const quantity = Math.max(0, numberValue(position.quantity, amount / entry))
    const stop = numberValue(position.stop_loss, entry)
    const target = numberValue(position.take_profit, entry)
    return {
      ...(position as unknown as PaperPosition),
      id:numberValue(position.id), symbol, direction:direction as 'LONG'|'SHORT', amount, quantity,
      entry_price:entry, current_price:numberValue(position.current_price, entry), stop_loss:stop,
      take_profit:target, tp1:numberValue(position.tp1, target), tp2:numberValue(position.tp2, target),
      tp3:numberValue(position.tp3, target), unrealized_pnl:numberValue(position.unrealized_pnl),
      status:String(position.status ?? 'AÇIK'), source:['MANUAL','AUTO','DEMO'].includes(String(position.source)) ? position.source as PaperPosition['source'] : 'DEMO',
      partial_targets_hit:Array.isArray(position.partial_targets_hit) ? position.partial_targets_hit.map(String) : [],
      lifecycle_events:Array.isArray(position.lifecycle_events) ? position.lifecycle_events as V20LifecycleEvent[] : [],
      grid_levels:Array.isArray(position.grid_levels) ? position.grid_levels.map(level => numberValue(level)).filter(level => level > 0) : [],
    }
  }

  const positions = row.positions.map(normalizePosition).filter((item):item is PaperPosition => item !== null)
  const pendingOrders = Array.isArray(row.pending_orders) ? row.pending_orders.filter(item => objectValue(item)) as PaperLimitOrder[] : []
  const recentLimitOrders = Array.isArray(row.recent_limit_orders) ? row.recent_limit_orders.filter(item => objectValue(item)) as PaperLimitOrder[] : []
  const recentTrades = Array.isArray(row.recent_trades)
    ? row.recent_trades.map(item => {
        const base = normalizePosition(item)
        const trade = objectValue(item)
        return base && trade ? {...base,realized_pnl:numberValue(trade.realized_pnl),fee:numberValue(trade.fee),closed_at:String(trade.closed_at ?? '')} as PaperTrade : null
      }).filter((item):item is PaperTrade => item !== null)
    : []
  const performance = objectValue(row.performance) ?? {}
  const risk = objectValue(row.risk) ?? {}
  const shadow = objectValue(row.shadow) ?? {}
  const brake = objectValue(row.emergency_brake) ?? {}

  return {
    ...(row as unknown as PaperAccount),
    balance:numberValue(row.balance), equity:numberValue(row.equity), available:numberValue(row.available),
    used_margin:numberValue(row.used_margin), reserved_margin:numberValue(row.reserved_margin),
    unrealized_pnl:numberValue(row.unrealized_pnl), positions, pending_orders:pendingOrders,
    recent_limit_orders:recentLimitOrders, recent_trades:recentTrades,
    performance:{
      closed_count:numberValue(performance.closed_count), wins:numberValue(performance.wins), losses:numberValue(performance.losses),
      win_rate:numberValue(performance.win_rate), realized_pnl:numberValue(performance.realized_pnl), average_pnl:numberValue(performance.average_pnl),
      profit_factor:performance.profit_factor === null || performance.profit_factor === undefined ? null : numberValue(performance.profit_factor),
      best_trade:numberValue(performance.best_trade), worst_trade:numberValue(performance.worst_trade), auto_trades:numberValue(performance.auto_trades),
      demo_trades:numberValue(performance.demo_trades), manual_trades:numberValue(performance.manual_trades),
    },
    risk:{
      status:String(risk.status ?? 'VERİ BEKLENİYOR'), auto_paused:Boolean(risk.auto_paused), daily_realized_pnl:numberValue(risk.daily_realized_pnl),
      daily_loss_limit:numberValue(risk.daily_loss_limit), remaining_loss_budget:numberValue(risk.remaining_loss_budget),
      consecutive_losses:numberValue(risk.consecutive_losses), consecutive_loss_limit:numberValue(risk.consecutive_loss_limit),
      cooldown_until:risk.cooldown_until ? String(risk.cooldown_until) : null, reason:String(risk.reason ?? 'Risk verisi bekleniyor.'),
    },
    shadow:{enabled:Boolean(shadow.enabled),events:Array.isArray(shadow.events) ? shadow.events as ShadowEvent[] : []},
    emergency_brake:{active:Boolean(brake.active),reason:String(brake.reason ?? ''),source:brake.source ? String(brake.source) : null,triggered_at:brake.triggered_at ? String(brake.triggered_at) : null},
    notifications:Array.isArray(row.notifications) ? row.notifications.filter(item => objectValue(item)) as AppNotification[] : [],
  }
}

const API = API_BASE
const fmt = (value?:number) => value === undefined ? '—' : value.toLocaleString('tr-TR', { maximumFractionDigits:value < 10 ? 5 : 2 })
const stamp = (value?:string) => value ? new Date(value).toLocaleString('tr-TR', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—'

function Chart({ symbol, interval, horizon, notional, onAnalysis, onFutureLab, onLivePrice, onStream, overlay, showAnalysisLevels=true }:{ symbol:string; interval:string; horizon:12|24; notional:number; onAnalysis:(result:Analysis|null)=>void; onFutureLab:(result:V8FutureLab|null)=>void; onLivePrice:(price:number|undefined)=>void; onStream:(connected:boolean)=>void; overlay?:PositionOverlay|null; showAnalysisLevels?:boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  const overlayKey = overlay ? [overlay.id,overlay.symbol,overlay.direction,overlay.entry,overlay.stop,overlay.tp1,overlay.tp2,overlay.tp3,...overlay.gridLevels].join('|') : 'none'
  useEffect(() => {
    if (!ref.current) return
    onAnalysis(null)
    onFutureLab(null)
    onLivePrice(undefined)
    const chart = createChart(ref.current, { autoSize:true, layout:{ background:{type:ColorType.Solid,color:'#fff'}, textColor:'#67705d' }, grid:{vertLines:{color:'#eff1e5'},horzLines:{color:'#eff1e5'}}, rightPriceScale:{borderColor:'#dde2cf'}, timeScale:{borderColor:'#dde2cf',timeVisible:true} })
    const candleSeries = chart.addSeries(CandlestickSeries, { upColor:'#159653', downColor:'#e6543f', wickUpColor:'#159653', wickDownColor:'#e6543f', borderVisible:false })
    const volumeSeries = chart.addSeries(HistogramSeries, { priceFormat:{type:'volume'}, priceScaleId:'' })
    volumeSeries.priceScale().applyOptions({scaleMargins:{top:.82,bottom:0}})
    const corridorUpper = chart.addSeries(LineSeries, {color:'#ddbd20',lineWidth:1,lineStyle:2,priceLineVisible:false,lastValueVisible:true,title:'V8 ÜST'})
    const corridorBase = chart.addSeries(LineSeries, {color:'#2ca65a',lineWidth:2,lineStyle:0,priceLineVisible:false,lastValueVisible:true,title:'V8 OLASI'})
    const corridorLower = chart.addSeries(LineSeries, {color:'#e0873c',lineWidth:1,lineStyle:2,priceLineVisible:false,lastValueVisible:true,title:'V8 ALT'})
    const applyFuture = (future:V8FutureLab) => {
      corridorUpper.setData(future.forecast.points.map(point => ({time:point.time as never,value:point.upper})))
      corridorBase.setData(future.forecast.points.map(point => ({time:point.time as never,value:point.base})))
      corridorLower.setData(future.forecast.points.map(point => ({time:point.time as never,value:point.lower})))
      onFutureLab(future)
    }
    const refreshAnalysis = () => fetch(`${API}/analysis/${symbol}?interval=${interval}`).then(response => response.json()).then((result:Analysis) => onAnalysis(result)).catch(() => onAnalysis(null))
    const refreshFuture = () => fetch(`${API}/v8/future-lab/${symbol}?interval=${interval}&horizon=${horizon}&notional=${notional}`).then(response => { if (!response.ok) throw new Error('V8 geçici olarak bekliyor'); return response.json() }).then((result:V8FutureLab) => applyFuture(result)).catch(() => onFutureLab(null))
    Promise.all([
      fetch(`${API}/klines/${symbol}?interval=${interval}&limit=500`).then(response => response.json()),
      fetch(`${API}/analysis/${symbol}?interval=${interval}`).then(response => response.json()),
      fetch(`${API}/v8/future-lab/${symbol}?interval=${interval}&horizon=${horizon}&notional=${notional}`).then(response => response.ok ? response.json() : null),
    ]).then(([rows, analysis, future]:[Candle[],Analysis,V8FutureLab|null]) => {
      candleSeries.setData(rows.map(row => ({ time:row.time as never, open:row.open, high:row.high, low:row.low, close:row.close })))
      volumeSeries.setData(rows.map(row => ({ time:row.time as never, value:row.volume, color:row.close >= row.open ? '#85d8a6' : '#f1a398' })))
      const addLine = (data:Point[], color:string) => chart.addSeries(LineSeries, {color,lineWidth:2,priceLineVisible:false,lastValueVisible:false}).setData(data.map(point => ({time:point.time as never,value:point.value})))
      addLine(analysis.series.ema20, '#20a75a'); addLine(analysis.series.ema50, '#f0a51c'); addLine(analysis.series.ema200, '#6f91ad')
      const level = (price:number,color:string,title:string,style=2) => candleSeries.createPriceLine({price,color,lineWidth:2,lineStyle:style,axisLabelVisible:true,title})
      if (showAnalysisLevels) {
        level(analysis.entry, '#169653', `${analysis.direction} GİRİŞ`, 0); level(analysis.stop_loss, '#e64e42', 'STOP LOSS')
        level(analysis.tp1, '#42a94f', 'TP1'); level(analysis.tp2, '#42a94f', 'TP2'); level(analysis.tp3, '#42a94f', 'TP3')
        level(analysis.support, '#ea6a5e', 'DESTEK', 3); level(analysis.resistance, '#3da769', 'DİRENÇ', 3)
      }
      if (overlay && overlay.symbol === symbol) {
        candleSeries.createPriceLine({price:overlay.entry,color:'#0b8f52',lineWidth:3,lineStyle:0,axisLabelVisible:true,title:`${overlay.direction} GİRİŞ`})
        candleSeries.createPriceLine({price:overlay.stop,color:'#e64e42',lineWidth:3,lineStyle:0,axisLabelVisible:true,title:'STOP'})
        ;[[overlay.tp1,'TP1'],[overlay.tp2,'TP2'],[overlay.tp3,'TP3']].forEach(([price,title]) => candleSeries.createPriceLine({price:price as number,color:'#23a95e',lineWidth:2,lineStyle:2,axisLabelVisible:true,title:title as string}))
        overlay.gridLevels.forEach((price,index) => candleSeries.createPriceLine({price,color:'rgba(216,171,20,.58)',lineWidth:1,lineStyle:2,axisLabelVisible:false,title:`G${index + 1}`}))
      }
      if (future) applyFuture(future); else onFutureLab(null)
      chart.timeScale().fitContent(); onAnalysis(analysis)
    }).catch(() => { onAnalysis(null); onFutureLab(null) })
    let socket: WebSocket | undefined
    let reconnectTimer: number | undefined
    let active = true
    const connectStream = () => {
      if (!active) return
      socket = new WebSocket(`wss://stream.binance.com:9443/ws/${symbol.toLowerCase()}@kline_${interval}`)
      socket.onopen = () => { if (active) onStream(true) }
      socket.onclose = () => {
        if (!active) return
        onStream(false)
        reconnectTimer = window.setTimeout(connectStream, 3000)
      }
      socket.onerror = () => socket?.close()
      socket.onmessage = event => {
        const payload = JSON.parse(event.data)
        const candle = payload.k
        if (!candle || !active) return
        const close = Number(candle.c)
        candleSeries.update({time:Math.floor(Number(candle.t) / 1000) as never,open:Number(candle.o),high:Number(candle.h),low:Number(candle.l),close})
        volumeSeries.update({time:Math.floor(Number(candle.t) / 1000) as never,value:Number(candle.v),color:close >= Number(candle.o) ? '#85d8a6' : '#f1a398'})
        onLivePrice(close)
      }
    }
    connectStream()
    const refreshTimer = window.setInterval(refreshAnalysis, 15000)
    const futureTimer = window.setInterval(refreshFuture, 15000)
    return () => { active = false; window.clearInterval(refreshTimer); window.clearInterval(futureTimer); if (reconnectTimer) window.clearTimeout(reconnectTimer); socket?.close(); chart.remove() }
  }, [symbol, interval, horizon, notional, overlayKey, showAnalysisLevels])
  return <div className="chart" ref={ref}/>
}

export default function App() {
  const [markets, setMarkets] = useState<Market[]>([])
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [interval, setInterval] = useState('15m')
  const [status, setStatus] = useState('BAĞLANIYOR')
  const [analysis, setAnalysis] = useState<Analysis|null>(null)
  const [consensus, setConsensus] = useState<Consensus|null>(null)
  const [marketGuard, setMarketGuard] = useState<MarketGuard|null>(null)
  const [lab, setLab] = useState<StrategyLab|null>(null)
  const [walkForward, setWalkForward] = useState<WalkForward|null>(null)
  const [stressTest, setStressTest] = useState<StressTest|null>(null)
  const [labLoading, setLabLoading] = useState(false)
  const [signalGate, setSignalGate] = useState<SignalGate|null>(null)
  const [freshness, setFreshness] = useState<SignalFreshness|null>(null)
  const [liquidity, setLiquidity] = useState<LiquidityShield|null>(null)
  const [regime, setRegime] = useState<MarketRegime|null>(null)
  const [regimeStability, setRegimeStability] = useState<RegimeStability|null>(null)
  const [portfolioGuard, setPortfolioGuard] = useState<PortfolioGuard|null>(null)
  const [adaptiveGate, setAdaptiveGate] = useState<AdaptiveGate|null>(null)
  const [sessionIntelligence, setSessionIntelligence] = useState<SessionIntelligence|null>(null)
  const [dailyReport, setDailyReport] = useState<DailyReport|null>(null)
  const [testnet, setTestnet] = useState<TestnetReadiness|null>(null)
  const [blackbox, setBlackbox] = useState<DecisionBlackbox|null>(null)
  const [decisionExplanation, setDecisionExplanation] = useState<DecisionExplanation|null>(null)
  const [gridPlan, setGridPlan] = useState<GridPlan|null>(null)
  const [gridSimulation, setGridSimulation] = useState<GridSimulation|null>(null)
  const [savedGridPlans, setSavedGridPlans] = useState<SavedGridPlans|null>(null)
  const [gridCapital, setGridCapital] = useState(1000)
  const [gridBusy, setGridBusy] = useState(false)
  const [gridMessage, setGridMessage] = useState('V6 grid motoru hazırlanıyor…')
  const [gridEngine, setGridEngine] = useState<GridEngine|null>(null)
  const [twinLab, setTwinLab] = useState<TwinLab|null>(null)
  const [orderbook, setOrderbook] = useState<OrderbookIntelligence|null>(null)
  const [v6Busy, setV6Busy] = useState(false)
  const [v6Message, setV6Message] = useState('Canlı Paper Grid motoru kullanıcı onayı bekliyor.')
  const [notificationsEnabled, setNotificationsEnabled] = useState(false)
  const notificationRef = useRef('')
  const v7NotificationRef = useRef('')
  const [orchestrator, setOrchestrator] = useState<V7Orchestrator|null>(null)
  const [v7Replay, setV7Replay] = useState<V7Replay|null>(null)
  const [v7Weekly, setV7Weekly] = useState<V7WeeklyReport|null>(null)
  const [replayHorizon, setReplayHorizon] = useState<'24h'|'7d'>('7d')
  const [orchestraCapital, setOrchestraCapital] = useState(3000)
  const [v7Busy, setV7Busy] = useState(false)
  const [v7Message, setV7Message] = useState('V7 Strateji Orkestrası kullanıcı onayı bekliyor.')
  const [futureLab, setFutureLab] = useState<V8FutureLab|null>(null)
  const [v8Horizon, setV8Horizon] = useState<12|24>(12)
  const [v8Notional, setV8Notional] = useState(1000)
  const [v9Twin, setV9Twin] = useState<V9Twin|null>(null)
  const [v9Busy, setV9Busy] = useState(false)
  const [v9Message, setV9Message] = useState('V9 canlı kayıt için kullanıcı onayı bekliyor.')
  const [v10Evolution, setV10Evolution] = useState<V10Evolution|null>(null)
  const [v10Preview, setV10Preview] = useState<V10Tournament|null>(null)
  const [v10Busy, setV10Busy] = useState(false)
  const [v10Capital, setV10Capital] = useState(1000)
  const [v10Message, setV10Message] = useState('V10 Paper Evrim Laboratuvarı kullanıcı onayı bekliyor.')
  const [v11Risk, setV11Risk] = useState<V11Risk|null>(null)
  const [v11Preview, setV11Preview] = useState<V11Report|null>(null)
  const [v11Busy, setV11Busy] = useState(false)
  const [v11Capital, setV11Capital] = useState(5000)
  const [v11Message, setV11Message] = useState('V11 Otonom Risk Beyni kullanıcı onayı bekliyor.')
  const [systemHealth, setSystemHealth] = useState<SystemHealth|null>(null)
  const [archive, setArchive] = useState<DecisionArchive|null>(null)
  const [clock, setClock] = useState(Date.now())
  const [paper, setPaper] = useState<PaperAccount|null>(null)
  const [paperBusy, setPaperBusy] = useState(false)
  const [paperMessage, setPaperMessage] = useState('Sanal bakiye hazırlanıyor…')
  const [livePrice, setLivePrice] = useState<number|undefined>()
  const [streamLive, setStreamLive] = useState(false)
  const [paperBot, setPaperBot] = useState<PaperBotState|null>(null)
  const [v20Command, setV20Command] = useState<V20Command|null>(null)
  const [query, setQuery] = useState('')
  const [tab, setTab] = useState<'TÜMÜ'|'LONG'|'SHORT'|'KIRILIM'>('TÜMÜ')
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('dashboard')
  const [workspaceView, setWorkspaceView] = useState<WorkspaceView>('v20-demo')
  const [scanning, setScanning] = useState(false)
  const [scanMessage, setScanMessage] = useState('İlk tarama bekleniyor')
  const [limitDirection, setLimitDirection] = useState<'LONG'|'SHORT'>('LONG')
  const [limitAmount, setLimitAmount] = useState('50')
  const [limitPrice, setLimitPrice] = useState('')
  const [limitStop, setLimitStop] = useState('')
  const [limitTp1, setLimitTp1] = useState('')
  const [limitTp2, setLimitTp2] = useState('')
  const [limitTp3, setLimitTp3] = useState('')
  const [limitGridLower, setLimitGridLower] = useState('')
  const [limitGridUpper, setLimitGridUpper] = useState('')
  const [limitGridCount, setLimitGridCount] = useState(8)
  const [limitExpires, setLimitExpires] = useState(1440)
  const [limitBusy, setLimitBusy] = useState(false)
  const [limitMessage, setLimitMessage] = useState('Analiz planını doldur veya seviyeleri kendin yaz.')
  const [selectedPositionId, setSelectedPositionId] = useState<number|null>(null)
  const [selectedLimitId, setSelectedLimitId] = useState<number|null>(null)

  const acceptPaperAccount = (value:unknown, failureMessage='Paper hesabı geçici olarak okunamadı; son sağlam görünüm korunuyor.'):boolean => {
    const normalized = normalizePaperAccount(value)
    if (!normalized) {
      setPaperMessage(failureMessage)
      return false
    }
    setPaper(normalized)
    return true
  }

  const scan = async () => {
    setScanning(true); setScanMessage('Piyasa analiz ediliyor…')
    try {
      const response = await fetch(`${API}/scan?limit=18&interval=15m`)
      const payload = await response.json()
      setMarkets(payload.results); setStatus('CANLI')
      setScanMessage(payload.cached ? 'Son tarama önbellekten getirildi' : 'Canlı tarama tamamlandı')
    } catch { setScanMessage('Tarama bağlantısı kurulamadı') }
    finally { setScanning(false) }
  }

  const refreshV9 = async () => {
    try {
      const response = await fetch(`${API}/v9/twin`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V9 Dijital Borsa İkizi okunamadı')
      setV9Twin(payload)
      if (payload.last_action) setV9Message(payload.last_action)
    } catch { setV9Twin(null) }
  }

  const toggleV9Twin = async () => {
    setV9Busy(true)
    try {
      const endpoint = v9Twin?.enabled ? 'stop' : 'start'
      const options:RequestInit = endpoint === 'start'
        ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbols:Array.from(new Set([symbol,'BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT'])).slice(0,8)})}
        : {method:'POST'}
      const response = await fetch(`${API}/v9/twin/${endpoint}`, options)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V9 durumu değiştirilemedi')
      setV9Twin(payload)
      setV9Message(payload.last_action)
    } catch (error) { setV9Message(error instanceof Error ? error.message : 'V9 bağlantısı kurulamadı') }
    finally { setV9Busy(false) }
  }

  const simulateV9Order = async () => {
    setV9Busy(true)
    try {
      const response = await fetch(`${API}/v9/paper/order`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol,side:analysis?.direction === 'SHORT' ? 'SELL' : 'BUY',notional:100,strategy:'V8 VETO SONRASI'})})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Sanal dolum üretilemedi')
      setV9Message(`${payload.fill.symbol} · ${payload.fill.status} · %${payload.fill.fill_pct} · ${payload.fill.latency_ms} ms`)
      await refreshV9()
    } catch (error) { setV9Message(error instanceof Error ? error.message : 'V9 Paper dolumu bekliyor') }
    finally { setV9Busy(false) }
  }

  const refreshV10State = async () => {
    try {
      const response = await fetch(`${API}/v10/evolution`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V10 Evrim durumu okunamadı')
      setV10Evolution(payload)
      if (payload.last_action) setV10Message(payload.last_action)
    } catch { setV10Evolution(null) }
  }

  const refreshV10Preview = async () => {
    const evolutionInterval = ['5m','15m','1h'].includes(interval) ? interval : '15m'
    try {
      const response = await fetch(`${API}/v10/evolution/${symbol}?interval=${evolutionInterval}&capital=${v10Capital}`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V10 turnuvası çalışmadı')
      setV10Preview(payload)
    } catch { setV10Preview(null) }
  }

  const toggleV10Evolution = async () => {
    setV10Busy(true)
    try {
      const endpoint = v10Evolution?.enabled ? 'stop' : 'start'
      const evolutionInterval = ['5m','15m','1h'].includes(interval) ? interval : '15m'
      const universe = Array.from(new Set([symbol,'BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT'])).slice(0,6)
      const response = await fetch(`${API}/v10/evolution/${endpoint}`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:endpoint === 'start' ? JSON.stringify({symbols:universe,interval:evolutionInterval,capital:v10Capital}) : undefined,
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V10 durumu değiştirilemedi')
      setV10Evolution(payload); setV10Message(payload.last_action)
    } catch (error) { setV10Message(error instanceof Error ? error.message : 'V10 Evrim bağlantısı kurulamadı') }
    finally { setV10Busy(false) }
  }

  const rollbackV10Champion = async () => {
    setV10Busy(true)
    try {
      const response = await fetch(`${API}/v10/evolution/rollback`, {method:'POST'})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Paper geri dönüşü uygulanamadı')
      setV10Evolution(payload); setV10Message(payload.last_action)
    } catch (error) { setV10Message(error instanceof Error ? error.message : 'Önceki şampiyon henüz yok') }
    finally { setV10Busy(false) }
  }

  const v11Universe = () => Array.from(new Set([symbol,'BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT'])).slice(0,8)

  const refreshV11State = async () => {
    try {
      const response = await fetch(`${API}/v11/risk`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V11 Risk Beyni okunamadı')
      setV11Risk(payload)
      if (payload.last_action) setV11Message(payload.last_action)
    } catch { setV11Risk(null) }
  }

  const refreshV11Preview = async () => {
    const riskInterval = ['5m','15m','1h'].includes(interval) ? interval : '15m'
    const universe = v11Universe().join(',')
    try {
      const response = await fetch(`${API}/v11/risk-lab?symbols=${encodeURIComponent(universe)}&interval=${riskInterval}&capital=${v11Capital}&simulations=500&horizon_candles=24`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V11 portföy laboratuvarı çalışmadı')
      setV11Preview(payload)
    } catch { setV11Preview(null) }
  }

  const toggleV11Risk = async () => {
    setV11Busy(true)
    try {
      const endpoint = v11Risk?.enabled ? 'stop' : 'start'
      const riskInterval = ['5m','15m','1h'].includes(interval) ? interval : '15m'
      const response = await fetch(`${API}/v11/risk/${endpoint}`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:endpoint === 'start' ? JSON.stringify({symbols:v11Universe(),interval:riskInterval,capital:v11Capital,simulations:500,horizon_candles:24}) : undefined,
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V11 durumu değiştirilemedi')
      setV11Risk(payload); setV11Message(payload.last_action)
    } catch (error) { setV11Message(error instanceof Error ? error.message : 'V11 Risk Beyni bağlantısı kurulamadı') }
    finally { setV11Busy(false) }
  }

  const resetV11Intervention = async () => {
    setV11Busy(true)
    try {
      const response = await fetch(`${API}/v11/risk/reset`, {method:'POST'})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Risk vetosu sıfırlanamadı')
      setV11Risk(payload); setV11Message(payload.last_action)
    } catch (error) { setV11Message(error instanceof Error ? error.message : 'Kritik risk devam ediyor') }
    finally { setV11Busy(false) }
  }

  useEffect(() => { scan() }, [])
  useEffect(() => { refreshV9(); const timer = window.setInterval(refreshV9, 3000); return () => window.clearInterval(timer) }, [])
  useEffect(() => { refreshV10State(); const timer = window.setInterval(refreshV10State, 4000); return () => window.clearInterval(timer) }, [])
  useEffect(() => {
    let active = true
    const refresh = async () => { if (active) await refreshV10Preview() }
    refresh()
    const timer = window.setInterval(refresh, 60000)
    return () => { active = false; window.clearInterval(timer) }
  }, [symbol, interval, v10Capital])
  useEffect(() => { refreshV11State(); const timer = window.setInterval(refreshV11State, 4000); return () => window.clearInterval(timer) }, [])
  useEffect(() => {
    let active = true
    const refresh = async () => { if (active) await refreshV11Preview() }
    refresh()
    const timer = window.setInterval(refresh, 60000)
    return () => { active = false; window.clearInterval(timer) }
  }, [symbol, interval, v11Capital])
  useEffect(() => {
    setConsensus(null)
    fetch(`${API}/consensus/${symbol}`).then(response => response.json()).then(setConsensus).catch(() => setConsensus(null))
  }, [symbol])
  useEffect(() => {
    const refreshGuard = () => fetch(`${API}/guard/${symbol}`).then(response => response.json()).then(setMarketGuard).catch(() => setMarketGuard(null))
    refreshGuard()
    const timer = window.setInterval(refreshGuard, 15000)
    return () => window.clearInterval(timer)
  }, [symbol])
  useEffect(() => {
    const refreshGate = () => fetch(`${API}/gate/${symbol}?interval=${interval}`).then(response => response.json()).then(setSignalGate).catch(() => setSignalGate(null))
    refreshGate()
    const timer = window.setInterval(refreshGate, 15000)
    return () => window.clearInterval(timer)
  }, [symbol, interval])
  useEffect(() => {
    const refreshFreshness = () => fetch(`${API}/freshness/${symbol}?interval=${interval}`).then(response => response.json()).then(setFreshness).catch(() => setFreshness(null))
    refreshFreshness()
    const timer = window.setInterval(refreshFreshness, 5000)
    return () => window.clearInterval(timer)
  }, [symbol, interval])
  useEffect(() => {
    const refreshLiquidity = () => fetch(`${API}/liquidity/${symbol}`).then(response => response.json()).then(setLiquidity).catch(() => setLiquidity(null))
    refreshLiquidity()
    const timer = window.setInterval(refreshLiquidity, 5000)
    return () => window.clearInterval(timer)
  }, [symbol])
  useEffect(() => {
    const refreshRegime = () => fetch(`${API}/regime/${symbol}?interval=${interval}`).then(response => response.json()).then(setRegime).catch(() => setRegime(null))
    refreshRegime()
    const timer = window.setInterval(refreshRegime, 15000)
    return () => window.clearInterval(timer)
  }, [symbol, interval])
  useEffect(() => {
    const refreshStability = () => fetch(`${API}/regime/stability/${symbol}?interval=${interval}`)
      .then(response => { if (!response.ok) throw new Error('Rejim kararlılığı geçici olarak bekliyor'); return response.json() })
      .then(setRegimeStability).catch(() => setRegimeStability(null))
    refreshStability()
    const timer = window.setInterval(refreshStability, 10000)
    return () => window.clearInterval(timer)
  }, [symbol, interval])
  useEffect(() => {
    const direction = analysis?.direction ?? 'BEKLE'
    const refreshPortfolio = () => fetch(`${API}/portfolio/guard/${symbol}?direction=${direction}`).then(response => response.json()).then(setPortfolioGuard).catch(() => setPortfolioGuard(null))
    refreshPortfolio()
    const timer = window.setInterval(refreshPortfolio, 15000)
    return () => window.clearInterval(timer)
  }, [symbol, analysis?.direction])
  useEffect(() => {
    const direction = analysis?.direction ?? 'BEKLE'
    const activeRegime = regime?.label ?? 'GENEL'
    const refreshAdaptive = () => fetch(`${API}/adaptive/gate?regime=${encodeURIComponent(activeRegime)}&direction=${direction}`).then(response => response.json()).then(setAdaptiveGate).catch(() => setAdaptiveGate(null))
    refreshAdaptive()
    const timer = window.setInterval(refreshAdaptive, 10000)
    return () => window.clearInterval(timer)
  }, [regime?.label, analysis?.direction])
  useEffect(() => {
    const direction = analysis?.direction ?? 'BEKLE'
    const activeRegime = regime?.label ?? ''
    const refreshSession = () => fetch(`${API}/session/intelligence?symbol=${symbol}&regime=${encodeURIComponent(activeRegime)}&direction=${direction}`).then(response => response.json()).then(setSessionIntelligence).catch(() => setSessionIntelligence(null))
    refreshSession()
    const timer = window.setInterval(refreshSession, 15000)
    return () => window.clearInterval(timer)
  }, [symbol, regime?.label, analysis?.direction])
  useEffect(() => {
    const refreshDecisionExplanation = () => fetch(`${API}/decision/explain/${symbol}?interval=${interval}`).then(response => response.json()).then(setDecisionExplanation).catch(() => setDecisionExplanation(null))
    refreshDecisionExplanation()
    const timer = window.setInterval(refreshDecisionExplanation, 12000)
    return () => window.clearInterval(timer)
  }, [symbol, interval])
  useEffect(() => {
    let active = true
    const refreshGrid = async () => {
      try {
        const [planResponse, simulationResponse] = await Promise.all([
          fetch(`${API}/grid/plan/${symbol}?interval=${interval}&capital=${gridCapital}`),
          fetch(`${API}/grid/lab/${symbol}?interval=${interval}&capital=${gridCapital}`),
        ])
        const [planPayload, simulationPayload] = await Promise.all([planResponse.json(), simulationResponse.json()])
        if (!planResponse.ok) throw new Error(planPayload.detail || 'Grid planı hesaplanamadı')
        if (active) {
          setGridPlan(planPayload)
          setGridSimulation(simulationResponse.ok ? simulationPayload : null)
          setGridMessage(planPayload.reason || 'V6 grid planı güncellendi')
        }
      } catch (error) {
        if (active) {
          setGridPlan(null)
          setGridSimulation(null)
          setGridMessage(error instanceof Error ? error.message : 'Grid motoruna bağlanılamadı')
        }
      }
    }
    refreshGrid()
    const timer = window.setInterval(refreshGrid, 20000)
    return () => { active = false; window.clearInterval(timer) }
  }, [symbol, interval, gridCapital])
  const refreshGridEngine = async () => {
    try {
      const response = await fetch(`${API}/grid/engine`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Canlı Paper Grid okunamadı')
      setGridEngine(payload)
      if (payload.last_action) setV6Message(payload.last_action)
    } catch { setGridEngine(null) }
  }
  useEffect(() => {
    refreshGridEngine()
    const timer = window.setInterval(refreshGridEngine, 3000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    let active = true
    const refreshTwins = async () => {
      try {
        const response = await fetch(`${API}/grid/twins/${symbol}?interval=${interval}&capital=${gridCapital}`)
        const payload = await response.json()
        if (!response.ok) throw new Error(payload.detail || 'Dijital İkiz laboratuvarı çalışmadı')
        if (active) setTwinLab(payload)
      } catch { if (active) setTwinLab(null) }
    }
    refreshTwins()
    const timer = window.setInterval(refreshTwins, 30000)
    return () => { active = false; window.clearInterval(timer) }
  }, [symbol, interval, gridCapital])
  useEffect(() => {
    let active = true
    const refreshOrderbook = async () => {
      try {
        const response = await fetch(`${API}/orderbook/intelligence/${symbol}`)
        const payload = await response.json()
        if (!response.ok) throw new Error(payload.detail || 'Emir defteri radarı çalışmadı')
        if (active) setOrderbook(payload)
      } catch { if (active) setOrderbook(null) }
    }
    refreshOrderbook()
    const timer = window.setInterval(refreshOrderbook, 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [symbol])
  useEffect(() => {
    if (!notificationsEnabled || !gridEngine?.events?.[0] || Notification.permission !== 'granted') return
    const event = gridEngine.events[0]
    const eventKey = `${event.created_at}-${event.kind}-${event.profile}`
    if (notificationRef.current && notificationRef.current !== eventKey) {
      new Notification('ProTreBot Elite X V6', {body:event.message})
    }
    notificationRef.current = eventKey
  }, [gridEngine?.events, notificationsEnabled])
  const refreshOrchestrator = async () => {
    try {
      const response = await fetch(`${API}/v7/orchestrator`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V7 Orkestra okunamadı')
      setOrchestrator(payload)
      if (payload.last_action) setV7Message(payload.last_action)
    } catch { setOrchestrator(null) }
  }
  useEffect(() => {
    refreshOrchestrator()
    const timer = window.setInterval(refreshOrchestrator, 4000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    let active = true
    const refreshReplay = async () => {
      const replayInterval = ['5m','15m','1h'].includes(interval) ? interval : '15m'
      try {
        const response = await fetch(`${API}/v7/replay/${symbol}?interval=${replayInterval}&horizon=${replayHorizon}&capital=${orchestraCapital}`)
        const payload = await response.json()
        if (!response.ok) throw new Error(payload.detail || 'V7 Piyasa Tekrarı çalışmadı')
        if (active) setV7Replay(payload)
      } catch { if (active) setV7Replay(null) }
    }
    refreshReplay()
    const timer = window.setInterval(refreshReplay, 60000)
    return () => { active = false; window.clearInterval(timer) }
  }, [symbol, interval, replayHorizon, orchestraCapital])
  useEffect(() => {
    const refreshWeekly = () => fetch(`${API}/v7/report/weekly`).then(response => response.json()).then(setV7Weekly).catch(() => setV7Weekly(null))
    refreshWeekly()
    const timer = window.setInterval(refreshWeekly, 30000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    if (!notificationsEnabled || !orchestrator?.events?.[0] || !('Notification' in window) || Notification.permission !== 'granted') return
    const event = orchestrator.events[0]
    const eventKey = `${event.created_at}-${event.kind}-${event.symbol}`
    if (v7NotificationRef.current && v7NotificationRef.current !== eventKey) {
      new Notification('ProTreBot Elite X V7', {body:event.message})
    }
    v7NotificationRef.current = eventKey
  }, [orchestrator?.events, notificationsEnabled])
  useEffect(() => { const timer = window.setInterval(() => setClock(Date.now()), 1000); return () => window.clearInterval(timer) }, [])
  const refreshPaper = async () => {
    try {
      const response = await fetch(`${API}/paper/account`)
      const payload = await response.json().catch(() => null)
      if (!response.ok) throw new Error(objectValue(payload)?.detail ? String(objectValue(payload)?.detail) : 'Paper hesabı geçici olarak yanıt vermedi')
      acceptPaperAccount(payload)
    } catch (error) {
      setPaperMessage(error instanceof Error ? `${error.message}; son sağlam görünüm korunuyor.` : 'Sanal hesap bağlantısı kurulamadı; son sağlam görünüm korunuyor.')
    }
  }
  useEffect(() => { refreshPaper(); const timer = window.setInterval(refreshPaper, 5000); return () => window.clearInterval(timer) }, [])
  const refreshGridPlans = async () => {
    try { const response = await fetch(`${API}/grid/plans`); setSavedGridPlans(await response.json()) }
    catch { setSavedGridPlans(null) }
  }
  useEffect(() => { refreshGridPlans(); const timer = window.setInterval(refreshGridPlans, 10000); return () => window.clearInterval(timer) }, [])
  const refreshPaperBot = async () => { try { const response = await fetch(`${API}/paper/bot`); setPaperBot(await response.json()) } catch { setPaperBot(null) } }
  useEffect(() => { refreshPaperBot(); const timer = window.setInterval(refreshPaperBot, 5000); return () => window.clearInterval(timer) }, [])
  const refreshV20 = async () => {
    try {
      const response = await fetch(`${API}/v20/command`)
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V20 Komuta Merkezi okunamadı')
      setV20Command(payload)
    } catch { setV20Command(null) }
  }
  useEffect(() => { refreshV20(); const timer = window.setInterval(refreshV20, 5000); return () => window.clearInterval(timer) }, [])
  useEffect(() => {
    const refreshSystem = () => fetch(`${API}/health`).then(response => response.json()).then(setSystemHealth).catch(() => setSystemHealth(null))
    refreshSystem()
    const timer = window.setInterval(refreshSystem, 5000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    const refreshArchive = () => fetch(`${API}/archive/decisions?limit=8`).then(response => response.json()).then(setArchive).catch(() => setArchive(null))
    refreshArchive()
    const timer = window.setInterval(refreshArchive, 10000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    const refreshReport = () => fetch(`${API}/report/daily`).then(response => response.json()).then(setDailyReport).catch(() => setDailyReport(null))
    refreshReport()
    const timer = window.setInterval(refreshReport, 10000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    const refreshTestnet = () => fetch(`${API}/testnet/readiness`).then(response => response.json()).then(setTestnet).catch(() => setTestnet(null))
    refreshTestnet()
    const timer = window.setInterval(refreshTestnet, 30000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    const refreshBlackbox = () => fetch(`${API}/decision/blackbox`).then(response => response.json()).then(setBlackbox).catch(() => setBlackbox(null))
    refreshBlackbox()
    const timer = window.setInterval(refreshBlackbox, 15000)
    return () => window.clearInterval(timer)
  }, [])
  const togglePaperBot = async () => {
    setPaperBusy(true)
    try {
      const response = await fetch(`${API}/paper/bot/${paperBot?.enabled ? 'stop' : 'start'}`, {method:'POST'})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Paper Bot durumu değiştirilemedi')
      setPaperBot(payload)
      setPaperMessage(payload.last_action)
    } catch (error) { setPaperMessage(error instanceof Error ? error.message : 'Paper Bot durumu değiştirilemedi') }
    finally { setPaperBusy(false) }
  }
  const togglePaperTraining = async () => {
    setPaperBusy(true)
    try {
      const response = await fetch(`${API}/paper/bot/training/toggle`, {method:'POST'})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Paper eğitim modu değiştirilemedi')
      setPaperBot(payload)
      setPaperMessage(payload.last_action)
    } catch (error) { setPaperMessage(error instanceof Error ? error.message : 'Paper eğitim modu değiştirilemedi') }
    finally { setPaperBusy(false) }
  }
  const selectV20Profile = async (profile:'TEMKINLI'|'DENGELI'|'HIZLI') => {
    setPaperBusy(true)
    try {
      const response = await fetch(`${API}/v20/profile/${profile}`, {method:'POST'})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V20 profili seçilemedi')
      setPaperBot(payload); setPaperMessage(payload.last_action)
      await refreshV20()
    } catch (error) { setPaperMessage(error instanceof Error ? error.message : 'V20 profili seçilemedi') }
    finally { setPaperBusy(false) }
  }
  const openDemoNow = async () => {
    setPaperBusy(true)
    setPaperMessage(`${symbol.replace('USDT','/USDT')} için canlı verili demo planı hazırlanıyor…`)
    try {
      const response = await fetch(`${API}/paper/demo/${symbol}?interval=${interval}`, {method:'POST'})
      const payload = await response.json()
      if (!response.ok) throw new Error(apiErrorMessage(payload, 'Demo Paper işlemi açılamadı'))
      if (!acceptPaperAccount(payload.account, 'Demo açıldı ancak hesap özeti doğrulanamadı; sayfa güvenli biçimde açık tutuldu.')) throw new Error('Paper hesabı doğrulanamadı; işlem sonucu yenileniyor')
      if (payload.bot) setPaperBot(payload.bot)
      setPaperMessage(payload.message)
    } catch (error) { setPaperMessage(error instanceof Error ? error.message : 'Demo Paper işlemi açılamadı') }
    finally { setPaperBusy(false) }
  }
  const runLab = async () => {
    setLabLoading(true)
    try {
      const [response, walkForwardResponse, stressResponse] = await Promise.all([
        fetch(`${API}/lab/${symbol}?interval=${interval}`),
        fetch(`${API}/lab/walk-forward/${symbol}?interval=${interval}`),
        fetch(`${API}/lab/stress/${symbol}?interval=${interval}`),
      ])
      const [payload, walkForwardPayload, stressPayload] = await Promise.all([response.json(), walkForwardResponse.json(), stressResponse.json()])
      if (!response.ok) throw new Error(payload.detail || 'Strateji testi yapılamadı')
      setLab(payload)
      setWalkForward(walkForwardResponse.ok ? walkForwardPayload : null)
      setStressTest(stressResponse.ok ? stressPayload : null)
    } catch { setLab(null); setWalkForward(null); setStressTest(null) }
    finally { setLabLoading(false) }
  }
  const toggleShadow = async () => {
    setPaperBusy(true)
    try { const response = await fetch(`${API}/shadow/toggle`, {method:'POST'}); const payload = await response.json(); if (!response.ok) throw new Error(payload.detail || 'Gölge Modu değiştirilemedi'); acceptPaperAccount(payload.account); setPaperMessage(payload.message) }
    catch (error) { setPaperMessage(error instanceof Error ? error.message : 'Gölge Modu değiştirilemedi') }
    finally { setPaperBusy(false) }
  }
  const toggleEmergency = async () => {
    setPaperBusy(true)
    const active = paper?.emergency_brake?.active
    try { const response = await fetch(`${API}/emergency/${active ? 'reset' : 'trigger'}`, {method:'POST'}); const payload = await response.json(); if (!response.ok) throw new Error(payload.detail || 'Acil fren güncellenemedi'); acceptPaperAccount(payload.account); if (payload.bot) setPaperBot(payload.bot); setPaperMessage(payload.message) }
    catch (error) { setPaperMessage(error instanceof Error ? error.message : 'Acil fren güncellenemedi') }
    finally { setPaperBusy(false) }
  }
  const openPaper = async (direction:'LONG'|'SHORT') => {
    if (!analysis || analysis.direction !== direction) return
    setPaperBusy(true); setPaperMessage('Sanal pozisyon açılıyor…')
    try {
      const response = await fetch(`${API}/paper/open`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({symbol,direction,amount:100,stop_loss:analysis.stop_loss,take_profit:analysis.tp1,tp2:analysis.tp2,tp3:analysis.tp3}) })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Sanal işlem açılamadı')
      acceptPaperAccount(payload.account); setPaperMessage('Sanal pozisyon açıldı')
    } catch (error) { setPaperMessage(error instanceof Error ? error.message : 'Sanal işlem açılamadı') }
    finally { setPaperBusy(false) }
  }
  const closePaper = async (positionId:number) => {
    setPaperBusy(true)
    try { const response = await fetch(`${API}/paper/close/${positionId}`, {method:'POST'}); const payload = await response.json(); if (!response.ok) throw new Error(payload.detail || 'Kapatılamadı'); acceptPaperAccount(payload.account); setPaperMessage('Sanal pozisyon kapatıldı') }
    catch (error) { setPaperMessage(error instanceof Error ? error.message : 'Kapatılamadı') }
    finally { setPaperBusy(false) }
  }
  const fillLimitFromAnalysis = () => {
    if (!analysis) { setLimitMessage('Önce canlı analiz tamamlanmalı.'); return }
    const side = analysis.direction === 'SHORT' ? 'SHORT' : analysis.direction === 'LONG' ? 'LONG' : limitDirection
    const input = (value:number) => Number(value.toPrecision(10)).toString()
    setLimitDirection(side)
    setLimitPrice(input(analysis.entry))
    setLimitStop(input(analysis.stop_loss))
    setLimitTp1(input(analysis.tp1)); setLimitTp2(input(analysis.tp2)); setLimitTp3(input(analysis.tp3))
    setLimitGridLower(input(Math.min(analysis.stop_loss, analysis.tp3)))
    setLimitGridUpper(input(Math.max(analysis.stop_loss, analysis.tp3)))
    setSelectedPositionId(null); setSelectedLimitId(null)
    setLimitMessage(`${symbol.replace('USDT','/USDT')} analiz seviyeleri forma aktarıldı; hepsini değiştirebilirsin.`)
  }
  const openPositionMap = (position:PaperPosition) => {
    setSymbol(position.symbol); setSelectedPositionId(position.id); setSelectedLimitId(null)
    setWorkspaceTab('dashboard'); setWorkspaceView('v20-limit')
  }
  const openLimitMap = (order:PaperLimitOrder) => {
    setSymbol(order.symbol); setSelectedLimitId(order.id); setSelectedPositionId(null)
    setWorkspaceTab('dashboard'); setWorkspaceView('v20-limit')
  }
  const submitLimitOrder = async () => {
    const values = [limitAmount,limitPrice,limitStop,limitTp1,limitTp2,limitTp3,limitGridLower,limitGridUpper].map(Number)
    if (values.some(value => !Number.isFinite(value) || value <= 0)) { setLimitMessage('Tutar, limit, stop, hedef ve grid alanlarının tamamını geçerli yaz.'); return }
    setLimitBusy(true); setLimitMessage('Paper limit emri kaydediliyor…')
    try {
      const response = await fetch(`${API}/paper/limit`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        symbol,direction:limitDirection,amount:Number(limitAmount),limit_price:Number(limitPrice),stop_loss:Number(limitStop),
        tp1:Number(limitTp1),tp2:Number(limitTp2),tp3:Number(limitTp3),grid_lower:Number(limitGridLower),grid_upper:Number(limitGridUpper),
        grid_count:limitGridCount,expires_minutes:limitExpires,
      })})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Paper limit emri kaydedilemedi')
      acceptPaperAccount(payload.account); setLimitMessage(payload.order.status === 'TETİKLENDİ' ? 'Limit fiyatı geçilmişti; Paper pozisyon hemen açıldı.' : payload.message)
      if (payload.order.position_id) { setSelectedPositionId(payload.order.position_id); setSelectedLimitId(null) }
      else { setSelectedLimitId(payload.order.id); setSelectedPositionId(null) }
    } catch (error) { setLimitMessage(error instanceof Error ? error.message : 'Paper limit emri kaydedilemedi') }
    finally { setLimitBusy(false) }
  }
  const cancelLimitOrder = async (orderId:number) => {
    setLimitBusy(true)
    try {
      const response = await fetch(`${API}/paper/limit/cancel/${orderId}`, {method:'POST'})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Limit emri iptal edilemedi')
      acceptPaperAccount(payload.account); setLimitMessage(payload.message)
      if (selectedLimitId === orderId) setSelectedLimitId(null)
    } catch (error) { setLimitMessage(error instanceof Error ? error.message : 'Limit emri iptal edilemedi') }
    finally { setLimitBusy(false) }
  }
  const saveGridPlan = async () => {
    if (!gridPlan) return
    setGridBusy(true)
    try {
      const response = await fetch(`${API}/grid/plan/save`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol,interval,capital:gridCapital})})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Grid planı kaydedilemedi')
      setGridMessage(payload.message)
      await Promise.all([refreshGridPlans(), refreshPaper()])
    } catch (error) { setGridMessage(error instanceof Error ? error.message : 'Grid planı kaydedilemedi') }
    finally { setGridBusy(false) }
  }
  const clearGridPlan = async (planId:string) => {
    setGridBusy(true)
    try {
      const response = await fetch(`${API}/grid/plan/clear/${planId}`, {method:'POST'})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Grid planı arşivlenemedi')
      setGridMessage(payload.message)
      await refreshGridPlans()
    } catch (error) { setGridMessage(error instanceof Error ? error.message : 'Grid planı arşivlenemedi') }
    finally { setGridBusy(false) }
  }
  const toggleGridEngine = async () => {
    setV6Busy(true)
    try {
      const endpoint = gridEngine?.enabled ? 'stop' : 'start'
      const response = await fetch(`${API}/grid/engine/${endpoint}`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:endpoint === 'start' ? JSON.stringify({symbol,interval,capital:gridCapital}) : undefined,
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Canlı Paper Grid güncellenemedi')
      setGridEngine(payload)
      setV6Message(payload.last_action || 'V6 Paper Grid güncellendi')
      await refreshPaper()
    } catch (error) { setV6Message(error instanceof Error ? error.message : 'V6 Paper Grid güncellenemedi') }
    finally { setV6Busy(false) }
  }
  const selectGridProfile = async (profile:string) => {
    setV6Busy(true)
    try {
      const response = await fetch(`${API}/grid/engine/profile`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({profile})})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Profil seçilemedi')
      setGridEngine(payload)
      setV6Message(payload.last_action)
    } catch (error) { setV6Message(error instanceof Error ? error.message : 'Profil seçilemedi') }
    finally { setV6Busy(false) }
  }
  const recenterGridEngine = async () => {
    setV6Busy(true)
    try {
      const response = await fetch(`${API}/grid/engine/recenter`, {method:'POST'})
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'Grid güvenle merkezlenemedi')
      setGridEngine(payload)
      setV6Message(payload.last_action)
    } catch (error) { setV6Message(error instanceof Error ? error.message : 'Grid güvenle merkezlenemedi') }
    finally { setV6Busy(false) }
  }
  const enableNotifications = async () => {
    if (!('Notification' in window)) { setV6Message('Bu tarayıcı masaüstü bildirimi desteklemiyor.'); setV7Message('Bu tarayıcı masaüstü bildirimi desteklemiyor.'); return }
    const permission = await Notification.requestPermission()
    setNotificationsEnabled(permission === 'granted')
    setV6Message(permission === 'granted' ? 'V6 masaüstü Paper bildirimleri açıldı.' : 'Bildirim izni verilmedi; panel içi zaman çizgisi çalışmaya devam ediyor.')
    setV7Message(permission === 'granted' ? 'V7 strateji ve karantina bildirimleri açıldı.' : 'Bildirim izni verilmedi; V7 olay günlüğü çalışmaya devam ediyor.')
  }
  const toggleOrchestrator = async () => {
    setV7Busy(true)
    try {
      const endpoint = orchestrator?.enabled ? 'stop' : 'start'
      const universe = Array.from(new Set([symbol,'BTCUSDT','ETHUSDT','SOLUSDT'])).slice(0,4)
      const orchestrationInterval = ['5m','15m','1h'].includes(interval) ? interval : '15m'
      const response = await fetch(`${API}/v7/orchestrator/${endpoint}`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:endpoint === 'start' ? JSON.stringify({symbols:universe,interval:orchestrationInterval,capital:orchestraCapital}) : undefined,
      })
      const payload = await response.json()
      if (!response.ok) throw new Error(payload.detail || 'V7 Orkestra güncellenemedi')
      setOrchestrator(payload)
      setV7Message(payload.last_action || 'V7 Orkestra güncellendi')
      await refreshPaper()
    } catch (error) { setV7Message(error instanceof Error ? error.message : 'V7 Orkestra güncellenemedi') }
    finally { setV7Busy(false) }
  }
  const active = markets.find(market => market.symbol === symbol)
  const filtered = markets.filter(market => {
    const queryMatch = market.display.includes(query.toUpperCase())
    const tabMatch = tab === 'TÜMÜ' || market.direction === tab || (tab === 'KIRILIM' && market.breakout)
    return queryMatch && tabMatch
  })
  const linearGrid = (lower:number, upper:number, count:number) => Number.isFinite(lower) && Number.isFinite(upper) && upper > lower
    ? Array.from({length:Math.max(3,Math.min(24,count))},(_,index) => lower + (upper - lower) * index / (Math.max(3,Math.min(24,count)) - 1))
    : []
  const selectedPosition = paper?.positions.find(item => item.id === selectedPositionId) ?? null
  const selectedLimit = paper?.recent_limit_orders?.find(item => item.id === selectedLimitId) ?? null
  const draftEntry = Number(limitPrice), draftStop = Number(limitStop), draftTp1 = Number(limitTp1), draftTp2 = Number(limitTp2), draftTp3 = Number(limitTp3)
  const draftGrid = linearGrid(Number(limitGridLower),Number(limitGridUpper),limitGridCount)
  const mapOverlay:PositionOverlay|null = selectedPosition ? {
    id:`position-${selectedPosition.id}`,symbol:selectedPosition.symbol,direction:selectedPosition.direction,label:`AÇIK POZİSYON #${selectedPosition.id}`,
    entry:selectedPosition.entry_price,stop:selectedPosition.stop_loss,tp1:selectedPosition.tp1 ?? selectedPosition.take_profit,tp2:selectedPosition.tp2 ?? selectedPosition.take_profit,tp3:selectedPosition.tp3 ?? selectedPosition.take_profit,
    gridLevels:selectedPosition.grid_levels?.length ? selectedPosition.grid_levels : linearGrid(Math.min(selectedPosition.stop_loss,selectedPosition.tp3 ?? selectedPosition.take_profit),Math.max(selectedPosition.stop_loss,selectedPosition.tp3 ?? selectedPosition.take_profit),8),
  } : selectedLimit ? {
    id:`limit-${selectedLimit.id}`,symbol:selectedLimit.symbol,direction:selectedLimit.direction,label:`BEKLEYEN LİMİT #${selectedLimit.id}`,
    entry:selectedLimit.limit_price,stop:selectedLimit.stop_loss,tp1:selectedLimit.tp1,tp2:selectedLimit.tp2,tp3:selectedLimit.tp3,gridLevels:selectedLimit.grid_levels,
  } : [draftEntry,draftStop,draftTp1,draftTp2,draftTp3].every(value => Number.isFinite(value) && value > 0) && draftGrid.length ? {
    id:'draft',symbol,direction:limitDirection,label:'FORM ÖNİZLEMESİ',entry:draftEntry,stop:draftStop,tp1:draftTp1,tp2:draftTp2,tp3:draftTp3,gridLevels:draftGrid,
  } : null
  const signal = analysis?.direction || 'HESAPLANIYOR'
  const twinProfiles = gridEngine?.enabled ? gridEngine.profiles : (twinLab?.profiles ?? [])
  const recommendedTwin = gridEngine?.enabled ? gridEngine.recommended_profile : (twinLab?.winner ?? 'DENGELİ')
  const activeRuntime = gridEngine?.active_runtime
  const councilLeader = orchestrator?.strategies?.slice().sort((left,right) => right.score - left.score)[0]
  const selectedOrchestraDecision = orchestrator?.symbols.find(item => item.symbol === symbol) ?? orchestrator?.symbols?.[0]
  const replayLeader = v7Replay?.profiles?.slice().sort((left,right) => right.ranking_score - left.ranking_score)[0]
  const v11Report = v11Risk?.latest_report ?? v11Preview
  const v11RiskColor = v11Report?.risk_level === 'KIRMIZI' ? '#e8533f' : v11Report?.risk_level === 'SARI' ? '#e5b800' : '#159653'
  const v10Tournament = v10Evolution?.latest_tournament?.symbol === symbol ? v10Evolution.latest_tournament : v10Preview
  const v10Champion = v10Tournament?.champion ?? v10Evolution?.active_champion ?? null
  const v10Leader = v10Tournament?.leader ?? v10Evolution?.leaderboard?.[0] ?? null
  const latestPaperActivity = paper?.notifications?.[0]
  const latestClosedTrade = paper?.recent_trades?.[0]
  const activityKind = latestPaperActivity?.kind ?? paperBot?.events?.[0]?.kind ?? (latestClosedTrade ? latestClosedTrade.status : 'HAZIR')
  const activityMessage = latestPaperActivity?.message ?? paperBot?.events?.[0]?.message ?? (latestClosedTrade ? `${latestClosedTrade.symbol} ${latestClosedTrade.direction} kapandı · ${latestClosedTrade.realized_pnl >= 0 ? '+' : ''}${fmt(latestClosedTrade.realized_pnl)} USDT` : paperMessage)
  const activityTime = latestPaperActivity?.created_at ?? paperBot?.events?.[0]?.created_at ?? latestClosedTrade?.closed_at
  const secondsToClose = signalGate ? Math.max(0, signalGate.next_close_at - Math.floor(clock / 1000)) : undefined
  const gateCountdown = secondsToClose === undefined ? '—' : `${Math.floor(secondsToClose / 60)}:${String(secondsToClose % 60).padStart(2, '0')}`
  const healthClass = (value?:string) => value === 'BAĞLI' || value === 'AKTİF' || value === 'ÇALIŞIYOR' || value === 'KALICI' ? 'healthOk' : 'healthPending'
  const workspaceTabs: {key:WorkspaceTab;label:string;detail:string;title:string;description:string;icon:typeof LayoutDashboard}[] = [
    {key:'dashboard',label:'V25 KOMUTA',detail:'Live Guard · Tek Paket',title:'V25 Live Guard',description:'Paper, Demo, canlı risk kasası, otomatik karar, satış, lisans ve bütün motorlar tek merkezde.',icon:LayoutDashboard},
    {key:'risk',label:'RİSK MERKEZİ',detail:'V11 · Portföy · Stres',title:'Otonom Risk Beyni',description:'Korelasyon, Monte Carlo, stres testleri ve sermaye tahsisi.',icon:Shield},
    {key:'strategy',label:'STRATEJİLER',detail:'V10 · V8 · Laboratuvar',title:'Strateji ve Gelecek Laboratuvarı',description:'Strateji turnuvası, olasılık senaryoları ve geçmiş testleri.',icon:FlaskConical},
    {key:'live',label:'CANLI VERİ',detail:'V9 · Sistem Sağlığı',title:'Canlı Veri ve Sistem Sağlığı',description:'Akış kalitesi, borsa ikizi, kayıt durumu ve servis kontrolleri.',icon:RadioTower},
    {key:'automation',label:'OTOMASYON',detail:'V7 · V6 · V5 Grid',title:'Paper Otomasyon Merkezi',description:'Strateji orkestrası, sanal grid motoru ve güvenli planlama.',icon:Grid3X3},
    {key:'records',label:'KAYITLAR',detail:'Komuta · Hafıza · Arşiv',title:'Kayıt ve Denetim Merkezi',description:'Karar kara kutusu, günlükler, raporlar ve kalıcı arşiv.',icon:History},
  ]
  const workspaceViews: {key:WorkspaceView;parent:WorkspaceTab;label:string;version:string;title:string;description:string}[] = [
    {key:'dashboard',parent:'dashboard',label:'Ana Kokpit',version:'V20',title:'Ultimate Unified Kokpit',description:'Canlı piyasa, grafik, yapay zekâ kararı ve Paper hesap özeti.'},
    {key:'v22-commercial',parent:'dashboard',label:'V25 Yönetim & Canlı Kasa',version:'V25',title:'Live Guard, Satış, Lisans ve Robot Operasyon Merkezi',description:'Yerel DPAPI anahtar kasası, 30 gün/100 Demo yayın kapısı, kullanıcı risk limitleri, süreli canlı emir kilidi, otomatik karar ve acil durdurma.'},
    {key:'v20-demo',parent:'dashboard',label:'V21 Demo Complete',version:'V21',title:'Binance Futures Demo Komuta Merkezi',description:'İşlem Masası, Risk Kasası, Canlı Günlük, kontrollü otomasyon, backtest ve Demo sertifikası; gerçek hesap kanalı yok.'},
    {key:'v20-limit',parent:'dashboard',label:'Limit & Pozisyon Haritası',version:'V20.2',title:'Manuel Limit ve Pozisyon Haritası',description:'Kendi limit, stop, hedef ve grid kademelerini ayarla; bekleyen ve açık Paper planını grafikte izle.'},
    {key:'v20-autopilot',parent:'dashboard',label:'Paper Autopilot',version:'V20',title:'Çok Aşamalı Paper Autopilot',description:'TP1–TP3 kısmi kâr, zarar durdurma, zaman aşımı ve profil kontrolü.'},
    {key:'v20-ghost',parent:'dashboard',label:'Hayalet İkiz',version:'V20',title:'Hayalet İkiz Karşı-Olasılık Merkezi',description:'Alınan ve engellenen kararların sonradan nasıl geliştiğini karşılaştırır.'},
    {key:'v20-certification',parent:'dashboard',label:'Güvenlik Sertifikası',version:'V20',title:'Paper Kanıt ve Yayın Kapısı',description:'İşlem sayısı, düşüş, Profit Factor ve güvenlik kilitlerini tek raporda denetler.'},
    {key:'risk-command',parent:'risk',label:'Portföy Komutanı',version:'V11',title:'Otonom Risk Beyni',description:'Korelasyon, Monte Carlo, stres testleri ve sermaye tahsisi.'},
    {key:'strategy-evolution',parent:'strategy',label:'Evrim Laboratuvarı',version:'V10',title:'Strateji Evrim Laboratuvarı',description:'Aday stratejilerin walk-forward turnuvası, karantina ve geri alma kontrolleri.'},
    {key:'strategy-future',parent:'strategy',label:'Gelecek Senaryosu',version:'V8',title:'Gelecek Senaryo Merkezi',description:'Olasılık koridoru, kaos testi ve yapay zekâ veto kurulu.'},
    {key:'strategy-lab',parent:'strategy',label:'Geçmiş Testi',version:'LAB',title:'Strateji Test Laboratuvarı',description:'Geçmiş veri, walk-forward ve maliyetli stres simülasyonu.'},
    {key:'live-health',parent:'live',label:'Sistem Sağlığı',version:'SYS',title:'Sistem Sağlık Merkezi',description:'API, veri akışı, veritabanı ve yerel Paper servislerinin durumu.'},
    {key:'live-twin',parent:'live',label:'Borsa İkizi',version:'V9',title:'Canlı Borsa İkizi',description:'Piyasa akışı, veri kalitesi, boşluk onarımı ve günlük operasyon raporu.'},
    {key:'automation-orchestra',parent:'automation',label:'Strateji Orkestrası',version:'V7',title:'Otonom Strateji Orkestrası',description:'Çoklu Paper strateji seçimi, sermaye dağılımı ve karantina.'},
    {key:'automation-grid',parent:'automation',label:'Canlı Grid Motoru',version:'V6',title:'Canlı Paper Grid Motoru',description:'Sanal dolum, envanter kilidi, dijital ikiz ve güvenli merkezleme.'},
    {key:'automation-plan',parent:'automation',label:'Grid Planlayıcı',version:'V5',title:'Akıllı Grid Planlayıcı',description:'ATR tabanlı seviye planı, maliyet filtresi ve Paper simülasyonu.'},
    {key:'records-command',parent:'records',label:'Denetim Merkezi',version:'V4+',title:'Komuta ve Denetim Merkezi',description:'Seans hafızası, günlük rapor, kara kutu ve güvenlik kapıları.'},
    {key:'records-journal',parent:'records',label:'İşlem Hafızası',version:'LOG',title:'Paper İşlem Hafızası',description:'Kapanan sanal işlemler, karar fotoğrafı ve performans özeti.'},
    {key:'records-archive',parent:'records',label:'Kalıcı Arşiv',version:'DB',title:'Kalıcı Karar Arşivi',description:'TimescaleDB üzerinde saklanan karar ve denetim kayıtları.'},
  ]
  const activeWorkspaceViews = workspaceViews.filter(item => item.parent === workspaceTab)
  const activeView = workspaceViews.find(item => item.key === workspaceView) ?? workspaceViews[0]
  const panelVisibility = (target:WorkspaceTab) => workspaceTab === target ? '' : ' moduleHidden'
  const openWorkspace = (target:WorkspaceTab) => {
    setWorkspaceTab(target)
    if (target === 'dashboard') {
      setWorkspaceView('v20-demo')
      return
    }
    setWorkspaceView(workspaceViews.find(item => item.parent === target)?.key ?? 'dashboard')
  }
  const navigateWorkspace = (target:WorkspaceView) => {
    const destination = workspaceViews.find(item => item.key === target)
    if (!destination) return
    setWorkspaceTab(destination.parent)
    setWorkspaceView(destination.key)
  }

  return <main className={`appShell tab-${workspaceTab} view-${workspaceView}`}>
    <header>
      <div className="brand"><span className="logo">X</span><div><b>PROTREBOT ELITE X</b><small>V25.1.2 · LIVE GUARD</small></div></div>
      <div className="live"><i/> {status}</div><div className={streamLive ? 'streamLive' : 'streamIdle'}>● {streamLive ? 'CANLI VERİ AKIŞI' : 'AKIŞ BAĞLANIYOR'}</div><div className="analysisLive">✓ GERÇEK ANALİZ AKTİF</div>
      <div className="v7Version">V25 · FAIL-CLOSED EXECUTION</div>
      <div className="safe"><ShieldCheck/> GÜVENLİ ANALİZ MODU</div>
    </header>
    <nav className="moduleNav" aria-label="ProTreBot çalışma alanları">
      {workspaceTabs.map(item => {
        const Icon = item.icon
        return <button key={item.key} type="button" className={workspaceTab === item.key ? 'moduleTab activeModuleTab' : 'moduleTab'} aria-current={workspaceTab === item.key ? 'page' : undefined} onClick={() => openWorkspace(item.key)}>
          <Icon/><span><b>{item.label}</b><small>{item.detail}</small></span><i/>
        </button>
      })}
    </nav>
    <section className="moduleContext">
      <div><small>AKTİF ÇALIŞMA ALANI · {activeView.version}</small><h1>{activeView.title}</h1><p>{activeView.description}</p></div>
      <div className="moduleQuickStatus"><span><i className={systemHealth?.api === 'BAĞLI' ? 'statusDot statusOk' : 'statusDot'}/>API {systemHealth?.api ?? 'KONTROL'}</span><span><i className={streamLive ? 'statusDot statusOk' : 'statusDot'}/>AKIŞ {streamLive ? 'CANLI' : 'BEKLEMEDE'}</span><span><ShieldCheck/>CANLI GİRİŞ V25 KASA KONTROLLÜ</span><b>{active?.display || symbol.replace('USDT','/USDT')} · {interval}</b></div>
    </section>
    {activeWorkspaceViews.length > 1 && <nav className="extensionNav" aria-label="Uzantı sekmeleri">{activeWorkspaceViews.map(item => <button type="button" key={item.key} className={workspaceView === item.key ? 'activeExtension' : ''} aria-current={workspaceView === item.key ? 'page' : undefined} onClick={() => setWorkspaceView(item.key)}><span>{item.version}</span><b>{item.label}</b></button>)}</nav>}
    <Suspense fallback={<section className="moduleLazyLoading">Profesyonel çalışma alanı yükleniyor…</section>}>
      <CommercialHub active={workspaceView === 'v22-commercial'} onNavigate={target => navigateWorkspace(target as WorkspaceView)}/>
      <BinanceDemo active={workspaceView === 'v20-demo'} symbol={symbol} analysis={analysis} chart={workspaceView === 'v20-demo' ? <Chart symbol={symbol} interval={interval} horizon={v8Horizon} notional={v8Notional} onAnalysis={setAnalysis} onFutureLab={setFutureLab} onLivePrice={setLivePrice} onStream={setStreamLive}/> : null}/>
    </Suspense>
    <section className={`paperPilotRibbon${panelVisibility('dashboard')}`}>
      <div className="pilotIdentity"><span className="pilotOrb"><i/></span><div><small>V20 · PAPER AUTOPILOT</small><b>{paperBot?.mode ?? 'V20 DENGELİ PAPER'}</b><p>{paperBot?.enabled ? `Otomatik tarama ${paperBot.scan_interval_seconds || 20} saniyede bir çalışıyor.` : 'Bot beklemede; tek tıkla demo işlem açabilir veya otomatik taramayı başlatabilirsin.'}</p></div></div>
      <div className="pilotMetrics">
        <span><small>SANAL VARLIK</small><b>{fmt(paper?.equity)} <em>USDT</em></b></span>
        <span><small>AÇIK DEMO</small><b>{paper?.positions.length ?? 0} <em>/ 3</em></b></span>
        <span><small>TARAMA</small><b>{paperBot?.cycles ?? 0} <em>tur</em></b></span>
        <span><small>ADAY</small><b>{paperBot?.last_candidate_count ?? 0} <em>sinyal</em></b></span>
      </div>
      <div className="pilotDecision"><small>SON BOT KARARI</small><b className={paperBot?.last_blocker ? 'waiting' : 'up'}>{paperBot?.events?.[0]?.kind ?? 'HAZIR'}</b><p>{paperBot?.events?.[0]?.message ?? paperBot?.last_action ?? paperMessage}</p><span><ShieldCheck/> Borsa emri: 0 · Paper simülasyonu</span></div>
      <div className="pilotActions">
        <button type="button" className="demoOpen" disabled={paperBusy || (paper?.positions.length ?? 0) >= 3} onClick={openDemoNow}><b>＋ ŞİMDİ 50 USDT DEMO AÇ</b><small>{symbol.replace('USDT','/USDT')} · canlı fiyat · TP1/TP2/TP3</small></button>
        <div><button className={paperBot?.enabled ? 'pilotStop' : 'pilotStart'} disabled={paperBusy} onClick={togglePaperBot}>{paperBot?.enabled ? '■ BOTU DURDUR' : '▶ PAPER BOTU BAŞLAT'}</button><button className={paperBot?.training_mode ? 'trainingActive' : 'trainingStrict'} disabled={paperBusy} onClick={togglePaperTraining}>{paperBot?.training_mode ? '🛡 SIKI DOĞRULAMAYA GEÇ' : '⚡ V20 PROFİLİNE DÖN'}</button></div>
      </div>
      <div className={`tradeActivityBar ${paperBot?.enabled ? 'activityRunning' : 'activityStopped'}`}>
        <div className="activityBotState"><span><Activity/><i/></span><div><small>CANLI İŞLEM AKIŞI</small><b>{paperBot?.enabled ? 'OTOPİLOT ÇALIŞIYOR' : 'OTOPİLOT DURUYOR'}</b><p>{paperBot?.enabled ? `${paperBot.scan_interval_seconds || 20} saniyede bir yeni Paper fırsatı taranıyor.` : 'Otomatik işlem açması için Paper Botu başlat.'}</p></div><button type="button" disabled={paperBusy} onClick={togglePaperBot}>{paperBot?.enabled ? 'DURDUR' : 'ŞİMDİ BAŞLAT'}</button></div>
        <div className="activityStats"><span><small>AÇIK</small><b>{paper?.positions.length ?? 0}<em>/3</em></b></span><span><small>BEKLEYEN LİMİT</small><b>{paper?.pending_orders?.length ?? 0}</b></span><span><small>KAPANAN</small><b>{paper?.performance.closed_count ?? 0}</b></span><span><small>GERÇEKLEŞEN PnL</small><b className={(paper?.performance.realized_pnl || 0) >= 0 ? 'up' : 'down'}>{(paper?.performance.realized_pnl || 0) >= 0 ? '+' : ''}{fmt(paper?.performance.realized_pnl)}</b></span></div>
        <div className="activityOpenStrip">{paper?.positions.length ? paper.positions.slice(0,3).map(position => <article className="activityPosition" key={position.id}><header><b>{position.symbol.replace('USDT','/USDT')}</b><span className={position.direction === 'LONG' ? 'up' : 'down'}>{position.direction}</span><em>{position.entry_order_type === 'LIMIT' ? 'LİMİT' : position.source === 'DEMO' ? 'DEMO' : position.source === 'AUTO' ? 'OTO' : 'ELLE'}</em></header><div><span>Giriş {fmt(position.entry_price)}</span><span>Şimdi {fmt(position.current_price)}</span></div><footer><small>{stamp(position.opened_at)}</small><strong className={position.unrealized_pnl >= 0 ? 'up' : 'down'}>{position.unrealized_pnl >= 0 ? '+' : ''}{fmt(position.unrealized_pnl)} USDT</strong><button type="button" onClick={() => openPositionMap(position)}>HARİTA</button></footer></article>) : <div className="activityEmpty"><b>Açık Paper pozisyonu yok</b><span>Limit planı kurabilir, demo açabilir veya otopilotu başlatabilirsin.</span></div>}</div>
        <div className="activityLatest"><div><small>SON HAREKET</small><b>{activityKind}</b><time>{stamp(activityTime)}</time></div><p>{activityMessage}</p>{(paper?.positions.length ?? 0) >= 3 && <span>Pozisyon sınırı dolu; yenisi için mevcut işlemlerden biri kapanmalı.</span>}</div>
        <button className="activityDetails" type="button" onClick={() => setWorkspaceView('v20-limit')}><Grid3X3/><b>LİMİT & HARİTA</b><span>Giriş · Stop · TP · Grid</span></button>
      </div>
    </section>
    <section className={`workspace${panelVisibility('dashboard')}`}>
      <aside className="panel scanner">
        <div className="panelTitle"><Activity/> AKILLI PİYASA TARAYICI</div>
        <input value={query} onChange={event => setQuery(event.target.value)} placeholder="Coin ara…"/>
        <div className="scanTabs">{(['TÜMÜ','LONG','SHORT','KIRILIM'] as const).map(item => <button key={item} className={tab === item ? 'activeTab' : ''} onClick={() => setTab(item)}>{item}</button>)}</div>
        <button className="scanButton" onClick={scan} disabled={scanning}><RefreshCw className={scanning ? 'spin' : ''}/>{scanning ? 'TARANIYOR…' : 'PİYASAYI TARA'}</button>
        <small className="scanMessage">{scanMessage}</small>
        {filtered.map(market => <button key={market.symbol} className={market.symbol === symbol ? 'market active' : 'market'} onClick={() => setSymbol(market.symbol)}>
          <b>{market.display}</b><span>{fmt(market.price)}</span><em className={market.direction === 'SHORT' ? 'down' : market.direction === 'LONG' ? 'up' : ''}>{market.direction || '—'} %{market.confidence ?? '—'}</em>
        </button>)}
      </aside>
      <div className="center">
        <div className="panel ticker"><div><small>SEÇİLEN PİYASA</small><h2>{active?.display || 'BTC/USDT'}</h2></div><strong>{fmt(livePrice ?? active?.price)}</strong><span className={(active?.change || 0) >= 0 ? 'up' : 'down'}>{active?.change.toFixed(2) || '0.00'}%</span><div className={signalGate?.entry_allowed ? 'gateBadge approved' : 'gateBadge'} title={signalGate?.reason}><small>MUM KAPANIŞ KAPISI</small><b>{signalGate?.status ?? 'ÖLÇÜLÜYOR'}</b><span>Sonraki mum: {gateCountdown}</span></div><div className={freshness?.auto_allowed ? 'freshBadge approved' : 'freshBadge'} title={freshness?.reason}><small>FİYAT SAPMA KALKANI</small><b>{freshness?.status ?? 'ÖLÇÜLÜYOR'}</b><span>Δ {freshness?.drift_atr ?? '—'} ATR</span></div><div className={liquidity?.auto_allowed ? 'liquidityBadge approved' : 'liquidityBadge'} title={liquidity?.reason}><small>EMİR DEFTERİ KALKANI</small><b>{liquidity?.mode ?? 'ÖLÇÜLÜYOR'}</b><span>Spread {liquidity?.spread_bps?.toFixed(1) ?? '—'} bp</span></div><div className="ranges">{['1m','5m','15m','1h','4h','1d'].map(item => <button key={item} className={item === interval ? 'selected' : ''} onClick={() => setInterval(item)}>{item}</button>)}</div></div>
        <div className="panel chartPanel"><Chart symbol={symbol} interval={interval} horizon={v8Horizon} notional={v8Notional} onAnalysis={setAnalysis} onFutureLab={setFutureLab} onLivePrice={setLivePrice} onStream={setStreamLive}/><div className="indicatorLegend"><span className="e20">EMA20 {fmt(analysis?.ema.ema20)}</span><span className="e50">EMA50 {fmt(analysis?.ema.ema50)}</span><span className="e200">EMA200 {fmt(analysis?.ema.ema200)}</span><span className="v8Legend">V8 OLASILIK KORİDORU · {v8Horizon} MUM</span></div></div>
      </div>
      <aside className="panel ai">
        <div className="panelTitle"><BrainCircuit/> YAPAY ZEKA KARAR MERKEZİ</div>
        <div className="score"><div>%{analysis?.confidence ?? '—'}<small>GÜVEN</small></div><section><small>SİNYAL</small><b className={signal === 'SHORT' ? 'down' : signal === 'BEKLE' ? 'waiting' : 'up'}>{signal}</b></section></div>
        {[['Trend',analysis?.trend],['Momentum',analysis?.momentum],['RSI (14)',analysis?.rsi.toFixed(1)],['MACD',analysis?.macd.toFixed(3)],['ADX (14)',analysis?.adx.toFixed(1)],['Hacim Oranı',analysis ? `${analysis.volume_ratio.toFixed(2)}x` : undefined],['ATR',fmt(analysis?.atr)],['Risk / Ödül',analysis ? `1 : ${analysis.risk_reward.toFixed(1)}` : undefined]].map(([key,value]) => <div className="metric" key={key}><span>{key}</span><b>{value || '—'}</b></div>)}
        <p>{analysis?.explanation || 'Gerçek piyasa verileri hesaplanıyor…'}</p>
        <div className="decisionExplain"><div className="decisionExplainHead"><span>AÇIKLANABİLİR KARAR</span><b className={decisionExplanation?.status === 'PAPER GİRİŞE YAKIN' ? 'up' : 'waiting'}>{decisionExplanation?.status ?? 'ÖLÇÜLÜYOR'}</b></div><div className="readiness"><b>%{decisionExplanation?.readiness_score ?? '—'}</b><span>kapı uyumu · {decisionExplanation?.direction ?? '—'}</span></div><div className="explainChecks">{decisionExplanation?.checks.map(check => <span key={check.key} className={check.passed ? 'up' : 'waiting'} title={check.detail}>{check.passed ? '✓' : '•'} {check.label}</span>)}</div><small>{decisionExplanation?.summary ?? 'Karar kapıları birlikte doğrulanıyor…'}</small></div>
        <div className="levels">{[['GİRİŞ',analysis?.entry],['STOP',analysis?.stop_loss],['TP1',analysis?.tp1],['TP2',analysis?.tp2],['TP3',analysis?.tp3]].map(([key,value]) => <div key={key as string}><span>{key}</span><b>{fmt(value as number|undefined)}</b></div>)}</div>
        <div className="actions"><button className="long" disabled={paperBusy || analysis?.direction !== 'LONG'} onClick={() => openPaper('LONG')}>SANAL LONG</button><button className="short" disabled={paperBusy || analysis?.direction !== 'SHORT'} onClick={() => openPaper('SHORT')}>SANAL SHORT</button><button className="wait">PAPER MOD</button></div>
      </aside>
    </section>
    <section className={`bottom${panelVisibility('dashboard')}`}>
      <div className="panel paper"><h3>◉ PAPER TRADING · CANLI DEMO CÜZDAN</h3><b>{fmt(paper?.equity)} USDT</b><div><span>Kullanılabilir</span><strong>{fmt(paper?.available)} USDT</strong></div><div><span>Açık PnL</span><strong className={(paper?.unrealized_pnl || 0) >= 0 ? 'up' : 'down'}>{fmt(paper?.unrealized_pnl)} USDT</strong></div><div><span>Demo / Oto kapanan</span><strong>{paper?.performance.demo_trades ?? 0} / {paper?.performance.auto_trades ?? 0}</strong></div>{paper?.positions.length ? <div className="paperPositionList">{paper.positions.slice(0,3).map(position => <article className="paperPositionCard" key={position.id}><div><span>{position.symbol.replace('USDT','/USDT')}</span><b className={position.direction === 'LONG' ? 'up' : 'down'}>{position.direction}</b><em>{position.source === 'DEMO' ? 'HIZLI DEMO' : position.source === 'AUTO' ? 'OTOMATİK' : 'MANUEL'}</em></div><div><span>{fmt(position.amount)} USDT</span><strong className={position.unrealized_pnl >= 0 ? 'up' : 'down'}>{position.unrealized_pnl >= 0 ? '+' : ''}{fmt(position.unrealized_pnl)}</strong><button disabled={paperBusy} onClick={() => closePaper(position.id)}>KAPAT</button></div></article>)}</div> : <small>{paperMessage}</small>}</div>
      <div className="panel radar"><h3>◉ TUZAK RADARI</h3><div><span>Kırılım Kalitesi</span><b>%{analysis?.radar.breakout_quality ?? '—'}</b></div><div><span>Tuzak Riski</span><b className={analysis?.radar.trap_level === 'YÜKSEK' ? 'down' : 'up'}>{analysis?.radar.trap_level ?? '—'} %{analysis?.radar.trap_score ?? '—'}</b></div><div><span>Giriş Zamanı</span><b>{analysis?.radar.entry_timing ?? '—'}</b></div><small>{analysis?.radar.wick_signal ?? 'Piyasa verisi hesaplanıyor…'} · Sıkışma: {analysis?.radar.squeeze ?? '—'}</small></div>
      <div className="panel consensus"><h3>◈ ÇOKLU ZAMAN ONAYI</h3><strong className={consensus?.verdict === 'GÜÇLÜ ONAY' ? 'up' : consensus?.verdict === 'UYUMSUZ' ? 'down' : 'waiting'}>{consensus?.verdict ?? 'HESAPLANIYOR'}</strong><b>Uyum %{consensus?.alignment ?? '—'} · {consensus?.direction ?? '—'}</b><div className="timeframes">{consensus?.timeframes.map(item => <span key={item.timeframe} className={item.direction === 'LONG' ? 'up' : item.direction === 'SHORT' ? 'down' : 'waiting'}>{item.timeframe} {item.direction} %{item.confidence}</span>)}</div><small>{consensus?.reason ?? '15m, 1h ve 4h analiz ediliyor…'}</small></div>
      <div className="panel guard"><h3>◈ PİYASA KALKANI · REJİM</h3><strong className={regimeStability?.auto_allowed ? 'up' : regime?.auto_allowed ? 'waiting' : marketGuard?.risk_score && marketGuard.risk_score >= 55 ? 'down' : 'waiting'}>{regime?.label ?? marketGuard?.market_mode ?? 'ÖLÇÜLÜYOR'}</strong><div><span>Oto Politika</span><b>{regime?.entry_policy ?? 'ÖLÇÜLÜYOR'}</b></div><div><span>Rejim Kararlılığı</span><b className={regimeStability?.auto_allowed ? 'up' : 'waiting'}>{regimeStability ? `${regimeStability.mode} · ${regimeStability.stable_samples}/${regimeStability.required_samples}` : 'ÖLÇÜLÜYOR'}</b></div><div><span>Rejim Gücü</span><b>%{regime?.strength ?? '—'}</b></div><div><span>Risk Skoru</span><b>%{marketGuard?.risk_score ?? '—'}</b></div><div><span>BTC Volatilite</span><b>%{marketGuard?.volatility_pct ?? '—'}</b></div><small>{regimeStability?.reason ?? regime?.reason ?? marketGuard?.reason ?? 'Piyasa koşulları ölçülüyor…'}</small></div>
      <div className="panel riskVault"><h3>◈ RİSK & PORTFÖY KASASI</h3><strong className={portfolioGuard?.auto_allowed && !paper?.risk.auto_paused ? 'up' : paper?.risk.auto_paused || portfolioGuard?.mode === 'KORELASYON KİLİDİ' ? 'down' : 'waiting'}>{portfolioGuard?.mode ?? paper?.risk.status ?? 'HESAPLANIYOR'}</strong><div><span>Günlük PnL</span><b className={(paper?.risk.daily_realized_pnl || 0) >= 0 ? 'up' : 'down'}>{fmt(paper?.risk.daily_realized_pnl)} USDT</b></div><div><span>Kalan Kayıp Limiti</span><b>{fmt(paper?.risk.remaining_loss_budget)} USDT</b></div><div><span>Portföy Isısı</span><b className={(portfolioGuard?.heat || 0) >= 70 ? 'down' : 'up'}>%{portfolioGuard?.heat ?? '—'}</b></div><div><span>Korelasyon</span><b>{portfolioGuard?.matched_symbol ? `${portfolioGuard.matched_symbol} · %${portfolioGuard.correlation_pct.toFixed(0)}` : 'Yok'}</b></div><small>{portfolioGuard?.reason ?? paper?.risk.reason ?? 'Portföy riski ölçülüyor…'}</small></div>
      <div className="panel adaptive"><h3>◈ ADAPTİF KALİTE KAPISI</h3><strong className={adaptiveGate?.auto_allowed ? adaptiveGate.mode === 'KALİBRASYON MODU' ? 'waiting' : 'up' : 'down'}>{adaptiveGate?.mode ?? 'ÖLÇÜLÜYOR'}</strong><div><span>Güven Eşiği</span><b>%{adaptiveGate?.min_confidence ?? '—'}</b></div><div><span>Örneklem</span><b>{adaptiveGate?.sample_size ?? '—'} işlem</b></div><div><span>Başarı</span><b className={(adaptiveGate?.win_rate || 0) >= 55 ? 'up' : 'waiting'}>{adaptiveGate ? `%${adaptiveGate.win_rate.toFixed(0)}` : '—'}</b></div><small>{adaptiveGate?.reason ?? 'Paper sonuçları ölçülüyor…'}</small></div>
      <div className="panel controls"><h3><Bot/> V20 PAPER KONTROLLERİ</h3><button type="button" className="demoOpenCompact" disabled={paperBusy || (paper?.positions.length ?? 0) >= 3} onClick={openDemoNow}>＋ 50 USDT DEMO AÇ</button><button className={paperBot?.enabled ? 'paperStop' : 'paperStart'} disabled={paperBusy} onClick={togglePaperBot}><Power/> {paperBot?.enabled ? 'PAPER BOTU DURDUR' : 'PAPER BOTU BAŞLAT'}</button><button className={paperBot?.training_mode ? 'trainingActive' : 'trainingStrict'} disabled={paperBusy} onClick={togglePaperTraining}>{paperBot?.training_mode ? `PROFİL: ${paperBot?.profile === 'TEMKINLI' ? 'TEMKİNLİ' : paperBot?.profile === 'HIZLI' ? 'HIZLI' : 'DENGELİ'}` : '🛡 SIKI DOĞRULAMA'}</button><button className={paper?.shadow?.enabled ? 'shadowActive' : 'shadowIdle'} disabled={paperBusy} onClick={toggleShadow}>{paper?.shadow?.enabled ? '◌ GÖLGE AÇIK' : '◌ GÖLGE KAPALI'}</button><button className={paper?.emergency_brake?.active ? 'brakeReset' : 'emergency'} disabled={paperBusy} onClick={toggleEmergency}>{paper?.emergency_brake?.active ? '✓ ACİL FRENİ KALDIR' : '⚠ ACİL FREN'}</button><div className="botDiagnostics"><b>{paperBot?.events?.[0]?.kind ?? 'HAZIR'}</b><span>{paperBot?.events?.[0]?.message ?? paperBot?.last_action ?? paperMessage}</span>{paperBot?.last_blocker && <small>Son engel: {paperBot.last_blocker}</small>}</div><button disabled className="realDisabled">GERÇEK + TESTNET EMRİ KAPALI</button></div>
    </section>
    <section className="v20Deck v20Limit" aria-label="V20.2 Manuel Limit ve Pozisyon Haritası">
      <div className="panel limitHero">
        <div><span className="v20Eyebrow">V20.2 · MANUEL PAPER LİMİT</span><h2>Seviyeyi sen belirle, sistem canlı fiyatı izlesin.</h2><p>Limit, stop, üç hedef ve grid kademeleri tamamen senin kontrolünde. Tetiklenen işlem sanal cüzdanda açılır ve bütün plan grafiğe taşınır.</p></div>
        <div className="limitHeroMetrics"><span><small>KULLANILABİLİR</small><b>{fmt(paper?.available)} USDT</b></span><span><small>LİMİTE AYRILAN</small><b>{fmt(paper?.reserved_margin)} USDT</b></span><span><small>BEKLEYEN</small><b>{paper?.pending_orders?.length ?? 0} / 8</b></span><span><small>AÇIK</small><b>{paper?.positions.length ?? 0} / 3</b></span></div>
      </div>
      <div className="limitWorkbench">
        <form className="panel limitComposer" onSubmit={event => { event.preventDefault(); submitLimitOrder() }}>
          <div className="v20SectionHead"><div><span>EMİR OLUŞTURUCU</span><h3>Kendi Paper Limit Planın</h3></div><b>SADECE SANAL</b></div>
          <div className="limitSymbolRow"><span><small>PARİTE</small><b>{symbol.replace('USDT','/USDT')}</b></span><span><small>CANLI FİYAT</small><b>{fmt(livePrice ?? active?.price)}</b></span></div>
          <div className="limitSidePicker"><button type="button" className={limitDirection === 'LONG' ? 'selectedLong' : ''} onClick={() => setLimitDirection('LONG')}>LONG LİMİT</button><button type="button" className={limitDirection === 'SHORT' ? 'selectedShort' : ''} onClick={() => setLimitDirection('SHORT')}>SHORT LİMİT</button></div>
          <button className="limitAutofill" type="button" onClick={fillLimitFromAnalysis}><BrainCircuit/> CANLI ANALİZ PLANINI DOLDUR</button>
          <div className="limitFormGrid">
            <label><span>Tutar (USDT)</span><input value={limitAmount} inputMode="decimal" onChange={event => setLimitAmount(event.target.value)}/></label>
            <label><span>Limit giriş</span><input value={limitPrice} inputMode="decimal" onChange={event => { setLimitPrice(event.target.value); setSelectedPositionId(null); setSelectedLimitId(null) }}/></label>
            <label className="limitStopField"><span>Stop loss</span><input value={limitStop} inputMode="decimal" onChange={event => setLimitStop(event.target.value)}/></label>
            <label><span>TP1</span><input value={limitTp1} inputMode="decimal" onChange={event => setLimitTp1(event.target.value)}/></label>
            <label><span>TP2</span><input value={limitTp2} inputMode="decimal" onChange={event => setLimitTp2(event.target.value)}/></label>
            <label><span>TP3</span><input value={limitTp3} inputMode="decimal" onChange={event => setLimitTp3(event.target.value)}/></label>
            <label><span>Grid alt</span><input value={limitGridLower} inputMode="decimal" onChange={event => setLimitGridLower(event.target.value)}/></label>
            <label><span>Grid üst</span><input value={limitGridUpper} inputMode="decimal" onChange={event => setLimitGridUpper(event.target.value)}/></label>
          </div>
          <div className="limitRange"><span>GRID KADEMESİ <b>{limitGridCount}</b></span><input type="range" min="3" max="24" value={limitGridCount} onChange={event => setLimitGridCount(Number(event.target.value))}/></div>
          <label className="limitExpiry"><span>Emir süresi</span><select value={limitExpires} onChange={event => setLimitExpires(Number(event.target.value))}><option value={60}>1 saat</option><option value={240}>4 saat</option><option value={1440}>1 gün</option><option value={4320}>3 gün</option><option value={10080}>7 gün</option></select></label>
          <div className="limitMessage">{limitMessage}</div>
          <button className="limitSubmit" type="submit" disabled={limitBusy}>{limitBusy ? 'KAYDEDİLİYOR…' : '＋ PAPER LİMİT EMRİNİ KAYDET'}</button>
          <small className="limitSafety"><ShieldCheck/> Binance’a, Testnet’e veya gerçek hesaba emir gönderilmez.</small>
        </form>

        <div className="panel limitChartCard">
          <div className="limitChartHead"><div><span>{mapOverlay?.label ?? 'POZİSYON / FORM ÖNİZLEMESİ'}</span><h3>{mapOverlay?.symbol.replace('USDT','/USDT') ?? symbol.replace('USDT','/USDT')} · {mapOverlay?.direction ?? limitDirection}</h3></div><div className="ranges">{['1m','5m','15m','1h','4h','1d'].map(item => <button type="button" key={item} className={item === interval ? 'selected' : ''} onClick={() => setInterval(item)}>{item}</button>)}</div></div>
          <div className="limitChartCanvas"><Chart symbol={mapOverlay?.symbol ?? symbol} interval={interval} horizon={v8Horizon} notional={v8Notional} overlay={mapOverlay} showAnalysisLevels={false} onAnalysis={setAnalysis} onFutureLab={setFutureLab} onLivePrice={setLivePrice} onStream={setStreamLive}/></div>
          <div className="mapLegend"><span className="mapEntry">GİRİŞ {fmt(mapOverlay?.entry)}</span><span className="mapStop">STOP {fmt(mapOverlay?.stop)}</span><span>TP1 {fmt(mapOverlay?.tp1)}</span><span>TP2 {fmt(mapOverlay?.tp2)}</span><span>TP3 {fmt(mapOverlay?.tp3)}</span><em>{mapOverlay?.gridLevels.length ?? 0} GRID</em></div>
          <div className="gridLevelStrip">{mapOverlay?.gridLevels.map((level,index) => <span key={`${level}-${index}`}><small>G{index + 1}</small><b>{fmt(level)}</b></span>) ?? <p>Formu doldurduğunda grid kademeleri burada ve grafikte görünür.</p>}</div>
        </div>

        <aside className="limitQueue">
          <div className="panel openMapList"><div className="v20SectionHead"><div><span>AÇIK POZİSYONLAR</span><h3>Haritada Göster</h3></div><b>{paper?.positions.length ?? 0}</b></div><div>{paper?.positions.length ? paper.positions.map(position => <button type="button" className={selectedPositionId === position.id ? 'selectedMapItem' : ''} key={position.id} onClick={() => openPositionMap(position)}><span><b>{position.symbol.replace('USDT','/USDT')}</b><small>{position.entry_order_type ?? position.source} · {fmt(position.original_amount ?? position.amount)} USDT</small></span><em className={position.direction === 'LONG' ? 'up' : 'down'}>{position.direction}</em><strong className={position.unrealized_pnl >= 0 ? 'up' : 'down'}>{position.unrealized_pnl >= 0 ? '+' : ''}{fmt(position.unrealized_pnl)}</strong></button>) : <p>Açık Paper pozisyonu yok. Limit tetiklenince burada görünür.</p>}</div></div>
          <div className="panel pendingLimitList"><div className="v20SectionHead"><div><span>BEKLEYEN LİMİTLER</span><h3>Canlı Emir Kuyruğu</h3></div><b>{paper?.pending_orders?.length ?? 0}</b></div><div>{paper?.pending_orders?.length ? paper.pending_orders.map(order => <article className={selectedLimitId === order.id ? 'selectedMapItem' : ''} key={order.id}><button type="button" onClick={() => openLimitMap(order)}><span><b>{order.symbol.replace('USDT','/USDT')}</b><small>Limit {fmt(order.limit_price)} · Şimdi {fmt(order.last_price)}</small></span><em className={order.direction === 'LONG' ? 'up' : 'down'}>{order.direction}</em></button><footer><span>{order.wait_reason ?? order.status} · Δ %{Math.abs(order.distance_pct).toFixed(3)}</span><button type="button" disabled={limitBusy} onClick={() => cancelLimitOrder(order.id)}>İPTAL</button></footer></article>) : <p>Bekleyen limit yok. Oluşturduğun emir burada canlı fiyatla izlenir.</p>}</div></div>
        </aside>
      </div>
    </section>

    <section className="v20Deck v20Autopilot" aria-label="V25.1 Otonom Paper Avcısı">
      <div className="v20Hero panel">
        <div className="v20HeroCopy"><span className="v20Eyebrow">V25.1 · OTONOM COIN AVCISI & SERMAYE MOTORU</span><h2>Coini kendi bulur, riski hesaplar, sanal parayı fırsata göre dağıtır.</h2><p>{paperBot?.autonomy?.universe_size ?? 24} likit USDT paritesi puanlanır; uygun aday Stop mesafesine göre boyutlandırılır. TP1–TP3 kısmi kâr, portföy tavanı ve günlük zarar kilidi korunur. Yalnızca Paper simülasyonu yapar.</p><div className="v20Pulse"><i/><b>{paperBot?.enabled ? 'OTONOM AVCI ÇALIŞIYOR' : 'KULLANICI ONAYI BEKLİYOR'}</b><span>{paperBot?.scan_interval_seconds ?? 20} sn döngü · kâr garantisi yok</span></div></div>
        <div className="v20HeroScore"><div><strong>{paper?.positions.length ?? 0}</strong><span>AÇIK / 3</span></div><small>{paperBot?.events?.[0]?.message ?? paperMessage}</small></div>
      </div>
      <div className="v20ProfileRail panel"><div><small>AKTİF OTONOM PROFİL</small><h3>{paperBot?.mode ?? 'V25.1 DENGELİ OTONOM PAPER'}</h3><p>Profil; tarama evrenini, Stop başına risk bütçesini ve toplam sanal maruziyeti birlikte sınırlar.</p></div>{(['TEMKINLI','DENGELI','HIZLI'] as const).map(profile => <button key={profile} className={paperBot?.profile === profile ? 'v20ProfileActive' : ''} disabled={paperBusy} onClick={() => selectV20Profile(profile)}><b>{profile === 'TEMKINLI' ? 'TEMKİNLİ' : profile === 'DENGELI' ? 'DENGELİ' : 'HIZLI'}</b><span>{profile === 'TEMKINLI' ? '18 coin · azami %8' : profile === 'DENGELI' ? '24 coin · azami %15' : '30 coin · azami %18'}</span></button>)}<button className={paperBot?.enabled ? 'v20Stop' : 'v20Start'} disabled={paperBusy} onClick={togglePaperBot}>{paperBot?.enabled ? '■ OTONOM AVCIYI DURDUR' : '▶ OTONOM AVCIYI BAŞLAT'}</button><button type="button" className="v20Demo" disabled={paperBusy || (paper?.positions.length ?? 0) >= 3} onClick={openDemoNow}>＋ {symbol.replace('USDT','/USDT')} MANUEL DEMO</button></div>
      <div className="v251Autonomy">
        <div className="panel v251Radar"><div className="v20SectionHead"><div><span>CANLI OTONOM KISA LİSTE</span><h3>Coin Avcısı</h3></div><b>{paperBot?.autonomy?.shortlist?.length ?? 0} ADAY</b></div><div className="v251Metrics"><span><small>TARANAN EVREN</small><b>{paperBot?.autonomy?.universe_size ?? 24} coin</b></span><span><small>STOP RİSK BÜTÇESİ</small><b>%{paperBot?.autonomy?.risk_per_trade_pct ?? 0.3}</b></span><span><small>TEK İŞLEM TAVANI</small><b>%{paperBot?.autonomy?.max_allocation_pct ?? 15}</b></span><span><small>TOPLAM MARUZİYET</small><b>%{paperBot?.autonomy?.current_exposure_pct ?? 0} / %{paperBot?.autonomy?.max_total_exposure_pct ?? 45}</b></span></div><div className="v251CandidateHead"><span># / PARİTE</span><span>YÖN</span><span>GÜVEN</span><span>EDGE</span><span>DURUM</span></div><div className="v251CandidateList">{paperBot?.autonomy?.shortlist?.length ? paperBot.autonomy.shortlist.slice(0,6).map(candidate => <article className={candidate.eligible ? 'candidateReady' : ''} key={candidate.symbol}><span><i>{candidate.rank}</i><b>{candidate.display.replace('USDT','/USDT')}</b><small>{fmt(candidate.price)} · Hacim {candidate.volume_ratio.toFixed(2)}x</small></span><em className={candidate.direction === 'LONG' ? 'up' : 'down'}>{candidate.direction}</em><strong>%{candidate.confidence}</strong><strong>{candidate.edge_score.toFixed(1)}</strong><small>{candidate.status}</small></article>) : <div className="v251Waiting"><RadioTower/><span>Otonom avcı başlatıldığında likit coinler burada puanlanacak.</span></div>}</div>
        </div>
        <aside className="panel v251Capital"><div className="v20SectionHead"><div><span>DİNAMİK SERMAYE</span><h3>Riskten Boyutlandırma</h3></div><b>PAPER ONLY</b></div><div className="v251Goal"><div style={{background:`conic-gradient(#16aa61 0 ${paperBot?.autonomy?.daily_reference?.progress_pct ?? 0}%,#e8edd8 0)`}}><span><strong>%{paperBot?.autonomy?.daily_reference?.progress_pct ?? 0}</strong><small>5 USDT REFERANS</small></span></div><p><small>BUGÜN GERÇEKLEŞEN</small><b className={(paperBot?.autonomy?.daily_reference?.realized_pnl_usdt ?? 0) >= 0 ? 'up' : 'down'}>{fmt(paperBot?.autonomy?.daily_reference?.realized_pnl_usdt)} USDT</b><em>Hedef değil, performans gözlemi</em></p></div>{paperBot?.autonomy?.last_allocation ? <div className="v251Allocation"><header><span><b>{paperBot.autonomy.last_allocation.display?.replace('USDT','/USDT') ?? paperBot.autonomy.last_allocation.symbol}</b><small>{paperBot.autonomy.last_allocation.direction} · %{paperBot.autonomy.last_allocation.confidence ?? 0} güven</small></span><strong className={paperBot.autonomy.last_allocation.approved ? 'up' : 'waiting'}>{paperBot.autonomy.last_allocation.status}</strong></header><div><span><small>SANAL TAHSİS</small><b>{fmt(paperBot.autonomy.last_allocation.amount)} USDT</b></span><span><small>PLAN NET SENARYOSU</small><b>{fmt(paperBot.autonomy.last_allocation.projected_plan_net_usdt)} USDT</b></span><span><small>STOP SENARYOSU</small><b className="down">-{fmt(paperBot.autonomy.last_allocation.projected_stop_loss_usdt)} USDT</b></span><span><small>STOP MESAFESİ</small><b>%{paperBot.autonomy.last_allocation.stop_distance_pct ?? 0}</b></span></div><p>{paperBot.autonomy.last_allocation.reason}</p></div> : <div className="v251Waiting"><BrainCircuit/><span>İlk uygun adaydan sonra tahsis, plan neti ve Stop senaryosu burada görünecek.</span></div>}<footer><ShieldCheck/><span><b>KÂR GARANTİSİ YOK</b>Güvenlik kapıları geçilmezse bot işlem açmaz; günlük 5 USDT için risk zorlamaz.</span></footer></aside>
      </div>
      <div className="v20PositionStage">
        <div className="panel v20Positions"><div className="v20SectionHead"><div><span>CANLI PAPER POZİSYONLARI</span><h3>İşlem Yaşam Döngüsü</h3></div><b>{paper?.positions.length ?? 0} AKTİF</b></div>{paper?.positions.length ? <div className="v20PositionGrid">{paper.positions.map(position => { const hitCount = position.partial_targets_hit?.length ?? 0; const target = position.tp3 ?? position.take_profit; const risk = Math.abs(position.entry_price - (position.initial_stop_loss ?? position.stop_loss)); const progress = risk > 0 ? Math.max(0, Math.min(100, (position.direction === 'LONG' ? position.current_price - position.entry_price : position.entry_price - position.current_price) / (risk * 3) * 100)) : 0; return <article key={position.id}><header><span className={position.direction === 'LONG' ? 'v20Long' : 'v20Short'}>{position.direction}</span><div><b>{position.symbol.replace('USDT','/USDT')}</b><small>{position.entry_order_type ?? position.source} · {fmt(position.original_amount ?? position.amount)} USDT</small></div><strong className={position.unrealized_pnl >= 0 ? 'up' : 'down'}>{position.unrealized_pnl >= 0 ? '+' : ''}{fmt(position.unrealized_pnl)} USDT</strong></header><div className="v20TargetTrack"><i style={{width:`${progress}%`}}/><span className={hitCount >= 1 ? 'hit' : ''}>TP1</span><span className={hitCount >= 2 ? 'hit' : ''}>TP2</span><span className={hitCount >= 3 ? 'hit' : ''}>TP3</span></div><div className="v20Levels"><span><small>Giriş</small><b>{fmt(position.entry_price)}</b></span><span><small>Stop</small><b className="down">{fmt(position.stop_loss)}</b></span><span><small>TP1</small><b>{fmt(position.tp1)}</b></span><span><small>TP2</small><b>{fmt(position.tp2)}</b></span><span><small>TP3</small><b>{fmt(target)}</b></span></div><div className="v20PositionFoot"><span>{position.protection_status ?? 'PLAN KORUNUYOR'}</span><b>Kısmi PnL {fmt(position.partial_realized_pnl)} USDT</b><button className="v20MapButton" type="button" onClick={() => openPositionMap(position)}>HARİTA</button><button disabled={paperBusy} onClick={() => closePaper(position.id)}>MANUEL KAPAT</button></div></article>})}</div> : <div className="v20Empty"><Bot/><b>Henüz açık demo işlem yok</b><span>“Demo Aç” düğmesine bas veya Paper Autopilot’u başlat; açılan işlem bu ekranda TP1–TP3 yolculuğuyla görünecek.</span></div>}</div>
        <aside className="panel v20Timeline"><div className="v20SectionHead"><div><span>CANLI OLAY AKIŞI</span><h3>Autopilot Günlüğü</h3></div><b>{paperBot?.cycles ?? 0} TUR</b></div><div>{paperBot?.events?.slice(0,8).map((event,index) => <article key={`${event.created_at}-${index}`}><i/><span><b>{event.kind}</b><small>{stamp(event.created_at)} · {event.symbol ?? 'SİSTEM'}</small><p>{event.message}</p></span></article>) ?? <p className="v20EmptyText">İlk tarama olayı bekleniyor.</p>}</div><footer><ShieldCheck/><span><b>ÇİFT FİZİKSEL KİLİT</b>Gerçek ve Testnet emirleri kapalı.</span></footer></aside>
      </div>
    </section>

    <section className="v20Deck v20Ghost" aria-label="V20 Hayalet İkiz">
      <div className="v20GhostHero panel"><div><span className="v20Eyebrow">V20 · HAYALET İKİZ</span><h2>Botun “hayır” dediği sinyaller sonra ne yaptı?</h2><p>Engellenen kararları 15, 30 ve 60 dakika sonra yeniden ölçer; kalkanın koruduğu kayıpları ve kaçan fırsatları aynı tabloda gösterir.</p></div><div className={(v20Command?.ghost_twin.shield_save_rate_pct ?? 0) >= 50 ? 'ghostDial good' : 'ghostDial'} style={{background:`conic-gradient(#12a35b 0 ${v20Command?.ghost_twin.shield_save_rate_pct ?? 0}%,#ecf0dc 0)`}}><span><strong>%{v20Command?.ghost_twin.shield_save_rate_pct ?? 0}</strong><small>KALKAN UYUMU</small></span></div></div>
      <div className="v20GhostMetrics"><div className="panel"><small>ALINAN PAPER İŞLEM</small><b>{v20Command?.ghost_twin.taken_trades ?? 0}</b><span className={(v20Command?.ghost_twin.taken_pnl_usdt ?? 0) >= 0 ? 'up' : 'down'}>{fmt(v20Command?.ghost_twin.taken_pnl_usdt)} USDT</span></div><div className="panel"><small>İNCELENEN ENGEL</small><b>{v20Command?.ghost_twin.blocked_reviewed ?? 0}</b><span>Karşı-olasılık örneği</span></div><div className="panel"><small>KALKAN KURTARIŞI</small><b className="up">{v20Command?.ghost_twin.shield_saves ?? 0}</b><span>Lehte gelişmeyen sinyal</span></div><div className="panel"><small>KAÇAN FIRSAT</small><b className="waiting">{v20Command?.ghost_twin.missed_opportunities ?? 0}</b><span>Eşik kalibrasyon adayı</span></div><div className="panel"><small>NET KARŞI-OLASILIK</small><b className={(v20Command?.ghost_twin.counterfactual_edge_pct ?? 0) >= 0 ? 'up' : 'down'}>{(v20Command?.ghost_twin.counterfactual_edge_pct ?? 0) >= 0 ? '+' : ''}%{v20Command?.ghost_twin.counterfactual_edge_pct ?? 0}</b><span>{v20Command?.ghost_twin.status ?? 'KANIT TOPLUYOR'}</span></div></div>
      <div className="panel v20GhostTable"><div className="v20SectionHead"><div><span>KARŞI-OLASILIK KAYITLARI</span><h3>Alınmayan İşlemlerin Sonucu</h3></div><b>{v20Command?.ghost_twin.status ?? 'ÖĞRENİYOR'}</b></div><div className="ghostTableHead"><span>PARİTE / YÖN</span><span>ENGEL NEDENİ</span><span>GÖZLEM</span><span>SONUÇ</span></div>{v20Command?.ghost_twin.rows.length ? v20Command.ghost_twin.rows.map((row,index) => <div className="ghostTableRow" key={`${row.created_at}-${index}`}><span><b>{row.symbol}</b><small className={row.direction === 'LONG' ? 'up' : 'down'}>{row.direction} · {stamp(row.created_at)}</small></span><p>{row.reason}</p><span><b>{row.review_minutes} dk</b><small className={row.counterfactual_return_pct > 0 ? 'up' : 'down'}>{row.counterfactual_return_pct > 0 ? '+' : ''}%{row.counterfactual_return_pct.toFixed(3)}</small></span><strong className={row.outcome === 'KALKAN KORUDU' ? 'up' : 'waiting'}>{row.outcome}</strong></div>) : <div className="v20Empty"><BrainCircuit/><b>Karşı-olasılık kanıtı toplanıyor</b><span>Paper Autopilot karar verdikçe bu tablo 15/30/60 dakika sonra kendiliğinden dolacak.</span></div>}<footer>{v20Command?.ghost_twin.method_note ?? 'Bu ölçüm analiz içindir; performans garantisi değildir.'}</footer></div>
    </section>

    <section className="v20Deck v20Certification" aria-label="V20 Güvenlik Sertifikası">
      <div className="v20CertHero panel"><div><span className="v20Eyebrow">V20 · PAPER KANIT SERTİFİKASI</span><h2>{v20Command?.certificate.status ?? 'PAPER KANITI TOPLANIYOR'}</h2><p>Botun güvenilirliği tek bir güzel sinyalle değil; kapanmış işlemler, düşüş, Profit Factor ve engellenen karar sonuçlarıyla ölçülür.</p></div><div className="certScore" style={{background:`conic-gradient(#16a45d 0 ${v20Command?.certificate.score ?? 0}%,#edf0de 0)`}}><span><strong>%{v20Command?.certificate.score ?? 0}</strong><small>{v20Command?.certificate.passed_gates ?? 0}/{v20Command?.certificate.total_gates ?? 6} KAPI</small></span></div><div className="certLocks"><span><ShieldCheck/> PAPER MOTORU <b>{v20Command?.certificate.paper_ready ? 'KANITLI' : 'EĞİTİMDE'}</b></span><span><Shield/> TESTNET <b>KİLİTLİ</b></span><span><Shield/> GERÇEK PARA <b>KİLİTLİ</b></span></div></div>
      <div className="v20CertGrid"><div className="panel certGates"><div className="v20SectionHead"><div><span>YAYIN KAPILARI</span><h3>Kanıt Kontrol Listesi</h3></div><b>{v20Command?.certificate.passed_gates ?? 0} / {v20Command?.certificate.total_gates ?? 6}</b></div>{v20Command?.certificate.gates.map(gate => <div className={gate.passed ? 'certGate passed' : 'certGate'} key={gate.key}><i>{gate.passed ? '✓' : '•'}</i><span><b>{gate.label}</b><small>{gate.value}</small></span><em>{gate.passed ? 'GEÇTİ' : 'BEKLİYOR'}</em></div>)}</div><div className="panel certModules"><div className="v20SectionHead"><div><span>TEK PAKET ENVANTERİ</span><h3>V6–V20 Motorları</h3></div><b>7 MODÜL</b></div>{v20Command?.modules.map(module => <div key={module.version}><span>{module.version}</span><p><b>{module.name}</b><small>{module.status}</small></p><i className={module.active ? 'moduleOn' : ''}>{module.active ? 'AKTİF' : 'HAZIR'}</i></div>)}</div><aside className="panel certTestnet"><div className="v20SectionHead"><div><span>SANDBOX HAZIRLIĞI</span><h3>Testnet Adayı</h3></div><b>{v20Command?.testnet.status ?? 'KİLİTLİ'}</b></div>{v20Command?.testnet.checks.map(check => <span key={check.label} className={check.passed ? 'passed' : ''}><i>{check.passed ? '✓' : '•'}</i><b>{check.label}</b><small>{check.status}</small></span>)}<p>{v20Command?.certificate.reason ?? 'Emir kanalları fiziksel olarak kapalıdır.'}</p><button disabled>TESTNET + GERÇEK EMİR KİLİTLİ</button></aside></div>
    </section>
    <section className="panel systemHealth"><div className="systemHead"><div><h3>◈ SİSTEM SAĞLIK MERKEZİ</h3><small>{systemHealth?.message ?? 'Servis durumu denetleniyor…'}</small></div><b className={healthClass(systemHealth?.self_healing)}>{systemHealth?.self_healing ?? 'KONTROL'}</b></div><div className="healthNodes"><div className={healthClass(systemHealth?.api)}><span>API</span><b>{systemHealth?.api ?? '—'}</b></div><div className={streamLive ? 'healthOk' : 'healthPending'}><span>Canlı Akış</span><b>{streamLive ? 'BAĞLI' : 'BEKLENİYOR'}</b></div><div className={healthClass(systemHealth?.database)}><span>TimescaleDB</span><b>{systemHealth?.database ?? '—'}</b></div><div className={healthClass(systemHealth?.redis)}><span>Redis</span><b>{systemHealth?.redis ?? '—'}</b></div><div className={healthClass(systemHealth?.paper_storage)}><span>Paper Cüzdan</span><b>{systemHealth?.paper_storage ?? '—'}</b></div><div className={healthClass(systemHealth?.paper_bot)}><span>Paper Bot</span><b>{systemHealth?.paper_bot ?? '—'}</b></div><div className={healthClass(systemHealth?.grid_engine)}><span>V6 Grid Motoru</span><b>{systemHealth?.grid_engine ?? 'BEKLEMEDE'}</b></div><div className={healthClass(systemHealth?.strategy_orchestrator)}><span>V7 Orkestra</span><b>{systemHealth?.strategy_orchestrator ?? 'BEKLEMEDE'}</b></div><div className={healthClass(systemHealth?.future_lab)}><span>V8 Gelecek Lab</span><b>{systemHealth?.future_lab ?? 'BEKLEMEDE'}</b></div><div className={healthClass(systemHealth?.market_twin)}><span>V9 Borsa İkizi</span><b>{systemHealth?.market_twin ?? 'BEKLEMEDE'}</b></div><div className={healthClass(systemHealth?.strategy_evolution)}><span>V10 Evrim Lab</span><b>{systemHealth?.strategy_evolution ?? 'BEKLEMEDE'}</b></div><div className={healthClass(systemHealth?.portfolio_risk)}><span>V11 Risk Beyni</span><b>{systemHealth?.portfolio_risk ?? 'BEKLEMEDE'}</b></div><div className="healthPending"><span>Testnet</span><b>{systemHealth?.testnet ?? 'HAZIRLIK'}</b></div></div></section>
    <section className="commandDeck">
      <div className="panel sessionPanel"><div className="commandHead"><div><h3>◈ SEANS HAFIZASI</h3><small>{sessionIntelligence?.scope ?? 'Paper sonuçları ölçülüyor…'}</small></div><b className={sessionIntelligence?.current_session.status === 'SEÇİCİ' ? 'down' : sessionIntelligence?.current_session.status === 'KANITLI' ? 'up' : 'waiting'}>{sessionIntelligence?.current_session.status ?? 'ÖĞRENİYOR'}</b></div><div className="commandMetrics"><div><span>Aktif Seans</span><b>{sessionIntelligence?.current_session.key ?? '—'}</b></div><div><span>Ek Güven</span><b>{sessionIntelligence ? `+${sessionIntelligence.confidence_bonus}` : '—'}</b></div><div><span>Örneklem</span><b>{sessionIntelligence?.sample_size ?? '—'}</b></div></div><div className="sessionChips">{sessionIntelligence?.sessions.map(item => <span key={item.key} className={item.status === 'SEÇİCİ' ? 'down' : item.status === 'KANITLI' ? 'up' : 'waiting'}>{item.key} %{item.win_rate.toFixed(0)}</span>)}</div><small className="commandNote">{sessionIntelligence?.reason ?? 'UTC seanslarına göre işlem hafızası hazırlanıyor…'}</small></div>
      <div className="panel reportPanel"><div className="commandHead"><div><h3>◈ GÜNLÜK RAPOR & BİLDİRİMLER</h3><small>{dailyReport?.date ?? 'Rapor hazırlanıyor…'}</small></div><b className={dailyReport?.emergency_active ? 'down' : dailyReport?.today_pnl && dailyReport.today_pnl > 0 ? 'up' : 'waiting'}>{dailyReport?.status ?? 'İZLEMEDE'}</b></div><div className="commandMetrics"><div><span>Günlük PnL</span><b className={(dailyReport?.today_pnl || 0) >= 0 ? 'up' : 'down'}>{fmt(dailyReport?.today_pnl)} USDT</b></div><div><span>Kapanan</span><b>{dailyReport?.closed_trades ?? '—'}</b></div><div><span>Gölge Kayıt</span><b>{dailyReport?.shadow_records ?? '—'}</b></div></div><div className="notificationLine"><b>{dailyReport?.notifications?.[0]?.kind ?? 'BİLDİRİM BEKLİYOR'}</b><span>{dailyReport?.notifications?.[0]?.message ?? dailyReport?.headline ?? 'Sistem uyarıları burada görünür.'}</span></div></div>
      <div className="panel testnetPanel"><div className="commandHead"><div><h3>◈ BINANCE TESTNET HAZIRLIK</h3><small>Gerçek para ve gerçek emir yok</small></div><b className="waiting">{testnet?.status ?? 'KONTROL'}</b></div><div className="commandMetrics"><div><span>Anahtar</span><b>{testnet?.credentials_configured ? 'ALGILANDI' : 'BEKLİYOR'}</b></div><div><span>Emir</span><b className="down">KAPALI</b></div><div><span>Mod</span><b>TESTNET</b></div></div><div className="testnetChecks">{testnet?.checks.map(check => <span key={check.label} className={check.passed ? 'up' : 'waiting'}>{check.passed ? '✓' : '•'} {check.label}</span>)}</div><small className="commandNote">{testnet?.reason ?? 'Testnet hazırlığı denetleniyor…'}</small><button disabled>TESTNET EMİRLERİ KAPALI</button></div>
    </section>
    <section className="v4Deck">
      <div className="panel blackboxPanel"><div className="v4Head"><div><h3>◈ KARAR KARA KUTUSU</h3><small>Açılan, Gölge Moduna alınan ve engellenen sinyallerin sonraki fiyat gözlemi</small></div><b className={blackbox?.status === 'KALKAN TUTARLI' ? 'up' : blackbox?.status === 'FIRSAT MALİYETİ İZLENİYOR' ? 'down' : 'waiting'}>{blackbox?.status ?? 'ÖĞRENİYOR'}</b></div><div className="blackboxMetrics"><div><span>Kayıt</span><b>{blackbox?.records ?? '—'}</b></div><div><span>Engellenen</span><b>{blackbox?.blocked ?? '—'}</b></div><div><span>Kalkan Uyumu</span><b className={(blackbox?.shield_accuracy_pct || 0) >= 60 ? 'up' : 'waiting'}>%{blackbox?.shield_accuracy_pct ?? '—'}</b></div><div><span>İzlemede</span><b>{blackbox?.pending ?? '—'}</b></div></div><div className="blackboxEvents">{blackbox?.events.length ? blackbox.events.slice(0,4).map(event => <div key={event.id}><span><b>{event.symbol}</b> <em className={event.direction === 'LONG' ? 'up' : 'down'}>{event.direction}</em> · {event.decision}</span><strong className={event.latest_review?.return_pct && event.latest_review.return_pct > 0 ? 'up' : event.latest_review?.return_pct && event.latest_review.return_pct < 0 ? 'down' : 'waiting'}>{event.latest_review ? `${event.latest_review.minutes}dk ${event.latest_review.return_pct >= 0 ? '+' : ''}${event.latest_review.return_pct.toFixed(2)}%` : 'GÖZLEMDE'}</strong><small>{event.reason}</small></div>) : <div className="blackboxEmpty">Paper Bot karar verdikçe burada 15/30/60 dakika sonrası karşı-olasılık kaydı oluşur.</div>}</div><p>{blackbox?.summary ?? 'Karar hafızası hazırlanıyor…'} {blackbox?.method_note}</p></div>
      <div className="panel v4SafetyPanel"><div className="v4Head"><div><h3>◈ V11 ÇİFT KİLİTLİ RİSK KAPISI</h3><small>Monte Carlo kuyruk riski, korelasyon ve dört stres senaryosu birlikte denetlenir</small></div><b className="waiting">EMİR KİLİTLİ</b></div><div className="safetySteps"><span>1. Risk yalnızca kapanmış mumlardan hesaplanır</span><span>2. Kritik veto sadece yerel Paper motorlarını durdurur</span><span>3. Gerçek ve Testnet emir kanalları hiç açılmaz</span></div><small>{v11Risk?.safety_note ?? v11Report?.safety_note ?? 'Portföy risk kapıları doğrulanıyor…'}</small><button disabled>GERÇEK VE TESTNET EMRİ KAPALI</button></div>
    </section>
    <section className="v11Deck">
      <div className="panel v11Command">
        <div className="v11Head"><div><span className="v11Tag">V11 AMİRAL</span><h3>OTONOM RİSK BEYNİ & PORTFÖY KOMUTANI</h3><small>Çoklu varlık korelasyonu · deterministik Monte Carlo · CVaR · otomatik Paper risk vetosu</small></div><b className={v11Risk?.enabled ? 'v11Running' : 'v11Idle'}><i/>{v11Risk?.busy ? 'RİSK HESAPLANIYOR' : v11Risk?.status ?? 'ONAY BEKLİYOR'}</b></div>
        <div className="v11Hero">
          <div className="v11RiskDial" style={{background:`conic-gradient(${v11RiskColor} 0 ${v11Report?.risk_score ?? 0}%,#f0f2df 0)`}}><span><strong>{v11Report?.risk_score ?? '—'}</strong><small>/99 PORTFÖY RİSKİ</small></span></div>
          <div><span>RİSK SEVİYESİ</span><strong style={{color:v11RiskColor}}>{v11Report?.risk_level ?? 'ÖLÇÜLÜYOR'}</strong><small>{v11Report?.paper_action ?? 'Paper politika bekliyor'}</small></div>
          <div><span>VaR %95</span><strong>%{v11Report?.monte_carlo.var_95_pct ?? '—'}</strong><small>{fmt(v11Report?.monte_carlo.var_95_usdt)} USDT olası eşik</small></div>
          <div><span>CVaR %95</span><strong className={(v11Report?.monte_carlo.cvar_95_pct ?? 0) > 5 ? 'down' : 'up'}>%{v11Report?.monte_carlo.cvar_95_pct ?? '—'}</strong><small>Kötü kuyruğun ortalama kaybı</small></div>
          <div><span>İFLAS OLASILIĞI</span><strong className={(v11Report?.monte_carlo.ruin_probability_pct ?? 0) > 2 ? 'down' : 'up'}>%{v11Report?.monte_carlo.ruin_probability_pct ?? '—'}</strong><small>{v11Report?.monte_carlo.simulations ?? 500} deterministik yol</small></div>
          <div><span>NAKİT REZERVİ</span><strong>{fmt(v11Report?.cash_reserve_usdt)} USDT</strong><small>Maruziyet %{v11Report?.exposure_ratio_pct ?? 0}</small></div>
        </div>
        <div className="v11AllocationTable">
          <div className="v11AllocationLabels"><span>PARİTE</span><span>RİSK PARİTESİ</span><span>PAPER BÜTÇESİ</span><span>VOLATİLİTE</span><span>KORELASYON</span><span>RİSK KATKISI</span><span>KÜME</span><span>KARAR</span></div>
          {v11Report?.allocations.map(item => <div className="v11AllocationRow" key={item.symbol}><span><b>{item.symbol.replace('USDT','/USDT')}</b></span><span><i style={{width:`${item.weight_pct}%`}}/><b>%{item.weight_pct}</b></span><strong>{fmt(item.paper_budget_usdt)} USDT</strong><span>%{item.volatility_pct}</span><span>%{item.average_correlation_pct}</span><span>%{item.risk_contribution_pct}</span><span>{item.cluster}</span><em className={item.status.includes('İNDİRİM') ? 'waiting' : 'up'}>{item.status}</em></div>)}
          {!v11Report?.allocations.length && <div className="v11Empty">En az iki piyasanın kapanmış mumlarıyla risk-paritesi dağılımı hazırlanıyor…</div>}
        </div>
        <div className="v11Controls"><div><span>Sanal portföy</span>{[2500,5000,10000].map(value => <button key={value} className={v11Capital === value ? 'v11CapitalActive' : ''} disabled={v11Risk?.enabled} onClick={() => setV11Capital(value)}>{value.toLocaleString('tr-TR')} USDT</button>)}</div><button className={v11Risk?.enabled ? 'v11Stop' : 'v11Start'} disabled={v11Busy} onClick={toggleV11Risk}>{v11Risk?.enabled ? '■ RİSK BEYNİNİ DURDUR' : '▶ V11 RİSK BEYNİNİ BAŞLAT'}</button><span><b>0 BORSA EMRİ</b><small>{v11Message}</small></span><em>Son ölçüm {stamp(v11Risk?.last_tick_at ?? v11Report?.generated_at)}</em></div>
        <p>{v11Report?.summary ?? 'V11 bütün Paper portföyünü tek bir risk bütçesi altında ölçmeye hazırlanıyor.'} <b>Bu ölçüm kesin tahmin veya getiri garantisi değildir.</b></p>
      </div>

      <div className="panel v11Monte">
        <div className="v11SideHead"><div><h3>MONTE CARLO KUYRUK RADARI</h3><small>{v11Report?.monte_carlo.horizon_candles ?? 24} mum · ortak tarih indeksli bootstrap</small></div><b className={(v11Report?.monte_carlo.expected_return_pct ?? 0) >= 0 ? 'up' : 'down'}>{(v11Report?.monte_carlo.expected_return_pct ?? 0) >= 0 ? '+' : ''}%{v11Report?.monte_carlo.expected_return_pct ?? 0}</b></div>
        <div className="v11Distribution">{v11Report?.monte_carlo.distribution.map(item => <div key={item.label}><span>{item.label}</span><i><b style={{width:`${Math.max(2,item.percentage)}%`}}/></i><strong>%{item.percentage}</strong></div>) ?? <small>Olasılık dağılımı hesaplanıyor…</small>}</div>
        <div className="v11MiniMetrics"><span><small>Kayıp olasılığı</small><b>%{v11Report?.monte_carlo.probability_loss_pct ?? '—'}</b></span><span><small>%5 düşüş yolu</small><b>%{v11Report?.monte_carlo.probability_drawdown_5_pct ?? '—'}</b></span><span><small>En kötü yol</small><b className="down">%{v11Report?.monte_carlo.worst_path_pct ?? '—'}</b></span><span><small>En iyi yol</small><b className="up">+%{v11Report?.monte_carlo.best_path_pct ?? '—'}</b></span></div>
        <small className="v11Note">{v11Report?.monte_carlo.method_note ?? 'Aynı tarih anı bütün coinlerde birlikte örneklenerek korelasyon korunur.'}</small>
      </div>

      <div className="panel v11Correlation">
        <div className="v11SideHead"><div><h3>KORELASYON DEPREM RADARI</h3><small>Birlikte düşebilecek varlık kümeleri</small></div><b className={(v11Report?.correlation_matrix.average_abs_correlation_pct ?? 0) >= 70 ? 'down' : 'up'}>%{v11Report?.correlation_matrix.average_abs_correlation_pct ?? 0}</b></div>
        <div className="v11Heatmap">
          <div className="v11HeatHead" style={{gridTemplateColumns:`36px repeat(${v11Report?.correlation_matrix.symbols.length ?? 4},minmax(28px,1fr))`}}><span/>{v11Report?.correlation_matrix.symbols.map(item => <b key={item}>{item.replace('USDT','')}</b>)}</div>
          {v11Report?.correlation_matrix.rows.map(row => <div className="v11HeatRow" style={{gridTemplateColumns:`36px repeat(${v11Report.correlation_matrix.symbols.length},minmax(28px,1fr))`}} key={row.symbol}><b>{row.symbol.replace('USDT','')}</b>{row.values.map((value,index) => <span key={`${row.symbol}-${index}`} style={{background:value === 100 ? '#dff2c3' : `rgba(229,184,0,${Math.max(.08,Math.abs(value)/125)})`}}>{value}</span>)}</div>)}
          {!v11Report && <small>Korelasyon matrisi hazırlanıyor…</small>}
        </div>
        <div className="v11Clusters">{v11Report?.clusters.map(cluster => <div key={cluster.id} className={cluster.size > 1 ? 'v11ClusterHot' : ''}><b>{cluster.id} · {cluster.status}</b><span>{cluster.members.map(item => item.replace('USDT','')).join(' + ')}</span><small>İç korelasyon %{cluster.average_correlation_pct}</small></div>)}</div>
      </div>

      <div className="panel v11Stress">
        <div className="v11SideHead"><div><h3>DÖRT KARA-KUĞU TATBİKATI</h3><small>Bütçe dağıtılmadan önce zorunlu dayanıklılık kontrolü</small></div><b className={v11Report?.worst_scenario.status === 'KRİTİK' ? 'down' : 'waiting'}>{v11Report?.worst_scenario.status ?? 'BEKLİYOR'}</b></div>
        <div className="v11StressGrid">{v11Report?.stress_scenarios.map(item => <div key={item.label} className={item.status === 'KRİTİK' ? 'v11StressCritical' : item.status === 'UYARI' ? 'v11StressWarn' : 'v11StressSafe'}><span><b>{item.label}</b><small>{item.description}</small></span><strong>%{item.portfolio_impact_pct}</strong><em>-{fmt(item.loss_usdt)} USDT</em></div>) ?? <small>Stres senaryoları çalıştırılıyor…</small>}</div>
      </div>

      <div className="panel v11Fingerprint">
        <div className="v11SideHead"><div><h3>PORTFÖY RİSK PARMAK İZİ</h3><small>Beş eksende açıklanabilir risk kimliği</small></div><b>{v11Report?.diversification_score ?? 0}/99 ÇEŞİTLİLİK</b></div>
        <div className="v11FingerprintBars">{v11Report?.risk_fingerprint.map(item => <div key={item.key}><span>{item.label}</span><i><b style={{width:`${item.score}%`,background:item.score >= 68 ? '#e8533f' : item.score >= 40 ? '#e5b800' : '#159653'}}/></i><strong>{item.score}</strong></div>) ?? <small>Risk parmak izi hazırlanıyor…</small>}</div>
        <div className="v11Reserve"><span><small>Yatırılabilir Paper bütçesi</small><b>{fmt(v11Report?.invested_budget_usdt)} USDT</b></span><i>+</i><span><small>Korunan nakit rezervi</small><b>{fmt(v11Report?.cash_reserve_usdt)} USDT</b></span><strong>= {fmt(v11Report?.capital)} USDT</strong></div>
      </div>

      <div className="panel v11Veto">
        <div className="v11SideHead"><div><h3>OTOMATİK PAPER RİSK VETOSU</h3><small>V6, V7 ve Paper Bot için sermaye emniyet şalteri</small></div><b className={v11Risk?.intervention.active ? 'down' : 'up'}>{v11Risk?.intervention.status ?? 'HAZIR'}</b></div>
        <div className="v11Gates">{v11Report?.gates.map(gate => <span key={gate.key} className={gate.passed ? 'v11GatePass' : 'v11GateBlock'}><b>{gate.passed ? '✓' : '×'} {gate.label}</b><small>{gate.detail}</small></span>) ?? <small>Altı güvenlik kapısı hazırlanıyor…</small>}</div>
        <div className="v11Intervention"><b>{v11Risk?.events?.[0]?.kind ?? 'MÜDAHALE YOK'}</b><span>{v11Risk?.intervention.reason ?? 'Risk kritik seviyeye gelirse yalnızca yerel Paper motorları otomatik durur.'}</span><small>{v11Risk?.intervention.stopped_engines?.join(' · ') || 'Gerçek/Testnet emirleri kalıcı olarak kapalı'}</small></div>
        <button disabled={v11Busy || !v11Risk?.intervention.active || v11Report?.veto_required} onClick={resetV11Intervention}>↺ RİSK NORMALE DÖNDÜĞÜNDE VETOYU SIFIRLA</button>
      </div>
    </section>
    <section className="v10Deck">
      <div className="panel v10EvolutionHub">
        <div className="v10Head"><div><span className="v10Tag">V10 AMİRAL</span><h3>YAPAY ZEKÂ STRATEJİ EVRİM LABORATUVARI</h3><small>12 strateji genomu · 3 kronolojik dönem · görünmeyen test · 2X maliyet stresi</small></div><b className={v10Evolution?.enabled ? 'v10Running' : 'v10Idle'}><i/>{v10Evolution?.busy ? 'TURNUVA ÇALIŞIYOR' : v10Evolution?.status ?? 'KANIT BEKLİYOR'}</b></div>
        <div className="v10Hero">
          <div><span>NESİL</span><strong>G{v10Evolution?.generation ?? v10Tournament?.generation ?? 1}</strong><small>{v10Evolution?.cycles ?? 0} tamamlanan canlı tur</small></div>
          <div><span>PAPER ŞAMPİYONU</span><strong className={v10Champion?.certified ? 'up' : 'waiting'}>{v10Champion?.label ?? 'KANIT BEKLİYOR'}</strong><small>{v10Champion ? `${v10Champion.family} · ${v10Champion.score}/99` : 'Terfi kapıları kapalı'}</small></div>
          <div><span>AKTİF REJİM</span><strong>{v10Tournament?.regime.label ?? 'ÖLÇÜLÜYOR'}</strong><small>{v10Tournament?.regime.preferred_family ?? '—'} ailesi · güç %{v10Tournament?.regime.strength ?? 0}</small></div>
          <div><span>TERFİ KARARI</span><strong className={v10Tournament?.promotion_ready ? 'up' : 'waiting'}>{v10Tournament?.promotion_status ?? 'KANIT TOPLUYOR'}</strong><small>Gerçek/Testnet emir: KAPALI</small></div>
        </div>
        <div className="v10Arena">
          <div className="v10ArenaLabels"><span>#</span><span>GENOM / AİLE</span><span>SKOR</span><span>3 DÖNEM</span><span>GÖRÜNMEYEN</span><span>2X MALİYET</span><span>AŞIRI ÖĞRENME</span><span>KARAR</span></div>
          {(v10Tournament?.leaderboard ?? v10Evolution?.leaderboard ?? []).slice(0,6).map(candidate => <div className="v10ArenaRow" key={candidate.id}><span>{candidate.rank}</span><span><b>{candidate.label}</b><small>{candidate.family} · {candidate.id}</small></span><strong>{candidate.score}</strong><span>{candidate.positive_folds}/3</span><span className={candidate.test_return_pct > 0 ? 'up' : 'down'}>{candidate.test_return_pct >= 0 ? '+' : ''}%{candidate.test_return_pct}</span><span className={candidate.stress_return_pct > 0 ? 'up' : 'down'}>{candidate.stress_return_pct >= 0 ? '+' : ''}%{candidate.stress_return_pct}</span><span className={candidate.overfit_risk < 55 ? 'up' : 'down'}>%{candidate.overfit_risk}</span><b className={candidate.certified ? 'v10Certified' : candidate.status.includes('RED') || candidate.status.includes('ELENDİ') ? 'v10Rejected' : 'v10Watching'}>{candidate.status}</b></div>)}
          {!v10Tournament?.leaderboard?.length && !v10Evolution?.leaderboard?.length && <div className="v10ArenaEmpty">Seçilen parite için 12 genomlu ilk Paper turnuvası hazırlanıyor…</div>}
        </div>
        <div className="v10Controls"><div><span>Sanal sermaye</span>{[500,1000,2000].map(value => <button key={value} className={v10Capital === value ? 'v10CapitalActive' : ''} disabled={v10Evolution?.enabled} onClick={() => setV10Capital(value)}>{value.toLocaleString('tr-TR')} USDT</button>)}</div><button className={v10Evolution?.enabled ? 'v10Stop' : 'v10Start'} disabled={v10Busy} onClick={toggleV10Evolution}>{v10Evolution?.enabled ? '■ PAPER EVRİMİ DURDUR' : '▶ V10 PAPER EVRİMİ BAŞLAT'}</button><span><b>0 BORSA EMRİ</b><small>{v10Message}</small></span><em>Son tur {stamp(v10Evolution?.last_tick_at ?? v10Tournament?.generated_at)}</em></div>
        <p>{v10Tournament?.explanation ?? 'V10, aday stratejileri farklı zaman dönemlerinde kanıtlamadan Paper şampiyonu yapmaz.'} <b>Geçmiş sonuç gelecek getiriyi garanti etmez.</b></p>
      </div>

      <div className="panel v10Overfit">
        <div className="v10SideHead"><div><h3>AŞIRI ÖĞRENME KALKANI</h3><small>Geliştirme başarısını görünmeyen dönemle karşılaştırır</small></div><b className={(v10Leader?.overfit_risk ?? 99) < 55 ? 'up' : 'down'}>%{v10Leader?.overfit_risk ?? 99}</b></div>
        <div className="v10Compare"><div><span>Geliştirme</span><b>{v10Leader ? `${v10Leader.train_return_pct >= 0 ? '+' : ''}%${v10Leader.train_return_pct}` : '—'}</b></div><i>→</i><div><span>Görünmeyen Test</span><b className={(v10Leader?.test_return_pct ?? 0) > 0 ? 'up' : 'down'}>{v10Leader ? `${v10Leader.test_return_pct >= 0 ? '+' : ''}%${v10Leader.test_return_pct}` : '—'}</b></div></div>
        <div className="v10Gates">{v10Tournament?.promotion_gates.map(gate => <span key={gate.key} className={gate.passed ? 'v10GatePass' : 'v10GateBlock'}><b>{gate.passed ? '✓' : '×'} {gate.label}</b><small>{gate.detail}</small></span>) ?? <small>Kanıt kapıları hazırlanıyor…</small>}</div>
      </div>

      <div className="panel v10WalkForward">
        <div className="v10SideHead"><div><h3>3 DÖNEMLİ ZAMAN TÜNELİ</h3><small>Her bölüm bağımsız ve kronolojik Paper tekrar</small></div><b>{v10Champion?.positive_folds ?? v10Leader?.positive_folds ?? 0}/3</b></div>
        <div className="v10Folds">{(v10Champion?.folds ?? v10Leader?.folds ?? []).map((fold,index) => <div key={fold.label}><i>{index + 1}</i><span><b>{fold.label}</b><small>{fold.trades} işlem · başarı %{fold.win_rate}</small></span><strong className={fold.net_return_pct > 0 ? 'up' : 'down'}>{fold.net_return_pct >= 0 ? '+' : ''}%{fold.net_return_pct}</strong></div>)}</div>
        <small className="v10Note">Son bölüm ayar üretiminde kullanılmaz; sahte başarıyı yakalamak için görünmeyen testtir.</small>
      </div>

      <div className="panel v10RegimeCouncil">
        <div className="v10SideHead"><div><h3>REJİM ŞAMPİYONLARI</h3><small>Tek strateji yerine piyasa karakterine özel uzman</small></div><b>{v10Tournament?.regime.label ?? 'ÖLÇÜLÜYOR'}</b></div>
        <div className="v10Regimes">{(['GRID','TREND','KIRILIM'] as const).map(family => { const item = v10Tournament?.regime_champions?.[family]; return <div key={family} className={v10Tournament?.regime.preferred_family === family ? 'v10RegimeActive' : ''}><span>{family}</span><b>{item?.label ?? 'Aday aranıyor'}</b><small>{item ? `${item.score}/99 · ${item.status}` : 'Kanıt bekliyor'}</small></div> })}</div>
        <p>{v10Tournament?.regime.reason ?? 'Piyasa rejimi kapanmış mumlardan ölçülüyor.'}</p>
      </div>

      <div className="panel v10MutationLab">
        <div className="v10SideHead"><div><h3>SONRAKİ NESİL MUTASYONLARI</h3><small>Şampiyonun çevresinde küçük, açıklanabilir ayar değişimleri</small></div><b>G{(v10Tournament?.generation ?? 0) + 1}</b></div>
        <div className="v10Mutations">{v10Tournament?.next_generation.map(item => <div key={item.id}><span><b>{item.label}</b><small>{item.id} · ebeveyn {item.parent_id}</small></span><strong>{Object.entries(item.params).slice(0,2).map(([key,value]) => `${key}:${value}`).join(' · ')}</strong></div>) ?? <small>İlk liderden sonra yeni nesil üretilecek.</small>}</div>
      </div>

      <div className="panel v10Rollback">
        <div className="v10SideHead"><div><h3>PAPER ŞAMPİYON HAFIZASI</h3><small>Yeni politika zayıflarsa güvenli önceki profile dönüş</small></div><b className={v10Evolution?.previous_champion ? 'up' : 'waiting'}>{v10Evolution?.previous_champion ? 'GERİ DÖNÜŞ HAZIR' : 'İLK ŞAMPİYON BEKLENİYOR'}</b></div>
        <div className="v10ChampionFlow"><span><small>ÖNCEKİ</small><b>{v10Evolution?.previous_champion?.label ?? '—'}</b></span><i>⇄</i><span><small>AKTİF PAPER</small><b>{v10Evolution?.active_champion?.label ?? 'Kanıt bekliyor'}</b></span></div>
        <button disabled={v10Busy || !v10Evolution?.previous_champion} onClick={rollbackV10Champion}>↶ ÖNCEKİ PAPER ŞAMPİYONUNA DÖN</button>
        <div className="v10Event"><b>{v10Evolution?.events?.[0]?.kind ?? 'OLAY BEKLİYOR'}</b><span>{v10Evolution?.events?.[0]?.message ?? 'Terfi ve geri dönüş kararları burada açıklanacak.'}</span></div>
      </div>
    </section>
    <section className="v9Deck">
      <div className="panel v9CommandCenter">
        <div className="v9Head"><div><span className="v9Tag">V9 AMİRAL</span><h3>CANLI DİJİTAL BORSA İKİZİ</h3><small>Çoklu WebSocket · TimescaleDB · boşluk onarımı · yalnızca Paper yürütme</small></div><b className={v9Twin?.stream_health === 'BAĞLI' ? 'v9Live' : v9Twin?.enabled ? 'v9Connecting' : 'v9Idle'}><i/>{v9Twin?.stream_health ?? 'BEKLEMEDE'}</b></div>
        <div className="v9Hero">
          <div><span>CANLI KAPSAMA</span><strong className={(v9Twin?.coverage_pct || 0) >= 75 ? 'up' : 'waiting'}>%{v9Twin?.coverage_pct ?? 0}</strong><small>{v9Twin?.symbols.filter(item => item.health === 'CANLI').length ?? 0}/{v9Twin?.universe.length ?? 0} parite güncel</small></div>
          <div><span>KAYDEDİLEN TICK</span><strong>{(v9Twin?.ticks_captured ?? 0).toLocaleString('tr-TR')}</strong><small>TimescaleDB: {v9Twin?.database ?? 'KONTROL'}</small></div>
          <div><span>VERİ KALİTESİ</span><strong className={(v9Twin?.daily_report.data_quality_pct || 0) >= 98 ? 'up' : 'waiting'}>%{v9Twin?.daily_report.data_quality_pct ?? 0}</strong><small>{v9Twin?.gap_count ?? 0} boşluk · {v9Twin?.recovered_candles ?? 0} mum onarıldı</small></div>
          <div><span>PAPER NET KATKI</span><strong className={(v9Twin?.pnl_attribution.net_pnl || 0) >= 0 ? 'up' : 'down'}>{fmt(v9Twin?.pnl_attribution.net_pnl)} USDT</strong><small>{v9Twin?.paper_fills.length ?? 0} V9 sanal dolum</small></div>
        </div>
        <div className="v9Markets">
          <div className="v9MarketLabels"><span>PARİTE</span><span>CANLI FİYAT</span><span>SPREAD</span><span>24S HACİM</span><span>GECİKME</span><span>SAĞLIK</span></div>
          {v9Twin?.symbols.map(item => <div className="v9MarketRow" key={item.symbol}><span><b>{item.symbol.replace('USDT','/USDT')}</b></span><span>{fmt(item.price ?? undefined)}</span><span>{item.spread_bps === null ? '—' : `${item.spread_bps.toFixed(2)} bp`}</span><span>{item.quote_volume_24h === null ? '—' : `${fmt(item.quote_volume_24h)} USDT`}</span><span>{item.age_seconds === null ? '—' : `${item.age_seconds.toFixed(1)} sn`}</span><span><b className={item.health === 'CANLI' ? 'up' : item.health === 'BAYAT' ? 'down' : 'waiting'}>● {item.health}</b></span></div>) ?? <div className="v9MarketEmpty">V9 canlı evreni hazırlanıyor…</div>}
        </div>
        <div className="v9Controls"><button className={v9Twin?.enabled ? 'v9Stop' : 'v9Start'} disabled={v9Busy} onClick={toggleV9Twin}>{v9Twin?.enabled ? '■ CANLI KAYDI DURDUR' : '▶ V9 CANLI KAYDI BAŞLAT'}</button><button className="v9PaperOrder" disabled={v9Busy || !v9Twin?.enabled || v9Twin?.symbols.find(item => item.symbol === symbol)?.health !== 'CANLI'} onClick={simulateV9Order}>◇ 100 USDT SANAL DOLUM TESTİ</button><span><b>0 BORSA EMRİ</b><small>{v9Message}</small></span><em>Son veri {stamp(v9Twin?.last_tick_at ?? undefined)}</em></div>
      </div>

      <div className="panel v9DriftRadar">
        <div className="v9SideHead"><div><h3>STRATEJİ DRIFT RADARI</h3><small>Yakın Paper dönemini geçmiş tabanla karşılaştırır</small></div><b className={v9Twin?.drift.rollback_required ? 'down' : v9Twin?.drift.status === 'DENGELİ' ? 'up' : 'waiting'}>{v9Twin?.drift.status ?? 'ÖĞRENİYOR'}</b></div>
        <div className="v9DriftScore"><div style={{background:`conic-gradient(${v9Twin?.drift.rollback_required ? '#e6543f' : '#43ad58'} 0 ${v9Twin?.drift.drift_score ?? 0}%,#edf0df 0)`}}><span><strong>{v9Twin?.drift.drift_score ?? 0}</strong><small>/99 SAPMA</small></span></div><section><b>{v9Twin?.drift.samples ?? 0} Paper örnek</b><small>Zayıf kaynak: {v9Twin?.drift.worst_strategy ?? 'henüz yok'}</small></section></div>
        <div className="v9DriftCompare"><div><span>Yakın Başarı</span><b>%{v9Twin?.drift.recent_win_rate ?? 0}</b><small>{(v9Twin?.drift.recent_return_pct ?? 0) >= 0 ? '+' : ''}%{v9Twin?.drift.recent_return_pct ?? 0}</small></div><div><span>Taban Başarı</span><b>%{v9Twin?.drift.baseline_win_rate ?? 0}</b><small>{(v9Twin?.drift.baseline_return_pct ?? 0) >= 0 ? '+' : ''}%{v9Twin?.drift.baseline_return_pct ?? 0}</small></div></div>
        <p>{v9Twin?.drift.reason ?? 'Kapanmış Paper işlemlerden davranış tabanı hazırlanıyor.'}</p>
      </div>

      <div className="panel v9Attribution">
        <div className="v9SideHead"><div><h3>PNL KATKI HARİTASI</h3><small>Kârı hangi Paper strateji getirdi, maliyet nerede oluştu?</small></div><b className={(v9Twin?.pnl_attribution.net_pnl || 0) >= 0 ? 'up' : 'down'}>{fmt(v9Twin?.pnl_attribution.net_pnl)} USDT</b></div>
        <div className="v9PnlTotals"><span><small>Gerçekleşen</small><b>{fmt(v9Twin?.pnl_attribution.total_realized_pnl)}</b></span><span><small>Açık / İkiz</small><b>{fmt(v9Twin?.pnl_attribution.total_unrealized_pnl)}</b></span><span><small>Maliyet</small><b className="down">-{fmt(v9Twin?.pnl_attribution.total_fees_usdt)}</b></span></div>
        <div className="v9PnlRows">{v9Twin?.pnl_attribution.items.length ? v9Twin.pnl_attribution.items.slice(0,5).map(item => <div key={item.source}><span><b>{item.source}</b><small>{item.trades} kayıt · ücret {fmt(item.fees_usdt)}</small></span><strong className={item.net_pnl >= 0 ? 'up' : 'down'}>{item.net_pnl >= 0 ? '+' : ''}{fmt(item.net_pnl)}</strong></div>) : <p>İlk kapanmış Paper işlem veya V9 sanal dolumdan sonra katkı dağılımı oluşur.</p>}</div>
      </div>

      <div className="panel v9Continuity">
        <div className="v9SideHead"><div><h3>VERİ SÜREKLİLİK KALKANI</h3><small>Kesinti algılama · mum geri doldurma · güvenli devam</small></div><b className={(v9Twin?.daily_report.data_quality_pct || 0) >= 98 ? 'up' : 'waiting'}>%{v9Twin?.daily_report.data_quality_pct ?? 0}</b></div>
        <div className="v9ContinuityGrid"><div><span>Yeniden Bağlanma</span><b>{v9Twin?.reconnect_count ?? 0}</b></div><div><span>Algılanan Boşluk</span><b>{v9Twin?.gap_count ?? 0}</b></div><div><span>Onarılan Mum</span><b>{v9Twin?.recovered_candles ?? 0}</b></div><div><span>Veri Deposu</span><b>{v9Twin?.database ?? 'KONTROL'}</b></div></div>
        <div className="v9EventLine"><b>{v9Twin?.events[0]?.kind ?? 'OLAY BEKLİYOR'}</b><span>{v9Twin?.events[0]?.message ?? v9Twin?.daily_report.headline ?? 'Canlı akış olayları burada görünür.'}</span><em>{stamp(v9Twin?.events[0]?.created_at)}</em></div>
      </div>

      <div className="panel v9Rollback">
        <div className="v9SideHead"><div><h3>OTOMATİK GÜVENLİ GERİ DÖNÜŞ</h3><small>Yalnızca Paper motorlarında kritik drift koruması</small></div><b className={v9Twin?.rollback.active ? 'down' : 'up'}>{v9Twin?.rollback.status ?? 'HAZIR'}</b></div>
        <div className="v9RollbackFlow"><span><b>NORMAL</b><small>V7 Paper Orkestra</small></span><i>→</i><span><b>DRIFT VETOSU</b><small>Eşik ≥ 70/99</small></span><i>→</i><span><b>GÜVENLİ PROFİL</b><small>V7 durur · V6 dengeli</small></span></div>
        <p>{v9Twin?.rollback.last_action ?? 'Kritik sapma görülürse Paper tahsis motoru otomatik durdurulur.'}</p><button disabled>GERÇEK / TESTNET EMRİ DAİMA KAPALI</button>
      </div>

      <div className="panel v9DailyOps">
        <div className="v9SideHead"><div><h3>GÜNLÜK OPERASYON RAPORU</h3><small>{v9Twin?.daily_report.date ?? 'Hazırlanıyor…'}</small></div><b className={v9Twin?.daily_report.status === 'CANLI VE SAĞLIKLI' ? 'up' : 'waiting'}>{v9Twin?.daily_report.status ?? 'BEKLEMEDE'}</b></div>
        <div className="v9OpsMetrics"><span><small>Tick</small><b>{(v9Twin?.daily_report.ticks_captured ?? 0).toLocaleString('tr-TR')}</b></span><span><small>Paper Dolum</small><b>{v9Twin?.daily_report.paper_fills ?? 0}</b></span><span><small>Net PnL</small><b className={(v9Twin?.daily_report.paper_net_pnl || 0) >= 0 ? 'up' : 'down'}>{fmt(v9Twin?.daily_report.paper_net_pnl)}</b></span></div>
        <p>{v9Twin?.daily_report.headline ?? 'V9 operasyon kanıtı hazırlanıyor.'}</p><small>{v9Twin?.daily_report.safety_note}</small>
      </div>
    </section>

    <section className="v8Deck">
      <div className="panel v8FutureCenter">
        <div className="v8Head"><div><span className="v8Tag">V8 AMİRAL</span><h3>GELECEK SENARYO MERKEZİ</h3><small>Geçmiş blok dağılımı · olasılık koridoru · maliyet sonrası LONG / SHORT / BEKLE</small></div><b className={futureLab?.veto_council.paper_scenario_allowed ? 'v8Ready' : 'v8Veto'}>{futureLab?.veto_council.status ?? 'CANLI VERİ BEKLENİYOR'}</b></div>
        <div className="v8Hero">
          <div className="v8FinalAction"><span>VETO SONRASI KARAR</span><strong className={futureLab?.veto_council.final_action === 'SHORT' ? 'down' : futureLab?.veto_council.final_action === 'LONG' ? 'up' : 'waiting'}>{futureLab?.veto_council.final_action ?? 'BEKLE'}</strong><small>Aday: {futureLab?.veto_council.candidate_action ?? '—'} · güven %{futureLab?.veto_council.confidence ?? 0}</small></div>
          <div className="v8ProbabilityDial" style={{background:`conic-gradient(#2dad5a 0 ${futureLab?.forecast.probabilities.YÜKSELİŞ ?? 0}%, #e2c22b ${futureLab?.forecast.probabilities.YÜKSELİŞ ?? 0}% ${(futureLab?.forecast.probabilities.YÜKSELİŞ ?? 0) + (futureLab?.forecast.probabilities.YATAY ?? 0)}%, #e8614e 0 100%)`}}><div><strong>%{futureLab?.forecast.dominant_probability ?? 0}</strong><small>{futureLab?.forecast.dominant_scenario ?? 'ÖLÇÜLÜYOR'}</small></div></div>
          <div className="v8ForecastMeta"><span>TAHMİN UFKU</span><strong>{v8Horizon} MUM</strong><small>{interval} · belirsizlik %{futureLab?.forecast.uncertainty_pct ?? '—'}</small><b>{futureLab?.forecast.confidence_status ?? 'DAĞILIM HAZIRLANIYOR'}</b></div>
        </div>
        <div className="v8Probabilities">
          <div className="v8Bull"><span>YÜKSELİŞ</span><strong>%{futureLab?.forecast.probabilities.YÜKSELİŞ ?? '—'}</strong><i style={{width:`${futureLab?.forecast.probabilities.YÜKSELİŞ ?? 0}%`}}/></div>
          <div className="v8Neutral"><span>YATAY</span><strong>%{futureLab?.forecast.probabilities.YATAY ?? '—'}</strong><i style={{width:`${futureLab?.forecast.probabilities.YATAY ?? 0}%`}}/></div>
          <div className="v8Bear"><span>DÜŞÜŞ</span><strong>%{futureLab?.forecast.probabilities.DÜŞÜŞ ?? '—'}</strong><i style={{width:`${futureLab?.forecast.probabilities.DÜŞÜŞ ?? 0}%`}}/></div>
        </div>
        <div className="v8CorridorLevels"><div><span>ALT SENARYO</span><b className="down">{fmt(futureLab?.forecast.targets.bear_case)}</b></div><div><span>ORTA SENARYO</span><b>{fmt(futureLab?.forecast.targets.base_case)}</b></div><div><span>ÜST SENARYO</span><b className="up">{fmt(futureLab?.forecast.targets.bull_case)}</b></div><div><span>ORT. HAREKET</span><b className={(futureLab?.forecast.terminal_mean_pct || 0) >= 0 ? 'up' : 'down'}>{futureLab ? `${futureLab.forecast.terminal_mean_pct >= 0 ? '+' : ''}%${futureLab.forecast.terminal_mean_pct}` : '—'}</b></div></div>
        <div className="v8Controls"><div><span>Ufuk</span>{([12,24] as const).map(value => <button key={value} className={v8Horizon === value ? 'v8ControlActive' : ''} onClick={() => setV8Horizon(value)}>{value} MUM</button>)}</div><div><span>Dijital emir</span>{[500,1000,2000].map(value => <button key={value} className={v8Notional === value ? 'v8ControlActive' : ''} onClick={() => setV8Notional(value)}>{value.toLocaleString('tr-TR')} USDT</button>)}</div><em>Güncelleme {stamp(futureLab?.generated_at)}</em></div>
        <p>{futureLab?.veto_council.reason ?? 'V8 olasılık dağılımı ve veto kurulu hazırlanıyor…'} <b>Kesin tahmin değildir.</b></p>
      </div>

      <div className="panel v8ChaosShield">
        <div className="v8SideHead"><div><h3>KAOS KALKANI</h3><small>Likidite şoku · rejim değişimi · ani hareket</small></div><b className={futureLab?.chaos.veto_required ? 'down' : futureLab?.chaos.level === 'UYARI' ? 'waiting' : 'up'}>{futureLab?.chaos.level ?? 'ÖLÇÜLÜYOR'}</b></div>
        <div className="chaosGauge"><div><strong>%{futureLab?.chaos.chaos_score ?? 0}</strong><small>KAOS</small></div><span><b>Spread {futureLab?.chaos.spread_bps ?? '—'} bp</b><small>Volatilite {futureLab?.chaos.volatility_ratio ?? '—'}x</small></span></div>
        <div className="chaosBars">{[['Likidite Şoku',futureLab?.chaos.liquidity_shock_score],['Rejim Değişimi',futureLab?.chaos.regime_shift_score],['Ani Hareket',futureLab?.chaos.flash_move_score]].map(([label,value]) => <div key={label as string}><span>{label}</span><i><b style={{width:`${value ?? 0}%`}}/></i><strong>%{value ?? 0}</strong></div>)}</div>
        <div className="chaosReasons">{futureLab?.chaos.reasons.map(reason => <span key={reason}>• {reason}</span>) ?? <span>Canlı risk kanıtı toplanıyor…</span>}</div>
      </div>

      <div className="panel v8Calibration">
        <div className="v8SideHead"><div><h3>TAHMİN DOĞRULUK KARNESİ</h3><small>İleriye bakmayan geçmiş doğrulama</small></div><b className={futureLab?.calibration.quarantined ? 'down' : futureLab?.calibration.status === 'KALİBRE' ? 'up' : 'waiting'}>{futureLab?.calibration.status ?? 'ÖĞRENİYOR'}</b></div>
        <div className="calibrationHero"><div><strong>{futureLab?.calibration.reliability_score ?? 0}</strong><small>/99 GÜVENİLİRLİK</small></div><span><b>%{futureLab?.calibration.accuracy_pct ?? 0} isabet</b><small>{futureLab?.calibration.hits ?? 0}/{futureLab?.calibration.samples ?? 0} örnek</small></span></div>
        <div className="calibrationMetrics"><span><small>Brier skoru</small><b>{futureLab?.calibration.brier_score ?? '—'}</b></span><span><small>Ort. güven</small><b>%{futureLab?.calibration.average_confidence_pct ?? '—'}</b></span><span><small>Aşırı güven farkı</small><b className={(futureLab?.calibration.overconfidence_gap_pct || 0) > 20 ? 'down' : 'up'}>%{futureLab?.calibration.overconfidence_gap_pct ?? '—'}</b></span></div>
        <div className="calibrationDots">{futureLab?.calibration.records.slice(0,8).map(record => <i key={`${record.forecast_at}-${record.predicted}`} className={record.hit ? 'calibrationHit' : 'calibrationMiss'} title={`${record.predicted} → ${record.outcome} · ${record.realized_move_pct}%`}/>)}</div>
        <small className="v8Method">{futureLab?.calibration.method_note ?? 'Model kendi tahminlerini sınamaya hazırlanıyor…'}</small>
      </div>

      <div className="panel v8ExecutionTwin">
        <div className="v8SideHead"><div><h3>DİJİTAL EMİR İKİZİ</h3><small>Kısmi dolum · fiyat etkisi · ücret · gecikme</small></div><b className={futureLab?.execution_twin.status === 'UYGULANABİLİR PAPER' ? 'up' : 'down'}>{futureLab?.execution_twin.status ?? 'SİMÜLE EDİLİYOR'}</b></div>
        <div className="executionMetrics"><div><span>Tahmini Dolum</span><strong>%{futureLab?.execution_twin.partial_fill_pct ?? 0}</strong><i><b style={{width:`${futureLab?.execution_twin.partial_fill_pct ?? 0}%`}}/></i></div><div><span>Tur Maliyeti</span><strong>%{futureLab?.execution_twin.round_trip_cost_pct ?? '—'}</strong><small>Etki {futureLab?.execution_twin.estimated_impact_bps ?? '—'} bp</small></div><div><span>Gecikme Varsayımı</span><strong>{futureLab?.execution_twin.latency_ms_assumption ?? '—'} ms</strong><small>{fmt(futureLab?.execution_twin.visible_depth_usdt)} USDT derinlik</small></div></div>
        <div className="executionScenarios">{futureLab?.execution_twin.scenarios.map(item => <div key={item.action} className={item.action === futureLab.execution_twin.best_action ? 'executionWinner' : ''}><span>{item.action}</span><b className={item.expected_net_pct > 0 ? 'up' : item.expected_net_pct < 0 ? 'down' : 'waiting'}>{item.expected_net_pct >= 0 ? '+' : ''}%{item.expected_net_pct}</b><small>olasılık %{item.probability} · kötü %{item.worst_case_pct}</small></div>)}</div>
        <small className="v8Method">{futureLab?.execution_twin.note ?? 'Emir defteri maliyetleri ölçülüyor…'}</small>
      </div>

      <div className="panel v8PortfolioChaos">
        <div className="v8SideHead"><div><h3>PORTFÖY KAOS TESTİ</h3><small>BTC şoku altında Paper maruziyet</small></div><b className={futureLab?.portfolio_chaos.veto_required ? 'down' : futureLab?.portfolio_chaos.level === 'DAYANIKLI' ? 'up' : 'waiting'}>{futureLab?.portfolio_chaos.level ?? 'HESAPLANIYOR'}</b></div>
        <div className="portfolioChaosHero"><div><span>BTC KORELASYONU</span><strong>%{futureLab?.portfolio_chaos.btc_correlation_pct ?? '—'}</strong><small>Beta {futureLab?.portfolio_chaos.btc_beta ?? '—'}</small></div><div><span>EN KÖTÜ SENARYO</span><strong className="down">%{futureLab?.portfolio_chaos.worst_case_pct ?? '—'}</strong><small>Güvenli tahsis ≤ %{futureLab?.portfolio_chaos.safe_allocation_pct ?? '—'}</small></div></div>
        <div className="portfolioChaosCases">{futureLab?.portfolio_chaos.scenarios.map(item => <div key={item.label}><span>{item.label}</span><b className={item.portfolio_impact_pct < 0 ? 'down' : 'up'}>{item.portfolio_impact_pct >= 0 ? '+' : ''}%{item.portfolio_impact_pct}</b><small>{item.status} · coin %{item.projected_symbol_move_pct}</small></div>)}</div>
        <small className="v8Method">{futureLab?.portfolio_chaos.exposure_source ?? 'Maruziyet hesaplanıyor'} · %{futureLab?.portfolio_chaos.exposure_pct ?? 0}</small>
      </div>

      <div className="panel v8VetoCouncil">
        <div className="v8SideHead"><div><h3>YAPAY ZEKA VETO KURULU</h3><small>Bir kritik kapı bile kalırsa sonuç BEKLE</small></div><b className={futureLab?.veto_council.paper_scenario_allowed ? 'up' : 'down'}>{futureLab?.veto_council.veto_count ?? 0} VETO</b></div>
        <div className="vetoGates">{futureLab?.veto_council.gates.map(gate => <div key={gate.key} className={gate.passed ? 'vetoPass' : 'vetoFail'}><i>{gate.passed ? '✓' : '!'}</i><span><b>{gate.label}</b><small>{gate.detail}</small></span></div>) ?? <p>V8 denetim kapıları hazırlanıyor…</p>}</div>
        <div className="vetoFinal"><span>SONUÇ</span><strong className={futureLab?.veto_council.final_action === 'LONG' ? 'up' : futureLab?.veto_council.final_action === 'SHORT' ? 'down' : 'waiting'}>{futureLab?.veto_council.final_action ?? 'BEKLE'}</strong><small>{futureLab?.veto_council.reason ?? 'Kanıt bekleniyor…'}</small></div>
        <button disabled>GERÇEK / TESTNET EMRİ YOK</button>
      </div>
    </section>
    <section className="v7Deck">
      <div className="panel strategyOrchestrator">
        <div className="v7Head">
          <div><span className="v7Tag">V7 AMİRAL</span><h3>OTONOM STRATEJİ ORKESTRASI</h3><small>Grid · Trend · Kırılım · Paper sermaye tahsisi · strateji karantinası</small></div>
          <b className={orchestrator?.enabled ? 'orchestraLive' : 'orchestraIdle'}><i/>{orchestrator?.status ?? 'KULLANICI ONAYI BEKLİYOR'}</b>
        </div>
        <div className="orchestraHero">
          <div className="conductor"><span>ŞEFİN ÖNÜNDEKİ STRATEJİ</span><strong>{selectedOrchestraDecision?.strategy ?? councilLeader?.strategy ?? 'BEKLE'}</strong><small>{selectedOrchestraDecision?.symbol ?? symbol} · {selectedOrchestraDecision?.regime ?? 'Rejim ölçülüyor'}</small></div>
          <div className="decisionGauge"><div><strong>%{selectedOrchestraDecision?.confidence ?? 0}</strong><small>KARAR GÜVENİ</small></div><span className={(selectedOrchestraDecision?.risk_score || 0) >= 60 ? 'down' : 'up'}>Risk %{selectedOrchestraDecision?.risk_score ?? 0}</span></div>
          <div className="orchestraPulse"><i className={orchestrator?.enabled ? 'pulseLive' : ''}/><span>{orchestrator?.enabled ? 'Her 12 sn bir parite inceleniyor' : 'Orkestra duruyor'}</span><b>{orchestrator?.cycles ?? 0} karar çevrimi</b></div>
          <div className="orchestraCertification"><span>KANIT DURUMU</span><strong>{orchestrator?.certification_status ?? 'KANIT BEKLİYOR'}</strong><small>{orchestrator?.quarantined_strategies?.length ? `${orchestrator.quarantined_strategies.join(', ')} karantinada` : 'Aktif karantina yok'}</small></div>
        </div>
        <div className="orchestraMetrics">
          <div><span>Toplam Paper Sermaye</span><b>{fmt(orchestrator?.capital ?? orchestraCapital)} USDT</b><small>Gerçek bakiye kullanılmaz</small></div>
          <div><span>Sanal Tahsis</span><b className="up">{fmt(orchestrator?.allocation_summary.allocated_usdt)} USDT</b><small>%{orchestrator?.allocation_summary.heat_pct ?? 0} portföy ısısı</small></div>
          <div><span>Boşta Bekleyen</span><b>{fmt(orchestrator?.allocation_summary.idle_usdt ?? orchestraCapital)} USDT</b><small>Kanıt yoksa nakit korunur</small></div>
          <div><span>İzlenen Evren</span><b>{orchestrator?.universe?.length ?? 0} parite</b><small>{orchestrator?.universe?.join(' · ') || 'Başlatınca seçilir'}</small></div>
          <div><span>Lider Konsey Skoru</span><b className={(councilLeader?.score || 0) >= 65 ? 'up' : 'waiting'}>{councilLeader?.score ?? '—'} / 99</b><small>{councilLeader?.strategy ?? 'Kanıt toplanıyor'}</small></div>
          <div><span>Son Karar</span><b>{selectedOrchestraDecision?.direction ?? 'BEKLE'}</b><small>{selectedOrchestraDecision ? stamp(selectedOrchestraDecision.updated_at) : 'Henüz yok'}</small></div>
        </div>
        <div className="orchestraControls">
          <div className="orchestraCapital"><span>Paper sermaye</span>{[1000,3000,5000].map(value => <button key={value} className={orchestraCapital === value ? 'capitalActive' : ''} disabled={orchestrator?.enabled} onClick={() => setOrchestraCapital(value)}>{value.toLocaleString('tr-TR')}</button>)}</div>
          <button className={orchestrator?.enabled ? 'stopOrchestra' : 'startOrchestra'} disabled={v7Busy} onClick={toggleOrchestrator}>{orchestrator?.enabled ? '■ V7 ORKESTRAYI DURDUR' : '▶ V7 PAPER ORKESTRAYI BAŞLAT'}</button>
          <button className={notificationsEnabled ? 'v7NotifyOn' : 'v7Notify'} onClick={enableNotifications}>{notificationsEnabled ? '● UYARILAR AÇIK' : '◌ UYARILARI AÇ'}</button>
          <button className={paper?.emergency_brake?.active ? 'v7BrakeReset' : 'v7Brake'} disabled={paperBusy} onClick={toggleEmergency}>{paper?.emergency_brake?.active ? '✓ ACİL FRENİ KALDIR' : '⚠ TÜM PAPER MOTORLARINI DURDUR'}</button>
        </div>
        <div className="orchestraMessage"><b>0 BORSA EMRİ</b><span>{v7Message}</span><em>Son gözlem {stamp(orchestrator?.last_tick_at ?? undefined)}</em></div>
      </div>

      <div className="panel strategyCouncil">
        <div className="v7SideHead"><div><h3>STRATEJİ KONSEYİ</h3><small>Kanıt zayıfsa otomatik Paper karantina</small></div><b>{orchestrator?.strategies?.length ?? 0}/3 AKTİF</b></div>
        <div className="councilCards">{orchestrator?.strategies?.length ? orchestrator.strategies.map(item => <div key={item.strategy} className={item.quarantined ? 'councilQuarantine' : item.status === 'KANITLI PAPER' ? 'councilCertified' : ''}>
          <div><span>{item.strategy}</span><strong>{item.score}</strong></div>
          <b className={item.quarantined ? 'down' : item.status === 'KANITLI PAPER' ? 'up' : 'waiting'}>{item.status}</b>
          <div className="councilBar"><i style={{width:`${Math.min(99,item.score)}%`}}/></div>
          <section><span>Örnek <b>{item.trades}</b></span><span>Sertifika <b>{item.certified_symbols}</b></span><span>Ort. Net <b className={item.average_return_pct >= 0 ? 'up' : 'down'}>{item.average_return_pct >= 0 ? '+' : ''}{item.average_return_pct.toFixed(2)}%</b></span><span>DD <b className="down">%{Math.abs(item.max_drawdown_pct).toFixed(2)}</b></span></section>
        </div>) : <p>Orkestra başladığında üç strateji aynı kapanışlarda sınanır.</p>}</div>
        <div className="quarantineRule"><b>KARANTİNA KURALI</b><span>Yeterli örnekte maliyet sonrası sonuç negatif ve hiçbir parite stres onaylı değilse yeni tahsis kesilir.</span></div>
      </div>

      <div className="panel capitalAllocator">
        <div className="v7SideHead"><div><h3>AKILLI PAPER SERMAYE DAĞITICI</h3><small>Aynı yönlü %82+ korelasyonda düşük puanlı tahsis kesilir</small></div><b className={(orchestrator?.allocation_summary.heat_pct || 0) >= 75 ? 'down' : 'up'}>ISI %{orchestrator?.allocation_summary.heat_pct ?? 0}</b></div>
        <div className="allocationTable"><div className="allocationHeader"><span>PARİTE</span><span>KARAR</span><span>GÜVEN / RİSK</span><span>TAHSİS</span><span>DURUM</span></div>{orchestrator?.symbols?.length ? orchestrator.symbols.map(decision => { const allocation = orchestrator.allocations.find(item => item.symbol === decision.symbol); return <div className="allocationRow" key={decision.symbol}><span><b>{decision.symbol}</b><small>{fmt(decision.price)}</small></span><span><b>{decision.strategy}</b><small>{decision.direction} · {decision.regime}</small></span><span><b className={decision.confidence >= 65 ? 'up' : 'waiting'}>%{decision.confidence}</b><small>risk %{decision.risk_score}</small></span><span><b>{fmt(allocation?.allocated_usdt)} USDT</b><small>%{allocation?.allocation_pct ?? 0}</small></span><span><b className={allocation?.status === 'PAPER TAHSİS' ? 'up' : allocation?.status === 'KORELASYON KİLİDİ' ? 'down' : 'waiting'}>{allocation?.status ?? 'BEKLE'}</b><small>{allocation?.correlation_with ? `${allocation.correlation_with} · %${allocation.correlation_pct}` : decision.reason}</small></span></div>}) : <div className="allocationEmpty">V7 başlatılınca seçili coin ile BTC, ETH ve SOL aynı Paper portföyde incelenir.</div>}</div>
        <div className="allocatorFooter"><span>Kullanılan <b>{fmt(orchestrator?.allocation_summary.allocated_usdt)} USDT</b></span><span>Boşta <b>{fmt(orchestrator?.allocation_summary.idle_usdt ?? orchestraCapital)} USDT</b></span><em>En fazla 3 eşzamanlı sanal tahsis</em></div>
      </div>

      <div className="panel replayLabV7">
        <div className="v7SideHead"><div><h3>HIZLANDIRILMIŞ PİYASA TEKRARI</h3><small>{v7Replay?.symbol ?? symbol} · kapanmış mum · ücret + kayma + gecikme</small></div><div className="replaySwitch"><button className={replayHorizon === '24h' ? 'activeReplay' : ''} onClick={() => setReplayHorizon('24h')}>24 SAAT</button><button className={replayHorizon === '7d' ? 'activeReplay' : ''} onClick={() => setReplayHorizon('7d')}>7 GÜN</button></div></div>
        <div className="replayWinner"><span>ÖNE ÇIKAN</span><strong>{v7Replay?.winner !== 'BEKLE' ? v7Replay?.winner : replayLeader?.strategy ?? 'BEKLE'}</strong><b className={v7Replay?.promotion_ready ? 'up' : 'waiting'}>{v7Replay?.status ?? 'TEST ÇALIŞIYOR'}</b><small>{v7Replay?.sampled_candles ?? 0} gözlenen mum · fark {v7Replay?.score_gap ?? 0} puan</small></div>
        <div className="replayProfiles">{v7Replay?.profiles?.map(profile => <div key={profile.strategy} className={profile.certified ? 'replayCertified' : ''}><div><span>{profile.strategy}</span><b>{profile.ranking_score}</b></div><strong className={profile.net_result_usdt >= 0 ? 'up' : 'down'}>{profile.net_result_usdt >= 0 ? '+' : ''}{fmt(profile.net_result_usdt)} USDT</strong><section><span>İşlem <b>{profile.trades}</b></span><span>Başarı <b>%{profile.win_rate.toFixed(0)}</b></span><span>Maliyet <b>{fmt(profile.costs_usdt)}</b></span><span>DD <b className="down">%{Math.abs(profile.max_drawdown_pct).toFixed(2)}</b></span></section><div className="stressDots">{profile.stress_cases.map(item => <i key={item.label} className={item.status === 'DAYANDI' ? 'stressPass' : 'stressFail'} title={`${item.label}: ${item.net_return_pct}%`}/>) }<small>{profile.stress_survived}/{profile.stress_total} stres testi</small></div><em>{profile.certification}</em></div>)}</div>
        {!v7Replay && <div className="replayEmpty">24 saat ve 7 günlük tekrar motoru hazırlanıyor…</div>}
        <p>{v7Replay?.note ?? 'Seçili parite için geçmiş kapanışlar indiriliyor.'}</p>
      </div>

      <div className="panel v7Journal">
        <div className="v7SideHead"><div><h3>V7 KARAR GÜNLÜĞÜ</h3><small>Açıklanabilir seçim ve haftalık Paper özeti</small></div><b>{orchestrator?.events?.length ?? 0} OLAY</b></div>
        <div className="weeklyStrip"><span><small>7G PAPER PnL</small><b className={(v7Weekly?.paper_pnl_usdt || 0) >= 0 ? 'up' : 'down'}>{fmt(v7Weekly?.paper_pnl_usdt)} USDT</b></span><span><small>İŞLEM / BAŞARI</small><b>{v7Weekly?.paper_trades ?? 0} / %{v7Weekly?.win_rate ?? 0}</b></span><span><small>ORKESTRA</small><b>{v7Weekly?.orchestrator_cycles ?? 0} çevrim</b></span></div>
        <div className="v7Events">{orchestrator?.events?.length ? orchestrator.events.slice(0,7).map((event,index) => <div key={`${event.created_at}-${index}`}><i className={event.kind.includes('KARANTİNA') ? 'v7EventDanger' : 'v7EventOk'}/><span><b>{event.kind} · {event.strategy}</b><small>{event.symbol} · {stamp(event.created_at)}</small><em>{event.message}</em></span></div>) : <p>İlk V7 karar çevrimi burada açıklamasıyla görünecek.</p>}</div>
        <div className="v7Safety"><b>YALNIZCA GÖLGE PORTFÖY</b><span>{v7Weekly?.note ?? 'Gerçek para ve borsa emri yok.'}</span></div>
      </div>
    </section>
    <section className="v6Deck">
      <div className="panel autonomousGrid">
        <div className="v6Head">
          <div><span className="v6Tag">V6 ÖZEL</span><h3>OTONOM PAPER GRID MOTORU</h3><small>Canlı fiyat akışı · sanal dolum · envanter kilidi · güvenli yeniden merkezleme</small></div>
          <b className={gridEngine?.enabled ? 'v6Running' : 'v6Stopped'}><i/>{gridEngine?.status ?? 'KULLANICI ONAYI BEKLİYOR'}</b>
        </div>
        <div className="engineHero">
          <div className="engineIdentity">
            <span>AKTİF DİJİTAL İKİZ</span>
            <strong>{activeRuntime?.profile ?? gridEngine?.active_profile ?? 'DENGELİ'}</strong>
            <small>{activeRuntime?.profile_label ?? 'Canlı Paper yarışını başlatınca ölçülür'}</small>
          </div>
          <div className="enginePulse"><i className={gridEngine?.enabled ? 'pulseLive' : ''}/><span>{gridEngine?.enabled ? '4 sn canlı Paper gözlemi' : 'Motor duruyor'}</span><b>{gridEngine?.symbol ?? symbol} · {gridEngine?.interval ?? interval}</b></div>
          <div className="engineResult"><span>MALİYET SONRASI SONUÇ</span><strong className={(activeRuntime?.marked_result_usdt || 0) >= 0 ? 'up' : 'down'}>{(activeRuntime?.marked_result_usdt || 0) >= 0 ? '+' : ''}{fmt(activeRuntime?.marked_result_usdt)} USDT</strong><small>{activeRuntime ? `%${activeRuntime.net_return_pct.toFixed(2)}` : 'Henüz canlı Paper verisi yok'}</small></div>
        </div>
        <div className="engineMetrics">
          <div><span>Sanal Dolum</span><b>{activeRuntime?.fill_count ?? 0}</b><small>{activeRuntime?.completed_cycles ?? 0} tamamlanan tur</small></div>
          <div><span>Ücret + Kayma</span><b className="cost">{fmt((activeRuntime?.fees_usdt || 0) + (activeRuntime?.slippage_usdt || 0))} USDT</b><small>{fmt(activeRuntime?.fees_usdt)} + {fmt(activeRuntime?.slippage_usdt)}</small></div>
          <div><span>Envanter</span><b>{activeRuntime?.open_grids ?? 0} / {activeRuntime?.inventory_limit ?? '—'}</b><small>Maks. {activeRuntime?.max_inventory_grids ?? 0} grid</small></div>
          <div><span>Maks. Düşüş</span><b className="down">%{Math.abs(activeRuntime?.max_drawdown_pct || 0).toFixed(2)}</b><small>Paper sermayeye göre</small></div>
          <div><span>Canlı Skor</span><b className={(activeRuntime?.score || 0) >= 65 ? 'up' : 'waiting'}>{activeRuntime?.score ?? '—'} / 99</b><small>Kanıt arttıkça olgunlaşır</small></div>
          <div><span>Merkezleme</span><b>{gridEngine?.recenter_count ?? 0}</b><small>{activeRuntime?.recenter_status ?? 'Aralık bekleniyor'}</small></div>
        </div>
        <div className="inventoryGuard">
          <div><span>ENVANTER KORUMASI</span><b>%{activeRuntime?.inventory_used_pct ?? 0} kullanılıyor</b></div>
          <div className="inventoryTrack"><i style={{width:`${Math.min(100, activeRuntime?.inventory_used_pct ?? 0)}%`}}/></div>
          <small>Sınır dolarsa V6 yeni sanal girişleri otomatik keser; açık envanter varken aralığı taşımaz.</small>
        </div>
        <div className="engineActions">
          <button className={gridEngine?.enabled ? 'stopEngine' : 'startEngine'} disabled={v6Busy} onClick={toggleGridEngine}>{gridEngine?.enabled ? '■ CANLI PAPER MOTORU DURDUR' : '▶ CANLI PAPER MOTORU BAŞLAT'}</button>
          <button className="recenterEngine" disabled={v6Busy || !gridEngine?.active_runtime} onClick={recenterGridEngine}>⌖ GÜVENLİ MERKEZLE</button>
          <button className={notificationsEnabled ? 'notifyOn' : 'notifyOff'} onClick={enableNotifications}>{notificationsEnabled ? '● BİLDİRİMLER AÇIK' : '◌ BİLDİRİMLERİ AÇ'}</button>
        </div>
        <div className="engineMessage"><b>YALNIZCA PAPER</b><span>{v6Message}</span><em>Son gözlem {stamp(gridEngine?.last_tick_at ?? undefined)}</em></div>
      </div>

      <div className="panel twinArena">
        <div className="v6SideHead"><div><h3>DİJİTAL İKİZ ARENASI</h3><small>Aynı veride 3 profil · ücret ve kayma dahil</small></div><b className={gridEngine?.promotion_ready || twinLab?.promotion_ready ? 'up' : 'waiting'}>{gridEngine?.recommendation_status ?? twinLab?.status ?? 'VERİ TOPLUYOR'}</b></div>
        <div className="twinRecommendation"><span>ÖNERİLEN PROFİL</span><strong>{recommendedTwin}</strong><small>{gridEngine?.enabled ? 'Canlı Paper kanıtı' : 'Geçmiş kapanış ön elemesi'} · gerçek emir yok</small></div>
        <div className="twinCards">{twinProfiles.map(profile => <div key={profile.profile} className={`${profile.profile === recommendedTwin ? 'twinWinner' : ''} ${profile.profile === gridEngine?.active_profile ? 'twinSelected' : ''}`}>
          <div className="twinTop"><span>{profile.profile}</span><b>{profile.score}</b></div>
          <small>{profile.profile_label}</small>
          <div className="twinStats"><span>Sonuç <b className={profile.marked_result_usdt >= 0 ? 'up' : 'down'}>{profile.marked_result_usdt >= 0 ? '+' : ''}{fmt(profile.marked_result_usdt)}</b></span><span>Tur <b>{profile.completed_cycles}</b></span><span>Dolum <b>{profile.fills}</b></span><span>DD <b className="down">%{Math.abs(profile.max_drawdown_pct).toFixed(2)}</b></span></div>
          <button disabled={v6Busy || !gridEngine?.profiles?.length || profile.profile === gridEngine?.active_profile} onClick={() => selectGridProfile(profile.profile)}>{profile.profile === gridEngine?.active_profile ? 'GÖRÜNTÜLENİYOR' : 'BU PROFİLİ GÖSTER'}</button>
        </div>)}</div>
        {!twinProfiles.length && <div className="twinEmpty">Üç profil aynı 500 mum üzerinde sınanıyor…</div>}
        <p>{gridEngine?.enabled ? gridEngine.last_action : twinLab?.reason ?? 'Dijital İkiz laboratuvarı hazırlanıyor…'}</p>
      </div>

      <div className="panel orderbookRadar">
        <div className="v6SideHead"><div><h3>KALICI EMİR DEFTERİ DUVAR RADARI</h3><small>Tek kareye güvenmez; aynı fiyat bölgesinde en az 3 gözlem arar</small></div><b className={(orderbook?.spoof_risk_score || 0) >= 55 ? 'down' : orderbook?.mode?.includes('KALICI') ? 'up' : 'waiting'}>{orderbook?.mode ?? 'DERİNLİK TOPLANIYOR'}</b></div>
        <div className="wallGrid">
          <div className="bidWall"><span>ALIŞ DUVARI</span><strong>{fmt(orderbook?.bid_wall?.price)}</strong><b>{fmt(orderbook?.bid_wall?.notional_usdt)} USDT</b><small>{orderbook?.bid_wall?.strength ?? '—'}x güç · {orderbook?.bid_wall?.persistence ?? 0}/{orderbook?.required_persistence_samples ?? 3} kalıcılık</small></div>
          <div className="bookPressure"><span>DERİNLİK BASINCI</span><strong className={(orderbook?.pressure_pct || 0) >= 0 ? 'up' : 'down'}>{(orderbook?.pressure_pct || 0) >= 0 ? '+' : ''}{orderbook?.pressure_pct ?? 0}%</strong><div><i className="pressureBid" style={{width:`${Math.max(4, Math.min(96, 50 + (orderbook?.pressure_pct || 0) / 2))}%`}}/><i className="pressureAsk"/></div><small>{orderbook?.dominant_side ?? 'NÖTR'} baskısı · spread {orderbook?.spread_bps ?? '—'} bp</small></div>
          <div className="askWall"><span>SATIŞ DUVARI</span><strong>{fmt(orderbook?.ask_wall?.price)}</strong><b>{fmt(orderbook?.ask_wall?.notional_usdt)} USDT</b><small>{orderbook?.ask_wall?.strength ?? '—'}x güç · {orderbook?.ask_wall?.persistence ?? 0}/{orderbook?.required_persistence_samples ?? 3} kalıcılık</small></div>
        </div>
        <div className="heatMap">{orderbook?.heatmap?.map((level,index) => <div key={`${level.side}-${level.price}-${index}`} className={level.side === 'ALIŞ' ? 'heatBid' : 'heatAsk'} title={`${fmt(level.price)} · ${fmt(level.notional_usdt)} USDT`}><span>{level.side}</span><i style={{width:`${Math.max(4, level.heat)}%`}}/><b>{level.heat}</b></div>)}</div>
        <div className="spoofLine"><span>SPOOF RİSKİ</span><div><i style={{width:`${orderbook?.spoof_risk_score ?? 0}%`}}/></div><b>%{orderbook?.spoof_risk_score ?? 0}</b></div>
        <p>{orderbook?.reason ?? 'Emir defteri kalıcılık örnekleri toplanıyor…'}</p>
      </div>

      <div className="panel gridTimeline">
        <div className="v6SideHead"><div><h3>CANLI PAPER ZAMAN ÇİZGİSİ</h3><small>Sanal dolum, tur, envanter ve merkezleme olayları</small></div><b>{gridEngine?.events?.length ?? 0} OLAY</b></div>
        <div className="timelineEvents">{gridEngine?.events?.length ? gridEngine.events.slice(0,7).map((event,index) => <div key={`${event.created_at}-${index}`}><i className={event.kind.includes('KİLİT') || event.kind.includes('BEKLİYOR') ? 'eventWarn' : 'eventOk'}/><span><b>{event.kind}</b><small>{event.profile} · {stamp(event.created_at)}</small><em>{event.message}</em></span>{event.price !== null && <strong>{fmt(event.price)}</strong>}</div>) : <p>Motor başladığında ilk canlı Paper olayı burada görünecek.</p>}</div>
        <div className="timelineSafety"><b>0 BORSA EMRİ</b><span>V6 yalnızca sanal dolum kaydı üretir.</span></div>
      </div>
    </section>

    <section className="v5Deck">
      <div className="panel gridCommandCenter">
        <div className="v5Head"><div><span className="v5Tag">V5 ÖZEL</span><h3>AKILLI GRID KOMUTA MERKEZİ</h3><small>ATR + destek/direnç + likidite + komisyon süzgeciyle canlı Paper planı</small></div><b className={gridPlan?.paper_eligible ? 'gridReady' : 'gridWatch'}>{gridPlan?.viability ?? 'HESAPLANIYOR'}</b></div>
        <div className="gridHero">
          <div className="gridMap">
            <div className="gridMapTitle"><span>{gridPlan?.mode ?? 'GRID MODU ÖLÇÜLÜYOR'}</span><b>{gridPlan?.symbol ?? symbol} · {interval}</b></div>
            <div className="gridRail">{gridPlan?.levels.map((level,index) => <i key={`${level}-${index}`} className={index === 0 || index === gridPlan.levels.length - 1 ? 'edge' : ''} style={{left:`${index / Math.max(1, gridPlan.levels.length - 1) * 100}%`}} title={fmt(level)}/>)}</div>
            <div className="gridBounds"><span><small>ALT SINIR</small><b>{fmt(gridPlan?.lower)}</b></span><span className="gridReference"><small>REFERANS</small><b>{fmt(gridPlan?.entry_reference)}</b></span><span><small>ÜST SINIR</small><b>{fmt(gridPlan?.upper)}</b></span></div>
            <div className="gridStops"><span>Güvenlik tabanı {fmt(gridPlan?.safety_floor)}</span><span>Güvenlik tavanı {fmt(gridPlan?.safety_ceiling)}</span></div>
          </div>
          <div className="gridScore"><div className={gridPlan?.paper_eligible ? 'scoreReady' : 'scoreWatch'}><strong>%{gridPlan?.safety_score ?? '—'}</strong><small>GRID GÜVENLİK</small></div><span>{gridPlan?.reason ?? 'Canlı piyasa aralığı ölçülüyor…'}</span></div>
        </div>
        <div className="gridMetrics"><div><span>Kademe</span><b>{gridPlan?.grid_count ?? '—'}</b><small>{gridPlan ? `%${gridPlan.grid_step_pct.toFixed(3)} aralık` : 'hesaplanıyor'}</small></div><div><span>Toplam Aralık</span><b>{gridPlan ? `%${gridPlan.range_width_pct.toFixed(2)}` : '—'}</b><small>ATR %{gridPlan?.atr_pct ?? '—'}</small></div><div><span>Kademe Sermayesi</span><b>{fmt(gridPlan?.capital_per_grid)} USDT</b><small>Maks. {fmt(gridPlan?.max_planned_exposure)}</small></div><div><span>Likidite</span><b className={gridPlan && gridPlan.liquidity_score >= 70 ? 'up' : 'waiting'}>%{gridPlan?.liquidity_score ?? '—'}</b><small>{gridPlan?.liquidity_mode ?? 'ölçülüyor'}</small></div><div><span>Komisyon Katı</span><b className={(gridPlan?.fee_assumption.step_to_fee_multiple || 0) >= 1.75 ? 'up' : 'down'}>{gridPlan?.fee_assumption.step_to_fee_multiple ?? '—'}x</b><small>çift yön %{gridPlan?.fee_assumption.round_trip_pct ?? '—'}</small></div><div><span>Net Kademe Payı</span><b className={(gridPlan?.estimated_per_cycle.net_edge_pct || 0) > 0 ? 'up' : 'down'}>%{gridPlan?.estimated_per_cycle.net_edge_pct ?? '—'}</b><small>{fmt(gridPlan?.estimated_per_cycle.net_usdt)} USDT / tur</small></div></div>
        <div className="gridActions"><div className="capitalPicker"><span>Paper sermaye</span>{[500,1000,2000].map(value => <button key={value} className={gridCapital === value ? 'selectedCapital' : ''} onClick={() => setGridCapital(value)}>{value.toLocaleString('tr-TR')} USDT</button>)}</div><button className="saveGrid" disabled={!gridPlan || gridBusy} onClick={saveGridPlan}>{gridBusy ? 'KAYDEDİLİYOR…' : 'PAPER HAFIZASINA KAYDET'}</button></div>
        <div className="gridSafetyNote"><b>EMİR MOTORU KİLİTLİ</b><span>{gridMessage} Bu merkez yalnızca plan üretir ve simülasyon yapar.</span></div>
      </div>
      <div className="v5Side">
        <div className="panel gridSimulator"><div className="v5SideHead"><div><h3>GRID PAPER SİMÜLATÖRÜ</h3><small>Son {gridSimulation?.sampled_candles ?? '—'} kapanış · konservatif seviye geçişi</small></div><b className={(gridSimulation?.marked_result_usdt || 0) > 0 ? 'up' : 'waiting'}>{gridSimulation?.verdict ?? 'ÇALIŞIYOR'}</b></div><div className="simResult"><span>İşaretli Sonuç</span><strong className={(gridSimulation?.marked_result_usdt || 0) >= 0 ? 'up' : 'down'}>{(gridSimulation?.marked_result_usdt || 0) >= 0 ? '+' : ''}{fmt(gridSimulation?.marked_result_usdt)} USDT</strong><em>{gridSimulation ? `%${gridSimulation.net_return_pct.toFixed(2)}` : '—'}</em></div><div className="simMetrics"><span><small>Tamamlanan Tur</small><b>{gridSimulation?.completed_cycles ?? '—'}</b></span><span><small>Dolum</small><b>{gridSimulation?.fills ?? '—'}</b></span><span><small>Komisyon</small><b className="down">-{fmt(gridSimulation?.fees_usdt)}</b></span><span><small>Maks. Envanter</small><b>{gridSimulation?.max_inventory_grids ?? '—'} grid</b></span><span><small>Açık Grid</small><b>{gridSimulation?.open_grids ?? '—'}</b></span><span><small>Maks. Düşüş</small><b className="down">%{gridSimulation?.max_drawdown_pct ?? '—'}</b></span></div><small className="simNote">{gridSimulation?.note ?? 'Grid senaryosu hazırlanıyor…'}</small></div>
        <div className="panel gridMemory"><div className="v5SideHead"><div><h3>KALICI GRID HAFIZASI</h3><small>TimescaleDB Paper plan kayıtları</small></div><b>{savedGridPlans?.active_count ?? 0} AKTİF</b></div><div className="savedPlans">{savedGridPlans?.plans.length ? savedGridPlans.plans.slice(0,3).map(plan => <div key={plan.id} className={plan.active ? 'savedActive' : ''}><span><b>{plan.symbol} · {plan.mode}</b><small>{plan.interval} · {fmt(plan.lower)} — {fmt(plan.upper)}</small></span><em>{plan.status}</em>{plan.active && <button disabled={gridBusy} onClick={() => clearGridPlan(plan.id)}>ARŞİVLE</button>}</div>) : <p>Henüz kaydedilmiş Paper grid planı yok.</p>}</div><small className="memoryNote">{savedGridPlans?.message ?? 'Plan hafızası hazırlanıyor…'}</small></div>
      </div>
    </section>
    <section className="panel lab"><div className="labHead"><div><h3>◈ STRATEJİ LABORATUVARI</h3><small>{active?.display || symbol} · {interval} · Geçmiş + Walk-Forward + Stres simülasyonu</small></div><button onClick={runLab} disabled={labLoading}><RefreshCw className={labLoading ? 'spin' : ''}/>{labLoading ? 'TEST EDİLİYOR…' : 'STRATEJİYİ TEST ET'}</button></div>{lab ? <><div className="labMetrics"><div><span>Durum</span><b className={lab.verdict === 'TUTARLI' ? 'up' : lab.verdict === 'TEMKİNLİ' ? 'waiting' : 'down'}>{lab.verdict}</b></div><div><span>İşlem</span><b>{lab.total_trades}</b></div><div><span>Başarı</span><b>{lab.win_rate.toFixed(1)}%</b></div><div><span>Ort. Getiri</span><b className={lab.avg_return_pct >= 0 ? 'up' : 'down'}>{lab.avg_return_pct.toFixed(2)}%</b></div><div><span>Net Simülasyon</span><b className={lab.net_return_pct >= 0 ? 'up' : 'down'}>{lab.net_return_pct.toFixed(2)}%</b></div><div><span>Maks. Düşüş</span><b className="down">{lab.max_drawdown_pct.toFixed(2)}%</b></div><div><span>Profit Factor</span><b>{lab.profit_factor ?? '—'}</b></div></div>{walkForward && <div className="walkForward"><div><span>Walk-Forward</span><b className={walkForward.verdict === 'ZAMAN TESTİNİ GEÇTİ' ? 'up' : 'waiting'}>{walkForward.verdict}</b></div><div><span>Tutarlı Bölüm</span><b>{walkForward.positive_folds}/{walkForward.total_folds}</b></div><div><span>Güncel Bölüm</span><b className={(walkForward.out_of_sample?.net_return_pct || 0) >= 0 ? 'up' : 'down'}>{walkForward.out_of_sample ? `${walkForward.out_of_sample.net_return_pct.toFixed(2)}%` : '—'}</b></div><div className="walkFolds">{walkForward.folds.map(fold => <span key={fold.label} className={fold.net_return_pct >= 0 ? 'up' : 'down'}>{fold.label}: {fold.net_return_pct.toFixed(2)}%</span>)}</div><small>{walkForward.note}</small></div>}{stressTest && <div className="stressTest"><div className="stressHead"><span>V4 STRES LABORATUVARI</span><b className={stressTest.verdict === 'STRESE DAYANIKLI' ? 'up' : 'waiting'}>{stressTest.verdict}</b><em>{stressTest.survived}/{stressTest.total_scenarios} senaryo</em></div><div className="stressScenarios">{stressTest.scenarios.map(scenario => <div key={scenario.label}><span>{scenario.label}</span><b className={scenario.net_return_pct >= 0 ? 'up' : 'down'}>{scenario.net_return_pct >= 0 ? '+' : ''}{scenario.net_return_pct.toFixed(2)}%</b><small>{scenario.status} · DD {scenario.max_drawdown_pct.toFixed(2)}%</small></div>)}</div><small>{stressTest.note} {stressTest.assumption}</small></div>}<p>{lab.note} {lab.cost_assumption}</p></> : <p>Bu araç, seçili zaman diliminde mevcut sinyal kurallarını geçmiş mumlarda, ardışık zaman bölümlerinde ve sentetik stres altında sanal olarak sınar. Sonuçlar tahmin veya yatırım tavsiyesi değildir.</p>}</section>
    <section className="panel journal"><div className="journalHead"><div><h3>◈ İŞLEM HAFIZASI</h3><small>Paper işlemlerin sonuçları ve karar fotoğrafı</small></div><b>{paper?.performance.closed_count ?? 0} KAPANAN İŞLEM</b></div><div className="journalMetrics"><div><span>Gerçekleşen PnL</span><b className={(paper?.performance.realized_pnl || 0) >= 0 ? 'up' : 'down'}>{fmt(paper?.performance.realized_pnl)} USDT</b></div><div><span>Başarı Oranı</span><b>{paper?.performance.win_rate.toFixed(1) ?? '—'}%</b></div><div><span>Ort. İşlem</span><b className={(paper?.performance.average_pnl || 0) >= 0 ? 'up' : 'down'}>{fmt(paper?.performance.average_pnl)} USDT</b></div><div><span>Profit Factor</span><b>{paper?.performance.profit_factor ?? '—'}</b></div><div><span>Demo / Oto / Manuel</span><b>{paper ? `${paper.performance.demo_trades} / ${paper.performance.auto_trades} / ${paper.performance.manual_trades}` : '—'}</b></div></div><div className="tradeTable"><div className="tradeHead"><span>ÇİFT / TİP</span><span>KARAR FOTOĞRAFI</span><span>SONUÇ</span><span>PNL</span><span>KAPANIŞ</span></div>{paper?.recent_trades.length ? paper.recent_trades.map(trade => <div className="tradeRow" key={trade.id}><span><b>{trade.symbol}</b><em className={trade.direction === 'LONG' ? 'up' : 'down'}>{trade.direction} · {trade.source === 'DEMO' ? 'HIZLI DEMO' : trade.source === 'AUTO' ? 'OTO' : 'MANUEL'}</em></span><span>{trade.source === 'DEMO' ? `Canlı fiyatlı eğitim planı · ${trade.guard_mode ?? 'Paper-only'}` : trade.source === 'AUTO' ? `%${trade.signal_confidence ?? '—'} · ${trade.guard_mode ?? '—'} · ${trade.freshness_status ?? trade.gate_status ?? '—'}` : 'Manuel Paper işlemi'}</span><span className={trade.realized_pnl >= 0 ? 'up' : 'down'}>{trade.status}</span><span className={trade.realized_pnl >= 0 ? 'up' : 'down'}>{trade.realized_pnl >= 0 ? '+' : ''}{fmt(trade.realized_pnl)} USDT</span><span>{stamp(trade.closed_at)}</span></div>) : <div className="emptyTrades">Henüz kapanmış Paper işlem yok. Bot veya manuel sanal işlemden sonra burada görünür.</div>}</div></section>
    <section className="panel archive"><div className="archiveHead"><div><h3>◈ KALICI KARAR ARŞİVİ</h3><small>{archive?.message ?? 'TimescaleDB karar arşivi kontrol ediliyor…'}</small></div><b className={archive?.available ? 'up' : 'waiting'}>{archive?.available ? 'KALICI KAYIT AKTİF' : 'BAĞLANTI BEKLİYOR'}</b></div><div className="archiveTable"><div className="archiveRow archiveLabels"><span>ZAMAN</span><span>PARİTE / YÖN</span><span>GÜVEN</span><span>GİRİŞ</span><span>KARAR DETAYI</span></div>{archive?.entries.length ? archive.entries.map(item => <div className="archiveRow" key={item.id}><span>{stamp(item.created_at)}</span><span><b>{item.symbol}</b> <em className={item.direction === 'LONG' ? 'up' : 'down'}>{item.direction}</em></span><span>%{item.confidence.toFixed(0)}</span><span>{fmt(item.entry_price ?? undefined)}</span><span>{item.explanation}</span></div>) : <div className="emptyArchive">Henüz kalıcı karar kaydı yok. İlk Paper işleminden sonra burada görünür.</div>}</div></section>
    <footer><span>API: <b className={healthClass(systemHealth?.api)}>{systemHealth?.api ?? 'KONTROL'}</b></span><span>Mod: <b>GERÇEK ANALİZ</b></span><span>Veritabanı: <b className={healthClass(systemHealth?.database)}>{systemHealth?.database ?? 'KONTROL'}</b></span><span>Emir gönderimi: <b>KAPALI</b></span></footer>
  </main>
}
