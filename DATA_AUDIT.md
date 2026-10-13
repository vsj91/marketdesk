# Local data audit — 11 October 2026

The initial exports are useful but are not a complete, independently reconciled fundamentals dataset. A successful HTTP response does not establish complete financial inputs.

## Imported coverage

- 2,333 NSE common-equity symbols verified against the supplied instrument master.
- 719,621 daily OHLCV observations for 2,325 symbols, latest session 9 October 2026.
- 84 non-equity/unknown instruments excluded and 10 instrument-master symbol aliases quarantined.
- 328 imported histories have fewer than 200 daily observations.
- Initially only seven admitted symbols had full Upstox income, balance-sheet and cash-flow statements. Even those could omit finance costs, borrowing components or lease liabilities.

The importer checks identity, timestamps, finite numeric values, positive prices, OHLC bounds, volume and conflicting daily sessions. Current-session daily bars are excluded. These checks do not independently reconcile prices against the exchange, verify the exchange holiday calendar or establish split/dividend adjustments.

## Repair behavior

The copied collector is `marketdesk/provider.py`; the original PlanSIP files are unchanged. It requests detailed annual Upstox statements and preserves successful historical evidence when requests fail. The local repair job then tries NSE audited annual filings and Yahoo supplemental statements. It stores source periods, basis, units, raw evidence and differences between sources. No missing finance cost or debt amount is replaced with an assumed zero.

NSE statements must match the symbol and ISIN, carry audited annual columns, and have a verified INR scale and statement basis. Unsupported templates, including some bank-specific statements, remain unresolved. The six-field completeness measure is a coverage indicator, not a sector-specific investment qualification.

Yahoo data is available, but its consolidated/standalone basis is unspecified. It is shown as supplementary research. Interest expense is not automatically treated as finance cost, and total debt is not automatically treated as borrowings.

## Verified repair example

For MARUTI, the [NSE audited consolidated filing for 31 March 2026](https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_INDAS_153810_28042026194740_iXBRL_WEB.html) supplied the missing inputs. The original filing is retained under `data/filings/MARUTI/`, with SHA-256 `0c40ba1a2428cd55aef90808e7b12b3c20fb9b82c0a2c8a76fcec328fc5af0be`.

| Annual input | INR crore |
|---|---:|
| Profit before tax | 18,833.0 |
| Finance costs | 238.7 |
| Equity | 107,156.3 |
| Current + non-current borrowings | 0.0 |
| Current + non-current lease liabilities | 102.5 |
| Cash and cash equivalents | 66.9 |

The zero borrowings come from two explicitly reported zeros. Upstox reported PBT of 19,118.5 for the same annual context; the UI retains this discrepancy and selects the qualified NSE record. This demonstrates why source disagreements must remain visible.

## Current execution and limitations

A full-universe fundamentals repair is running from VS Code. The Data health tab shows current progress and counts; these change as requests complete. A completed refresh means every symbol was attempted, not that every missing field became available. Keep the server and Mac running. Network latency and provider limits can make a full run take many hours. Resume interrupted full runs through the repair button.

The user's token is an Upstox Analytics token. [Upstox documents support for fundamentals, history, quotes and WebSockets](https://upstox.com/developer/api-documentation/analytics-token/) without static IP for these market-data APIs. The token cannot place actual trades. REST and WebSocket access now both work. The connection failure was caused by unreachable IPv6 addresses and websocket-client stopping at the first timeout. The connector now tries subsequent DNS addresses and verifies TLS before upgrading to WebSocket. On 11 October 2026 at 23:33 IST, the app connected over IPv4, received NSE_EQ `CLOSING_END`, and saved snapshots for all 2,333 subscribed stocks. These are closed-market last-trade snapshots; intraday streaming must still be observed during an open session. Paper orders require a connected feed, open NSE session and prices no older than 30 seconds.

Validation: 43 isolated regression tests pass (including IPv6 timeout fallback, certificate rejection, connection cancellation and closed-market snapshot handling), JavaScript syntax checks pass, and local HTTP host/origin/CSRF and private-file protection checks pass. Both actual paper portfolios remain unfunded by real money and contain no orders; each has ₹10,00,000 simulated starting cash.

Automatic paper trading is now enabled for both Trader and Investor at the user’s request. The [README strategy table](README.md#automatic-paper-strategies) documents the experimental rules, position budgets, entry window, exits and daily limits. Separate temporary databases verified automatic buys/sells, pause and restart behavior, stale-data rejection, daily halts, duplicate prevention and failure containment. The actual ledger remains empty while the exchange is closed.


## Local quality and execution upgrade — 12 October 2026

The original raw exports and PlanSIP project remain unchanged. A SQLite backup was taken before the migration (`data/backups/before-quality-depth.sqlite3`). Existing trades are preserved and labeled with their old fill model.

The new audit found 1,953 of 2,333 histories passing its initial Trader checks, leaving 380 flagged histories queued for a full 550-day candle repair after the current fundamentals job. Investor counts change as the repair continues. Passing checks is eligibility for paper simulation only. The audit checks local session gaps, daily history length/age, source identity and OHLCV, recent zero volume, large discontinuities, financial period/basis/units, missing inputs, source conflicts, and age of underlying evidence. It runs every five minutes, and buy execution rechecks current candles and financial evidence inside the transaction. It neither interpolates gaps nor guesses corporate-action adjustments.

The execution engine now uses the read-only Upstox V3 full-quote endpoint with identity validation, 5-second snapshot/receipt freshness, 30-second trade freshness, both sides of the book, circuit bounds and spread validation. It walks up to 10% of displayed depth per level within 0.5% of the best price. Partial fills cancel unfilled shares. Ledger evidence contains the snapshot and consumed levels. Book consumption is persistent and shared between portfolios; duplicate requests return the original result. Spread and book depth now affect prices, and delivery-style charges are itemized (see README for rates and limitations).

A live read-only check returned HTTP 200 for MARUTI with matching instrument identity and five levels on each side. The closed-market book was unsuitable and correctly rejected; no real or simulated order was submitted in that probe. Open-market execution still needs forward testing. Unit tests use isolated temporary databases and explicit quote fixtures, never the user's portfolio.

Still unresolved: independently verified exchange calendar, official corporate-action reconciliation/adjusted holdings, dividends, broker queue/latency behavior, same-day tax netting and settlement. Background repair completion means sources were attempted, not that every stock is usable. The app remains fully local and paper only, with real execution unavailable.
