# ProTreBot Elite X V28 — Uygulama İçi Borsa Bağlantıları

Production deployment trigger verified through the repository commit pipeline.

## E-posta doğrulama v2 (varsayılan kapalı)

Backend ayarı `PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED=true` yeni doğrulama
katmanını açar. Ayar eksik veya `false` olduğunda eski kayıt, 24 saatlik bağlantı,
e-posta şablonu ve doğrulama ekranı korunur. Ayrı Supabase Auth sistemi yoktur;
frontend özelliğin durumunu mevcut `/api/v22/public` yanıtından öğrenir.

### Önce migration, sonra bayrak

1. Backend kodunu bayrak kapalıyken dağıt. Mevcut hesap deposunun yedeğini al.
2. Backend ortamında `DATABASE_URL` tanımlıyken depo kökünde çalıştır:

   ```powershell
   .venv\Scripts\python.exe -B -m backend.tools.migrate_email_verification_v2
   ```

   Alternatif olarak PostgreSQL konsolunda
   [migration dosyasını](backend/migrations/20261009_001_email_verification_v2.sql)
   çalıştır (`psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f
   backend\migrations\20261009_001_email_verification_v2.sql`).
3. `commercial_feature_migrations` tablosundaki tek seferlik kayıt yalnız migration
   sırasında var olan, silinmemiş hesapları doğrular. Tekrar çalıştırmak daha
   sonra açılmış hesapları doğrulamaz; `active`, rol veya oturum sürümü değişmez.
   `email_verified_at` mevcut canonical kullanıcı kaydının `security` JSONB
   alanında saklanır. Google ile oluşturulan yeni hesaplarda da yazılır.
4. Backend ortamında `PROTREBOT_EMAIL_VERIFICATION_V2_ENABLED=true` tanımlayıp
   yeniden dağıt/başlat. `APP_BASE_URL` gerçek site adresi olmalı; bağlantı
   `/verify-email?token=...` biçiminde kalır. Frontend için ikinci bayrak yoktur.
5. Mevcut Gmail/Resend sağlayıcısını koru. Resend kullanılıyorsa `EMAIL_PROVIDER=resend`,
   `RESEND_API_KEY` ve doğrulanmış `EMAIL_FROM`; eski Gmail yolu için mevcut
   `GMAIL_*` ayarları gerekir. Bu değerleri `VITE_` değişkenlerine koyma.

Yerel SQLite hesabında açıkça belirtilmiş dosyalarla aynı tek seferlik geçiş:

```powershell
.venv\Scripts\python.exe -B -m backend.tools.migrate_email_verification_v2 --sqlite "<account_settings.sqlite3 yolu>" --snapshot "<v22_commercial_state.json yolu>"
```

Yeni 32 bayt rastgele bağlantılar 30 dakika geçerli; yalnız SHA-256 özetleri
mevcut hesap deposunda tutulur. `used_at`, iptal ve doğrulama kaydı aynı
transaction'da yazılır. Yeni bağlantı öncekileri iptal eder; bayrak öncesindeki
24 saatlik bağlantılar yeni bir bağlantı istenmediği sürece eski son kullanma
zamanına kadar çalışır. Yeniden gönderim yolları tek kalıcı kullanıcı sayacını
paylaşır: 60 saniye bekleme, ilk gönderim dahil kayan bir saatte en fazla beş mail.

E-posta bağlantısı tek başına oturum açmaz. Yeni kayıt yapılan cihaza 30 dakikalık
HttpOnly, Secure, SameSite=Lax bekleyen kayıt çerezi verilir. Bu yetki yalnız
durum/yeniden gönderim ve doğrulandıktan sonra tek seferlik oturum çevirme içindir;
2FA veya değişmiş kimlik doğrulama sürümü normal girişe yönlendirir. Farklı cihaz
yalnız doğrulama başarısını ve e-postası doldurulmuş giriş ekranını görür.
Doğrulanmamış müşteri için mevcut 403, OWNER istisnası, abonelik/arm/consent/2FA
ve LIVE/Demo yürütme kuralları değişmez.

`/verify-email` sunucudan durumu okur; dört saniyede bir ve görünür sekmeye
dönüşte sorgular, aynı anda sorguları çoğaltmaz. Başarı sadece sunucu onayıyla
gösterilir. Sora/Manrope npm paketlerinden yalnız lazy doğrulama ekranında yüklenir;
Google Fonts isteği yoktur. Yeni koyu HTML/text şablonu yalnız doğrulama mailinde
kullanılır; diğer mail türleri korunur.

Regresyonlar: [backend lifecycle testleri](backend/tests/test_email_verification_v2.py)
ve [iki frontend için tarayıcı testleri](frontend/tests/email-verification-v2.spec.ts).
Migration ve mail teslimatı bu belgede bir talimattır; geliştirme testleri gerçek
veritabanına, e-posta sağlayıcısına veya borsaya istek göndermez.

## Authentication security

- Password login keeps the existing request limiter and adds durable failure-only
  limits: five failures per normalized email and twenty per client IP in fifteen
  minutes. Locks increase from fifteen to thirty to sixty minutes; escalation
  resets after twenty-four hours without failures. Unknown emails use the same
  counters and generic error as inactive accounts and wrong passwords. Forwarded
  IP headers are not trusted directly by the application.
- All TOTP and recovery-code verification uses one account-wide limit: ten wrong
  codes in an hour locks verification for one hour, including new login
  challenges. The five-attempt challenge limit, single-use recovery codes and
  TOTP replay checks remain in place. Required durable-store outages fail closed.
- Registration, password reset and password change share
  `backend/app/password_policy.json` through Python/browser helpers: 10–256 Unicode
  code points, ASCII uppercase/lowercase/digits and the existing registration
  non-alphanumeric symbol rule. Login and existing-password hash migration do
  not require adopting the new-password policy.
- Duplicate registration sends an existing-account notice without creating
  another account. Public replies have the same status, message and envelope,
  the same password-hashing cost and mail delivery path, and a common two-second
  minimum response time.
  Duplicate verification-status proofs are unlinked to the existing account;
  real proofs read canonical security. Browser polling is every four seconds
  with no overlapping status requests; the resend countdown stays sixty seconds.
- Password change and 2FA enable/disable revoke only the same account's other
  sessions. The current session receives a renewed token with the same session
  ID and original expiry, and a new auth version. Browser renewal uses HttpOnly
  cookies; native clients must use the replacement `token` returned by the API.
  Password reset deliberately revokes **all** sessions of the target account.
  Other users' sessions are never revoked by these operations.
- Existing unverified-email behavior is unchanged: correct credentials can
  issue a verification-gated session, but ordinary protected API access is 403.
  Verification resend and the existing OWNER exception remain unchanged.
- `PROTREBOT_EXPOSE_DEV_TOKENS` defaults off. Startup rejects enabling it unless
  `PROTREBOT_ENVIRONMENT` is explicitly `development` or `test`, with no production
  or hosted deployment signal. Production indicators override that local mode;
  request-time checks also prevent exposing codes after configuration changes.
  Set `PROTREBOT_ENVIRONMENT=production` in production; never enable dev tokens
  on Render, Vercel or Heroku.

Offline regressions: `backend/tests/test_auth_security_requirements.py`,
the existing account/commercial auth suites, and
`node --test tools/password-policy.test.mjs tools/browser-session.test.mjs tools/browser-request.test.mjs`.
Browser auth checks use the shipping preview (4176), not Vite's auth-bypassing
test-mode workspace: `frontend/tests/email-verification.spec.ts`,
`frontend/tests/account-settings.spec.ts` and
`frontend/tests/auth-request-lifecycle.spec.ts`, with mocked APIs and external
network/WebSockets blocked.

## Browser entry and session recovery

Owner verification, authentication and Master Trade access reads have a
15-second deadline covering the response body as well as connection headers.
Lazy workspace downloads, including the Live screen, are also bounded;
stalled or failed downloads reach the existing safe recovery screen rather
than an indefinite spinner or a silent blank fallback.
Timeouts show an explicit retryable error and never grant access. Authentication
POSTs are not automatically retried. Navigation/unmount cancels stale session
reads; earlier responses cannot clear a newer verified session.
Temporary session failures preserve only the existing public cookie-session
hint, not authenticated access. Use **Oturumu yeniden kontrol et** to verify
again. An owner denial unlocks the form without waiting for a stalled logout;
logout failures remain logged. A Master Trade verification outage is shown as
unavailable rather than an invented subscription denial.

Local preview URLs require a running preview server; they are not deployment
addresses. These client changes do not alter exchange order paths, LIVE safety
gates or the default-off Original Demo flags.

## Offline backend regression tests

Native Windows spawn/asyncio tests run their unchanged assertions in a
main-guarded child harness. This prevents an external pytest launcher from
running the complete suite again when multiprocessing imports its entry point.
The harness blocks external networking, DNS and protected research paths in
both the test process and spawned workers. Only the standard library's internal
loopback socketpair is permitted, including through existing offline wrappers.
The full-suite runner, default temporary directory and application sources
remain unchanged.

The blank `.env.example` is a settings inventory, not a runnable environment:
leave unused optional entries unset, as its header requires. `env.example` and
the deployment manifest contain the assistant defaults. Empty/invalid explicit
assistant settings still raise validation errors; blank placeholders are not
passed to the settings parser by the template regression test.

## Kais Original v2 Demo controls

Both `PROTREBOT_BINANCE_DEMO_KAIS_ORIGINAL_V2_ENABLED` and
`PROTREBOT_BINANCE_DEMO_KAIS_ORIGINAL_V2_SEND_ORDERS` default to **false**.
Restart the backend after changing its process environment. The Demo
Automation screen shows these server settings read-only; it cannot enable
them. Select **Kais Original v2** explicitly to send
`strategy_id=kais-original-v2-demo-v1`. The existing server-default selection
retains its behavior: feature flag on selects Original, flag off selects legacy.
Already-open Original plans retain their fixed-stop exit policy after flag-off.

For the first no-order check, set ENABLED=true and SEND_ORDERS=false.
Sign in with your own user session, save/verify only Demo/Testnet credentials
through the existing connection screen, then open Demo > Automation.
Stop any automation, arm with `DEMO`, and type `DEMO OTOMATİK` in the second
confirmation field. Click **Emirsiz tek karar döngüsü**, not the automation
start button. This explicit endpoint cannot call the entry executor, including
when both server flags are true. It records eight canonical decisions in the
owner's dry-run plans/journal, creates no exchange position, does not enable
automation and reuses the same candle's records on repeated clicks.
Failed status/data/authorization checks are explicit errors, not permission
to send. Do not use the legacy smoke-test button as an Original parity test.

The fixed profile ignores editable legacy settings: eight symbols,
leverage 3, risk 3 USDT, five positions / two per direction, UTC three entries,
10 USDT daily loss and three consecutive losses. TP1 is minimum-aware
floor(quantity * 0.60), no TP2 exit; the remainder uses TP3 or the initial stop.
No BE/trailing. Native next-open/STOP_FIRST/funding/future-gap modeling is not
a guarantee about actual Testnet execution.

Only a later, deliberate change to SEND_ORDERS=true permits Original orders,
and existing owner/session, arm and explicit automation confirmation checks
still apply. A no-order check does **not** certify actual venue fills,
credentials, funding or profitability. LIVE settings and defaults are unchanged.

## LIVE risk-gate repairs (stage 1)

Total position exposure uses `abs(quantity) * mark_price`, including normalized
positions without a pre-existing notional. Unknown/unpriced exposure blocks
entry. The shared default and API/UI ceiling are 350 USDT. Current consecutive
losses are counted newest-first; starting SHORT-only policies no longer probes
a forbidden LONG. LIVE performance preserves direction and reports
`demo_only=false`; DEMO defaults and immutable initial-risk/R accounting remain
unchanged.

Periodic Stop verification requires exact owned identity, planned trigger,
symbol, closing side, STOP_MARKET type, full-position close, active status and
MARK_PRICE working type. Mismatches are UNKNOWN and lock new entries; they are
not treated as permission to cancel/replace an uncertain order.

LIVE entry requires `require_isolated=true`, verified single-asset mode and a
verified ISOLATED symbol configuration. Multi-assets/CROSSED configurations
and Binance -4168 are rejected, never silently accepted. If account information
does not include its asset mode, a separate signed read verifies it; unknown
mode fails explicitly. Maintenance brackets are read before entry submission.
`policy.liquidation_buffer_pct` defaults to 0.5 (range 0.05-5), measured as a
percentage of the entry price, not of Stop distance. The conservative isolated
estimate includes maintenance tiers/deductions and entry/exit fee reserves.
The directional Stop must be before the estimated liquidation price by at
least that buffer. Missing/inconsistent tiers, unsupported non-unit
`notionalCoef`, or insufficient distance reject entry. The proof is persisted
with the intent/plan and restored after restart.

This is a pre-fill estimate, not an exchange liquidation guarantee: future
funding, adverse gaps/slippage, fee differences and external account changes
can invalidate the buffer. No liquidation/PnL data is invented. A Stop order
also uses Binance's existing price-protection setting and may be delayed by
extreme mark/contract divergence.

TP monitoring has **no automatic market-close fallback**. Unbacked targets,
including rejected/below-minimum targets or a TP response without an order ID,
are explicitly `UNPROTECTED` / `TP KORUMASIZ (İZLEME)`. Entry/activity messages
distinguish pending protection, verified Stop with exchange-installed TP,
unprotected TP, safety-close submission and an authoritative no-position read.
An accepted safety-close submission is not a verified closure.

`PROTREBOT_LIVE_REQUIRE_DEMO_CERTIFICATE` can restore the advanced Demo
certificate gate. Its default is false (existing waiver); true requires
`DEMO SERTİFİKALI`. No credentials are needed for this configuration.
The new policy field changes the policy digest: existing approvals/scoped
authorizations may need renewal through the unchanged approval/2FA flow.
No entry-strategy, BE/trailing or SHORT alignment filter changes are included.

Offline regressions: `backend/tests/test_v25_risk_gate_repairs.py` plus the
existing execution, partial-fill, ownership, scope, direction and R suites.

## LIVE signal observations (stage 2)

`PROTREBOT_SIGNAL_JOURNAL_ENABLED` defaults to **true**; set it to `false`
to disable observation. `PROTREBOT_SIGNAL_JOURNAL_PATH` defaults to
`DATA_DIR/live-signal-journal.sqlite3`. This must be a separate `.sqlite3`
file: other applications' databases and native persistence files are refused.
No exchange request, strategy, approval, order, protection or native R
calculation is added or changed.

The execution path only submits events to a bounded, nonblocking queue.
SQLite writes, indicator projections, shadow gates and diagnostics run in a
daemon worker. A slow/failed store or full queue never waits on or rejects
an order. Overflow/storage errors are logged; overflow loses observations,
not trades. Shutdown drains for at most two seconds off the event loop.
If the OS cannot start the worker, enqueue errors remain in bounded memory
until a worker recovers; a process crash can lose queued observations.
This is best-effort research telemetry, **not an audit ledger**.

Signals use SHA-256 of uppercase symbol, direction and decision-candle close
epoch seconds. Repeated signals do not duplicate; per-round evaluations are
retained separately in `rounds`. A later acceptance promotes the main signal
record, never downgrades it. `ACCEPTED` means the native execution accepted
the entry, not profitable execution or verified Stop/TP protection.
First rejection is from the actual short-circuiting path; all later pure
gates are also evaluated in the worker. `gate_results` distinguishes ACTUAL
and SHADOW results; unavailable evidence has `passed=null` and appears in
`unknown_gates`. No speculative gate changes the native decision.

Stored features include both direction scores, confidence, trap/breakout,
per-frame confidence/direction and native aggregate MTF alignment,
EMA20/50/200, MACD histogram, RSI, ATR, ADX, BB width
`(upper-lower)/middle`, volume ratio, spread bp and funding rate.
`alignment_15m/1h/4h` are the native per-frame confidence scores, not
calibrated probabilities. Early universe rejects often have no candles,
spread or funding evidence: these stay null/UNKNOWN. A guessed candle
anchor has `decision_time_verified=false`; outcomes are not produced for it.
The separate offline import can supply time-valid missing evidence without
adding network calls. Known native features and decisions are preserved;
supplementary gate results use `enriched_*` fields.

### Offline jobs and exports

From the repository root, using a separate data directory:

```powershell
$db = 'C:\observations\live-signal-journal.sqlite3'
.\.venv\Scripts\python.exe backend\signal_journal_cli.py --db $db summary
.\.venv\Scripts\python.exe backend\signal_journal_cli.py --db $db export --format csv --output C:\observations\signals.csv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r backend\requirements-observation.txt
.\.venv\Scripts\python.exe backend\signal_journal_cli.py --db $db export --format parquet --output C:\observations\signals.parquet
.\.venv\Scripts\python.exe backend\signal_journal_cli.py --db $db import-data --input C:\observations\evidence.json --as-of 2026-01-01T04:00:00+00:00
.\.venv\Scripts\python.exe backend\signal_journal_cli.py --db $db fill --as-of 2026-01-01T04:00:00+00:00
```

CSV/journaling use only the standard library; Parquet has an optional pinned
dependency. Nested fields export as JSON; missing values are blank/null,
including uncomputed outcome columns. Summary reports decision counts,
first/all rejection distributions and unknown gates.

Offline evidence JSON has optional `candles`, `market` and `trades` arrays:

```json
{
  "candles": [{
    "symbol": "BTCUSDT", "interval": "15m",
    "rows": [{"time": 1767224700, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}]
  }],
  "market": [{
    "signal_id": "<existing signal SHA-256>", "observed_at": 1767225600,
    "spread_bp": 2.5, "funding_rate": 0.0001
  }],
  "trades": [{
    "intent_id": "<existing accepted intent>", "observed_at": 1767240000,
    "gross_pnl": 4, "commission_usdt": 0.2, "commission_complete": true,
    "funding_usdt": -0.1, "funding_complete": true
  }]
}
```

Candle dictionaries use open epoch **seconds**, or use original Binance
array rows with open/close **milliseconds**. Supported intervals are
15m/1h/4h. Only closed, valid OHLCV is stored; a provider-declared future
close is excluded. Market evidence must be observed no later than the
original scan and job cutoff; future market evidence is counted/ignored.
Optional market `spec` and `brackets` use the existing sizing/maintenance
shapes for pure liquidation evaluation. Indicator recomputation uses only
candles closed by the original decision. Trade evidence requires an accepted
intent and observation time between decision and job cutoff; completeness
declarations are trusted offline inputs, not newly authenticated exchange
proof. Import cannot overwrite the frozen initial risk.

`fill` is a separate job: schedule it externally or run it after offline
imports. It never fetches data. Exact contiguous 4/8/16 closed 15m bars are
required for 1h/2h/4h raw price returns; gaps stay null. Four-hour hypothetical
results use the recorded entry/Stop/TP1/TP3, 60% TP1 and remainder TP3,
without BE/trailing. MAE/MFE are price excursions in initial per-unit Stop
distance R through the exit bar, not reconstructed intrabar position equity.
Stop-and-target in one bar uses STOP_FIRST and flags ambiguity; extrema
later within an exit bar cannot be timed with OHLC. Stop/target fills are
modeled at trigger prices, so gaps and real fill quality are not simulated.
Gross and estimated fee/slippage-adjusted fields are separate (policy costs,
normally 5/3 bp per side); hypothetical funding is explicitly excluded.
Earlier `as-of` reruns are refused once later outcomes exist; use an isolated
database for an earlier research snapshot.

Verified entry-position averages/owned trade fills record actual
fill-minus-original-expected-price slippage. Owned closure evidence records
gross PnL and complete USDT fees; missing/non-USDT/incomplete fees leave net
R unknown. Funding is null until complete offline evidence is supplied;
then observer `net_pnl = gross - fees + funding`, `net_r = net_pnl /
initial_risk_usdt`. Missing/nonpositive risk produces no R. Native
funding-excluded accounting and immutable initial risk remain unchanged.
This journal is for one execution/account lineage per database; multi-account
research must use separate instance/database paths rather than merging
identical native intent IDs. There is no retention policy or Stage 3
backtester/performance claim in this stage.

Offline regressions: `backend/tests/test_signal_journal.py`, together with
the existing LIVE scope, execution, partial-fill, protection and R suites.

## Offline conditional baseline (stage 3)

