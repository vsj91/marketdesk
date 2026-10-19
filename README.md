# MarketDesk Local

An independent local NSE equity research and **automatic and manual paper-trading** application. This is a new Git repository, not the PlanSIP website. Its UI follows the stock-list/chart layout of your `stock_dashboard.html`, with new Trader, Investor, Paper portfolios and Data health views. No Cloudflare, remote database, CDN, telemetry or brokerage order API is used.

## Open and run

Open this entire folder in Visual Studio Code. The supplied `.vscode` settings select `.venv/bin/python`; tasks cover running, testing and importing. The debugger configuration works when Microsoft's Python/debugpy extension is installed.

On this Mac the login service runs MarketDesk automatically in the background. Open <http://127.0.0.1:8765> or double-click `start.command` to open the existing server. VS Code and a browser do not need to stay open. After source edits, deploy them with `.venv/bin/python scripts/macos_service.py install`.

For foreground development, first remove the service with `.venv/bin/python scripts/macos_service.py remove`, then run:

```sh
.venv/bin/python app.py serve --live
```

Stop a foreground server with Control-C, then run the service install command to restore automatic startup. Do not run two instances against the same database, even on different ports.

The server binds only to 127.0.0.1. With the login service installed, macOS runs it in the background for feed and refresh jobs. This is a personal, single-user local application; it is not configured for LAN/public hosting.

## Environment

Python 3.12 or newer with OpenSSL is recommended. The prepared `.venv` contains the tested dependencies. For a fresh checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
```

`requirements.txt` declares supported direct dependencies; the lock file records the tested versions. If your Python reports LibreSSL, use a modern Python installation for reliable TLS.

The supplied token is an Upstox Analytics token, which supports read-only fundamentals, candles, quotes and WebSockets. Use **Connection** in the UI to save or replace the token and start the feed. Credentials are in `.env` with owner-only permissions and are excluded from Git. They are never returned by the API. Renew an expired token in that form. Internet and valid Upstox access are needed for new data; saved research and the ledger are local.

## Data and repair

The starting inputs were copied from:

- `/Users/vinayjain/Documents/Codex/2026-10-09/root-cause-investigation-low-data-valid/outputs/plansip_fetch_data.py`
- The adjacent `RAW_CANDLES`, `RAW_FUNDAMENTALS`, `VALID_STOCK_INPUT.csv`, and `complete (1).json`.
- The adjacent `stock_dashboard.html` is the design reference. Its original file is unchanged.

The original project and exports are untouched. `marketdesk/provider.py` is the copied and corrected collector. Corrections include rejecting JSON API errors returned with HTTP 200, validating NSE common-equity instruments against the master, portable paths, atomic export writes, and preserving all merged candle windows in JSON.

Data lives in `data/marketdesk.sqlite3` (SQLite WAL), with raw imports under `data/imports/`. All runtime data, prior raw-response archives, credentials and virtual environments are excluded from Git. A code-only clone will therefore need a data copy or a fresh import.

```sh
.venv/bin/python app.py import --source /absolute/path/to/export-folder
.venv/bin/python app.py audit
```

Import validates identity, finite OHLCV values, positive prices, high/low consistency, timezones, future timestamps and conflicting duplicate sessions. It upserts rather than appends duplicates. Non-EQ instruments and symbol aliases that disagree with the instrument master are excluded. Do not mistake structural validation for independent verification of company accounts, exchange calendars or split-adjusted historical prices.

**Data health → Repair all data** starts a paced background refresh, prioritizing failed endpoints. It requests profiles, detailed annual income/balance/cash-flow statements (`fs=true`), ratios and incremental completed daily candles. Previous raw financial responses are archived before refresh. Successful candle downloads merge with existing history. Failed candle requests cannot erase saved candles. If Upstox access expires, NSE/Yahoo fundamental repair continues and the UI reports that candles need a renewed token. An interrupted full-universe refresh can resume through the same action, skipping completed symbols; the currently interrupted stock is retried. Keep this process alive. A large universe can take many hours depending on network latency and provider limits.

A reported `COMPLETE` fundamental record means the six required annual fields and sector are present in the selected qualified record. Upstox-only records also require all five endpoints; an NSE annual filing can repair missing financial fields. It does **not** establish freshness, an audit opinion or suitability for investment. Financial amounts retain source periods, statement basis and units. Borrowings are not total liabilities; omitted finance costs and lease liabilities are not zero. Exact-date, exact-basis matching prevents silently mixing standalone/consolidated statements or monetary units. Some requested fields remain unavailable even in successful detailed Upstox responses. The repair pipeline therefore tries NSE audited annual filings, validates the symbol/ISIN, annual column, audit status, statement basis and currency scale, and retains the original filing and SHA-256. A more complete matching NSE annual record can replace the selected Upstox record, with any conflicting values disclosed. Yahoo Finance is the next supplemental source. Its unspecified statement basis and different interest/debt definitions prevent silently mixing its fields into the qualified record; the UI shows those statements separately.

The app currently shows annual research and provider ratio evidence, plus source-qualified NSE data and separate Yahoo annual evidence. It does not implement PlanSIP's automated selection engine, quarterly qualification model or automatic rebalancing.

## Live prices and paper portfolios

The V3 LTPC feed uses one WebSocket connection, fallback across resolved IPv6/IPv4 addresses after connection timeouts, TLS certificate verification, the official Upstox protobuf schema, bounded reconnects, exchange-status messages and monotonic trade timestamps. There is no per-stock REST polling for live prices. All imported NSE equities are subscribed, up to the provider's 5,000-key LTPC ceiling. Account-wide subscriptions in other apps may reduce available capacity. The browser reads the latest local snapshot every five seconds.

`LIVE` requires a connected feed, NSE `NORMAL_OPEN`, and both trade and receipt timestamps no older than 30 seconds. Outside that, the UI says `LAST TRADE` or `EOD`; Sunday snapshots are never presented as live. A connected closed-market feed shows `MARKET CLOSED` and the saved snapshot count. The Connection panel shows the last market message and any connection error.

Trader and Investor display starting capital from each account’s `portfolios.initial_cash` ledger value. The current accounts were explicitly set to **₹1,00,000 each**; existing ledger balances must not be reseeded. The Paper portfolios tab has independent Enable/Pause controls for both automatic strategies. Pause a portfolio’s automation before placing a manual buy/sell. When enabled, its strategy manages all holdings in that portfolio, including existing manual holdings. Every order stays in SQLite. The engine rejects stale prices, a closed/unknown market, overspending, overselling and invalid quantities. Transactions and request IDs prevent duplicate fills from repeated submissions.

Both portfolios now use a **depth-limited immediate-or-cancel paper simulation**. Before a fill, a read-only Upstox full quote is fetched. Snapshot generation/receipt must be within 5 seconds and its last trade within 30 seconds. Buys walk asks; sells walk bids. Each level supplies at most 10% of displayed quantity, within 0.5% of the best price. Crossed, missing, invalid or >2% spread books are rejected. Execution uses the weighted price of consumed levels, so spread and depth slippage affect P&L. Partial fills cancel the remainder; they are not queued. Repeated identical depth cannot be consumed again, including after a restart. Different portfolios share this consumption budget. New quotes may represent replenished liquidity; this is an assumption, not a queue-position model.

Fills retain the book, requested/filled quantities, model version, consumed levels, slippage versus last trade, and itemized costs. Old fills retain their original prices and are labeled `legacy_ltp`. Costs use a delivery-style estimate for both swing portfolios: ₹20 brokerage/order, 0.1% STT each side, 0.015% buy stamp duty, 0.00307% NSE transaction fee, ₹10/crore SEBI, ₹0.01/crore IPFT, ₹20 DP once per stock/portfolio/day sold, and 18% GST on brokerage/exchange/IPFT/DP. Rates were checked against [Upstox's published schedule](https://upstox.com/brokerage-charges/) on 12 October 2026. **Same-day tax netting, settlement, broker rounding and account-specific pricing are not modeled**, so these are conservative delivery-style estimates rather than contract-note totals. Money is rounded to paise with decimal arithmetic at execution; stored ledger values remain SQLite REAL.

Automatic exits may partially fill or be blocked by absent depth. Remaining holdings are re-evaluated on subsequent scans, rather than guaranteed to exit. Holdings use marked last-trade values with timestamps, not guaranteed liquidation proceeds. No leverage, shorts, corporate-action processing, real-money orders or broker portfolio access are connected.

## Checks

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check web/app.js
```

