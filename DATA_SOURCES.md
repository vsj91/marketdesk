# Nifty 500 data and repair policy

The active research universe is the official NSE Indices Nifty 500 list, matched
by symbol and ISIN to the Upstox instrument master. Saved excluded records are
retained but excluded from research, routine data jobs and health totals. Membership
is verified daily; new buys require today's verified list. Newly listed members
start with missing data, never invented history. Official BE/RR constituents are
included for coverage; normal strategy eligibility still applies.

The local login service runs independently of Codex and of basket enablement:

- Live prices: Upstox WebSocket, with bounded reconnects. Read-only Upstox REST
  quotes recover missed held quotes. An old exchange trade is never made fresh by
  a new HTTP response. No reliable independent real-time execution feed is
  configured; Yahoo/history cannot replace a fresh executable bid/ask book.
- Completed daily candles: refresh daily after 06:00 IST, then audit. Upstox is
  primary. On failure, try Yahoo Finance through yfinance with unadjusted OHLCV,
  NSE/INR metadata checks and comparison against saved primary closes. Do not
  overwrite primary candles. Unconfirmed alternate history blocks new buys until
  reconciled. No interpolation, synthetic history or silent split adjustment.
- Fundamentals: refresh missing or seven-day-old observations, then repair gaps
  daily. Try Upstox, audited NSE filings, company annual reports from NSE archives,
  known company sites and BSE. Yahoo is supplemental where statement basis cannot
  be verified. Comparable growth requires matching annual periods, units and basis.
- Financial repairs recover audited comparative growth and reverify supported
  source conflicts. Unresolved conflicts and missing values remain blocked.
- Annual-report repairs recognize explicit lease carrying-value rollforwards and
  capital-management totals. Capital totals require three matching balance
  anchors and reconciled net-debt arithmetic; no dash is interpreted as zero.
- Large daily moves can be reconciled against NSE's historical OHLCV, previous
  close, corporate-action records and verified official constituent ISINs.
  Evidence is rechecked against current candles at audit and execution time and
  expires after 24 hours. A verified market move needs no price adjustment;
  demergers and other corporate actions remain blocked pending adjusted history.
- Each job finishes with an audit. Interrupted jobs resume from saved checkpoints;
  failed jobs back off. Provider helpers retain rate limits, timeouts and source
  caches. A five-minute trading audit independently checks readiness.

`maintenance.py` schedules jobs; `refresh.py` and `fundamental_repair.py` run them;
`automatic_repair.py` handles growth/conflict alternatives; `candle_fallback.py`
validates alternate history. Every repair keeps provenance and actual evidence
times. A repair attempt does not mean a stock became eligible. New listings with
fewer than 200 completed candles cannot legitimately pass the current history rule.

Annual-report extraction also supports explicitly headed Note/Page reference
columns. Known rotated navigation labels are excluded only when they lie wholly
outside all horizontal page text; financial headings and value columns remain
intact. Evidence excerpts retain the printed pages without interactive PDF form
controls, which can contain cyclic trees. Source and excerpt SHA-256 hashes are
kept, and every recovered amount still requires annual context and matching
financial anchors.

Interrupted public PDF downloads restart from byte zero at most twice, through
the existing provider pacing/cooldown helpers. Incomplete files are removed;
access rejections, invalid PDFs and oversized responses are not retried. The
bounded download limit is 80 MB, covering large primary annual reports. Parser
subprocesses use the package root explicitly, so repair commands also work when
launched from another directory. These changes do not modify strategy rules,
the 200-session history requirement, financial validation, or paper ledgers.

`blend_candidate.py` contains original research-only replacement buy hypotheses.
It is not imported by the trading engine. Investor selection requires documented
valuation, governance and business-quality reviews in addition to numerical
screens. No proprietary strategy or guaranteed profit is claimed. AMFI size badges
are display metadata, not valuation or quality signals.