This is **CONDITIONAL BASELINE / KOŞULLU BASELINE**, not a verified historical
exchange replay. It is a standalone offline CLI, never started by the web
application. No LIVE execution file or stage 1/2 commit is changed.

The required assumptions are listed first in every result:

- Current `exchangeInfo` tick/step/minimum-notional rules, **not historical**.
- Current public maintenance brackets held constant, **not historical**.
- Constant spread (baseline 2 bp). At 1/2/5 bp below the native 8 bp ceiling,
  the historical spread gate is effectively untested, not validated.
- Entry at the decision candle's following **15m open plus adverse slippage**;
  a complete instantaneous MARKET fill is assumed. Baseline slip is 3 bp,
  commission is 5 bp per side. The native planned-spec initial risk is frozen.
- One scan per closed 15m candle, not intrabar 120-second scans. Continuous
  authorization/session availability and 1,000 USDT initial equity are modeled.

The engine calls the existing canonical historical decision, which calls
`analysis.analyze` and the native MTF decision. It passes only CLOSED 15m/1h/4h
candles, using the native 260-fetch-equivalent 259 closed-candle window.
Future primary/higher-frame values cannot influence that decision. Existing
`risk_sized_order`, `build_live_spec`, protection/isolated validation,
`evaluate_entry_gates`, `daily_execution_metrics`, liquidation estimator,
`floor_step`, initial-risk and R helpers are reused. The native MARKET sizing
and fee/slippage minimum-reward filter execute against a local two-response
client; signed and unsupported endpoints raise immediately. No order function
is called. The CLI alone memo-wraps the native pure analyzer by its exact
OHLCV input and restores the binding on exit; cached and uncached decisions
are regression-compared. No indicator or strategy implementation is copied.

Stop and TP triggers use historical **MARK_PRICE** candles. TP1 closes
`floor_step(actual_quantity * 0.60, step)` only when native quantity/notional
minimums allow it; otherwise it remains explicitly unprotected, as in LIVE
monitoring-only behavior. TP3 closes the remainder. There is no BE, trailing,
time stop, pullback entry or forced period-end close.

Intrabar Stop/target ambiguity defaults to STOP_FIRST; TP_FIRST is a separate
sensitivity run. Intrabar market exits use trigger price plus adverse slip;
opening gaps use contract open plus slip. Intrabar exits are timestamped at
the last second of the bar; equal-time closed-equity points are grouped.
Funding uses the archived settlement rate, settlement-bar mark open and
remaining quantity, preserving subsecond settlement timestamps. Exact
opening settlements occur before opening exits; later settlements precede
modeled intrabar exits. This ordering and mark-open valuation are OHLC
approximations, not tick-level fills.

Price-protection divergence delays, real API/protection failures, liquidation
penalties after gaps/funding, BNB fee discounts and orderbook depth are not
simulated. Liquidation proof is the same **pre-entry** buffer gate as LIVE,
not a guarantee of protection after entry. Missing funding remains null and
is excluded from primary net-R statistics; `net_*_ex_funding` is explicitly
separate. Unclosed positions have no realized net R. A missing open-position
price bar creates an unverified data-gap outcome and activates the shared
PnL-verification gate rather than inventing a Stop fill.

### Data and commands

All archives/results must be **outside the repository**. Public acquisition
is a separate program; the replay program has no downloader or real network
client. Acquisition supports only static USD-M archives and, with explicit
`--public-metadata`, two unauthenticated read-only endpoints: exchangeInfo
and the public website's maintenance table. No API key, signed request or
order endpoint is used.

```powershell
$data = 'C:\research\protrebot\data'
$output = 'C:\research\protrebot\baseline'
.\.venv\Scripts\python.exe backend\backtest_download.py --output $data --start-month 2025-02 --end-month 2026-09 --public-metadata
.\.venv\Scripts\python.exe backend\backtest_cli.py --data $data --metadata "$data\current-metadata.json" --output $output --start 2025-04-01T00:00:00+00:00 --end 2026-10-01T00:00:00+00:00 --conditional-current-metadata
```

February/March are warmup only; the evaluation is the last 18 **complete**
months, April 2025 through September 2026. Archive SHA-256 checksums are
verified during download and load. Duplicate candles are reported; conflicts,
invalid OHLCV, misaligned/provider-invalid timestamps and altered files fail
explicitly. Missing candles/archive files and funding coverage gaps are
reported; no candles or funding payments are synthesized.

Static daily mark-price supplements can repair a diagnosed monthly gap:

```powershell
.\.venv\Scripts\python.exe backend\backtest_download.py --output $data --start-month 2026-06 --end-month 2026-06 --repair-mark-date 2026-06-29
```

Preserve the original monthly data-quality report before repair. Repairs
and their checksummed daily sources remain in the manifest and quality
output; only actual official candles may fill a gap.

Local CSV/Parquet is also supported via the same `manifest.json` contract.
Each `archives` entry supplies `symbol`, `kind` (`klines`, `markPriceKlines`,
`fundingRate`), `interval` (`15m`, `1h`, `4h`; null for funding), `month`,
relative `path`, actual `sha256` and `status="OK"`. CSV/Parquet candle
columns are `time` (seconds) or `open_time` (Binance milliseconds),
`open/high/low/close/volume`, and `quote_volume` for the 24h universe filter.
Original Binance CSV ZIPs/headerless klines are supported. Funding requires
`calc_time`/`time`, `last_funding_rate`/`rate`, `funding_interval_hours`.
Metadata JSON contains `historical`, `observed_at`, an `exchange_info`
symbol/filter response and `brackets` keyed by symbol with the native
maintenance-tier shape. Current metadata requires the explicit conditional
flag. Parquet uses the existing optional observation dependency.

### Outputs and measurements

- Seven JSON/trade-CSV pairs: baseline STOP_FIRST (spread 2/slip 3),
  TP_FIRST (2/3), and STOP_FIRST spread **1/2/5 × slip 3/6**.
- `comparison.json`/CSV report expectancy/PF differences against baseline.
- `data-quality.json` includes source hashes, counts, gaps, duplicates,
  funding status and repair provenance.
- Trade fields match stage 2 accounting names: signal/intent IDs,
  gross PnL, fees, actual fill/slip, funding, net PnL, immutable initial risk,
  `net_r`; quantities, exits and ambiguity/unprotected-target flags are added.
- Summary includes closed/open/unverified counts, net expectancy R, net
  USDT profit factor, win rate, average win/loss in USDT and R, closed-curve
  max drawdown in USDT/R, LONG/SHORT breakdown and funding-null count.
- Seeded paired-trade bootstrap gives percentile 95% expectancy/PF intervals
  (2,000 samples, seed 2026). No-loss PF samples are explicitly unbounded,
  never converted to a fake finite PF; IID intervals do not account for
  serial dependence or prove future profitability.
- Stage 1 gate counts include each named gate, even zero counts, plus
  evaluations and first/all rejection distributions. Counts concern
  candidates reaching that native gate, not a speculative shadow audit of
  signals already rejected by quality/MTF. Multiple failed gates can count
  the same candidate, so all-rejection counts are not disjoint.

Offline regressions: `backend/tests/test_backtest_baseline.py` plus the
unchanged stage 1/2, canonical, scope, protection and accounting suites.
Stage 4 variants and strategy tuning are deliberately absent.

## Offline diagnosis (stage 3b)

This is an opt-in **conditional diagnosis**, not a strategy change or a LIVE
policy upgrade. The stage 3 engine, signals, sizing, exits and LIVE source stay
unchanged. The separate command varies only total exposure **350 / 700 / 1050 /
unlimited**. The LIVE sanitizer normally clamps values to 350. Inside each
serial offline replay only, a scoped adapter replaces that one sanitized field
and restores the original function even on exceptions. Unlimited uses infinity
internally and NULL in exported policy; unknown position exposure is still
rejected. No LIVE API/config limit is raised.

```powershell
$data = 'C:\research\protrebot\data'
$output = 'C:\research\protrebot\diagnosis'
.\.venv\Scripts\python.exe backend\backtest_diagnostics_cli.py --data $data --metadata "$data\current-metadata.json" --output $output --start 2025-04-01T00:00:00+00:00 --end 2026-10-01T00:00:00+00:00 --conditional-current-metadata --reference-baseline 'C:\research\protrebot\baseline\stop_first-spread2-slip3.json'
```

No download/network client is used. Closed-candle native decisions are computed
once and reused across all caps. `--workers` accepts 1-4 spawned processes
(default: at most four, no more than half the reported CPUs); serial and
parallel native results are regression-tested. Workers compute only independent
symbol signals, not portfolio/risk/order decisions. Replays are serial. The
optional reference requires exact 350 parity for trades, policy, summary,
bootstrap and rejection counts before any higher-cap result is accepted.

Outputs are outside Git:

- `diagnosis.json` and `exposure-comparison.csv`: counts, expectancy, USDT PF,
  closed-curve drawdown in USDT/R, trade/day/week 95% bootstrap intervals.
- Four `cap-*.json` / trade-CSV / monthly-CSV sets; complete funding/R and
  inherited conditional-current-metadata assumptions remain explicit.
- Added and removed trades versus 350 **and the preceding cap**, matched by
  deterministic signal ID; common-trade changes are counted too. Higher caps
  are not necessarily supersets: portfolio occupancy and daily gates alter
  later eligibility. Added-trade expectancy is measured on those trades alone,
  not inferred from subtracting aggregate expectancy.
- Exit groups: Stop before TP1, TP1 then Stop, TP3, other/unclosed; counts,
  eligible mean net R, MAE/MFE median and quartiles, including pre-Stop MFE.
- LONG/SHORT totals and every UTC **closure month**, including empty months
  with NULL expectancy/PF/PnL rather than fabricated results.

**Excursion bounds:** MARK_PRICE price movement is scaled by original quantity
over immutable initial risk, never using the TP1 remainder as the denominator.
The actual modeled entry fill is the origin (including entry slippage);
fees, funding and exit slippage are not included in price excursions.
MAE is positive adverse movement and MFE positive favorable movement.
`mae_r` / `mfe_r` are conservative lower bounds at the terminal candle;
`*_upper` adds possible pre-exit extremes. With STOP_FIRST, the terminal high
cannot be assumed to precede the Stop. TP3 censors movement beyond its trigger;
opening-gap exits use only the open. No candle after closure is read. Missing
marks, initial risk or verified closure produce explicit NULL/status values.
These are not tick-level exact MAE/MFE or proof of the pre-Stop path.

**Bootstrap:** unchanged paired-trade IID intervals are compared with whole
UTC entry-day and Monday-week clusters. All calendar clusters, including empty
days/weeks and partial boundary blocks, are sampled with replacement. Each draw
uses every eligible trade within selected clusters; expectancy is trade-weighted,
PF is resampled aggregate positive/negative net USDT PnL. Empty resamples are
counted and omitted, not treated as zero; fewer than two active clusters returns
NULL intervals. No-loss PF tails are explicitly unbounded and all-zero-PnL PF
is undefined. Clustering preserves within-block, not cross-block dependence.
Monthly differences describe temporal concentration; they do not establish
ADX/trend/volatility regime causation. No Stage 4, tuning or walk-forward claim.

### Measured conditional diagnosis

BTCUSDT/ETHUSDT/SOLUSDT/BNBUSDT, 2025-04-01 through 2026-09-30 UTC;
current metadata observed 2026-10-06, equity 1,000 USDT, fees 5 bp/side,
slippage 3 bp/side, spread 2 bp, STOP_FIRST. The 350 replay matched the
stage 3 trades/summary/bootstrap/gates exactly. Funding-null and remaining
data-gap counts are zero. These are conditional historical results, not a
verified historical-rules baseline or a prediction.

| Exposure cap USDT | Trades | Mean net R | Net USDT PF | Closed DD USDT | Added vs 350 | Added mean net R | Removed vs 350 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 350 | 103 | -0.0999 | 0.7624 | 49.98 | 0 | NULL | 0 |
| 700 | 178 | -0.0559 | 0.8755 | 102.33 | 104 | -0.0538 | 29 |
| 1050 | 185 | -0.0563 | 0.8776 | 96.83 | 110 | -0.0446 | 28 |
| Unlimited (offline) | 188 | -0.0547 | 0.8766 | 97.58 | 113 | -0.0422 | 28 |

Every shared trade retained identical fill/quantity/closure/net R. Against the
preceding cap, 1050 added 18 (mean +0.0023 R) and removed 11; unlimited added
4 (mean +0.3910 R) and removed 1. Tiny incremental samples are not evidence
of an improved strategy.

Bootstrap: 2,000 draws, seed 2026; 548 calendar days / 79 Monday weeks.
Baseline has 99 active days and 56 active weeks. No empty/no-loss resamples
occurred in the actual clustered runs.

| Cap | Sampling | Mean net R 95% CI | USDT PF 95% CI |
|---|---|---|---|
| 350 | Trade | [-0.3245, 0.1146] | [0.4495, 1.1672] |
| 350 | Day | [-0.3253, 0.1369] | [0.4529, 1.2020] |
| 350 | Week | [-0.3357, 0.1502] | [0.4346, 1.2155] |
| 700 | Trade | [-0.2300, 0.1342] | [0.6140, 1.2407] |
| 700 | Day | [-0.2398, 0.1459] | [0.6047, 1.2689] |
| 700 | Week | [-0.2609, 0.1682] | [0.5718, 1.3100] |
| 1050 | Trade | [-0.2313, 0.1243] | [0.6174, 1.2176] |
| 1050 | Day | [-0.2392, 0.1421] | [0.6098, 1.2619] |
| 1050 | Week | [-0.2572, 0.1777] | [0.5831, 1.3351] |
| Unlimited | Trade | [-0.2238, 0.1152] | [0.6206, 1.1952] |
| Unlimited | Day | [-0.2365, 0.1439] | [0.6169, 1.2603] |
| Unlimited | Week | [-0.2568, 0.1793] | [0.5838, 1.3405] |

Baseline exits: Stop before TP1 **43 / -1.1719 R**; TP1 then Stop
**34 / -0.1817 R**; TP3 **26 / +1.7799 R** (count / mean net R).
MFE quartiles before any TP1 on Stop trades: **0.1190 / 0.4155 / 0.6824 R**.
Pre-Stop MFE on TP1-then-Stop trades: **1.1683 / 1.5005 / 2.0155 R**.
All-trade MAE quartiles: **0.9641 / 1.0313 / 1.0330 R**; MFE:
**0.5188 / 1.1370 / 2.8522 R**. Bound columns remain separate even though
these aggregate quartiles coincide in this dataset.

Baseline LONG: 69 trades, +0.0100 mean R, PF 0.9550, net -4.36 USDT;
SHORT: 34 trades, -0.3231 mean R, PF 0.4683, net -33.78 USDT.
Mean R weights trades equally; USDT PF/PnL weights their actual initial
risks, so a slightly positive mean R need not imply positive USDT profit.
Largest negative closure months were 2025-06 (-15.40), 2026-04 (-20.31)
and 2026-09 (-22.18 USDT); 2026-02 had no trades. Monthly/direction details
are retained in every scenario's JSON/monthly CSV. This temporal concentration
does not establish a particular trend/volatility regime as its cause.

All caps still have point PF below 1 and negative mean R; every displayed
95% expectancy interval crosses zero. Relaxing exposure is not by itself a
demonstrated profitability fix and roughly doubles closed-curve drawdown.

Regressions: `backend/tests/test_backtest_diagnostics.py` and the unchanged
stage 3/native risk/accounting suites: 20 new tests, 743 passed / 216 subtests;
one optional PostgreSQL skip and one previously proven stale-source baseline
deselection. No strategy, LIVE source or stage 4 change.

## Offline exit ablation (stage 4)

`backend/backtest_ablation_cli.py` is a new opt-in offline command. Existing
baseline/diagnosis/LIVE modules are unchanged. Default is **A only**, STOP_FIRST;
new exits require `--all-variants`. It consumes the original stage 3 **350 USDT,
spread 2 bp, slip 3 bp** baseline, checksums local data, and rebuilds only its
approved entry specs with the original canonical/sizing/order helpers. No
market rescan, new entry logic, network client or real order is used.

```powershell
$data = 'C:\research\protrebot\data'
$baseline = 'C:\research\protrebot\baseline\stop_first-spread2-slip3.json'
.\.venv\Scripts\python.exe backend\backtest_ablation_cli.py --data $data --metadata "$data\current-metadata.json" --baseline $baseline --output 'C:\research\protrebot\ablation' --conditional-current-metadata --all-variants --tp-first-sensitivity --include-time-stop
```

The parameter plan and baseline/metadata SHA-256 are written **before results**.
Fixed presets, no tuning after viewing outcomes:

| Preset | Only intentional change |
|---|---|
| A | Original exits/control; exact stage 3 trade/accounting/summary/bootstrap parity required |
| B | Post-TP1 fee/slippage break-even Stop |
| C_ATR2 / C_ATR3 | Post-TP1 Chandelier trailing, multiplier 2.0 / 3.0; no BE |
| D_ATR2 / D_ATR3 | B plus the same trailing; choose the tighter Stop |
| E40 / E50 / E60 | TP1 fraction 40% / 50% / 60%; original Stop/TP3, no BE; E60 is a second control |
| G_NO_SHORT | Deliberately remove baseline SHORT entries; exits unchanged |
| F8 / F16 / F32 | Isolated time stop; run last, in a separate output group |

**Frozen primary cohort:** every non-G preset retains the same original entry
time, quantity, actual fill and immutable risk. Each trade is an independent
counterfactual, so this primary statistic is not an executable risk-gated
portfolio. G explicitly excludes SHORTs; paired exit delta is calculated only
on common LONG trades and does not represent the direction-selection effect.

**Separate feasibility audit:** reattempt the ordered baseline entry schedule
using native exposure, same-direction, daily loss/count/streak, duplication,
open-loss and available-balance gates under changed exits. Report rejected
entry IDs/reasons, matched performance, added/removed sets, and results
separately. This audit does **not** recover formerly rejected/new scan signals.
A must match the original stage 3 in both frozen and scheduled modes.

**User-selected definitions, fixed before results:**

- Chandelier uses the best **closed MARK_PRICE high/low since the TP1 bar**,
  minus/plus 2.0 or 3.0 times native `analysis.atr` (14) on closed contract
  candles. Stops never loosen and TP3 stays active. Native tick rounding.
- BE is based on actual entry fill (entry slip already included) and covers
  per-unit 5 bp entry fee, 5 bp exit fee and 3 bp exit slip on the remainder.
  LONG threshold is `entry * (1 + fee) / ((1 - slip) * (1 - fee))`;
  SHORT is `entry * (1 - fee) / ((1 + slip) * (1 + fee))`. Round toward coverage.
  Funding remains in net PnL, not the BE threshold. Gaps can still lose money.
- Updates use the prior **closed** candle and become active at the next
  candle's open, before checking its Stop/TP events. Never retroactively apply
  a new Stop to the source candle. If update and trigger occur in the same
  active execution candle, STOP_FIRST is pessimistic; TP_FIRST is sensitivity.
- F checks whether mark-price MFE ever reached **+0.3 initial R** in N complete
  holding candles. If not, exit at the following contract open plus adverse
  slip. Original protective Stop has priority on a simultaneous opening gap.
  TIME_STOP is a fourth category, never mislabeled as a protective Stop.
- TP1 uses native downward step rounding and minimum qty/notional checks;
  too-small TP1 remains explicitly unprotected. All fees/funding/initial-risk
  accounting delegates to the unchanged native offline Position/Engine.

Outputs outside Git: preregistered plan, source quality, summary comparison
CSV/JSON, per-preset trades and paired-delta CSV, portfolio-audit trades, Stop
update traces, LONG/SHORT summaries and TP_FIRST sensitivities. Time presets
are in `time-stop-last`, after all `exits-and-direction` runs.

Paired inference matches deterministic signal IDs and verifies identical
entry/fill/quantity/risk before computing each delta net R. Both seeded paired
trade and whole UTC entry-day bootstrap give nominal 95% mean-delta intervals;
missing net R/completion is excluded and counted, not fabricated as zero.
Full mode registers 13 configurations including A/E60 controls, 11 noncontrol
configurations, 26 primary ordering runs and 26 portfolio audits. All viewed
runs are disclosed. Intervals are **unadjusted exploratory comparisons**:
no family-wise correction, chosen winner, parameter search or unseen test.

