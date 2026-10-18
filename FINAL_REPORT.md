# MarketDesk Nifty 500 data-validity report

The repair run started at Trader **489/500** and Investor **355/500**. The live result after evidence-backed repairs is Trader **490/500** and Investor **359/500**. The same counts are returned by `/api/health` and `/api/fundamental-repair`.

Three stocks became Investor-valid after annual-report evidence was repaired: **BHEL**, **CRISIL**, and **WHIRLPOOL**. Each accepted lease-liability value is retained with its source document, evidence page, annual period, statement basis, currency and units in the annual-report evidence records. The parser fixes cover explicit Note/Page columns, rotated navigation text outside financial tables, PDF widget failures, interrupted downloads and large annual reports.

The current 141 Investor blockers are classified live as follows, and the totals sum to 500 − 359:

- **106 Fixable**: annual inputs or comparable annual growth still need a validated source or parser repair.
- **25 Needs source data**: the checked sources did not provide enough admissible annual evidence.
- **9 Too new**: fewer than 200 completed daily candles; the UI reports the earliest eligibility date available from the observed session calendar.
- **1 Sources disagree**: INDIACEM has an unresolved audited-source/vendor discrepancy requiring review.

The exact stock-by-stock list, exact quality reasons, missing fields, evidence references and categories is in [remaining-gaps.json](outputs/remaining-gaps.json), and the live repair endpoint exposes the same categories. No annual value was estimated or filled with zero.

Trader now clears VEDL after an evidence-backed adjustment: NSE corporate-action data and the official SPOS circular establish the 30-Apr-2026 demerger session, with pre-ex close ₹773.60 and SPOS base/open ₹289.50. The recorded factor is **0.3742244054**, applied to pre-ex VEDL OHLC prices with the original-row hash retained in `price_adjustment_VEDL`. HEGAM remains blocked on its 2026-09-07 demerger discontinuity because no verified adjustment base/factor was available. The nine short-history symbols remain BAGMANE, BHARATCOAL, CLEANMAX, CMPDI, FRACTAL, VAML, VEDPOWER, VISL and VOGL.

The UI now shows a plain-language blocker category and exact reasons for every blocked stock. Accepted annual evidence links remain visible in the stock-by-stock repair table. The live app was restarted and checked against both API endpoints.

The full test suite passes **384 tests**. Runtime code matches the repository. A database backup was taken before changes; paper ledgers, open positions, account history and automation configuration match the backup afterward. Eligibility thresholds, discontinuity checks, annual period/basis/units validation, staleness limits and source-agreement rules are unchanged.

Supporting files: [repair-results.json](outputs/repair-results.json), [remaining-gaps.json](outputs/remaining-gaps.json), [final-verification.json](outputs/final-verification.json), and [marketdesk-repairs.patch](outputs/marketdesk-repairs.patch).