Tests use temporary databases and synthetic fixtures, never your live ledger. They cover identity rejection, repeat imports, malformed candles, extraction units/periods, API errors, cash accounting, overselling, stale quotes and duplicate submissions.

## Project map

- `app.py`: CLI and startup
- `marketdesk/provider.py`: corrected original Upstox collector
- `marketdesk/importer.py`, `analytics.py`: validation, financial extraction and daily analysis
- `marketdesk/storage.py`: local database
- `marketdesk/feed.py`: read-only live market data
- `marketdesk/refresh.py`: paced data repair and resumable progress
- `marketdesk/fallbacks.py`: NSE annual filing validation and Yahoo supplemental statements
- `marketdesk/paper.py`: simulation accounting and order ledger
- `marketdesk/autotrading.py`: automatic signals, position limits, daily risk limits and activity history
- `marketdesk/server.py`: loopback API and static-file server
- `web/`: entirely local HTML, CSS and JavaScript; no CDN dependencies
- `tests/`: isolated regression tests

## Provider references

- [Market Data Feed V3](https://upstox.com/developer/api-documentation/v3/get-market-data-feed/)
- [Income statement](https://upstox.com/developer/api-documentation/get-income-statement/)
- [Balance sheet](https://upstox.com/developer/api-documentation/get-balance-sheet/)
- [Cash flow](https://upstox.com/developer/api-documentation/get-cash-flow/)
- [NSE integrated filings](https://www.nseindia.com/companies-listing/corporate-integrated-filing)
- [Yahoo client financial-statement documentation](https://ranaroussi.github.io/yfinance/reference/yfinance.financials.html)
- [API rate limits](https://upstox.com/developer/api-documentation/rate-limiting/)

Local hosting removes Cloudflare application quotas. Upstox limits and data coverage still apply.

Detailed initial findings and a verified NSE repair example are in [DATA_AUDIT.md](DATA_AUDIT.md).

## Automatic paper strategies

The user requested automation for **both** portfolios. These are new experimental strategies with explicit rules; no performance claim or backtest is implied. Settings persist in SQLite and resume when the server restarts. Pause stops subsequent automatic orders without liquidating holdings. Existing paper orders remain untouched.

Paper Investor now also has a **3% portfolio risk cap for new buys** (`portfolio_risk_cap_pct`). Combined risk uses each remaining fee-inclusive position cost, its unchanged 10% stop, estimated delivery selling fees and a 0.25%-per-side slippage allowance. Proposed buys are sized downward and rechecked at actual fill against post-fill equity. Being above the cap blocks new buys without liquidating existing holdings or changing their stop/trend exits. The 2% daily buy halt, 10% position budget, fee-efficiency check and all other rules remain.

Investor supports a saved `buy_target_inr` for future automatic purchases, currently ₹3,500 including entry fees. Sizing uses the smaller of this target, available cash, and the existing 10% position maximum, then applies the existing portfolio risk sizing. The actual fill rechecks the target using current settings. Whole shares, execution depth and risk allowance can reduce the purchase; the fee check can reject a small fill. Existing holdings are never resized by this setting, and stops, trend exits, risk caps, daily limits and Trader sizing are unchanged. The dashboard's Investor entry rules display the saved target. Omitting the setting preserves the original sizing behavior.

Trader's optional `live_breakout: true` replaces its completed-close breakout entry with a fresh live price above the highest high of the latest 20 completed sessions, including yesterday. The unfinished current-session candle is excluded. Both the fresh trade price and actual depth fill must remain above that level at execution. This is an above-level check, not a requirement to observe a tick crossing from below. The existing completed-session trend, non-overlapping volume windows, ATR, turnover, momentum ranking, 15% price-gap guard, session checks and quality filters remain. Existing holdings and all stop, risk, capacity, fee, no-averaging and no-reentry rules are unchanged. Live entries can increase or decrease the candidate count. Omitting or disabling this option retains the original completed-close entry behavior.

Both paper portfolios now cap estimated round-trip buy/sell fees at **0.5% of the actual filled buy value** (`max_round_trip_fee_pct`). The estimate includes delivery DP charges at an unchanged exit price; spread, future slippage and partial-exit costs are excluded. With the current fee model, this requires roughly ₹26,000 per fill. The check runs inside the fill transaction after depth matching, so tiny partial fills are rejected without consuming cash, depth, holding slots or daily buys. It never enlarges an order or relaxes risk caps. Existing holdings, sell orders and protective stops are exempt.

The engine checks every 15 seconds, uses fresh quotes, and executes only during the provider's NSE `NORMAL_OPEN` session. If a held price is stale, each live portfolio scan makes one bounded, read-only Upstox full-quote batch request for its affected holdings. Recovery requires matching instrument identity, a provider snapshot no more than five seconds old, and an actual last trade no more than 30 seconds old. Older trades are never re-dated, and newer WebSocket quotes are never overwritten. The portfolio status names any remaining stale holdings. Freshness is evaluated at the current scan/execution time after loading data, not at an earlier scan-start time. Trader’s saved capacity is now **10 holdings and 5 automatic new buys per session**, enforced in both scanning and final fill checks. The 0.5% entry-risk cap, 10% position budget, and stops remain unchanged. Trader’s **3% portfolio risk cap now includes buy/sell fees and the existing 0.25%-per-side slippage reserve** for held positions and each proposed buy. Held-position risk uses remaining fee-inclusive cost minus stop proceeds after estimated delivery selling fees (floored at zero), plus the reserve on recorded entry value. Legacy buy fees are not counted twice; partial exits reduce the remaining cost and quantity. New buys are sized and rechecked at actual fill against this combined risk and post-fill equity. Stops, exits and Investor rules are unchanged. Investor’s saved capacity is also **10 holdings and 5 automatic new buys per session**. Its 10% position budget, 2% daily loss halt, 10% stop, 200-day trend exit, fundamental filters are unchanged. Both portfolios now have `allow_stale_held_valuation` enabled: an existing holding’s last valid quote from the current session can value that holding for equity/risk calculations even after 30 seconds. Investor’s 2% daily loss halt uses these marks and remains latched once triggered. Both portfolios block valuation on missing, invalid, future-dated or prior-session quotes. New buys and stop exits retain their fresh-price/depth requirements, and all position, daily-buy, risk and heat limits remain. The dashboard identifies marks used only for valuation; automatic buy reasons record them when used. Both running paper portfolios have full-session buying enabled: **09:15–15:30 IST**, only while the connected provider reports `NORMAL_OPEN`. The saved `full_session_buying` option expands the entry window; all other stock filters, daily buy limits, position/risk limits, and stops still apply. Trader’s Nifty 500 trend filter remains disabled as separately configured. Trader continues to use completed daily breakout signals and fresh execution depth; signals are not filled retrospectively. Without this option, the baseline window remains 09:20–15:00 IST and Claude’s original window remains 09:15–09:20 IST. Exits remain active throughout the normal session. There is no forced end-of-day sale: Trader is a swing strategy and both portfolios can hold overnight. Stops and targets are checked on observations; gaps, lost connectivity, sleep or a closed market can delay exits and cause threshold overshoot.

| Rule | Trader | Investor |
|---|---|---|
| Entry | Price above prior 20-session high; DMA50 > DMA200; historical 10/30-session volume ratio ≥ 1.2 | Complete annual inputs, positive PBT/equity and revenue/profit YoY, interest cover ≥ 3 or explicitly zero finance cost, net debt/equity ≤ 0.5, price > DMA200 |
| Exit | 3% below average cost, 6% above average cost, or price < DMA50 | 10% below average cost or price < DMA200 |
| Maximum holdings | 5 | 8 |
| Per-position budget | 10% of starting capital, including entry fees | Same |
| New positions per session | 3 | 2 |
| Daily buy halt | 2% fall from the first fully marked equity observed that session | Same |

Entry requires 200 completed daily candles from the latest stored session (no more than seven calendar days old), average 20-session turnover of at least ₹1 crore, price at least ₹20, and no candle/identity issues. Quotes more than 15% from the last daily close are excluded for review. After the engine has observed a market session, subsequent-session entries require candles at least as recent as that prior session. Price, budget, pause state and limits are checked again inside the same database transaction as the fill.

Investor also requires matching financial periods, statement basis and units; annual periods no older than 550 days; and no recorded source conflicts. Missing inputs exclude entry. Missing fundamentals alone do not force liquidation. Candidates rank by breakout strength for Trader and capped revenue/profit growth for Investor. No averaging into existing positions and no same-session re-entry are allowed. A daily loss halt survives pause/resume and restart until the next session; exits continue. If any held stock lacks a fresh price, new buys wait.

After 06:00 IST on a later day, the engine requests a completed-daily-candle refresh for its last observed market session, when the existing repair job is free. This avoids importing an unfinished daily bar. Keep the server running for data maintenance. Failed refreshes remain visible in Data health and can be resumed there; stocks with old history do not qualify for new automatic entries.

Every automatic fill records its source and reason in the trade ledger. The activity panel records enable/pause actions, fills, blocked execution and risk halts. The engine pauses both enabled portfolios after an unexpected internal error. All orders stay in this app's local paper ledger; no broker order API is connected.

## Data eligibility checks (12 October 2026)

A local audit runs at startup, every five minutes while the server runs, and after refresh completion. Data health shows separate Trader/Investor pass counts, exclusion reasons, paper validation status and live execution availability. Research can filter to stocks passing data checks and shows per-stock reasons.

Both manual and automatic **new buys** require an audit no older than ten minutes, 200+ completed daily candles, recent history (at most seven calendar days), valid OHLCV, no source identity/candle issue, no missing sessions against the majority of local histories, and no zero-volume bars in the last 20 sessions. Changes are rechecked in the fill transaction. A price discontinuity exceeding 25% is flagged as a possible corporate action or bad history; it is not automatically adjusted. A live previous-close mismatch over 25% blocks both sides pending position review.

Investor buys additionally require complete finite annual inputs, no source conflicts, annual period age ≤550 days, matching annual growth periods/basis/units, and financial evidence observed within 30 days. Underlying endpoint/NSE filing timestamps are checked, so reimporting old data does not make the evidence fresh. Missing financial data blocks new buys without forcing liquidation.

The calendar is a local consensus, not an independently verified exchange calendar. Corporate actions, dividends, delistings and share quantities are not yet reconciled with official event records. The UI deliberately says **pass current data checks**, not investment-ready or live-ready. Backtests and forward-market validation remain outstanding.

The queued candle repair targets the 380 stocks failing the initial candle audit, after the current fundamentals repair. It requests up to 550 days rather than only a seven-day overlap for histories with issues. Successful source data is merged without interpolation; a completed attempt does not guarantee a repaired stock (recent listings and suspended/illiquid stocks may remain excluded). The queue and interrupted fundamentals repair survive app restarts. Keep the Mac awake and the background service running.

Depth API: [Upstox Full Market Quotes V3](https://upstox.com/developer/api-documentation/get-full-market-quote-v3/). Only this read endpoint is called for execution evidence; every order remains inside SQLite.

## Start automatically at Mac login

A per-user macOS LaunchAgent starts this project's deployed server when Vinay logs in. It runs independently of VS Code and the browser; closing either does not stop it. macOS restarts the server if its process exits, with a 30-second restart throttle. The server still binds only to `127.0.0.1:8765`, uses the same local database, and contains no real-order execution. Trader and Investor keep their saved enabled/paused settings; installing the service does not override later pauses.

The Mac must be logged in, awake and online for live paper execution. The server prevents idle system sleep Monday–Friday, 09:00–15:40 IST, including weekday exchange holidays. Outside this window it releases its own sleep assertion; it does not change display sleep or global power settings. A bounded native `caffeinate -i` process is tied to the server PID and ends if the server exits. Status is available as `sleep_guard` in `/api/health`. This cannot wake an already sleeping Mac, override closing the lid or forced sleep, trade while powered off, or renew an expired data token. Keep the lid open and power connected, and have the Mac awake/logged in by 09:00. No scheduled wake or administrator power-setting change is installed. After startup, open <http://127.0.0.1:8765/> or double-click `start.command`. The launcher reuses the background server. The background runtime lives in `~/Library/Application Support/MarketDeskLocal`, outside the protected Documents folder. The project’s `data` and `.env` are links to that same runtime data and credential file; there is no second ledger. Source code remains in this VS Code project. After editing Python or web files, run the install command below to deploy the changes and restart the background service. The runtime has its own copy of the Python environment; installation reuses it, so dependency changes must also be applied to that environment. Do not move the project or its Python runtime without reinstalling the service.

From this project's terminal:

```sh
.venv/bin/python scripts/macos_service.py status
.venv/bin/python scripts/macos_service.py restart
.venv/bin/python scripts/macos_service.py remove
.venv/bin/python scripts/macos_service.py install
```

`remove` unloads the service and moves its plist into `data/backups`; it preserves the database and settings. To pause trading while keeping research/data refresh available, use the two Pause buttons in Paper portfolios instead. The installed plist is `~/Library/LaunchAgents/com.vinayjain.marketdesk-local.plist`. Startup diagnostics are in `data/logs/server.out.log` and `data/logs/server.err.log`; these local logs are ignored by Git and restricted to your user. No token is stored in the plist.

## Isolated historical paper testcase

**Paper portfolios → View the latest isolated candle replay** opens the last generated report at `/replay.html`. This is a separate offline simulation; it opens the input database read-only, never changes either running portfolio and never requests broker orders. Rerun from this source project:

```sh
.venv/bin/python app.py replay --sessions 5 --end 2026-10-09 --slippage-bps 10
.venv/bin/python scripts/macos_service.py install
```

Omit `--end` for the latest five completed locally observed sessions. The command writes JSON and HTML to `data/replays/last-week.*` and a local UI copy to `web/replay.html`; deployment updates the running UI. `--output` changes the JSON/HTML archive destination. Each report retains an input hash, coverage counts, simulated fills, cancelled quantities, daily equity and explicit limitations. The generated report is a snapshot, not a live portfolio.

This **daily-open proxy** reuses the current strategy signal functions, cash/position/daily-entry limits and delivery-cost function. Indicators and the assumed 1% daily liquidity cap use only prior completed candles. The daily open substitutes for the first 09:20 observation, with adverse slippage (default 10 bps each side); exits are checked only once per session. Same-session highs/lows/volume never choose fills, and the close only marks holdings after decisions. It cannot reconstruct actual 09:20 prices, intraday stop/target order, continuous daily-risk halts or historical depth. It does not claim the live strategy would have earned the same result.

Investor requires matching historical financial observations and underlying evidence available by each simulated session; later snapshots are rejected. The 5–9 October run has **no eligible historical Investor snapshots**, so that strategy is untestable for this week. Current repaired fundamentals must not be backdated. Trader results still use today's instrument universe and saved/revised price history; survivorship, corporate actions and dividends remain limitations. A five-session testcase is not performance validation or readiness for real money.

### Investor testcase with current fundamentals

The user-requested hypothetical mode uses current qualified fundamentals alongside older candles:

```sh
.venv/bin/python app.py replay --sessions 5 --end 2026-10-09 --fundamentals-mode current --output data/replays/current-fundamentals-2026-10-09.json
```

The snapshot is frozen once for the run. Financial freshness is checked at snapshot capture time; missing inputs, conflicts, period/basis/unit mismatch and the strategy’s financial filters still block entry. Historical candle cutoffs are unchanged. Investor fills retain the selected mode and financial observation timestamp. This deliberately uses financial information collected after the candle dates and is labeled **hypothetical · current fundamentals** in the local UI; it is not historical performance. The historical mode remains the default. The earlier strict report remains in `data/replays/last-week.json`. Both modes leave the running paper ledger untouched.


### One-month paper testcase

To replay a calendar month while preserving the earlier candles needed for indicators:

```sh
.venv/bin/python app.py replay --start 2026-09-12 --end 2026-10-11 --fundamentals-mode current --output data/replays/month-current-fundamentals-2026-10-11.json
```

`--start` and `--sessions` are alternative ways to select the period. The range is inclusive and must contain 1–60 completed locally observed sessions. The 12 September–11 October window contains 18 saved sessions, 15 September–9 October; the report shows both the requested window and actual candle dates. Each run starts both isolated portfolios at ₹10 lakh. The same daily-open model, current-fundamentals qualification, estimated fees, adverse slippage and position limits apply. Weekly reports remain archived under `data/replays/`; `/replay.html` shows the latest generated result.


### Trader loss review and research-only entry cap

`/trader-review.html` contains the September–October monthly loss attribution and comparisons with an earlier August–September window. The original monthly replay is preserved. All experiments open the input database read-only and write separate JSON files under `data/replays/trader-review/`; neither portfolio configuration nor the running strategy rules are modified.

`marketdesk.trader_review.run_review` builds this fixed-period research report. `replay(..., trader_variant='no_chase_v1')` tests one fixed hypothesis: entry no more than 2% above the prior 20-session high and 3% above the prior close, checked again at modeled fill price. The default remains `baseline`. This option is only in the offline replay and is not called by `AutoTrading`. Candidate results retain the daily-open model limitations. Earlier-period comparison is a cross-check, not evidence of forward profitability.

Loss diagnostics show entry gaps, prior breakout extension, prior ATR14 as a percentage of price, and the first daily range touching a modeled stop or target. A range touch cannot prove an executable fill, nor determine stop/target ordering when both occur within one bar. Before changing intraday exit logic, obtain timestamped intraday data and validate order sequencing, stale-price behavior, gaps and partial fills. Do not substitute guaranteed stop prices for missing observations.


The fixed entry cap was rejected for promotion: reviewed-month P&L worsened from −₹34,923.76 to −₹41,319.46, and the earlier-period cross-check worsened from −₹6,409.67 to −₹26,241.17. Earlier-period daily-close drawdown improved, but net P&L did not. The baseline monthly result was reproduced exactly. The running paper strategies retain their existing rules.

### One-minute Trader replay (paper only)

`/intraday-replay.html` shows a separate chronological Upstox replay. Download a completed range of at most 31 inclusive days, freeze prior-day inputs and replay:

```sh
.venv/bin/python -m marketdesk.intraday_replay --fetch --start 2026-09-12 --end 2026-10-09
```

Rerun the frozen inputs without any network request or token requirement:

```sh
.venv/bin/python -m marketdesk.intraday_replay
```

Use `--directory data/replays/another-period` to preserve separate periods. Files are local and Git-ignored: `plan.json`, raw compressed Upstox responses, a minute SQLite cache, download progress, and `result.json`/`result.html`. API requests use the saved data token, official read-only historical-candle V3 endpoint, one request per second, bounded retries and validated timestamps/OHLCV. No orders are sent to Upstox. The source paper database is read-only; neither live portfolio is changed.

Candidates are selected from prior-day inputs across the whole local universe, including candidates not chosen by the daily-open replay. A completed minute generates a signal; execution uses the next consecutive minute's open with 10 bps adverse slippage, estimated delivery charges, revalidation and IOC partial fills. Capacity is capped by 10% of the preceding minute's volume and 1% of prior 20-day average daily volume, shared across both sides. Missing and zero-trade minutes cannot fill orders. No same-day re-entry, 09:20–14:59 entries, session-long exit checks and a persistent daily buy halt are modeled. Positions may remain overnight. The final minute cannot create an after-hours fill.

The main scenario checks minute closes; a separate range-touch sensitivity queues an exit after a completed minute touches a stop/target. Both touched in one minute is explicitly ambiguous. Neither scenario guarantees threshold-price execution. Minute bars cannot reproduce the live 15-second checks or actual historical depth. Full-session coverage and daily open/high/low reconciliation are reported, with gaps making results provisional. The final minute close is used for marking; the exchange's official daily closing price may differ. Current-universe bias, historical revisions, corporate actions, settlement and exact fill uncertainty remain limitations. This updates the replay, not the running strategy's rules. Deploy static web files to the runtime (or use the service installer) to show the latest report in the running local UI.

### Trader revisions: same-candle before/after

`/trader-comparison.html` audits all 41 original losing positions and shows every tested candidate, including failures. Rerun locally, without network calls:

```sh
.venv/bin/python -m marketdesk.trader_experiment
```

The original frozen plan, minute cache and result are hashed and preserved; both original exit-model baselines must reproduce exactly. Protocols, complete ledgers, metrics and reports are saved under `data/replays/intraday/confirmed-breakout-v1/`. `comparison.json` is the first confirmation comparison; `comparison-with-breadth.json` additionally includes the risk-abstention test. The command generates `web/trader-comparison.html`; deploy it and the navigation page to the running runtime to update the UI.

`confirmed_breakout_v1` waits for 15 traded opening minutes and three consecutive closes above the opening high, prior daily breakout level and cumulative minute typical-price VWAP estimate, then applies a three-session cooldown after a losing sell fill. Missing minutes reset confirmation. Fill-open confirmation is checked again. This failed: minute-close loss worsened from ₹59,240.23 to ₹63,347.95; range-touch loss worsened from ₹52,097.91 to ₹67,814.03. The candidate is rejected for promotion.

After that failure, a second hypothesis inspected prior-day breadth. `breadth_guard_v1` permits baseline entries only if more than half of at least 500 valid stocks are above each of their 50- and 200-day averages. It leaves exits active. This blocked all 18 sessions, producing ₹0 P&L with zero trades and zero exposure. This is loss avoidance by remaining in cash, **not evidence of a profitable trading strategy**. Neither variant changes the running Trader or Investor rules/settings. Both are explicitly offline paper research.

All selection was informed by the same period; there is no untouched holdout. These are exploratory comparisons, not validated improvements. Fixed position sizes, stop/target rules, fill timing, slippage, liquidity caps and fees remain the same. Reported win/loss counts group partial exits by entry position; exposure and turnover changes are shown rather than hidden.

### Targeted fundamentals repair

`/fundamental-repair.html` shows the processed count, newly complete stocks, recovered fields, the remaining gaps and field-level source checks. The dashboard links to it whenever a run exists. Start a targeted run with the local CSRF-protected `/api/refresh` endpoint using `{"mode":"fundamental_repair"}`. It selects only incomplete records and shares the refresh lock, so a broad import cannot overwrite it concurrently. An interrupted/failed targeted run resumes its saved original universe and completed results when started again.

The repair reparses saved NSE filings and checks public NSE annual filings for both consolidated and standalone results. A replacement is a whole, verified annual statement with fewer missing inputs, at the same or a newer period; financial fields are never assembled across statement bases. It recognizes explicit lease captions and NBFC borrowing components, verifies audited annual columns, and requires all components before summing. Blanks, absent leases, total liabilities and interest expense are not silently converted to zero, borrowings or finance costs. Yahoo identity-checked sector data can fill a missing sector; numeric statements with unspecified basis remain supplemental. Source conflicts continue to block Investor buys. Complete annual fields do not guarantee sufficient comparable growth, recent evidence, or strategy eligibility.

The before snapshot, report checkpoints, source responses and SHA-256-addressed NSE HTML are saved under Git-ignored `data/fundamental-repair/`, `data/fallbacks/` and `data/filings/`. Eight local I/O workers share a public NSE request limiter of at most one start every 0.4 seconds; access/rate-limit responses trigger a cooldown. A targeted run writes fundamentals and quality reports only. Existing paper ledgers, candles and automation settings are preserved. After source changes, deploy with `.venv/bin/python scripts/macos_service.py install`; a restart reconnects the read-only quote feed and retains paper settings.


Company annual-report PDFs are an additional balance-input source. The targeted repair checks the NSE annual-report index, then reviewed company investor pages where configured and BSE when needed. BSE security codes must match the exact ISIN in BSE’s active equity security list. Company-site PDFs must contain the expected ISIN. Reports must match the exact fiscal year, statement basis, INR currency and unit, and at least two existing balance-sheet values. Missing leases, cash or borrowings can then be filled without overwriting existing values. Finance costs additionally require an explicitly labelled annual income statement in the same report, with the same period and basis and a matching profit-before-tax anchor; interest expense alone is not substituted. The income page and its own unit conversion are retained. Two-page spreads are split before parsing. A basis omitted from a balance-sheet heading can be established only by an explicit nearby auditor section; its page is saved with the evidence. PDF whitespace, lacs/lakhs spelling and alphanumeric note references are normalized. Unsupported or unspecified statement bases remain unresolved. Both current and non-current leases must be reported (or one explicitly identified total in a non-classified balance sheet); a dash is not zero. Existing source conflicts and growth checks remain enforced.

The report shows NSE/BSE/company source checks, accepted report providers, per-page parser diagnostics, source pages and final Trader/Investor data eligibility. BSE/company requests have a separate host-aware 0.6-second limiter and access/rate-limit cooldown. A BSE report that is byte-identical to an already parsed NSE report is not parsed twice. PDF parsing runs in isolated subprocesses (maximum six). Downloads are limited to 40 MB, stopped below 1 GB free space, and discarded after extraction. Only the relevant PDF pages, full-document hash, excerpt hash, original page numbers and source URL are retained in `data/annual-reports`. This is bounded parser coverage; unsupported layouts, absent explicit basis, scanned statements, unavailable reports and unmatched annual values remain unresolved rather than guessed. Install updated `requirements.txt` into both the development and existing login-service virtual environments before deployment.

A targeted report repair reuses NSE/Yahoo results checked within 24 hours by the current filing-parser revision, preserving their original timestamps and recorded failures. Revised PDF parsing has its own cache revision, so changes to layout handling still rerun. Later raw imports retain an accepted report supplement only when identity, period, basis, at least two baseline anchors and the saved evidence hash still match; they never refresh the age of that evidence.

Accepted PDF excerpts use both the full-report hash and excerpt hash in their filenames, so a later extraction with extra evidence pages cannot overwrite an earlier evidence file. Annual income parsing has a separate cache revision; adding income coverage does not force another download for stocks whose finance costs are already present.

`marketdesk.repair_summary.combine_reports` can combine two completed passes against the first pass’s original missing-field baseline. It validates the stock sets and final completeness totals, preserves accepted evidence when a follow-up cannot recover another field, and counts recovered fields and stocks once. Individual pass checkpoints are retained. This reporting function never changes financial inputs or paper ledgers.

Annual-report parsing also supports numeric and non-March fiscal dates, adjacent continued balance-sheet pages, Roman row/note labels and ITFRupee glyphs. An explicitly labelled lease carrying-value reconciliation can supply its reported closing balance after the balance sheet matches at least two independent fields; blank components and undiscounted maturity totals are never used as zero or as carrying values. Accepted reports survive unsuccessful follow-up checks, and distinct verified excerpts are retained across raw reimports. The results page lists remaining missing inputs.

The primary checkout is `/Users/vinayjain/Documents/MarketDesk`. Its `data` and `.env` links use the existing Application Support runtime, keeping one database and one paper ledger accessible to the macOS login service. Source and tests remain in the primary checkout.

To shorten repeated repair passes, a previously downloaded NSE/BSE archive is skipped only when its SHA-256 was recorded within 24 hours and an identical fresh copy has already been parsed in the current check. Mutable company-site URLs are still fetched. Six bounded PDF workers are used; source request pacing is unchanged.

Investor growth recovery (`scripts/repair_investor_growth.py --apply`) targets stocks blocked only by missing comparable annual growth. It uses exact prior-year, matching-basis NSE filings from the saved index, including older explicitly rupee-denominated templates. Prior-year inputs are not subjected to the latest-statement 550-day limit. Both annual income statements must independently prove identity, audited full-year dates, basis and units; the current statement must match PBT and finance-cost anchors. Revenue and profit growth require positive prior denominators. Original HTML hashes and observation times are retained. No current annual values, source conflicts, trading limits or price data are overwritten. Raw reimports retain growth only while the selected NSE context and evidence hashes still match.

Financial source conflict verification (`scripts/repair_source_conflicts.py --apply`) targets existing conflicts, prioritizing stocks with no other Investor blocker. It locates the newest indexed NSE filing for the exact symbol, annual period and statement basis, including legacy records missing a top-level filing URL. It downloads and reparses the audited filing, verifies identity and units, and requires every shared financial value to match the selected annual data. Only matching Upstox-versus-NSE discrepancies are resolved in favor of the audited official value. Financial values, growth, and trading rules are unchanged. Conflicts between primary reports remain blocked. Original discrepancies and full hashed HTML evidence remain in `source_conflict_resolutions`; a raw reimport requires the exact same discrepancy, annual context, intact evidence and fresh verification to retain a resolution. Changes require another verification. Results, eligibility changes and per-stock evidence are shown at `/source-conflicts.html`; `scripts/verify_source_conflicts.py` audits the finished run against its baseline.


### Nifty 500 buy universe

Both running paper portfolios use `buy_universe: "nifty500"` for new purchases, including manual purchases while automation is paused. All instruments, candles and financial records remain in the database. Existing holdings retain their normal exits, stops, valuation and portfolio-limit treatment even outside the index; index removal never forces a sale.

`marketdesk.buy_universe` downloads the official NSE Indices constituent CSV in a background worker, validates its schema, unique symbols/ISINs and 500–510 real securities (temporary corporate-action additions are allowed; explicitly marked dummy entries with dummy identifiers are excluded), and stores its source URL, hash and retrieval time in SQLite. A stock must match both symbol and ISIN. Membership must have been verified on the current IST date; unavailable, expired or invalid membership blocks new buys without blocking exits. Refresh retries at most every 15 minutes and never waits inside an order transaction or the exit scan. The last verified list survives a failed refresh but cannot authorize buys on a later date.

The universe is applied before candidate ranking and checked again inside the fill transaction after fetching depth. All existing signal, financial, freshness, fee, position and risk checks still apply. The dashboard shows the buy universe and the number passing data checks within it, separately from the full-database readiness counts. This constituent filter is independent of Claude's `nifty500_filter_enabled` index-trend option.

## Signal baskets: AI News Momentum, AI News Catalyst, EMA + Price Action

Three more paper baskets in **Paper baskets**, each with its own ₹1,00,000 ledger (`portfolios`), positions, orders, stops, risk limits and P&L. Code: `marketdesk/signal_baskets/`; tables `sb_*` are additive (no existing table or ledger is changed). All orders go through `paper.trade` with the same depth-limited IOC simulation, data checks and Nifty 500 gate as the other baskets; nothing is sent to a broker.

- **AI News Momentum Trader** (1–20 sessions): NSE corporate announcements (`/api/corporate-announcements`, public but undocumented) are polled every 5 minutes, deduplicated, identity-checked against symbol + ISIN, and scored by deterministic rules (materiality vs annual revenue, sentiment, novelty, credibility, freshness, pre-news run-up, post-news move). A positive verified filing is confirmed on its completed news-session candle (relative volume, reaction ≤ 10%, close > EMA20 > EMA50, ATR band, relative strength vs Nifty 500, regime, RR ≥ 2 to resistance) and bought the next session. Exits: 2-ATR stop, 4-ATR/resistance target, 2.5-ATR chandelier trail, close below EMA20, negative verified filing, 20 sessions, no +1R within 5 sessions.
- **AI News Catalyst Investor** (3–24 months): verified catalysts plus an explainable Investor Opportunity Score (catalyst 25, earnings 20, quality 20, valuation 15, management 10, risk 10 — initial hypotheses). Missing fields are flagged; critical gaps block buys. Weekly thesis review; negative sentiment alone never sells.
- **EMA + Price Action Trader** (intraday): completed Upstox 5-minute candles for a liquidity-ranked universe (default 50); EMA9/21 crossover above EMA200, RSI 55–70, ATR band, confirmed swing structure, stop-limit entry, hybrid stop, ATR/resistance target, breakeven → 50% partial → EMA20 trail, square-off at 15:12 IST. Intraday charge estimate. Shorts are research-only (the ledger is long-only).

Parameters and (before the first trade) starting capital are editable in each basket's **Parameters** section; values are range-checked. With the default hybrid stop and a 3-ATR target, risk-reward cannot exceed 2.0, and tick rounding pulls it just below 2.0, so the EMA basket's default `min_rr` is 1.8 (configurable 1.5–2.0) rather than the 2.0 in the original specification. Backtests: `.venv/bin/python scripts/backtest_signal_baskets.py` (report in `data/backtests/signal-baskets-latest.json`); the catalyst basket is not backtested because its horizon exceeds the news history.