Inherited limitations: conditional current rules/tiers, static spread,
instantaneous full fills, OHLC path ambiguity, funding settlement mark-open
approximation and no real cancel/replace/price-protection/API-failure model.
This is not permission to deploy a variant to LIVE. No stage 5 work.

### Measured stage 4 ablation

Same stage 3 dataset/conditional metadata. A and E60 matched original
trades/accounting/summary/bootstrap exactly. Non-G exits retained 103
identical entry times/fills/quantities/initial risks; G kept 69 original LONGs
and removed 34 SHORTs. All 26 primary and 26 portfolio runs were inspected.
No parameter changes after results: 13 configurations, including two
controls and 11 noncontrol configurations.

STOP_FIRST exit counts below mean **no executed TP1 then Stop / executed
TP1 then Stop / TP3 / time exit**, not price touches of an uninstalled TP1.

| Preset | N | Mean net R | USDT PF | Win % | DD USDT | Exit counts | Paired mean delta R |
|---|---:|---:|---:|---:|---:|---|---:|
| A | 103 | -0.0999 | 0.7624 | 35.92 | 49.98 | 43 / 34 / 26 / 0 | 0 |
| B | 103 | -0.0063 | 0.9193 | 58.25 | 31.00 | 43 / 42 / 18 / 0 | +0.0936 |
| C_ATR2 | 103 | +0.0027 | 0.9338 | 58.25 | 32.70 | 43 / 56 / 4 / 0 | +0.1026 |
| C_ATR3 | 103 | -0.0124 | 0.9099 | 58.25 | 33.06 | 43 / 53 / 7 / 0 | +0.0875 |
| D_ATR2 | 103 | +0.0027 | 0.9338 | 58.25 | 32.70 | 43 / 56 / 4 / 0 | +0.1026 |
| D_ATR3 | 103 | -0.0204 | 0.8926 | 58.25 | 35.93 | 43 / 54 / 6 / 0 | +0.0795 |
| E40 | 103 | -0.1087 | 0.7910 | 25.24 | 57.42 | 50 / 27 / 26 / 0 | -0.0088 |
| E50 | 103 | -0.1117 | 0.7549 | 25.24 | 54.73 | 43 / 34 / 26 / 0 | -0.0118 |
| E60 | 103 | -0.0999 | 0.7624 | 35.92 | 49.98 | 43 / 34 / 26 / 0 | 0 |
| G_NO_SHORT | 69 | +0.0100 | 0.9550 | 39.13 | 22.90 | 25 / 24 / 20 / 0 | 0 on matched LONGs |
| F8 | 103 | -0.1054 | 0.6905 | 28.16 | 41.78 | 18 / 21 / 15 / 49 | -0.0055 |
| F16 | 103 | -0.1065 | 0.7182 | 29.13 | 49.08 | 28 / 27 / 20 / 28 | -0.0066 |
| F32 | 103 | -0.1186 | 0.7344 | 33.01 | 51.51 | 36 / 31 / 23 / 13 | -0.0187 |

Paired nominal 95% intervals, 2,000 draws / seed 2026:

| Preset | Trade mean-delta R CI | UTC day-block mean-delta R CI |
|---|---|---|
| A / E60 / G matched LONGs | [0, 0] | [0, 0] |
| B | [-0.0083, 0.1835] | [-0.0018, 0.1833] |
| C_ATR2 / D_ATR2 | [-0.0313, 0.2322] | [-0.0290, 0.2313] |
| C_ATR3 | [-0.0305, 0.2041] | [-0.0304, 0.2071] |
| D_ATR3 | [-0.0408, 0.1969] | [-0.0418, 0.2015] |
| E40 | [-0.0973, 0.0824] | [-0.0939, 0.0738] |
| E50 | [-0.0349, 0.0098] | [-0.0342, 0.0098] |
| F8 | [-0.1544, 0.1392] | [-0.1563, 0.1362] |
| F16 | [-0.1187, 0.0993] | [-0.1221, 0.0957] |
| F32 | [-0.1179, 0.0642] | [-0.1111, 0.0647] |

All nontrivial delta intervals cross zero: unadjusted multiple comparisons
do not establish improvement. Every overall USDT PF remains below 1.
C_ATR2/D_ATR2 have slightly positive equal-weight mean R but net **-9.54
USDT**, not monetary profit (risks differ). B is -11.63 versus A -38.14
USDT; lower point drawdown is not an out-of-sample claim.

E40 has **16 TP1 orders below step/minimum constraints**, explicitly
`TP1_UNPROTECTED_MINIMUM`; entries were not dropped or amounts rounded up.
This contributes to changed exit groups. E50/E60 have no such failures.

TP_FIRST was run for all presets: all matched deltas and ambiguous Stop/TP
bar counts are zero. Synthetic tie tests verify ordering behavior; identical
historical results do not prove tick-path assumptions.

The native-gated audit retained every scheduled entry: zero gate rejections
and common-trade financial deltas, apart from G's intentional SHORT removals.
No formerly rejected/new signals were recovered. Funding-null/unverified
closure counts are zero. Full LONG/SHORT splits, paired per-trade deltas and
Stop updates are retained in output JSON/CSV.

Validation: 21 new tests; **764 passed / 225 subtests**, one optional
PostgreSQL skip and one previously proven stale-source baseline deselection.
Regressions: `backend/tests/test_backtest_ablation.py` plus unchanged native
baseline, diagnosis, causal-data, risk and accounting suites.

## Offline stage 5 regime/entry study

This is a separate offline module/CLI, not a LIVE strategy change. Earlier
stage 3/3b/4 modules and `v25_execution.py` are unchanged. The study protocol
is `backend/studies/stage5-plan.json`; the first command creates an exclusive,
hashed `study-lock.json` outside Git. Mismatched reruns refuse to overwrite it.

The user's revised holdout protocol reserves the **oldest unseen quarter**,
2023-10-01 through 2024-07-01, exclusively for stage 6. TRAIN is
2024-07-01 through 2025-12-31, VALIDATION 2025-12-31 through 2026-10-01;
all bounds are UTC, end-exclusive. These are exact duration fractions
25% / 50% / 25%. This is a **reverse-time holdout, not forward walk-forward**.
The first 93 TRAIN days are embargo/warmup, so evaluation starts 2024-10-02.
TEST/pre-TEST archives live in physically separate `sealed` storage and are
only checksum/presence audited: no TEST CSV parsing, feature calculation,
warmup, decisions, outcomes or price-gap reporting before stage 6.

Research scope is BTC/ETH/SOL/BNB plus XRP/DOGE/ADA/AVAX. Eight instruments
fit the unchanged native policy sanitizer; this never changes a user's
production whitelist. Static archives cover June 2023 through September
2026, with checksums. Original four-symbol metadata remains byte-provenanced
from stage 3; extra four use explicitly conditional current public metadata.
The original 18-month A/B comparison resets the original four-symbol account
and restores the original warmup/prefix-sum scope using expanded verified rows.
The expanded universe/stateful account is not claimed to have the old cohort.

```powershell
$study = 'C:\research\protrebot\stage5'
$oldData = 'C:\research\protrebot\data'
.\.venv\Scripts\python.exe backend\regime_download.py --output $study --previous-data $oldData
.\.venv\Scripts\python.exe backend\regime_cli.py --output $study --reference-data $oldData --reference-a 'C:\research\protrebot\baseline\stop_first-spread2-slip3.json' --reference-b 'C:\research\protrebot\ablation\exits-and-direction\B-stop_first.json'
.\.venv\Scripts\python.exe backend\regime_report.py --output $study
```

Acquisition permits only checksummed public archives and the same two fixed
unauthenticated metadata GET endpoints. Replay blocks external sockets/DNS;
loopback remains available for Windows asyncio. Standalone runtime persistence
stays outside Git; no signed/order endpoint exists
in the data command. Verified native decisions and causal feature caches
persist outside Git with source/input/protocol fingerprints for fast reruns.

Presets, registered before inspecting outcomes:

| Preset | Closed-data condition |
|---|---|
| F1 | Native 1h ADX >= 20 / 25 |
| F2 | Native 4h ADX >= 20 / 25 |
| F3 | Native 15m ATR/price trailing-90-day percentile >= 30 / 50 |
| F4 | 15m Bollinger width trailing-90-day percentile >= 30 / 50 |
| Only combination | 1h ADX >= 20 AND ATR percentile >= 30 |
| Direction combination | B exits, remove SHORT entries |
| Separate experiment | Existing SHORT alignment >=80 rejection ON/OFF |

ATR/ADX call unchanged native helpers on the same 259-closed-candle windows.
BB width uses the native population-standard-deviation formula, regression
checked against native analysis (there is no standalone native BB helper).
Percentile ties use empirical CDF `<= current`, including the current closed
decision observation. Every 90-day sample must be present; missing features
are explicitly unknown, never silently imputed or selected.

All ordinary filters only delete frozen B-entry rows; remaining exits, fees,
funding and immutable initial risk are identical. They do not refill portfolio
capacity or rerun daily gates. The SHORT>=80 OFF experiment is separate:
the native canonical/helper functions are called with only this boolean
disabled in an isolated offline worker, restored even after exceptions.
Other quality/MTF/risk gates remain intact; its rescanned portfolio can add,
remove or resize entries. STOP_FIRST is primary; TP_FIRST sensitivity uses
the same frozen STOP_FIRST schedules, never newly selected entries.

Reports include retained/eliminated sets, USDT PF/PnL and closed-equity DD,
LONG/SHORT and UTC-close-month distributions. TRAIN-only descriptive metrics
exclude trades closing across the validation boundary; combined development
retains and counts them. Selection inference is **filtered mean R minus
whole-cohort mean R**, with joint membership resampling, not an independently
sampled or fictitious paired-exit delta. Common filter exits have exactly zero
paired deltas. Trade and UTC entry-day bootstrap use 20,000 draws, seed 2026,
empty calendar days and explicit undefined-resample/active-cluster counts.
Fewer than two active selected/reference days gives no day CI.

Eleven noncontrol comparisons: eight singles, one fixed combination,
B+SHORT off, and SHORT>=80 OFF. Two orderings are inspected; TP_FIRST is a
sensitivity check, not independent confirmation. Both nominal unadjusted 95%
and Bonferroni family-95% intervals are provided. Fewer than 60 complete
net-R trades is **YETERSIZ_ORNEKLEM (YETERSIZ ORNEKLEM)**, with interpretation
disabled. There is no winner selection, new threshold, parameter tuning,
TEST inspection, stage 6 or deployment authorization.

The final report command reuses frozen outcomes only. It supplies the requested
A-to-B paired exit improvement and A-LONG-to-B-LONG combination contrast through
the unchanged stage 4 pairing/bootstrap helper (20,000 trade/day draws).
Removed SHORTs are not fabricated paired outcomes; their selection effect is
the separately reported B_NO_SHORT vs all-B contrast. A/B control intervals
are descriptive nominal95, distinct from the corrected 11-filter family.

### Stage 5 measured development results

Measured on 2026-10-06, evaluated 2024-10-02 through 2026-10-01 exclusive,
eight symbols, exposure 350 USDT, spread 2 bp, slippage 3 bp, STOP_FIRST.
The original four-symbol 18-month reset matched A and B exactly (103 trades).
All eight development symbols have zero contract/mark gaps, duplicate candles,
missing archives or trailing missing funding events. Funding-null trades: zero.
TEST archives were checksum/presence audited only, never parsed.

| Configuration | Trades | Mean net R | USDT PF | Max DD USDT | Net PnL USDT |
|---|---:|---:|---:|---:|---:|
| A | 129 | 0.0331 | 1.0073 | 47.23 | 1.36 |
| B | 129 | 0.0494 | 1.0437 | 21.19 | 7.32 |
| F1 1h ADX >=20 * | 58 | -0.0050 | 0.9037 | 32.75 | -7.80 |
| F1 1h ADX >=25 * | 41 | 0.1002 | 1.1205 | 19.06 | 6.46 |
| F2 4h ADX >=20 | 66 | 0.0824 | 1.1386 | 14.52 | 11.47 |
| F2 4h ADX >=25 * | 49 | 0.1098 | 1.1785 | 18.03 | 10.67 |
| F3 ATR percentile >=30 * | 28 | 0.2244 | 1.4071 | 16.72 | 13.45 |
| F3 ATR percentile >=50 * | 9 | 0.3958 | 2.4131 | 4.99 | 8.99 |
| F4 BB percentile >=30 * | 47 | 0.1781 | 1.4345 | 11.53 | 19.85 |
| F4 BB percentile >=50 * | 16 | 0.3490 | 2.3422 | 4.89 | 13.17 |
| F1>=20 + F3>=30 * | 16 | 0.1882 | 1.3677 | 13.23 | 7.16 |
| B, SHORT disabled | 87 | 0.0594 | 1.0633 | 15.04 | 6.82 |
| SHORT alignment>=80 gate OFF, B exits | 164 | 0.0151 | 0.9903 | 36.28 | -2.18 |

`*` = YETERSIZ_ORNEKLEM: fewer than 60 complete trades, no interpretation.
Only F2>=20 and B_NO_SHORT meet that guard among the ordinary filters.
All selection-difference trade/day nominal and Bonferroni intervals cross zero.
No winner is selected; even configurations passing the count guard are not
evidence of an out-of-sample edge.

B minus A paired mean: +0.01634 R, trade95 [-0.08837, 0.11472],
day95 [-0.08858, 0.11358], 129 matched entries. B-LONG minus A-LONG:
+0.01013 R, trade95 [-0.12547, 0.13692], day95 [-0.12613, 0.13578],
87 matched entries. All use 20,000 draws. SHORT-gate OFF adds 56 and removes
21 entries, with 108 unchanged common trades; it is not merely 35 added rows.

B LONG: 87 trades, +0.05939 R; SHORT: 42, +0.02882 R (descriptive small
subgroup only). TRAIN-only: 76 trades, +0.15740 R; VALIDATION-only: 52,
-0.11666 R, YETERSIZ_ORNEKLEM. One boundary-crossing entry is retained
combined but excluded from TRAIN-only metrics. Monthly and directional
distributions for every preset, retained/eliminated expectancy and all
nominal/corrected intervals are in `regime-study.json`; `comparison.csv`
provides the filter table and `combination-comparisons.json` the paired controls.
TP_FIRST has exactly the same four frozen financial cohorts as STOP_FIRST;
observed ambiguous bars are zero, so this dataset cannot discriminate ordering.
Eleven noncontrol configurations and both orderings (22 comparisons) were
inspected, without tuning any threshold or opening TEST.

## Offline stage 6 one-shot holdout

The immutable preregistration is `backend/studies/stage6-plan.json`. Only three
candidates are authorized: B (BE+fee), B with closed native 4h ADX >=20, and
B with SHORT removed. No new strategy/threshold, LIVE edit or stage 7 is allowed.
The stage 5 historical TEST is 2023-10-01 through 2024-07-01 exclusive;
June-September 2023 is decision warmup, not scored outcomes.

Point success requires **net expectancy R >0 AND USDT PF >1.2 AND at least
60 complete net-R trades**. Otherwise the label is `BASARISIZ` (Turkish
`BA\u015eARISIZ` in the machine report). Confidence bounds are descriptive,
not an extra success criterion. Open/unverified/missing-R observations are
disclosed and excluded, never fabricated or silently funded with zero.
No-loss PF with positive monetary gains is explicitly unbounded; zero-gain
zero-loss PF is undefined, not an invented numerical factor.

```powershell
$stage5 = 'C:\research\protrebot\stage5'
$holdout = 'C:\research\protrebot\stage6'
.\.venv\Scripts\python.exe backend\holdout_cli.py register --stage5-root $stage5 --output $holdout
# Run only after offline synthetic regression gates pass.
.\.venv\Scripts\python.exe backend\holdout_cli.py run --stage5-root $stage5 --output $holdout --workers 4
```

Registration hashes the protocol and checks the already-acquired sealed
manifest/current metadata and unchanged native/prior-stage sources, without
opening candle ZIPs. Output must be the fixed sibling `stage6` directory.
`test-once.json` is exclusively created **before the first TEST candle read**.
Once claimed, successful, failed or interrupted attempts all refuse another
run. There is deliberately no reset/resume/override flag; changing output
paths does not create another permitted run.
The protocol also binds input/output paths and the run claim in Git's shared
metadata directory (`protrebot-research/holdout`), not branch history or trading
persistence. Another output, relocated copy or shared worktree cannot bypass
that repository-level receipt. Synthetic tests isolate these receipts.

One bundle contains four cost pairs (slippage/spread in bp: 3/2, 6/2, 3/5, 6/5)
and both STOP_FIRST/TP_FIRST: eight scenarios, 24 candidate cells.
The registered primary is STOP_FIRST, slippage3/spread2. As explicitly chosen
by the user before TEST, native risk/cost gates are recomputed per cost.
Within each cost, B follows the native A STOP_FIRST entry schedule; both
filters delete B rows only, with no capacity refill. TP_FIRST retains that
cost's STOP_FIRST schedule. Different cost cohorts are separately reported.
All candidates use unchanged stage 4 B exits, 60% TP1 and no ATR trailing.

Native canonical decisions are calculated once with the existing SHORT>=80
gate enabled; no fourth candidate or SHORT-gate-OFF experiment is added.
Source/protocol-stamped caches persist outside Git. Bootstrap uses 20,000
draws, seed2026, complete candidate trades and UTC entry-day blocks including
empty calendar days. Absolute expectancy-R and **monetary** PF have nominal95
and Bonferroni-family95 (three candidates) bounds. Unbounded PF endpoints,
undefined draws and insufficient day clusters are explicit. Bonferroni3 is
the registered primary family, not simultaneous coverage of all sensitivity
cells; sensitivity is not independent confirmation or a tuning opportunity.

Seen overlap conservatively includes stage 3 input warmup from 2025-02-01
through the stage 4 replay end 2026-10-01. It does not intersect this TEST.
Full/clean/partly-seen reports derive from the same outcomes, not another
replay; a trade crossing into a seen interval is not labelled clean.
Partly-seen/cross-boundary groups retain their actual entry-day clusters on
the full TEST calendar; entry dates are never moved into the seen interval.
For this all-clean TEST, full and clean reports reuse identical statistics.
Outputs include `holdout-results.json`, `comparison.csv`, per-scenario trade
CSV/JSON, source-quality audit and the irreversible run receipt.

Limitations: reverse-time historical holdout, **not forward walk-forward**;
the stage 5 current exchange metadata/universe is held fixed historically.
Frozen B/filter cohorts are not prospective capacity-refilled portfolios.
Tests in `backend/tests/test_holdout_study.py` are synthetic/offline; they
do not consume the real TEST claim.

### Stage 6 single-attempt outcome: blocked by archival data gap

The registered protocol SHA256 is
`853dfa4714e8f9bf348350c3616666bafa172da660b383c15f67b86dd63fcc66`.
The sole real invocation started on 2026-10-06 at 14:03:15 UTC. It failed
at source-quality validation, before native signal preprocessing or any
entry/exit replay. Both the external and repository-level receipts are
`FAILED`, with `rerun_allowed=false`; the attempt has not been repeated.

Every symbol (BTC, ETH, SOL, BNB, XRP, DOGE, ADA and AVAX) has the same
missing 15m mark-price candle: **2023-11-10 03:45-04:00 UTC**.
Direct inspection of all eight November ZIPs verified their registered
SHA256 checksums, 2,879 rows instead of 2,880, the missing timestamp and
both adjacent timestamps. This is an archival gap, not a loader inference.
The audit read existing input rows only; it did not recompute decisions.

| Source, per symbol | Expected TEST rows | Actual TEST rows | Missing |
|---|---:|---:|---:|
| Contract 15m | 26,304 | 26,304 | 0 |
| Contract 1h | 6,576 | 6,576 | 0 |
| Contract 4h | 1,644 | 1,644 | 0 |
| Mark price 15m | 26,304 | 26,303 | 1 |

No archives were missing, no duplicate candles were reported, and historical
funding was present for all eight symbols with no reported gaps or trailing
missing events. The strict mark-price coverage gate was not relaxed.
No candle interpolation, replacement dataset, strategy/parameter change,
network request or second TEST invocation was used.

| Candidate | Holdout assessment |
|---|---|
| B, unfiltered | NOT EVALUATED: source-quality gate failed |
| B + 4h ADX >=20 | NOT EVALUATED: source-quality gate failed |
| B, SHORT removed | NOT EVALUATED: source-quality gate failed |

Trade counts, R/PF/PnL/drawdown, direction/monthly statistics, sensitivity
cells and confidence intervals are **unavailable**, not zero. Neither
`BASARILI` nor `BASARISIZ` is a measured strategy verdict here: the research
attempt failed before those criteria could be evaluated. The 24 registered
cells remain unevaluated; no winner or deployment claim is made.
The quality report and both irreversible receipts remain outside tracked
source. Related pre-TEST offline gates passed 821 tests and 239 subtests
(one optional skip, one previously proven stale baseline deselection).
After the final source-verification guard change, the 27 holdout regressions
and six subtests passed again; Pylance reported no CLI diagnostics.
The final related synthetic suite also passed all 821 tests and 239 subtests
with the same skip/deselection, without reopening the real TEST bundle.

## LIVE entry partial fills

Exact entry identity, symbol and direction remain mandatory. A positive actual
position no larger than the original requested quantity confirms provenance;
the plan keeps `requested_quantity` and updates `quantity` to the actual fill.
Overfills, zero quantities and mismatched identities are not adopted.

The existing `closePosition` Stop is installed before cancelling only the
owned entry remainder. A fresh position read handles fills racing cancellation.
TP1/TP2 retain the 60% split, rounded down to the market quantity step, with
minimum quantity/notional checked using the current mark price. Below-minimum
partial targets are explicitly TP-unprotected monitoring only (no fallback
close); Stop and TP3
retain full-position close semantics. Uncertain cancellation locks new entries
and raises an error without removing the installed Stop; reconciliation also
settles remaining entries when an owned Stop is already present.

Offline regressions: `backend/tests/test_v25_partial_fills.py`. No real exchange
requests are needed for these tests.

## LIVE Auto Trade symbol whitelist

LIVE scanning and automatic candidate selection are restricted to the saved
`policy.allowed_symbols`. The same registered list is mandatory at execution
and immediately before the entry POST; a caller-provided list cannot override
it. Explicitly empty LIVE policies stay empty when saved or restored: no scan
or new automatic entry is allowed, with an event/log reason and a visible
automation status message. Existing positions and protection management are
unaffected. Manual entry retains its existing symbol behavior.

Offline regressions: `backend/tests/test_v25_auto_symbol_scope.py`.

LIVE Auto Trade requires the saved policy timeframe `15m`. The LIVE panel
shows a warning for other selected/saved timeframes and blocks only starting
Auto Trade; STOP and manual-order controls keep their existing behavior.
Changing a draft back to `15m` is not enough when the saved policy still uses
another timeframe: save the policy and complete the existing approval gates.
`POST /api/v25/auto/start` rejects other timeframes with HTTP 422 and
`detail.code=LIVE_AUTO_TIMEFRAME_UNSUPPORTED`, without enabling an auto session.
The existing canonical-analysis WAIT rule is unchanged.
Offline regressions: `backend/tests/test_v25_auto_timeframe.py` and
`frontend/tests/live-trading-panel.spec.ts`.

## LIVE execution recovery and Stop verification

Exchange and unexpected loop errors are recorded and retried with bounded
backoff (5 seconds, doubling to 60); existing rate-limit and transient-read
handling remains in place. A supervisor restarts an unexpectedly returned,
failed or independently cancelled worker without restoring trading approval.
Normal application shutdown cancels both tasks and never restarts the worker.

Status `heartbeat.last_successful_cycle` and `last_successful_cycle_epoch`
advance only after successful reconciliation and an automation pass without
an error status. Request-level heartbeat fields keep their existing meaning.

Stop installation has at most three attempts with exact active-order
verification (client identity, symbol, side, type, trigger and full-position
close and MARK_PRICE working type). Known rejections may be retried with the same client identity.
Accepted or ambiguous submissions are only verified, not blindly reposted.
If verification still fails, the existing owned-entry cancellation and
reduce-only safety close path runs. This is an attempt bound, not a guaranteed
millisecond deadline; exchange timeouts and rate-limit cooldowns still apply.
Regression tests use mocked transport and no real exchange requests.

## Directional entry limits

`policy.max_same_direction_positions` defaults to 2 (range 1-5), independently
for LONG and SHORT. Optional `policy.max_direction_exposure_usdt` applies the
same USDT cap separately to each direction; omitted/null disables this cap.
An explicit null in the policy update clears a previously configured cap.
Policy changes retain the existing ownership checks and approval reset.

Entry gates count open positions, unfilled entry orders, and candidates already
accepted in the current automatic round. A position and pending entry for the
same symbol/direction use one slot, but exposure includes both the position and
the remaining entry quantity. Snapshot-visible reservations are not added a
second time; the larger exposure is retained conservatively during snapshot
lag. Reduce-only, close-position, and terminal orders do not consume entry
capacity. Unpriced positions always fail the mandatory global exposure gate;
unpriced pending/directional exposure also blocks when the optional cap is enabled.
unknown directions are conservatively counted against either direction.
Rejections appear in gate/status details and logs, without sending an entry.

Offline regressions: `backend/tests/test_directional_entry_limits.py`.

## Entry risk and verified R performance

New DEMO and LIVE entry plans capture immutable `initial_risk_usdt` as
`abs(planned_entry_price - initial_stop_price) * submitted_quantity`, after
quantity/price rounding and risk adjustment. Stop moves and partial fills do
not rewrite this baseline. LIVE intents persist it before submission and
restore only the recorded value after a restart; legacy/external plans are
not backfilled from their current Stop or position.

Verified LIVE closures record `r_multiple = realized_pnl / initial_risk_usdt`.
Here `realized_pnl` is the existing verified net trade PnL after USDT commission,
with the existing funding/non-USDT commission limitations unchanged. Missing,
zero, invalid, or non-finite initial risk produces null R.

`performance_payload` adds `avg_r` and `expectancy_r` from R-eligible verified
closures only, under the existing period and deduplication filters. Expectancy
is win probability times mean winning R minus loss probability times absolute
mean losing R; breakevens remain in the sample. With this same sample,
expectancy equals mean R. Both fields are null when no eligible R exists.
Unknown-risk trades still contribute to the existing USDT metrics, not R.

DEMO and LIVE journal sources remain separate. DEMO's existing fill-level PnL
is not relabelled as verified net position-close PnL; those entries have no R.
This change does not introduce new DEMO closure accounting.
Offline regressions: `backend/tests/test_trade_r_metrics.py`.

## Site favicon

Both Vite entrypoints reference shared, same-origin site icons in
`frontend/public`: a multi-size `favicon.ico`, a 96px PNG for search/browser
surfaces, and a 180px Apple touch icon. These use the existing KaisTrade logo's
round emblem, without changing the in-app wordmark. Keep these public URLs
stable and crawlable. Google Search updates its icon after recrawling the home
page; deployment does not guarantee an immediate search-result change.
Regression checks: `node --test tools/site-icons.test.mjs`.

The public brand spelling is `KaisTrade`. Both HTML entrypoints declare this
in the document title, application name, Open Graph site name/title, and static
`WebSite` JSON-LD for `https://kaistrade.com/`. Public policy titles and public
logo labels use the same spelling. Google chooses its search-result site name
automatically; request a home-page recrawl in Search Console after deployment
and allow time for the updated name to be processed.

After building both frontends, run the unchanged public-page content/access
checks against their production bundles with
`npx playwright test --config playwright.public-policies.config.ts` from
`frontend`. This includes raw-HTML site-name metadata checks, so search engines
do not have to execute JavaScript to discover the preferred name.

## KaisTrade transactional email / Render

Mail transport is centralized in `backend/app/email_service.py` using the
already pinned `httpx` dependency; no Resend SDK or SMTP password is needed.
The following backend-only variables are secret-store settings, never `VITE_`
variables. `.env.example` lists their names with empty values:

| Variable | Purpose |
|---|---|
| `EMAIL_PROVIDER` | Select `resend` explicitly; absent/blank preserves the legacy transport |
| `RESEND_API_KEY` | Required only with the Resend provider |
| `EMAIL_FROM` | Verified Resend sender address; displayed as KaisTrade |
| `EMAIL_REPLY_TO` | Optional reply address for either provider |

For compatibility, `EMAIL_PROVIDER=smtp` names the existing **Gmail OAuth HTTP
API** path, not a new SMTP socket transport. Existing `GMAIL_*` credentials
remain valid on this default path. With `resend`, missing/invalid configuration
or an API failure is explicit; there is no fallback to Gmail.

Render rollout: deploy the code first without setting `EMAIL_PROVIDER` to
preserve current delivery. Verify the already configured Resend key and sender
in Render's secret store without copying their values into logs or source.
Then set `EMAIL_PROVIDER=resend`, confirm `APP_BASE_URL` is the production
KaisTrade origin, and redeploy/restart. The verified domain's `eu-west-1`
region does not change the HTTPS API endpoint. Only after deployment, manually
register a controlled account, inspect both HTML and plain text, follow its
verification link, and check Spam plus the Resend dashboard. API acceptance is
not proof of inbox delivery.

Verification resend requires the existing signed registration-status token:
60-second backend cooldown between resend attempts and five attempts per hour,
with shared IP/account limits. The registration UI also waits 60 seconds after
the initial request. Initial delivery failure retains the existing registration
rollback; its retry submits the same validated registration form.

Bounce/complaint webhooks and admin delivery history are intentionally not
enabled in this change. Durable event storage, replay/idempotency protection,
raw-body signature verification, and recipient mapping need a separate change;
no unsigned webhook exists and `RESEND_WEBHOOK_SECRET` is not currently used.
Provider tests mock HTTPS/Gmail and send no real messages.

## Ortam dosyaları ve secret yapılandırması

Kök `.env` yalnızca yerel dosyadır ve Git tarafından ignore edilir; gerçek
değerleri commit'e eklemeyin. `.env.example` aynı yapılandırma anahtarlarını
değerleri boş olarak listeler. Zorunlu anahtarları doldurun; kullanılmayan
isteğe bağlı ayarların satırlarını yerel `.env` dosyasında silin veya yorumlayın.
Özellikle boş sayı/boolean ayarları, mevcut doğrulayıcılar için geçerli
varsayılan değer değildir. Üretimde dosya yerine sunucunun secret store'unu kullanın.

Backend'i kök dizinden yerel ortam dosyasıyla başlatma:
`python -m uvicorn app.main:app --app-dir backend --env-file .env`.
Mevcut süreç ortamı dosyadaki değerlerden önceliklidir.
Kök eski `main.py` içindeki gömülü veritabanı bağlantı varsayılanı kaldırılmıştır;
bu giriş noktası boş/eksik `DATABASE_URL` ile açık hata verir.
Taşınan yerel bağlantının gerçek bir servis üzerinde geçerliliği doğrulanmamıştır.

| Ayarlar | Nerede tanımlanır? | Gereklilik |
|---|---|---|
| `DATABASE_URL` | Backend sunucu | Kalıcı üretim depolaması için zorunlu |
| `SESSION_SECRET` veya eski adı `PROTREBOT_SESSION_SECRET` | Backend secret store | En az 32 karakter; tüm worker'larda aynı ve kalıcı. İkisi farklıysa başlangıç hata verir |
| `PROTREBOT_WEB_ACCESS_TOKEN` | Backend secret store | Owner erişimi için güçlü token; eski session/vault türetme uyumu korunur |
| `PROTREBOT_VAULT_MASTER_KEY` | Backend secret store | Ayrı kasa anahtarı önerilir; mevcut owner-token uyumu korunur |
| `PROTREBOT_DURABLE_AUTH_REQUIRED`, `PROTREBOT_WEB_REQUIRE_AUTH` | Backend sunucu | Üretimde `true`; kalıcı oturum anahtarı yoksa başlangıç durur |
| `APP_BASE_URL`, `PROTREBOT_CORS_ORIGINS` | Backend sunucu | Gerçek frontend URL/origin listesi |
| `ANTHROPIC_API_KEY` | Backend secret store | LLM asistanı kullanılacaksa zorunlu |
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` | Backend secret store | Varsayılan/eski Gmail posta yolu için zorunlu; Resend yolunda kullanılmaz |
| `GMAIL_FROM_EMAIL`, `GMAIL_FROM_NAME` | Backend sunucu | Gönderici yapılandırması |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_MASTER_MODE_MONTHLY` | Backend secret store / sunucu | Stripe etkinse zorunlu; plan kimliği sunucuda tutulur |
| `REDIS_URL`, `PROTREBOT_DATA_DIR`, `PROTREBOT_BOOTSTRAP_OWNER_EMAIL` | Backend sunucu | İsteğe bağlı mevcut altyapı ayarları |
| `ASSISTANT_*`, `ANALYST_*`, `CREDIT_WINDOW_HOURS`, `PROTREBOT_EXPOSE_DEV_TOKENS` | Backend sunucu | Bütçe/özellik ayarları; dev token üretimde `false` |
| `POSTGRES_PASSWORD` | Yerel Docker ortamı | Docker PostgreSQL için zorunlu; tarayıcıya verilmez |
| `VITE_OWNER_PREVIEW`, `VITE_WEB_ACCESS_REQUIRED` | Vercel frontend build ortamı | Gizli olmayan UI bayrakları; üretimde erişim kapısını kapatmayın |
| `VITE_BUILD_COMMIT` | Vercel frontend build ortamı | İsteğe bağlı, gizli olmayan build kimliği |
| `VITE_API_BASE` | Eski frontend yapılandırması | Gizli olmayan legacy URL; üretim `/api` taşımasını değiştirmez |

`SESSION_SECRET`, API secret/token ve `DATABASE_URL` asla `VITE_` öneki
almamalıdır. Vercel'de backend secret'larını frontend build ortamına koymayın.
Binance kullanıcı anahtarları mevcut şifreli kasa üzerinden kaydedilir;
üretimde paylaşımlı `BINANCE_*` ortam anahtarlarıyla üyelik kapsamı atlatılmaz.
Testlerdeki açıkça sentetik anahtar/token değerleri gerçek deployment ayarı değildir.

### Custom domain authentication

The production frontend origin is `https://kaistrade.com`. The backend explicitly
trusts this origin and the existing `https://frontend-nu-two-18.vercel.app` origin
for both CORS and cookie/CSRF checks. Additional exact origins from
`PROTREBOT_CORS_ORIGINS` use the existing validated parser; custom domains are no
longer discarded by the Vercel-preview filter. Wildcards and untrusted browser
origins remain rejected.

Render production settings must use `APP_BASE_URL=https://kaistrade.com` and
`PROTREBOT_CORS_ORIGINS=https://kaistrade.com,https://frontend-nu-two-18.vercel.app`.
The blueprint contains these non-secret settings, but an existing Render service
must receive the settings and deploy the updated backend; editing the blueprint
alone does not prove its live environment changed. Frontend requests remain
same-origin `/api`; no frontend secret or absolute backend URL is required.

### Google login (existing commercial authentication)

Google login uses server-side Authorization Code + PKCE S256 and OIDC, not
Supabase or Google Identity Services. Configure the existing Web Application
OAuth client's `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in the backend secret
store only. Do not create a client, change Gmail credentials or use `VITE_` for
these settings. Missing configuration/storage rejects the flow explicitly;
email/password login remains available through its existing endpoints.

Add BOTH exact Authorized redirect URIs to that existing Google client:

- `https://kaistrade.com/api/v22/auth/google/callback`
- `https://frontend-nu-two-18.vercel.app/api/v22/auth/google/callback`

Endpoints: `POST /api/v22/auth/google/start`, `GET /api/v22/auth/google/callback`,
`GET /api/v22/auth/google/pending`, `POST /api/v22/auth/google/complete`.
Start uses the existing Origin/XHR checks and returns Google's authorization URL
for top-level navigation. The callback uses a 5-minute, browser-bound, atomically
consumed PostgreSQL attempt, PKCE and nonce; it checks signature, issuer,
audience, expiry and verified email before creating the existing USER cookie.
Remember-me follows the existing session lifetimes. Tokens are not returned in
frontend URLs/storage. The application/Uvicorn callback query is stripped before
access logging; external proxy/provider log policies must separately redact
query strings. No extra CORS or third-party script permission is needed.

Identity binding is unique `(issuer, sub)`. An unbound identity with an existing
email is NOT merged: the user is told to use email/password; no account-linking
bypass is introduced. New Google users explicitly accept terms/privacy before
CUSTOMER/FREE creation with no fabricated password. Existing bound users retain
their original ID, role, subscription and auth version. Account erasure removes
Google bindings; ordinary logout also clears pending OAuth cookies.

Apply `backend/migrations/20261004_002_google_oauth.sql` after the commercial
auth/erasure migration, before release. It adds the two OAuth tables, unique
identity constraint and expiry index; runtime also ensures the idempotent
schema on first OAuth use. Attempts contain purpose-separated encrypted data
and hashed handles/browser bindings, not Google access/refresh tokens. Existing
session keys must remain stable across workers. No provider dashboard changes
or real Google-account login are claimed by offline tests.

## Public policy pages and canonical domain

The canonical production host is `https://kaistrade.com`. The same Vercel
frontend project also binds `www.kaistrade.com` with its own TLS certificate
and a path-preserving HTTP 308 redirect to the canonical host.

`/privacy`, `/terms`, and `/risk` (including trailing-slash variants) render
without user or owner authentication in both frontend entry points. They reuse
the existing Trust Center paragraphs from `compliance-content.ts`; the signed-in
modal uses the same source. All other routes retain their existing access gates.
Login badges use neutral feature descriptions, not unverified encryption, 2FA,
or round-the-clock support promises. Publishing these local changes requires a
separate release; they do not clear a Google Safe Browsing warning automatically.

## Güvenlik ve tarayıcı oturumları

Tarayıcı üyelik oturumu ve yönetici erişimi `HttpOnly`, `SameSite=Lax`,
üretimde `Secure`, host-only `/api` çerezleriyle taşınır. Frontend depolarında
yalnızca gizli olmayan kullanıcı/oturum göstergesi bulunur; eski bearer
kayıtları kaldırılır ve yeniden giriş gerekir. Native istemcilerin imzalı
bearer desteği korunur. Frontend ile backend bu değişiklik için birlikte
yayınlanmalıdır; bu çalışma kendiliğinden deploy/commit/push yapmaz.

Üretim frontend'i aynı-origin `/api` kullanır. Vercel API rewrite'ı SPA
fallback'inden önce backend'e yönlendirir; iki Vite geliştirme sunucusu da
yerel backend proxy'si kullanır. Çerezli durum değiştiren isteklerde
`X-Requested-With: XMLHttpRequest` ve izinli `Origin` zorunludur.
Üretim/custom domain `PROTREBOT_CORS_ORIGINS` listesinde açıkça tanımlanmalıdır.
Vercel CSP inline script/eval'i engeller; dinamik React stilleri için yalnızca
`style-src` içinde inline stil izni bulunur. Güvenlik başlıkları yapılandırma
üzerinden tanımlıdır; canlı edge ayarları ayrıca doğrulanmalıdır.
İki Vite build'i fontları aynı-origin dosyalar olarak çıkarır; küçük fontlar
`data:` URL'e çevrilmez ve `font-src 'self'` politikası gevşetilmez. Diğer
asset'lerin varsayılan inline eşiği ve coin logolarının lazy yüklemesi korunur.
Eski mutlak `VITE_API_BASE`/`VITE_API_URL` ayarları tarayıcı taşımasını değiştirmez;
başka bir backend gerekiyorsa aynı-origin proxy hedefi değiştirilmelidir.

Abonelik ekranında yüklenemeyen veri süresi dolmuş üyelik gibi gösterilmez:
hata ve yeniden deneme sunulur. Fatura geçmişi yüklenmediğinde boş geçmiş
iddiası yerine Stripe portalına yönlendiren açıklama gösterilir. Master Trade
kısayolu mevcut yetki kontrolü üzerinden `/master-trade` çalışma alanına gider.
Genel ana sayfaya dönüş `/billing`, `/pricing` ve `/master-trade` adreslerini
`/` olarak günceller; sayfa yenileme eski çalışma alanını yeniden açmaz.
Bu regresyonlar iki build sonrası `frontend` klasöründen
`npx playwright test --config playwright.site-checkup.config.ts site-checkup.spec.ts`
ile üretim bundle'ı ve mock API üzerinden doğrulanır; gerçek ödeme veya emir
gönderilmez.
Kais karşılama balonunun yerleşim RAF döngüsü gizli sekmede durur ve sekme
görünür olduğunda yeniden başlar; 2 saniyelik bekleme ve 8 saniyelik görünür
yaşam süresi yalnızca görünür sekmede ilerler. Karşılama testlerinin API,
harici HTTP ve WebSocket bağlantıları offline fixture'larla izole edilir.

Aktif LIVE kontrolü kullanıcı/oturum/hesap sahibine bağlıdır; başka bir premium
üye veya OWNER aynı kontrolü devralamaz. Yeni bir oturuma geçiş açık yeniden
bağlama/onay gerektirir. LIVE anahtar kabulünde Binance API izinleri okunur;
para çekme izni açık veya doğrulanamayan anahtar reddedilir. Testnet'in para
çekme desteği bulunmadığı ayrıca belirtilir; eski LIVE kasa kayıtları tekrar
doğrulanmalıdır.

Üyelik iptali ve parola/rol değişiklikleri her korunan istekte ortak auth
kaydından doğrulanır. Arka plan LIVE/Demo yetkileri ilk doğrulanmış oturumun
sürümüne ve imzalı bitiş zamanına bağlıdır; yeni bir status isteği eski yetkiyi
yenilemez. Demo ARM ve otomasyon başlangıcı açık yetki verir; yeniden başlatma
eski yetkiyi geri yüklemez. Depolama veya silme metadata senkronizasyonu
doğrulanamazsa özel borsa işlemleri kapatılır, mevcut borsa koruma emirleri
iptal edilmez. Parolalar yeni scrypt maliyetiyle saklanır; uyumlu eski kayıtlar
başarılı girişte yükseltilir.

`PROTREBOT_DURABLE_AUTH_REQUIRED=true` olduğunda asistan muhasebesi PostgreSQL
olmaksızın SQLite'a düşmez. Grid planları üyeye özeldir; sahipliği bilinmeyen
eski planlar başka üyeye atanmaz. Paper kapısı grid mutasyonlarını da kapsar.
API docs/redoc/OpenAPI HTTP uçları kayıtlı değildir.

Hesap silme kişisel verileri, sohbet/kota bağlantılarını ve yerel anahtar
kasasını kaldırır; finansal/işlem/denetim kayıtları kimlikten ayrılarak saklanır.
Eski snapshot'ların silinen kişiyi geri getirmemesi için ortak silme işaretleri
ve auth sürümleri uygulanır. Finansal kayıtların takma kimlikle saklanması,
geri döndürülemez anonimlik veya yasal uyumluluk garantisi değildir.
Bu işlem borsadaki açık pozisyonları veya emirleri kapatmaz; anahtar kaldırılınca
yerel takip durur. Borsa pozisyonları ve sağlayıcı yedeklerinin saklama/silme
politikası ayrıca yönetilmelidir; dış yedeklerin silindiği iddia edilmez.

Yerel Docker PostgreSQL başlatılmadan önce ayrı, güçlü bir `POSTGRES_PASSWORD`
ortam değişkeni verilmelidir; kaynak kodda varsayılan parola yoktur.
CI action referansları commit SHA ile sabittir ve yalnızca `contents: read`
izni kullanır. Python doğrudan bağımlılıkları sabittir; platformlar arası
transitif kilit/CVE doğrulaması ayrıca gereklidir.

Üretim yayını öncesinde çalışan worker/yazıcılar durdurularak
`backend/migrations/20261004_001_commercial_auth_erasure.sql` elle uygulanmalıdır
(`psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f backend/migrations/20261004_001_commercial_auth_erasure.sql`).
Bu DDL yerel PostgreSQL/Docker bulunmadığı için gerçek PostgreSQL üzerinde
doğrulanmamıştır. İsteğe bağlı store tabloları daha sonra oluşturulursa koşullu
trigger kurulumu için aynı migration tekrar uygulanır; runtime bootstrap ve
canonical auth/silme senkronizasyonu yine gereklidir.

Ana sayfa başlık logosu 252 × 65,8 px hedef boyutuyla önceki boyuttan %40
büyüktür; dar ekranlarda başlık kontrollerini örtmemek için kullanılabilir
genişliğe sığar. Diğer çalışma ekranlarındaki logo boyutu değişmez.

## Profil, hesap güvenliği ve yönetici kullanıcıları

`/settings` ve `/profile` aynı sunucuya bağlı profil ekranını açar. Hesap özeti,
paket/özellikler, e-posta doğrulaması, oturumlar ve etkinlik kayıtları
`/api/v22/account/overview` verisidir; bulunmayan tarihler `—` gösterilir.
Yönetici rolü ücretli Premium abonelik ile aynı şey değildir.

E-posta değişikliğinde eski adres doğrulama tamamlanana kadar korunur. Yeni
adrese gönderilen süreli, tek kullanımlık bağlantı `/profile?email_token=...`
üzerinden açık onay gerektirir. Parola değişikliği mevcut parolayı; Google-only
hesapta mevcut doğrulanmış adrese gönderilen kodu gerektirir. Etkin 2FA için
Authenticator veya tek kullanımlık kurtarma kodu da gerekir. Mevcut Gmail
OAuth sunucu yapılandırması varsayılan olarak korunur; `EMAIL_PROVIDER=resend`
ile aynı işlemler merkezi Resend posta yolunu kullanır. Tarayıcıya posta
anahtarı verilmez. Sağlayıcı yapılandırılmamışsa posta gerektiren işlemler
kullanılamaz ve arayüz sebebi gösterir; başarılı gönderim taklit edilmez.

Hesap 2FA'sı TOTP/Authenticator kurulumudur. QR ve kurtarma kodları yalnızca
kurulum penceresinde gösterilir, tarayıcı depolamasına yazılmaz. Parola ve
Google girişinde ikinci aşama tamamlanmadan oturum açılmaz. Oturum yönetimi
gerçek sunucu oturumlarını kapatır; parola/e-posta değişikliği yeniden giriş
gerektirir. Bu hesap 2FA'sı Canlı İşlem'in ayrı ARM, step-up, Premium ve
consent kapılarının yerine geçmez.

İşlem tercihleri sunucuda kullanıcıya bağlı saklanır: bölüm, zaman dilimi,
Binance, %0.1–1 risk ve USDT sembolleri. Varsayılanlar ilk yüklemede uygulanır;
gecikmiş cevap kullanıcının değiştirdiği seçimi ezmez. `AUTO` tercihi yalnızca
mevcut Auto Trade bölümüne gezinir/odaklanır, otomasyonu açmaz. Riskten marj
hesaplama açık bir kullanıcı eylemidir; yalnızca taslak alanını doldurur,
geçersiz/eksik veya yetersiz bakiyede sebep gösterir ve emir göndermez.

Profilde **hesabı kapat** kalıcı silme değildir: giriş ve oturumlar kapatılır,
kullanıcı ve işlem kayıtları yönetici panelinde tutulur. Ana yönetici ve
güvenli kapatmaya engel olan işlem durumları korunur. Önceki kalıcı erasure
API'si ayrı bir işlemdir ve bu ekran onu çağırmaz.

Admin **Users** bölümü aynı hesap kayıtlarını sayfalı `/admin/accounts`
uçlarından okur; ad/e-posta, rol ve giriş yöntemi, gerçek paket/Premium
erişimi, doğrulama, 2FA durumu, aktif/kapalı hesabı, tarihler, tercihler,
oturumlar ve etkinlikler görüntülenir. Detay/mutasyon uçları yalnızca owner
erişimine açıktır. Parola hash'i, token, 2FA anahtarı, kurtarma kodu ve API
anahtarı döndürülmez. Parola yenileme eylemi kullanıcıya e-posta gönderir,
ham sıfırlama bağlantısını yöneticiye vermez.

QR çizimi build içine gömülü `qrcode.react@4.2.0` (ISC) ile yapılır; çalışma
anında harici QR servisine istek gönderilmez. Offline arayüz testleri:
`npm run build`, ardından `cd frontend` ve
`npx playwright test --config playwright.account-settings.config.ts`.
Bu testler gerçek hesapları değiştirmez veya gerçek e-posta göndermez.

Backend kurulumu `backend/requirements.txt` içindeki `pyotp==2.9.0` ekini
de kapsamalıdır. Startup schema senkronizasyonu hesap belgeleri ve hash'li
tek kullanımlık token tablolarını oluşturur; veritabanında gerekli DDL
yetkileri gerekir. Üretimde `PROTREBOT_DURABLE_AUTH_REQUIRED=1` PostgreSQL
olmadan kapalı kalır. Yerel/offline kayıtlar kalıcı `DATA_DIR` altında
SQLite kullanır. Worker'lar aynı, en az 32 karakterlik sabit `SESSION_SECRET`
(veya mevcut `PROTREBOT_SESSION_SECRET`) kullanmalıdır. TOTP şifreleme anahtarı
bu sırdan türetilir; TOTP taşıma/kurtarma planı olmadan anahtar döndürülmez.
2FA etkinleştirmede eski oturumlar hemen kapatılır; kurtarma kodları
yetkisiz, yalnızca bellekteki ayrı ekranda kullanıcı kaydedene kadar korunur.

## Master Trade / Canlı İşlem arayüzü

Referans tasarımlı Analiz görünümü `/master-trade?tab=analiz` için varsayılandır.
`masterLayoutV2=0` önceki görünümü açıkça seçer. 1280 px ve üzerinde üç kolon,
768–1279 px arasında üstte yatay piyasa şeridi, daha dar ekranlarda tek kolon
ve yatay gösterge kartları kullanılır. Sekme geçişleri parametreleri korur;
karar/skor hesapları, API çağrıları ve işlem güvenlik akışları değişmez.
Sağ karar paneli viewport'a göre tek dikey scroll kullanır. Auto Trade kısayolu
yalnız Canlı İşlem sekmesine geçip mevcut Auto Trade bölümünü odaklar;
otomasyonu açmaz, onay/2FA/ARM kapılarını atlamaz ve istek göndermez.
Analiz ekranı mevcut LIVE snapshot'ını gösterir; Demo durumu varsayılmaz veya
ek istekle sorgulanmaz. Durum bilinmiyorsa `—` gösterilir. Auto Trade toggle'ı
salt okunur durum/navigasyon sunumudur; eksik güvenlik kapılarında kilitli ve
devre dışıdır. Limit/Piyasa Analiz'den emir göndermez, kilit bilgisi gösterir.
Yenile mevcut hesap yenileme akışını kullanır. Test/önizleme verileri yalnız
test yardımcılarında bulunur: 200 mum, yaklaşık %1–2 fiyat aralığı ve birbirinden
%0,3–1,5 uzak seviyeler; ürün veri akışı bunları içe aktarmaz.

Masaüstünde sol liste ve sağ rapor kendi içinde kayar; orta kolon viewport'u
doldurur, grafik kartı en az 460 px olur. Grafik ekseni yalnız görünür mumların
high/low aralığını üst ve altta %8 payla kullanır; entry/TP/SL veya güncel fiyat
ekseni genişletmez. Aralık dışı seviyeler gerçek değerleriyle oklu kenar pill'i
olarak kalır. Seviye pill'leri aralanır ve yakındaki eksen rakamları gizlenir.
Hacim alt %15 ile sınırlıdır; dar grafikte gösterilen mum sayısı okunur gövde
genişliğine göre uyarlanır. Bunlar yalnız çizim kurallarıdır; analiz hesapları
ve mum/veri istekleri değişmez.

Coin logoları `cryptocurrency-icons` 0.18.1 paketinin CC0-1.0 (CC0 1.0 Universal)
asset'lerinden alınmıştır. Paket yalnız devDependency'dir; 483 yerel renkli SVG
[`src/assets/coins/`](src/assets/coins/) altında lisansın tam metniyle birlikte
tutulur. Ortak `CoinIcon` bileşeni Vite'ın lazy glob import'larını kullanır;
yalnız monte edilen satırların SVG modülleri aynı kaynaktan yüklenir, dış CDN
yoktur. Bilinmeyen base asset tutarlı renkli harf avatarıyla gösterilir.
1000/10000/1000000/1M önekleri yalnız logo eşlemesinde kaldırılır; 1INCH korunur,
sembol/işlem seçim mantığı değişmez.

Güncel logo kaynağı [Web3 Icons](https://github.com/0xa3k5/web3icons),
`@web3icons/core` 4.0.58'dir (MIT, Copyright (c) 2024 0xa3k5).
Tam lisans [`LICENSE-web3icons.txt`](src/assets/coins/LICENSE-web3icons.txt)
içinde bulunur. Logolar ilgili projelerin ticari markalarıdır; bu uygulama
projelerle ortaklık veya onay iddiasında bulunmaz. Kaynak revizyonu, kaynak
tarihi ve indirme tarihi [`logo-manifest.json`](src/assets/coins/logo-manifest.json)
içinde kaydedilir. İlk güncel veri ölçümü 525 gerçek USDT perpetual sembolünde
228 logo / 297 harf fallback'idir; 218 optimize SVG yerel olarak üretilmiştir.

[`tools/fetch-coin-logos.mjs`](tools/fetch-coin-logos.mjs) iki frontend build'inde
çalışır. Sembol evreni Binance'in herkese açık exchangeInfo verisidir; offline
test fixture'ları kaynak olarak kullanılmaz. GitHub'ın MIT lisansı doğrulanır,
metadata bir commit SHA'sına sabitlenir; npm'den tek toplu arşiv indirilip
SHA-512 bütünlüğü denetlenir. SVG string modülleri **çalıştırılmaz**; yalnız
statik string export'ları okunur, dış kaynak/aktif içerik reddedilir ve SVGO
ile 64×64 boyuta optimize edilir. Kaynak eşleşmesi belirsizse otomatik
seçim yapılmaz. [GitHub REST sınırı](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)
kimlik doğrulamasız 60 istek/saattir; yenilemede tek REST isteği ve toplu
dosyalar kullanılır, 24 saat cache vardır, otomatik tekrar yoktur. HTTP
429/diğer hatalar açıkça uyarılır; mevcut dosyalar/manifest korunur, build
kırılmaz. [GitHub kullanım şartları](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service)
ve kaynak MIT lisansı geçerlidir; API anahtarı veya CoinGecko hesabı gerekmez.

Manuel yenileme: `node tools/fetch-coin-logos.mjs --refresh`. Ağsız build:
`COIN_LOGO_OFFLINE=1` veya script'e `--offline`. `CoinIcon` önce yeni manifest'i,
sonra eski CC0 SVG'yi, en son deterministik renkli/ortalı harf avatarını dener;
yükleme ve görüntü çözümleme hatalarında da bu sırayı izler. Yalnız görünür
sanal satırlar logo modüllerini lazy yükler; çalışma anında CDN isteği yoktur.
[`logo-overrides.json`](src/assets/coins/logo-overrides.json) kaynak kimliği,
eski dosya ve görsel alias override alanıdır. BTCDOM bir dominance endeksidir,
BTC logo proxy'si açıklamalı kullanılır; sembol veya analiz BTC'ye dönüştürülmez.
LUNA2 yalnız doğru `terra-luna-2` kaynağına bağlanır; eski LUNA markası
fallback olarak kullanılmaz.

650 satırlı performans preview'ındaki 637 sentetik `TOKENxxxxUSDT` satırı
`test: true` ile işaretlidir: panelde **OFFLINE TEST** uyarısı, satırda **TEST**
etiketi gösterilir. Bunlar yalnız test/önizleme fixture'ıdır, gerçek backend
sembol evrenine veya üretim bundle'ına eklenmez.

Referans Analiz piyasa listesi `/api/markets?all=true` ile aktif `TRADING`,
`PERPETUAL`, USDT kontratlarının tamamını okur. Varsayılan/`limit` endpoint'i,
scanner'ın 40 sembollük kapsamı ve analiz/kredi akışı değişmez. Borsa metadatası
600 saniye, tek toplu 24 saat ticker sonucu 3 saniye süreç-içi cache'lenir;
eşzamanlı yenilemeler birleştirilir. Hatalarda son başarılı sonuç
`X-Market-Stale: 1` ile ve UI'da uyarıyla gösterilir; soğuk cache hatası 502/503
döndürür. 429/418 yanıtında en az 30 saniye veya daha uzun `Retry-After` beklenir.
İstemci tek uç noktayı 3 saniyede bir, örtüşmeden ve görünürken yeniler;
her sembol için ayrı fiyat/analiz isteği atmaz.

Bu toplu feed sol listeyle seçili sembolün fiyat, 24 saat değişim ve hacim
sunumunda ortaktır; ilk 50 markete veya scanner kapsamına girmeyen coinler de
kendi ticker verisini gösterir. Sembol seçimi mevcut `/api/klines/{symbol}`
ve `/api/analysis/{symbol}` akışını, seçili zaman dilimini ve beş MTF isteğini
kullanır. Önceki sembol/zaman diliminin snapshot'ı yeni seçimde gösterilmez.
AI raporu, göstergeler ve TP/SL aynı seçili analizin çıktısından gelir;
küçük fiyatlı coinlerin MACD değerlerinde anlamlı basamaklar korunur,
sıfıra yuvarlanmaz. Her ticker yenilemesinde tüm coinler analiz edilmez. Canlı İşlem'e geçiş
seçili ham sembolü mevcut emir formuna taşır; premium, kredi, consent, ARM,
2FA ve backend sembol/quantity kuralları değişmez.
Yeni listelenen kontratta yeterli mum yoksa backend'in mevcut 422 yanıtı
korunur; rapor veya hedef uydurulmaz. Offline görsel önizleme canlı işlem
ortamı değildir ve fixture'ı olmayan bir coin için gerçek analiz sunmaz.

Seçili sembolün veri hatasında grafik, sebebiyle birlikte
**Bu sembol için yeterli veri yok** mesajını gösterir; rapor rozeti **Analiz yok**
olur. Mum geçmişi yetersizliği (422), borsanın geçersiz sembol yanıtı ve
backend/erişim hataları ayrılır; veri hatası AL/SAT/BEKLE sonucu üretmez.
Yalnız bir MTF zaman dilimi eksikse geçerli ana analiz korunur, eksik zaman
dilimi ve nedeni açıkça bildirilir; teyit veya hedef uydurulmaz.
Grafik araç çubuğundaki **YENİLE**, seçili sembol/zaman diliminin mumlarını,
ana analizini ve beş MTF analizini yeniden alır; hesap veya işlem yetkisini
değiştirmez. Aynı seçim yenilenirken son geçerli snapshot yanıt gelene kadar
korunur; sembol/zaman dilimi değişiminde eski veri gösterilmez.
Toplu fiyat akışının mevcut güncelleme sıklığı korunur.
Offline preview sunucusu fixture'ı olmayan sembole
`{code: "PREVIEW_DATA_UNAVAILABLE", detail: "Önizleme: bu sembol için örnek veri yok"}`
ile 422 döndürür. UI bu durumu gerçek borsa/mum geçmişi hatası olarak sunmaz.

Liste sabit 52 px satırlarla, iki satır overscan'li sanal pencere kullanır;
1280 px altında 260 px yatay chip'lere geçer. Arama 150 ms debounce'ludur.
Skor/yön yalnız mevcut scanner verisinden gelir; diğerleri `—` kalır.
Favoriler `protrebot:master-market-favorites:<userId>` localStorage anahtarında
kullanıcıya özeldir; okuma/yazma hataları açıkça bildirilir. Fiyat veya değişim
yoksa sayı üretilmez; düşük fiyatlarda anlamlı basamaklar korunur.

650 sembollü native kaydırma/asset bütçesi, geliştirme JSX stack
enstrümantasyonu olmadan shipping build üzerinde ölçülür. Önce `npm run build`,
ardından frontend Playwright CLI ile
`--config frontend/playwright.master-market-performance.config.ts` kullanılır.
Konfigürasyon tek yerel preview sunucusu açar; testler API'leri offline mock'lar,
tanımsız API'yi 501 ile reddeder ve dış HTTPS/WSS erişimini engeller.
Performans testi diğer projelerde atlanır, üretim preview projesinde 50 ms p95
bütçesini ve sınırlı DOM/lazy asset sayısını zorunlu tutar. Test çıktıları
varsayılan olarak repo dışındaki geçici dizine, isteğe göre
`MASTER_TEST_RESULTS` / `MASTER_ANALYSIS_SCREENSHOTS` yollarına yazılır.

Trigger Monitor'un durum pill'i, 2×2 veri alanı ve accordion özetleri mevcut
snapshot'tan üretilir; eksik veriler `—` kalır. Accordion'lar varsayılan kapalıdır.
Auto Trade durum/açıklama satırları ve outline kısayol yalnız sunumdur; mevcut
kilit ve onay akışını değiştirmez. Bağlantı özeti yalnız üst barda gösterilir.

Sağ kolonun altı danışma accordion'u aynı adlı native `details` grubuyla
varsayılan kapalı ve aynı anda tek-açık çalışır. Enter/Space, odak halkası ve
`aria-expanded` desteklenir; 180 ms yükseklik geçişi azaltılmış hareket tercihinde
kapatılır. Koşul segmentleri, kontrol durum chip'leri, zaman çizelgesi, skor
çubukları ve LONG/SHORT görünümü yalnız mevcut verinin sunumudur. Skor veya
işlem hesaplaması yapılmaz; sayısal çubuklar yalnız çizim alanına sınırlandırılır.
Olaylar görünümde en yeni üstte sıralanır, kaynak dizi değiştirilmez. Boş
accordion içerikleri statik soluk placeholder kullanır. Bütün yeni içerik
mevcut `PremiumBoundary` içinde kalır; görünüm seçimleri istek göndermez.

`/master-trade?tab=canli` koyu/yeşil temada kompakt kartlar, yatay stepper,
responsive metrik kutuları ve masaüstünde yan yana manuel emir/özet görünümü kullanır.
Eksik metrikler skeleton ile, sinyal fiyatları yalnızca gösterimde iki ondalıkla sunulur;
ham değerler tooltip'te korunur. Dar ekranlarda içerik tek kolona, metrikler iki kolona
geçer. LOCKED, ARM, consent, backend doğrulamaları ve mevcut işlem koşulları değişmez.

Hesap durumu okunamazsa önceki LIVE yetkisi temizlenir; ağ istekleri zaman aşımıyla
sınırlıdır. Onaylar aynı anda yalnız bir işlem gönderir. Aktif Auto Trade oturumunu
yeniden başlatmak HTTP 409 döndürür ve yetki süresini uzatmaz. Bir canlı kapı kapanırsa
otomasyon, ARM ve otomatik oturum yetkisi kapatılır; mevcut koruyucu emirler korunur.
Sembol/zaman dilimi değişiminde eski analiz yanıtları yeni görünümü değiştiremez.
LIVE geçmişi ve performans yalnız `/v25/status` içindeki doğrulanmış kapanmış LIVE
planlarından üretilir; DEMO günlüğü kullanılmaz. Kapatma sonucu yenilenmiş plan
kimliği ve o işlemin doğrulanmış PnL değeriyle kontrol edilir.

Owner trading-account listesi, kullanıcıya ait `account_reference` dahil güvenli
mapping alanlarını SELECT eder; API key/secret döndürmez. Test fixture'ları da
istek kapsamındaki canonical oturum doğrulamasını kullanır; OWNER kontrolü ve
doğrulanmamış oturumun reddi korunur.

Doğrulama: kökte `npm run build`; `frontend` içinde
`npx playwright test master-trade-live-ui.spec.ts master-trade-readonly.spec.ts --project=chromium`.
UI testi gerçek emir/bağlantı işlemi yapmadan mock verilerle masaüstü, tablet ve mobil
layout ölçülerini, kilitli butonları ve yerel form/toggle davranışlarını denetler.
Ekran görüntüleri test çıktısına eklenir; isteğe bağlı `LIVE_UI_SCREENSHOTS` değişkeniyle
ayrı bir çıktı klasörü, `LIVE_UI_PHASE` ile dosya adı öneki belirtilebilir.
Frontend lint: kökte `npm run lint`.
Build, kredi/premium modüllerinin strict TypeScript kontrolünü de çalıştırır
(`npm run typecheck:access` ile ayrı çalıştırılabilir).
Analyst coin seçici aynı anda en fazla beş satır gösterir; diğer coinlere liste
içinden kaydırılarak erişilir. Arama sonucu azaldığında kart içerik kadar küçülür.

## Analyst kredileri ve Master Trade Premium

Tüm korumalı API çağrıları aktif, doğrulanmış üyelik oturumu gerektirir; yönetici
önizleme anahtarı tek başına üyelik yerine geçmez. Ücretsiz üyeler Master Trade'i
salt-okunur görüntüler. İşlem/bağlantı yetkisi mevcut OWNER veya aktif abonelik
kurallarından belirlenir; istemci plan bilgisi yetki vermez. Premium, mevcut
LOCKED / ARM / consent ve risk kapılarını kaldırmaz.

Sunucu ayarları: `ANALYST_DAILY_CREDITS=100`, `ANALYST_COST=10`,
`CREDIT_WINDOW_HOURS=24`, `ANALYST_CACHE_MINUTES=15`.
Varsayılan analiz maliyeti `backend/app/analyst_credits.py` içindeki
`ANALYSIS_COST` sabitinden gelir; `ANALYST_COST` mevcut ortam ayarıyla değiştirilebilir.
100 kredilik bütçeyle 24 saatlik pencerede 10 taze analiz açılır; 11. taze analiz
kredi yetersizliğiyle reddedilir. Cache sonuçları bu bütçeden harcamaz.

- `GET /api/analyst/credits`: `remaining`, `total`, `analysis_cost`, `resetsAt`, `unlimited`.
- `POST /api/analyst/consume`: `{symbol, timeframe, idempotency_key}`.
  Yanıt aynı bütçe alanlarıyla birlikte `result`, `cached`, `cacheExpiresAt` döndürür.
  Sembol USDT paritesidir; mevcut `1m/5m/15m/30m/1h/4h/1d` zaman dilimleri desteklenir.
  Her yeni kullanıcı aksiyonunda yeni bir anahtar, aynı isteğin yeniden gönderiminde
  aynı anahtar kullanılmalıdır.
- İlk harcamada başlayan kullanıcıya özel 24 saatlik pencere dolunca bütçe sonraki
  istekte yeniden 100 olur; kredi devretmez. Aynı kullanıcı/sembol/zaman dilimi
  15 dakika içinde yeniden açılırsa kayıtlı sonuç ücretsiz döner.
- Kredi yetersizliği: `429` + `remaining`/`resetsAt`. Dakikada 30 consume isteği
  sınırı cache ve premium isteklerini de kapsar; rate-limit yanıtında `Retry-After`
  bulunur. Geçersiz/engellenen çağrılar loglanır.
- Analiz başarısızlığı harcanan maliyet kadar (varsayılan 10 kredi) tek iade ile
  `502` döndürür. Harcama, cache ve idempotency
  kaydı atomiktir; aynı anahtar çift harcama veya çift iade üretmez.
- Premium için `remaining=null`, `unlimited=true`; kredi düşmez. Master Trade
  kredi endpoint'lerine çağrı yapmaz ve sayaç göstermez.

Üretimde PostgreSQL transaction ve kullanıcı satırı kilidi kullanılır.
`PROTREBOT_DURABLE_AUTH_REQUIRED=true` iken PostgreSQL yoksa servis `503` ile kapalı
kalır. Yerel geliştirmede `DATA_DIR/analyst_credits.sqlite3` kalıcı SQLite deposu
kullanılır. Mevcut otomatik testler SQLite üzerinden çalışır; canlı PostgreSQL
entegrasyonu ayrıca deployment ortamında doğrulanmalıdır.

Premium olmayanın emir/arm/dry-run/bağlantı ve alternatif otomasyon başlangıç
endpoint'leri `403 PREMIUM_REQUIRED` döndürür. Ücretsiz trading GET yanıtlarında
yalnız izinli özet/piyasa alanları gönderilir; giriş/SL/TP, karar gerekçeleri,
vakalar ve trigger detayları sunucuda kaldırılır. Arayüz kilitleri gerçek içeriği
render etmez; yalnız örnek skeleton gösterir. İlk kilit tıklaması oturumda premium
modalı (mobilde sheet), sonraki tıklamalar toast açar.

Doğrulama:
```powershell
npm run build
npm run lint
$env:PYTHONPATH='backend'
.\.venv\Scripts\python.exe -m pytest backend\tests\test_analyst_credits.py backend\tests\test_member_premium_api.py backend\tests\test_subscription.py -q
.\.venv\Scripts\python.exe -m ruff check --select E9,F63,F7,F82 backend\app\analyst_credits.py backend\app\premium_access.py backend\app\main.py backend\app\v22_commercial.py backend\app\v25_execution.py backend\tests\test_analyst_credits.py backend\tests\test_member_premium_api.py
Set-Location frontend
npx playwright test member-premium.spec.ts master-trade-live-ui.spec.ts master-trade-readonly.spec.ts --project=chromium
```

V28, V27 bulut operasyon ve kanıt altyapısını korur; Testnet ve gerçek Binance USD-M
Futures API bağlantılarını doğrudan programın içine taşır. Render'a Binance anahtarı yazmak
gerekmez. Yönetici panelindeki **Borsa Bağlantıları** sekmesinden API Key ve Secret Key
test edilir, şifreli kaydedilir, aktifleştirilir, kapatılır veya silinir.

## Kais AI yapılandırması

Asistan ayarlarının tek kaynağı `backend/app/assistant_config.py` içindeki
`AssistantConfig` / `load_assistant_config()` yapısıdır. Ayarlar backend ortamından
okunur; diğer modüller model, limit veya fiyat değerlerini tekrar sabitlememelidir.

| Ortam değişkeni | Varsayılan | Amaç |
| --- | --- | --- |
| `ASSISTANT_ENABLED` | `true` | Özelliği açma/kapatma |
| `ASSISTANT_MODEL` | `claude-haiku-4-5-20251001` | Kullanılacak model kimliği |
| `ANTHROPIC_API_KEY` | boş | Yalnız backend secret store |
| `ASSISTANT_MAX_INPUT_CHARS` | `500` | Tek mesaj karakter sınırı |
| `ASSISTANT_HISTORY_MESSAGES` | `4` | LLM'e gönderilecek geçmiş mesaj sayısı |
| `ASSISTANT_HISTORY_MESSAGE_MAX_CHARS` | `2000` | Her geçmiş mesajı sunucuda bu uzunluğa kırpılır |
| `ASSISTANT_PAGE_CONTEXT_MAX_CHARS` | `300` | Sayfa bağlamı karakter sınırı |
| `ASSISTANT_REQUEST_TIMEOUT_SECONDS` | `30` | Sağlayıcı ve yerel veritabanı bekleme sınırı |
| `ASSISTANT_PROTECTION_STALE_SECONDS` | `30` | Koruma/pozisyon verisinin taban eskime eşiği; etkin eşik `max(ayar, 2 × RECONCILE_SECONDS)` |
| `ASSISTANT_PROACTIVE_ENABLED` | `true` | Uygulama içi, LLM'siz durum yoklamaları |
| `ASSISTANT_PROACTIVE_INACTIVE_DAYS` | `7` | Asistanın ölçtüğü önceki uygulama ziyaretinden sonra hatırlatma eşiği |
| `ASSISTANT_PROACTIVE_COOLDOWN_HOURS` | `24` | Kullanıcı başına yoklamalar arası kayan bekleme; en az 24 saat |
| `ASSISTANT_PROACTIVE_POLL_SECONDS` | `300` | Görünür sayfada kontrol aralığı; en az 60 saniye |
| `ASSISTANT_SECRET_MIN_ALPHANUMERIC_CHARS` | `40` | Uzun anahtar benzeri dizilerin engellenme eşiği |
| `ASSISTANT_BUDGET_WARNING_FRACTION` | `0.8` | Aylık bütçe uyarısı eşiği |
| `ASSISTANT_PER_MINUTE_LIMIT` | `6` | Kullanıcı başına dakikalık sınır |
| `ASSISTANT_DAILY_LIMIT` | `20` | Kullanıcı başına UTC takvim günü sınırı |
| `ASSISTANT_MONTHLY_BUDGET_USD` | `50` | Aylık USD bütçesi |
| `ASSISTANT_MAX_OUTPUT_TOKENS` | `500` | Her üretim çağrısının çıktı token sınırı |
| `ASSISTANT_MAX_LLM_CALLS_PER_MESSAGE` | `3` | Tek mesajın üretim çağrısı sınırı; en fazla üç olabilir |
| `ASSISTANT_MAX_TOTAL_INPUT_TOKENS_PER_MESSAGE` | `12000` | Mesaj boyunca toplam girdi token sınırı; cache yazma/okuma dahil |
| `ASSISTANT_MAX_TOTAL_OUTPUT_TOKENS_PER_MESSAGE` | `1500` | Mesaj boyunca toplam çıktı token sınırı |
| `ASSISTANT_INPUT_PRICE_USD_PER_MILLION` | `1` | Milyon girdi token'ı başına USD |
| `ASSISTANT_OUTPUT_PRICE_USD_PER_MILLION` | `5` | Milyon çıktı token'ı başına USD |
| `ASSISTANT_CACHE_WRITE_PRICE_USD_PER_MILLION` | `1.25` | Milyon kısa süreli prompt-cache yazma token'ı başına USD |
| `ASSISTANT_CACHE_READ_PRICE_USD_PER_MILLION` | `0.10` | Milyon prompt-cache okuma token'ı başına USD |

Fiyat varsayılanları Claude Haiku 4.5'in
[resmi temel token tarifesine](https://platform.claude.com/docs/en/about-claude/pricing)
dayanır. Model seçimi yalnız `ASSISTANT_MODEL` ile değişir; maliyet hesabının doğru
kalması için seçilen modelin güncel girdi/çıktı/cache tarifeleri ilgili fiyat ayarlarında
tutulmalıdır. Para değerleri kayan nokta yerine `Decimal` olarak okunur.

Anahtar yoksa/boşsa veya özellik kapalıysa `available=False` ve
`unavailable_reason` üzerinden "Asistan kullanılamıyor" durumu alınır; config
yükleme uygulamayı çökertmez veya LLM çağrısı yapmaz. Anahtar repr ve config
serileştirmesine dahil edilmez. Geçersiz ayarlar açık doğrulama hatası üretir;
sessizce başka limitlere düşülmez. Model ve pozitif istek sınırları doğrulanır;
geçmiş sayısı, bütçe ve fiyatlar sıfır olabilir, negatif veya sonsuz olamaz.

Render anahtarı `sync: false` olarak tanımlar; anahtar değeri repoya veya frontend
`VITE_*` değişkenlerine yazılmamalıdır.

### Üyelik korumalı asistan API

- `POST /api/assistant/chat`: `{message, history?: [{role, content}], page_context?}`.
  Roller yalnız `user` / `assistant`; kimlik sadece doğrulanmış üyelik token'ından
  alınır. Gövde, geçmiş veya query içindeki `user_id` kullanılmaz.
  Yanıt `{reply, language: "tr"|"en", sources: [...]}`. Plan/fiyat/trial, kendi planı
  ve kredi sorularında aşağıdaki hızlı yol kullanılır; diğer mesajlar LLM'e gider.
  Türkçe karakter/anahtar kelime varsa TR, yoksa EN seçilir.
- `GET /api/assistant/usage`: `{remaining, total, resetsAt}`; UTC ertesi gece
  yarısında günlük hak yenilenir. Premium üyeler de aynı asistan limitlerine tabidir.
- Boş/geçersiz, yeni mesaj/sayfa bağlamı sınırını veya geçmiş mesaj sayısını aşan
  istekler `422`; geçmiş içeriği uzun olduğunda reddedilmez, kırpılır. Secret kontrolü
  geçmiş kırpılmadan önce yapılır. Secret/anahtar benzeri içerik LLM'e gönderilmez
  ve hak düşürülmeden uyarı döner. Bu desen tabanlı önlem tüm secret türlerini
  saptama garantisi değildir; kullanıcı sohbetle hiçbir kimlik bilgisi paylaşmamalıdır.
- Dakika penceresi ilk kabul edilen istekte başlar; günlük pencere UTC takvim günüdür.
  Limit aşımı `429`, yerelleştirilmiş bekleme süresi ve `Retry-After` içerir.
  Sağlayıcıya gönderilmiş başarısız çağrı da mesaj hakkından düşer; doğrulama,
  secret, bütçe ve yapılandırma nedeniyle reddedilen istekler düşmez.
  Çok turlu yanıt tek mesaj hakkı kullanır; sonraki turlar hak düşürmez.
- Aylık bütçe UTC ayına göre uygulama genelindedir. Gerçek input/output ve ayrı
  cache yazma/okuma token'ları kendi fiyatlarıyla `Decimal` maliyete çevrilir.
  Usage yoksa/geçersizse system/araç/mesaj zarfının UTF-8 bayt sayısı temkinli
  girdi tahmini olarak, çıktı için o çağrının çıktı tavanı kullanılır; girdi
  tahmini en yüksek yapılandırılmış girdi/cache tarifesiyle ücretlenir. Çağrı kaydında
  `estimated=true` ve metadata uyarısı bulunur. Asistan bu durumda çalışmayı sürdürür.
  Bütçe uyarısı her ay eşik ilk geçildiğinde yazılır.
- Bütçeye ulaşılmışsa LLM çağrısı yapılmaz (`503`, yoğunluk mesajı).
  Son kabul edilen çağrı gerçek/tahmini maliyetiyle tavanı aşabilir; bundan sonraki
  çağrılar kesilir. Bu bir ön rezervasyonla garanti edilen mutlak harcama tavanı değildir.
- Sayaçlar, aylık toplam ve yalnız metadata içeren çağrı kayıtları PostgreSQL'de
  `assistant_usage`, `assistant_monthly_spend`, `assistant_calls` tablolarında;
  Yalnız `PROTREBOT_DURABLE_AUTH_REQUIRED=false` olan yerel/legacy ortamda,
  DB pool yoksa mevcut `DATA_DIR` altında `assistant_usage.sqlite3` kullanılabilir.
  Cache token sütunları eski şemaya mevcut bakiye/kayıtlar korunarak eklenir;
  bu şema güncellemesi backend'ler arasında veri taşıma anlamına gelmez.
  `PROTREBOT_DURABLE_AUTH_REQUIRED=true` ile üretimde PostgreSQL zorunludur;
  ilk bağlantıda da SQLite'a geçilmez. Backend değiştirmek
  mevcut kayıtların taşınmasını gerektirir; otomatik migration yapılmaz.
- Aylık satır kilidi (PostgreSQL `FOR UPDATE`, SQLite `BEGIN IMMEDIATE`) sağlayıcı
  her bir üretim çağrısı boyunca tutulur; maliyet araç çalıştırılmadan ve sonraki
  çağrıdan önce ayrı transaction ile kaydedilir. Eşzamanlı bütçe/limit kontrolünü
  korur ancak LLM çağrılarını
  aynı ay içinde global olarak sıraya koyar. PostgreSQL kullanıcı sayacı ayrıca
  satır kilidiyle korunur; ay geçişindeki eşzamanlı istekler de limiti atlayamaz.
  İstemci bağlantısı kesilse de hesaplama tamamlanır.
  Çağrı sonrası kayıt/commit başarısızlığı maliyeti belirsiz bırakırsa aylık
  `accounting_blocked` kaydı sonraki çağrıları kapatır. Veritabanına bu işaret de
  yazılamazsa yerel kalıcı `assistant-accounting-YYYY-MM.blocked` dosyası kullanılır;
  yönetici kayıtları uzlaştırmadan bu işaretler kaldırılmamalıdır. Yerel dosya
  farklı sunucular arasında paylaşılmaz; dağıtık DB kesintisinde uzlaştırma gerekir.
- Backend mesaj, geçmiş, sayfa bağlamı ve API anahtarını saklamaz veya loglamaz. Loglarda
  kullanıcı/zaman/token/maliyet/araç listesi ve tahmin işareti bulunur; sağlayıcı
  HTTP debug içerikleri ilgili çağrı sırasında bastırılır.

İşlem, ARM, consent veya abonelik değiştirme işlevi asistana bağlanmaz.
Analyst harcaması yalnız aşağıdaki ayrı açık onay endpoint'inden yapılabilir.
Aktif `TestnetFirstApp.tsx`, ayrı `AssistantChat.tsx` / `assistant.css` bileşenini
üyelik bağlamı altında kullanır. Kais AI başlık düğmesi desktop yan panelini veya
mobil sheet'i native dialog olarak açar; Escape/kapatma odağı açan düğmeye döndürür.
Başlık düğmesi mobilde de en az 44 × 44 px hedefle, diğer kontrollerin solunda
kalır; mevcut menü/işlem düğmeleri küçültülmez. Okunmamış yoklama rozeti gözün
üstünde gösterilir. Panel kendiliğinden açılmaz; çalışma alanı değişince kapanır.
Launcher yalnız header içinde, çalışma alanlarında 58 px yuvarlak dokunma
hedefinde 56 px göz ve hafif turkuaz parlamayla gösterilir. Ana sayfada göz
67,2 px, hedef 69,6 px olur; logo ve yenile/bildirim/profil simgeleri de gerçek
boyutlarıyla %20 büyüktür. Diğer dokunma hedefleri en az 44 px kalır.
320/390 px ekranlarda logonun yalnız boş raster kenarları kırpılır; görünür
logo küçültülmez, header yüksekliği ve çevrimiçi göstergesi değişmez.
Sağ-alt floating yerleşimi veya ekrana
göre küçültme yoktur. Erişilebilir adı her dilde `Kais AI` olur. İlk sekme
oturumunda küçük etiket 5 saniye görünür; `sessionStorage` ile reload ve
header değişimlerinde tekrarlanmaz. Kalıcı düğme yazısı yoktur.
Gözün altındaki sabit karşılama balonu yalnız oturum açıkken yaklaşık 2 saniye
sonra çıkar ve 8 saniye görünür. Kullanıcıya özel `protrebot-kais-greeting:<id>`
localStorage anahtarında yerel tarih tutularak günde bir kez gösterilir; depolama
engelliyse gösterilmez. Gizli sekmede süreler durur, reduced-motion animasyonu
kapatır. X balonu kapatır; metin veya göz mevcut sohbeti açar. Balon LLM/API
çağrısı yapmaz, sohbet açıkken gösterilmez ve sayfa odağını kendiliğinden almaz.
Sohbet geçmişi yalnız tarayıcıda `kais-chat:v1:<encodeURIComponent(userId)>`
localStorage kaydında `{version: 1, savedAt, messages}` olarak tutulur; sunucuya
arşiv gönderilmez. Son 50 tamamlanmış mesaj, mesaj başına 4000 Unicode karakter
saklanır; 30 günden eski/bozuk/uyumsuz kayıt silinir. Bekleyen/başarısız mesajlar
ve onay nesneleri kaydedilmez. Depolama kopyasında Bearer/Basic, JWT, sk- ve
api/secret/token/key/parola değerleri, 32+ karakterlik anahtar benzeri dizeler
ve bilinen onay kanıtları `[MASKED]` yapılır; mevcut ekrandaki metin değişmez.
Depolama hatası yalnız console uyarısı üretir, sohbet bellekte çalışmaya devam eder.
Sıfırlama kaydı siler; ortak `clearUserSessionToken` çıkış/oturum temizliği tüm
`kais-chat:*` kayıtlarını siler. Yeni doğrulanmış kullanıcı açılırken diğer
kullanıcıların kayıtları temizlenir; eski sessionStorage arşivleri taşınmaz, silinir.
Sekmeler native storage olayıyla son yazan kazanır şeklinde eşitlenir; uzaktan
değişim bekleyen sohbet isteğini iptal eder. Model bağlamı sunucunun history_messages
sınırıyla ve ayrıca en fazla 12 tamamlanmış, boş olmayan, proaktif olmayan mesajla
sınırlıdır; örnek yapılandırmanın mevcut history_messages değeri 4'tür.
Profil yenilemesinde token yokluğu veya 401 ve owner erişim temizliği sohbet
arşivini de siler; auth kararları değişmez. Başka sekmedeki user-session anahtarı
silinince/değişince sohbet durdurulur. Nesil koruması eski yazarların kayıtları
yeniden oluşturmasını engeller; doğrulanmış profil sonrası yeni sohbet oturumu
başlar. Asistanın kendi 401'i yalnız mevcut kullanıcı kaydını temizler.
Üye bakım yoklamasının 401 fail-open davranışı aynen korunur. Playwright gerçek
AuthGate tıklamaları için `auth-e2e` modunda 4175, gerçek owner kapısı için root
production build preview 4176 kullanır; tam testlerden önce root build alınmalıdır.
CI bu nedenle hem root hem frontend bağımlılıklarını kurar ve preview öncesinde
root build alır. Ortak Demo/v21 UI fixture'ları durum limitlerini ve scanner,
settings, stream, history/performance koleksiyonlarını sözleşmeye uygun sağlar;
gerçek işlem veya ARM yanıtı üretmez.
Panel masaüstünde viewport'a sığan 400×600 px koyu cam yüzey, 14 px blur ve
desteklenmeyen tarayıcılarda opak koyu fallback kullanır. Başlıkta 36 px göz,
çevrimiçi noktası, temizle/kapat ve yoklama kutusunu içeren "⋯" ayarı vardır.
Kullanım bilgisi ince kalan mesaj çubuğuyla gösterilir; işlem yapmama rozeti korunur.
Asistan balonunun solunda 24 px avatar, sağda turkuaz kullanıcı balonu vardır.
Hazır sorular dört seçenektir: "Premium üyelik ne kadar?",
"Günlük kullanım limitim ne kadar?", "API anahtarlarımı nasıl girerim?" ve
"BTC için analiz durumu ne?". İngilizce arayüz karşılıklarını kullanır;
düğmede görünen soru değiştirilmeden sohbet isteğinin `message` alanına gider.
Öneriler tek satırda yatay kaydırılır ve ilk mesajdan sonra gizlenir.
Asistan yanıtları yerel panoya kopyalanabilir; kopyalama hatası açıkça gösterilir.
Karakter sayacı giriş limitinin %80'ine ulaşınca görünür.
Kontrollü textarea 48–120 px aralığında kendiliğinden büyür
(dar klavye görünümünde panel yüksekliğine göre 48–80 px ile sınırlanır). Typing noktaları yalnız
transform/opacity ile nabız atar. Mobil sheet mevcut visualViewport'un
%75'ini ve safe-area boşluklarını kullanır; input/gönder sabit
alt bölümde kalır. Panel yüksekliği dvh/visualViewport ve safe-area ile sınırlıdır;
iç yerleşim bu sınıra bağlıdır. Kısa görünümde işlem yapmama rozeti gizlenerek
başlık ve yazı alanına yer ayrılır. Native dialog, onay, CopyProtection ve odak akışı korunur.
Kısa viewport ve açık klavyede başlık/yazı alanı kaydırılmaz; aradaki kullanım,
mesajlar ve öneriler tek kaydırılabilir bölgededir. Panel yüksekliği dinamik
viewport ve safe-area sınırlarıyla kısıtlanır.
Panel ve karşılama balonu, başlık düzeni değişince düğme/başlık
geometrisini yeniden ölçer; içerik veya alan değeri okunmaz. Karşılama balonu
başlık ve görünür başlık düğmelerinin ölçülen en alt kenarına göre 12 px
boşlukla yerleşir; dar ekranda sarılan başlık kontrollerini de örtmez.
ResizeObserver, resize/scroll ve başlık geçişleri konumu günceller; Master Trade
başlığı yüklenirken geçici slot kaybı günlük karşılama süresini sonlandırmaz.
Masaüstü Master Trade ortak başlığı gizlediği için aynı düğme, mevcut terminal
başlığındaki boş slota portal ile taşınır; ikinci asistan oturumu oluşturulmaz.
Kök `main.tsx` → `TestnetFirstApp.tsx` → `AssistantChat.tsx` aktif zincirdir;
`frontend/src` altındaki aynı isimli eski uygulama bu tasarımın hedefi değildir.
`KaisEye.tsx` bağımsız, şeffaf arka planlı katmanlı bir SVG'dir. Dış segmentli
halka/balon kuyruğu, üst/alt kapak, göz akı, iris, bebeği, beyaz nabız çizgisi,
yansıma ve durum göstergeleri ayrı SVG gruplarıdır. `useKaisEye.ts`, idle
durumda 3–6 sn rastgele aralıkla 150 ms kırpma ve 30–60 sn aralıkla 500 ms
kısa yana bakış uygular. Masaüstü mouse örnekleri rAF ile kare başına tek kez
işlenir; iris/bebek 120 ms ease geçişle en fazla 3/2 SVG birimi kayar.
Mobilde mouse takibi yoktur; dokunulan noktaya bakış 650 ms sonra merkeze döner.
Hover/tıklama gözü açıp halkayı parlatır. Thinking durumunda halka döner/nabız
çizgisi atar; unread halkayı nabızlandırır. Kapalı/hatalı/özel alan state'leri
hareketsizdir; bütçe hatası error, kullanılamayan/sona eren oturum off gösterir.
API: `size` (varsayılan 56), `state` (`idle`, `thinking`, `private`, `error`,
`off`), `lookAt?: {x, y}` (-1..1 aralığına kırpılır), `unreadBadge?: boolean`.
`lookAt` verilirse otomatik takip/yan bakış yerine kontrollü bakış kullanılır.
`private` kapalı göz/kilit, `error` ve `off` sönük/yarı kapalı göz gösterir.
56/36/24 px boyutlarında kullanılır; küçük boyutlarda ince detaylar sadeleşir.
Renkler `tokens.css` içindeki `--kais-accent`, `--kais-glow`, `--kais-ink`,
`--kais-highlight` değişkenleriyle ayarlanır. `role="img"` ve state'e göre
sayfanın dilinde TR/EN etiketi vardır; standart `aria-label` prop'u yerelleştirilmiş etiketle
override edilebilir. SVG tanımları her instance için benzersizdir.
Göz bileşeni sayfa/form değeri okumaz, storage veya ağ çağrısı yapmaz.
Yalnız pointer koordinatları, kendi SVG geometrisi ve odaktaki/masaüstünde
üzerine gelinen alanın/atasının tam `data-private="true"` işareti kullanılır.
`useKaisPrivacy.ts` tüm gözler için tek document focusin/focusout ve
pointerover/pointerout dinleyici setini paylaşır. Odak veya hover gizli
alandaysa göz kapanır; ikisi de ayrılınca 400 ms sonra açılır. Yeniden giriş
beklemeyi iptal eder. Mobilde hover kullanılmaz. Kapak kapanışı/açılışı 150 ms
transform geçişidir; halka opacity ile sönükleşir. Reduced-motion'da bunlar
da statiktir. Marker değişimi ve hedef kaldırılması gözlenir; mutation
içeriği ve hiçbir alanın değeri okunmaz.
API Key/Secret ve tüm parola alanları (göster/gizle dahil) açıkça işaretlidir.
Yeni parola alanlarına `data-private="true"` eklemek zorunludur:
`kais/private-password` ESLint kuralı literal/koşullu password tiplerini
denetler. Eski frontend uyumluluk kopyalarına yalnız bu metadata eklenmiştir;
asistan davranışı aktif kök zincirdedir. Alan envanteri aktif import zincirini,
input/textarea türünü, konumunu ve marker'ı AST ile listeler:
`node tools\input-inventory.mjs --out <csv-dosyası>`.
Tüm göz animasyonları yalnız transform/opacity kullanır. Reduced-motion veya
gizli sekmede motion sınıfları, takip ve zamanlayıcılar kaldırılır; bakış merkezlenir.
Gizli sekmede pointer/focus dinleyicileri çıkarılır; yalnız görünürlük ve hareket
tercihi gözlemcileri yeniden etkinleştirme için kalır. Unmount hepsini temizler.
Mevcut yazıyor göstergesinin animasyonu da gizli sekmede/reduced-motion'da durur.
Yeni bağımlılık yoktur. Test-only örnek `frontend/tests/fixtures/kais-eye.html`,
iki temada tüm state/boyutları gösterir; production girişine eklenmemiştir.
Sayfa tepkileri `kais:react` adlı tek yönlü, istemci içi CustomEvent ile bağlanır.
`kais-reactions.ts` yalnız `premium-open`/`navigation` için yayıncının kendi ref
geometrisinden elde edilen viewport koordinatlarını, `error`/`unread` için yalnız
türü iletir. Metin, alan değeri, DOM içeriği, kimlik veya olay hedefi taşınmaz;
LLM/backend/ağ/storage çağrısı yoktur. Premium kartı açıldığında ve ana çalışma
alanı/Master Trade/Demo sekmeleri değiştiğinde 650 ms bakış, açıkça `error`
türündeki bildirimlerde 600 ms şaşırma, yeni okunmamış yoklamada 1200 ms halka
nabzı kullanılır. Okunmamış rozetinin mevcut mesaj/okuma akışı korunur.
60 saniye hareketsizlikte 600 ms yavaş kırpma, 3 dakikada yarı kapanma olur.
Pointer/dokunma, klavye ve scroll yalnız olay oluşumu olarak sayılır; tuş veya
içerik okunmadan göz uyanır. Private/error/off durumları tepkilere üstün gelir.
Reduced-motion ve gizli sekmede tepkiler tamamen kapalıdır; bekleyen gezinme
kareleri iptal edilir, görünürlük geri geldiğinde eski tepkiler oynatılmaz.
Premium dialoguna yalnız açılıştan sonra geometrik olay yayımı eklenir;
erişim, consent, ARM ve ticaret iş mantığı değişmez. Mobil composer `visualViewport`
yükseklik/offset değişikliklerini izler; küçük görünümde öneriler gizlenerek
klavye sırasında giriş alanına yer bırakılır.

`GET /api/assistant/usage` mevcut sayaçların yanında `limits` döndürür:
`max_input_chars`, `history_messages`, `history_message_max_chars`,
`page_context_max_chars`, `secret_min_alphanumeric_chars`. Bunlar mevcut
`AssistantConfig` değerleridir; frontend iş limitlerini tekrar sabitlemez.
Geçmiş mesaj sayısı/uzunluğu gönderilmeden önce bu ayarlara göre kırpılır;
sunucu doğrulaması ayrıca devam eder. Hata yanıtlarındaki isteğe bağlı
`error_code`, özellikle bütçe kesiciyi diğer sağlayıcı hatalarından ayırır.

Sohbet metni üyeye göre adlandırılmış `sessionStorage` kaydında, sunucuda değil
tarayıcı sekmesinde tutulur. HTML çalıştırılmaz; yalnız satır sonları ve basit
kalın metin render edilir. Kontrollü taslak mevcut `assistant_api.py`
secret deseni ve sunucudan gelen minimum uzunlukla eşleşince sohbet alanı
private işaretlenir ve altında TR/EN Secret uyarısı duyurulur. Bu yeni kontrol
yalnız sunum içindir; taslağı incelemek ağ/storage yazımı yapmaz. Önceden var
olan istemci gönderim/geçmiş kontrolü ve asıl backend engellemesi değişmez.
Bilinen secret/credential desenleri istemcide de engellenir. Secret içeren geçmiş
LLM'e gönderilmez. CopyProtection yalnız sohbet kapsayıcısı/seçimi için
istisna tanır; diğer sayfalarda mevcut davranış devam eder.

`needs_confirmation` gösterildiğinde yalnız kullanıcı **Onayla** seçerse
`POST /api/assistant/analysis/confirm` üzerinden `confirm=true`, hedef ve
sunucunun verdiği onay token'ı gönderilir; chat endpoint'ine otomatik tekrar
gönderim yapılmaz. Onay token'ı storage/geçmiş/LLM'e eklenmez.
Vazgeçmek istek göndermez; reload sonrasında bekleyen onay yeniden alınır.
Ağ hatasında aynı onayın yeniden kullanılması backend idempotency davranışını
korur. Canlı emir/ARM/consent/abonelik mutasyonları bu UI'ye bağlanmaz.

UI doğrulama (frontend'in kendi Playwright kurulumunu kullanın):
```powershell
node --test tools\eslint-private-fields.test.mjs
Set-Location frontend
npx playwright test assistant-chat.spec.ts --project chromium
npx playwright test kais-eye.spec.ts --project chromium
```

Mobil sunum regresyonları `frontend/tests/mobile-layout.spec.ts` içinde
320, 390 ve 768 piksel genişlikte giriş/kayıt/parola kurtarma, hesap,
abonelik, tüm Master Trade sekmeleri, çalışma alanları ve emirsiz Original
Demo seçimini sahte API yanıtlarıyla denetler. Profil penceresi ayrıca
360/412 piksel ve yatay, kısa ekranlarda sınanır. Kontroller sayfa genişliği,
kesilen metin, üst şerit çakışması, birincil dokunma hedefleri ve form
yazı boyutunu ölçer; navigasyonun işlem isteği üretmediğini doğrular.
Reddedilen site sahibi oturumunda yalnız mevcut cookie logout isteği beklenir.
Harici HTTP ve WebSocket bağlantıları engellenir.

```powershell
$env:COIN_LOGO_OFFLINE = '1'
npm run build
npm --prefix frontend exec -- playwright test mobile-layout.spec.ts --project chromium
```

`MOBILE_LAYOUT_SCREENSHOTS=1` isteğe bağlı ekran görüntüsü üretir; çıktıları
repo dışına yönlendirmek için Playwright `--output` seçeneğini kullanın.
Bu kontroller işlem yetkilerini, feature flag varsayılanlarını veya API
sözleşmelerini değiştirmez.

Son UI sözleşme kontrolleri: `node --test tools\eslint-private-fields.test.mjs tools\kais-ui-contracts.test.mjs`.
Kök ve frontend Vite girişleri ortak bileşenler için `react`/`react-dom`
dedupe kullanır; iki ayrı node_modules kopyası soğuk başlangıçta farklı hook
dispatcher'ları oluşturamaz. Bu ayar da sözleşme testleriyle korunur.
Live fixture'ları doğrulanmış yetkili kullanıcıyı taklit eder; premium ve
consent/ARM kapıları üretimde değişmez. Scanner eşzamanlı tarama testi ilk mock
yanıtını bekletir, işlem sırasında kontrollerin kilitli ve POST sayısının bir
olduğunu, tamamlandıktan sonra yeni taramanın hâlâ mümkün olduğunu doğrular.
Aktif `main.tsx` import zincirinin UI metinlerinde eski asistan adı ve göz
bileşeni/hook'larında input değeri veya DOM içeriği erişimi AST ile denetlenir.
Playwright 10 dakikalık sanal boşta kalmayı çalıştırır; sahip olunan listener,
timer ve rAF sayılarının büyümediğini, gizli sekme/unmount temizliğini ve GC
sonrası heap artışının 2 MiB'yi aşmadığını kontrol edip ölçümleri ekler.
Bu bir gerçek-zamanlı uzun süreli heap/retainer incelemesinin yerine geçmez.
Manuel ek kontrol: Chrome DevTools Memory'de GC sonrası başlangıç snapshot'ını
alın, görünür sayfayı 10 gerçek dakika boşta bırakın, GC + ikinci snapshot alın.
Göz/hook closure'ları ve detached SVG/input node'larında birikim olup olmadığını
karşılaştırın; 5 aç/kapat veya mount/unmount turundan sonra tekrar ölçün.
Sekmeyi gizleyip Performance kaydında göz animasyonlarının durduğunu doğrulayın.

### Asistan sözleşme ve isteğe bağlı gerçek sağlayıcı testleri

`backend/tests/test_assistant_contracts.py` kullanıcı izolasyonu, ücretsiz
çıktı filtreleri, premium dahil seviye gizleme/yönlendirme, araç/import salt-okunur sınırı,
onay/cache/iade, injection, secret, limit, bayat koruma ve dil çiftlerini
test eder. Kurala uygun mock yanıtı gerçek model davranışını kanıtlamaz;
ayrı adversarial-output testleri kurala uymayan sağlayıcı yanıtlarını sınar.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
$assistantTests = (Get-ChildItem backend\tests\test_assistant_*.py).FullName
.\.venv\Scripts\python.exe -m pytest @assistantTests -q
```

Gerçek Anthropic testleri yalnız açık opt-in ile çalışır. API key'in mevcut
olması tek başına yeterli değildir. Sadece sentetik sorular gönderilir;
gerçek kullanıcı, pozisyon veya credential verisi gönderilmez. Üç canlı
senaryo sağlayıcı ücreti oluşturabilir; kayıtlar geçici test veritabanındadır,
üretim aylık bütçesi/kotasıyla ortak değildir. Gerçek çağrılar için:

```powershell
$env:ASSISTANT_LIVE_TESTS = '1'
# ANTHROPIC_API_KEY ve diğer asistan ayarlarını backend ortamında yapılandırın.
.\.venv\Scripts\python.exe -m pytest backend\tests\test_assistant_live.py -q
Remove-Item Env:\ASSISTANT_LIVE_TESTS
```

Frontend copy/plain-text kontrolleri:
```powershell
Set-Location frontend
npx playwright test assistant-chat.spec.ts --project chromium --grep "copy exception" --timeout 60000
```

### Uygulama içi durum yoklamaları

`GET /api/assistant/proactive/preferences` ayarı okur;
`POST /api/assistant/proactive/preferences` yalnız `{enabled: boolean}` kaydeder.
`POST /api/assistant/proactive/check-in` yalnız dil alır ve uygun durum özeti
varsa döndürür. Üç endpoint de mevcut üyelik doğrulaması altındadır; kimlik
yalnız authenticated kullanıcıdan alınır. Ücretsiz ve premium üyeler aynı
salt-okunur, kullanıcıya bağlı araç filtrelerini kullanır.

Görünür uygulamada kontrol yapılır; sohbet kendiliğinden açılmaz. Yeni özet
yalnız açma düğmesinde rozet ve sohbet içinde mesaj olarak görünür. Paneli
açmak rozeti temizler. Sohbetteki **Durum yoklamaları** anahtarı sunucuda
kullanıcıya bağlı saklanır; kapalıyken yeni yoklama üretilmez.

Önce açık pozisyon sayısı, toplam gerçekleşmemiş PnL ve doğrulanmış koruma
durumu mevcut araçlardan okunur. Veri eski/belirsizse açıkça belirtilir;
eksik PnL sıfır kabul edilmez. Pozisyon yoksa yapılandırılan uzun ziyaret
arası için kısa hatırlatma verilebilir. Ziyaret geçmişi bu özelliğin kendi
`last_seen_at` kaydıdır; ilk kullanımda geçmiş yoksa yokluk tahmin edilmez.
Kapalı ayarın yeniden açılması mevcut ziyareti kaydeder.

Sunucu yalnız ayar ve ziyaret/yoklama zamanlarını saklar; mesaj gövdelerini
saklamaz. PostgreSQL kullanıcı satırı kilidi veya SQLite işlem kilidiyle
son yoklamadan itibaren kayan bekleme atomik uygulanır; gece yarısı
sıfırlanmaz. Yanıt tesliminden önce hak ayrılır: ağda kaybolan yanıt aynı
bekleme süresinde yeniden üretilmez. İptalden önce teslim edilmiş mesajlar
geçmişte kalır.

Bu akış LLM çağırmaz, kredi harcamaz ve sohbet mesaj kotasını kullanmaz.
Push/e-posta, acil SL uyarısı, emir/ARM/consent/abonelik işlemi veya yatırım
önerisi içermez. Araçlar mevcut kullanıcıya ait backend snapshot'larını
okur; yoklamalar bağımsız canlı exchange izleme/koruma garantisi değildir.
Yoklama metinleri sonraki LLM sohbet geçmişine gönderilmez.

### Asistan araçları ve LLM'siz hızlı yol

`backend/app/assistant_tools.py` kimliği dışarıdan doğrulanmış üyeden alır ve
istekteki token ile tekrar eşleştirir. Araç şemalarında `user_id` yoktur; fazladan
kimlik/onay/mutasyon argümanları reddedilir. Tüm sonuçlar
`{data, fetched_at, stale}` ve API seviyesinde `sources` içerir.

| Araç | Salt-okunur kaynak / davranış |
| --- | --- |
| `get_plans` | `subscription_core.PLAN_CATALOG`, `TRIAL_DAYS`, `CANCELLATION_RULES`; iade politikası tanımlı değilse belirtilmez |
| `get_my_access` | `access_snapshot` premium yetkisi + mevcut `subscription_for_user` plan/durumu; yalnız bu üç alan |
| `get_my_credits` | Mevcut Analyst servisi; bakiye, bütçe, `analysis_cost`, kalan süre ve mevcut pencere/cache ayarları |
| `search_help` | `assistant_kb/tr.json` ve `en.json` içindeki kodla doğrulanmış yardım; kullanıcının dilinde en fazla `HELP_RESULT_LIMIT` sonuç; iş değerleri çalışma anında doldurulur |
| `get_analysis` | Kullanıcıya özel cache salt-okunur kontrolü; geçerli cache ücretsiz, cache yoksa sonuç yerine `needs_confirmation` |
| `get_my_positions` | V25'in yalnız doğrulanmış kullanıcı **ve** exchange oturumu eşleşen snapshot'ı; sadece sembol/yön/miktar/PnL |
| `get_protection_status` | Aynı sahiplik kontrolü, backend reconciliation ve exact-stop sınıflandırması; hiçbir borsa çağrısı/koruma onarımı yapılmaz |

- `POST /api/assistant/tools/{name}`: gövde yalnız araç argümanlarıdır.
  Örnekler: `get_analysis` için `{symbol, timeframe}`, `search_help` için
  `{query, language}`, `get_protection_status` için `{symbol, direction?, language?}`.
- Cache miss sonucundaki `needs_confirmation` hedef, gerçek harcama maliyeti ve
  kullanıcı/hedef/maliyet/son kullanma zamanına bağlı imzalı `confirmation_token`
  içerir. Premium'da gerçek harcama maliyeti sıfırdır; yeni analiz yine onay ister.
- `POST /api/assistant/analysis/confirm`:
  `{symbol, timeframe, confirm: true, confirmation_token}`.
  UI açık onayından sonra bu **ayrı** istek gönderilmelidir. Token süresi mevcut
  cache penceresinden alınır. Hedef/kimlik/maliyet değişimi veya süre aşımı reddedilir.
  Token'dan sunucuda üretilen idempotency anahtarı mevcut Analyst consume akışına
  verilir; aynı onay tekrar gönderilince çift harcama/iade olmaz.
  Chat gövdesindeki veya araç argümanındaki `confirm` analiz çalıştıramaz.
- Analiz özeti yön, Final Decision skoru, confidence, opportunity ve MTF uyumunu
  taşır. Mevcut producer piyasa zaman damgası vermediğinde veri yaşı `null`,
  `data_age_reason=ANALYSIS_TIMESTAMP_UNAVAILABLE` ve `stale=true` olur;
  cache açılma zamanı piyasa veri yaşıymış gibi sunulmaz.
- Ücretsiz çıktılarda `premium_access.public_projection` uygulanır.
  Entry/SL/TP ve strateji gerekçeleri hiçbir asistan özetine eklenmez.
  Premium dahil seviyeler yalnız Master Trade ekranından incelenir; sohbet
  ve onay sonrası analiz özeti bu ekrana yönlendirir.
  Korumanın güvenli doğrulama nedeni ayrı `verification_reason` alanıdır.
- Koruma/pozisyon etkin eşiği asistan ayarı ile mevcut reconciliation aralığından
  hesaplanır. Eşiğin üstündeki koruma verisinde `verified=false`, `stale=true`;
  mesajda `data_age_seconds` kadar önce alınmış olduğu belirtilir. Zaman/sahiplik
  bilinmiyorsa yaş uydurulmaz. Exact backend kanıtı olmayan veya hata/ambiguity
  içeren veri doğrulanmış koruma sayılmaz. `verified=true, protected=false`
  doğrulanmış stop bulunmaması anlamına gelir; güvenli/korumalı anlamına gelmez.
- `public_status` mevcut ARM durumunu normalize edebildiğinden asistan onu
  çağırmaz; yeni `read_owned_account_state` sadece sahiplik eşleştirip veri kopyalar.
  İşlem, ARM, consent ve otomasyon state'i değişmez.
- `assistant_fastpath.py` fiyat/trial, kendi planı ve kredi sorularına yalnız
  araç verisiyle TR/EN şablon yanıtı üretir. Bu yanıtlar LLM/dakika/gün kotasından
  düşmez; API anahtarı eksik veya LLM bütçesi dolu olsa da çalışır.
  `ASSISTANT_ENABLED=false` tüm asistan yollarını kapatır.
- Türkçe/İngilizce fiyat, API bağlantısı ve günlük kullanım soruları deterministik
  cevaplanır. Kullanım `/usage` ile aynı salt okunur snapshot'tan gelir; veri
  alınamazsa sayı uydurulmaz ve panel sayacına yönlendirilir. Sembol+analiz durumu
  sorusu mevcut `get_analysis` aracını zorunlu çağırır (varsayılan 15m); cache
  bulunamazsa yeni analiz önerilir, otomatik analiz veya kredi harcaması yapılmaz.
  Bu hızlı yollar da LLM, günlük mesaj ve dakika kotası tüketmez.
- Kais AI kimlik sorularına (`Sen kimsin?`, `Who are you?`) kısa TR/EN hızlı
  yanıt verir: yapay zeka asistanıdır; plan, kredi, API bağlantısı ve platform
  kullanımı hakkında yardım eder, işlem yapmaz. Bu kimlik yanıtı araç/model
  çağırmaz ve kredi harcamaz. Ek talimat içeren mesajlar kimlik hızlı yoluna
  alınmaz. Sistem promptu insan olduğunu iddia etmeyi ve altyapı şirketi/modeli
  uydurmayı yasaklar; altyapı sorularında bilginin paylaşılamadığını belirtir.

### Paketlenmiş TR/EN bilgi tabanı

`backend/app/assistant_kb/tr.json` ve `en.json` aynı makale kimliklerini içerir.
Her makalede başlık, içerik, arama anahtar kelimeleri ve doğrulama için
`code_sources` bulunur. API bağlantısı/Secret güvenliği, ortam farkları, üyelik,
plan/Billing, kredi, premium, aktif ekranlar, izin sayaçları, terim sözlüğü
ve risk uyarıları kapsanır. Paper kapalı dağıtım özelliği olarak açıklanır;
aktif menüye bağlı olmayan eski ticari/lisans/ajan ekranları, doğrulanmamış
iki faktörlü doğrulama veya destek hizmetleri kullanılabilir diye tanıtılmaz.

Makale metinlerinde fiyat/süre/kredi sayısı sabitlenmez. Placeholder'lar her
okumada kaynak değerlerle doldurulur; yalnız makale şablonları cache'lenir:

| Placeholder | Kaynak |
| --- | --- |
| `{ANALYST_BUDGET}`, `{ANALYSIS_COST}`, `{CREDIT_WINDOW_HOURS}`, `{ANALYST_CACHE_MINUTES}` | Mevcut Analyst servisinin `CreditConfig` değerleri |
| `{MASTER_PLAN_NAME}`, `{MASTER_MONTHLY_PRICE}`, `{TRIAL_DAYS}` | `subscription_core.PLAN_CATALOG` / `TRIAL_DAYS` |
| `{DEMO_ARM_MINUTES}` | `binance_demo.ARM_SECONDS` |
| `{LIVE_CONSENT_HOURS}` | `v25_execution.LIVE_CONSENT_SECONDS` |
| `{LIVE_ARM_HOURS}` | `v25_execution.LIVE_ARM_SECONDS` |
| `{AUTO_SESSION_MINUTES}`, `{CONSENT_GRACE_MINUTES}` | `v25_execution.LIVE_AUTO_SESSION_SECONDS` / `LIVE_CONSENT_GRACE_SECONDS` |

`assistant_help.py` başlık/anahtar kelime/gövde eşleşmelerini ağırlıklandırır;
Türkçe karakterleri normalize eder, ortak soru sözcüklerini çıkarır ve yalnız
seçilen dilde arar. Harici vektör DB veya yeni arama bağımlılığı yoktur.
LLM araç döngüsünde `search_help.language` modelin seçimi yerine sunucunun
sohbet için belirlediği dile sabitlenir. Boş/eşleşmeyen sorgu boş sonuç döner;
eksik/bozuk KB veya geçersiz placeholder açık metadata logu ve güvenli
`503` araç hatası üretir. KB dosyalarını değiştirdikten sonra backend'i
yeniden başlatmak şablon cache'ini yeniler.

### Anthropic araç döngüsü ve sistem politikası

`backend/app/assistant_llm.py` Anthropic Messages API'yi araç şemalarıyla kullanır.
`backend/app/assistant_prompt.py` kısa TR/EN yardım, doğrulanmış sayılar, bayat veri,
yatırım riski, salt-okunur yetki, premium detaylar ve prompt-injection sınırlarını
tanımlar. Sistem politikası ve son araç şeması `cache_control: ephemeral` içerir;
kullanıcıya ait dinamik mesajlara cache işareti eklenmez. Gerçek cache hit'leri
sağlayıcının model/prefix uygunluğuna bağlıdır; canlı sağlayıcı testi yapılmamıştır.

Her mesajda en fazla yapılandırılmış sayıda (üst sınır üç) üretim çağrısı yapılır.
Her tur öncesi bütçe/kota kontrolü ve Messages token-count ön kontrolü uygulanır.
Toplam girdi sınırına cache token'ları dahil edilir; çıktı tavanı kalan token
bütçesine göre küçültülür. Token-count ile gerçek usage farklı olabilir:
gerçek usage tavanı aşarsa maliyet yine kaydedilir ve yeni tur yapılmaz.
Token-count başarısızlığı üretim çağrısı/hak düşümü oluşturmaz.

Bütçe döngünün ortasında dolarsa yeni üretim/token-count çağrısı yapılmaz;
o ana kadarki güvenli araç özetleriyle yoğunluk yanıtı verilir. API hataları
genel/yerelleştirilmiş yanıt döndürür; sağlayıcı hata ayrıntıları paylaşılmaz.
Taze analiz için onay bilgisi doğrudan HTTP yanıtına çıkar; onay token'ı LLM'e
gönderilmez ve araç döngüsü analiz harcamasını onaylayamaz. Analiz/koruma
yanıtlarında backend özeti kullanılır; doğrulanmamış koruma iddiası engellenir.

`assistant_response.py` son model metnini sunucuda denetler. JSON/nested JSON,
Markdown ve düz metindeki seviye çıktıları tüm üyelerde; özel gerekçe alanları
ve analiz gerekçesi kalıpları ücretsiz üyelerde güvenli TR/EN yanıtla değiştirilir.
Sistem politikasının ayırt edici başlıkları ve talimat bölümlerinin birebir
alıntıları genel ret yanıtına çevrilir. Reddedilen içerik loglanmaz; çağrının
mevcut token/maliyet kaydı korunur. Bu kontrol bilinen metin/alan kalıpları
üzerindedir; her olası anlamsal paraphrase'i doğrulayan ayrı bir model değildir.

Doğrulama:
```powershell
$env:PYTHONPATH='backend'
.\.venv\Scripts\python.exe -m pytest backend\tests\test_assistant_config.py backend\tests\test_assistant_api.py backend\tests\test_assistant_tools.py backend\tests\test_assistant_llm.py backend\tests\test_assistant_help.py -q
.\.venv\Scripts\python.exe -m ruff check backend\app\assistant_config.py backend\app\assistant_api.py backend\app\assistant_storage.py backend\app\assistant_tools.py backend\app\assistant_fastpath.py backend\app\assistant_help.py backend\tests\test_assistant_config.py backend\tests\test_assistant_api.py backend\tests\test_assistant_tools.py
```

## V28'de yeni olanlar

- Testnet ve gerçek hesap için ayrı bağlantı kartları
- Emir oluşturmayan imzalı hesap/pozisyon modu testi
- Secret'ları PostgreSQL'de Fernet ile şifreleyen sunucu kasası
- Secret değerini hiçbir API yanıtında veya arayüzde geri göstermeyen tasarım
- Bağlantı aktifleştirme, devre dışı bırakma ve kalıcı silme kontrolleri
- Bakiye, kullanılabilir bakiye, açık PnL, pozisyon sayısı ve One-way/Hedge görünümü
- Gerçek hesapta bağlantı aktivasyonu ile emir yetkisinin kesin ayrımı
- V25'in Demo kanıtı, 24 saatlik risk izni, limit onayı ve 5 dakikalık son kilidi korunur
- Anahtar değiştirilince tüm kısa süreli işlem izinleri otomatik sıfırlanır

> Kâr garantisi yoktur. Testnet sonucu gerçek piyasayı garanti etmez. Vadeli işlemlerde
> yatırılan sermayenin tamamı kaybedilebilir.

## Güvenlik modeli

1. Yönetici giriş kodu olmadan bağlantı API'lerine erişilemez.
2. API anahtarı yalnızca HTTPS isteğiyle kendi arka ucunuza gönderilir.
3. Sunucu, anahtar çiftini önce seçilen Binance hostunda imzalı ve salt-okunur olarak test eder.
4. Başarılı çift PostgreSQL'e yalnızca şifreli veri olarak yazılır.
5. Arayüze yalnızca geri döndürülemez SHA-256 anahtar izi ve güvenli hesap özeti gelir.
6. **Bağlantıyı aktifleştir** gerçek emir açmaz.
7. Para çekme/transfer uçları yazılımda desteklenmez.

## Bir defalık yayın ayarları

Render arka uç servisinde yalnızca mevcut temel değerler gerekir:

| Değişken | Değer |
|---|---|
| `DATABASE_URL` | Render PostgreSQL bağlantısı |
| `PROTREBOT_WEB_ACCESS_TOKEN` | En az 24 karakterlik yönetici kodunuz |
| `PROTREBOT_CORS_ORIGINS` | Tam Vercel adresiniz |
| `PROTREBOT_EXECUTION_MODE` | `TESTNET_FIRST` |
| `PROTREBOT_LIVE_CHANNEL_ENABLED` | `true` |

Backend Gmail API OAuth2 secrets (Render secret store):

| Variable | Description |
|---|---|
| `GMAIL_CLIENT_ID` | Google OAuth client ID |
| `GMAIL_CLIENT_SECRET` | Google OAuth client secret |
| `GMAIL_REFRESH_TOKEN` | Gmail OAuth refresh token with `gmail.send` scope |

Binance API anahtarları bu listeye eklenmez. İsterseniz mevcut yönetici kodundan ayrı bir
kasa anahtarı için `PROTREBOT_VAULT_MASTER_KEY` kullanabilirsiniz; zorunlu değildir.

Vercel ön yüzde:

| Değişken | Değer |
|---|---|
| `VITE_API_URL` | Örneğin `https://tradebt15.onrender.com` |
| `VITE_WEB_ACCESS_REQUIRED` | `true` |

## Program içinden bağlantı sırası

1. Yönetici koduyla panele girin.
2. **Borsa Bağlantıları** sekmesini açın.
3. Önce **Binance Futures Testnet** kartını seçin.
4. API Key ve Secret Key'i girip **Bağlantıyı Test Et** düğmesine basın.
5. Saklama kutusunu işaretleyip **Şifreli Kaydet** düğmesine basın.
6. **Bağlantıyı Aktifleştir** düğmesine basın.
7. Testnet Komuta ekranında bakiye, pozisyon, Stop ve TP görünümünü doğrulayın.
8. Gerçek hesap anahtarını ancak Testnet kanıt hedefleri tamamlandıktan sonra ekleyin.

## Testnet ve gerçek hesap ayrımı

- **Testnet:** Sanal bakiye kullanır. Bağlantı aktivasyonundan sonra 10 dakikalık Demo emir
  kilidi ayrıca açılır.
- **Gerçek:** Aktivasyon yalnızca salt-okunur hesap bağlantısıdır. Gerçek emir için bütün V25
  güvenlik kapıları ve kısa süreli emir kilidi ayrıca geçmelidir.

## Yerel geliştirme

Arka uç:

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

Ön yüz:

```bash
cd ..
npm install
npm run dev
```

On yuz deposunun kok dizininden calistirilir. Yerel gelistirmede API adresi
ayarlanmamissa `/api` istekleri Vite tarafindan `http://127.0.0.1:8000`
adresine yonlendirilir; backend ve frontend birlikte calismalidir.
`VITE_API_BASE` veya `VITE_API_URL` tanimlanmissa bu adres kullanilir.

Ilk yonetici kurulumu normal giris ve musteri kayit ekranlarinda gosterilmez.
Yalnizca yerel Vite gelistirmesinde `/local-owner-setup` adresinden erisilir.
Backend uzak baglantilari ve `PROTREBOT_BOOTSTRAP_OWNER_EMAIL` ile eslesmeyen
hesaplari reddeder; yonetici atandiktan sonra kurulum tekrar acilamaz.
Mevcut bir hesabin ilk yoneticiye atanmasi dogru parolasini gerektirir.
