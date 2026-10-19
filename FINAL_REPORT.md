# MarketDesk follow-up 10 — 36 lease stocks

Investor **379 → 380/500**. Trader **490 → 490/500**. All 36 targets were checked in batches of 15, 15 and 6: **1 accepted, 35 still blocked**. No banks, insurers or REITs were targeted. The existing parked list contains 38 blocked constituents; they remain inside the unchanged Investor denominator.

| Category | Before | After |
|---|---:|---:|
| Investor ready | 379 | 380 |
| Trader ready | 490 | 490 |
| Fixable | 84 | 83 |
| Needs source data | 26 | 26 |
| Sources disagree | 2 | 2 |
| Too new | 9 | 9 |
| Total Investor blocked | 121 | 120 |
| Lease targets still blocked | 36 | 35 |

## Accepted: TATAINVEST

**Total lease liabilities = ₹1.3978 crore**, from printed **139.78 lakh** at 31 March 2026 in note 13, PDF page **219**. Prior-year closing is 221.65 lakh. The separate undiscounted maturity total 151.94 was not used. Balance sheet PDF **194** supplies two unchanged anchors: cash 192.83 lakh = 1.9283 crore and total equity 2,922,115.29 lakh = 29,221.1529 crore.

The note page prints both standalone and consolidated navigation tabs, but only **“Consolidated Financial Statements”** is white text in the blue selected tab. The parser now reads that visible selected title using the actual text/rectangle coordinates, retaining the exact title and PDF page as evidence. It also recognizes numbered “Disclosures for leasing arrangements”, dotted table leaders, and the lease table’s own dates/units. No basis is inferred from the company type or filename. Ambiguous selection, conflicting body basis, missing own dates, maturity totals and dashes remain rejected.

Parser revision **34 → 35**; financial rule revision remains **2**. The two-anchor requirement, anchor tolerance, same-basis/date/currency checks, and restrictions on zero values are unchanged. The runtime parser file is copied and its hash matches the tested repo file. The repaired data was applied directly and verified through the local app endpoints; no server restart was performed.

Evidence: [full acceptance](TATAINVEST-evidence.json), [source re-audit](source-audit.json), [note page](TATAINVEST-219.png), [balance sheet](TATAINVEST-194.png), [parser diff](parser-change.diff). Source audit: **1/1 newly accepted value matches (100%)**. That rate covers this pass only; it is not a claim of a new audit of every historic acceptance.

## Every target and first remaining blocker

| Stock | First failing gate | Actual page versus requirement | Accepted? |
|---|---|---|---|
| BAJAJHFL | Explicit statement basis missing | PDF 189 full: title does not print standalone; no qualifying nearby auditor section supplied it. | No |
| CANFINHOME | Explicit statement basis missing | PDF 249 full: title does not print standalone; no qualifying nearby auditor section supplied it. | No |
| CASTROLIND | Explicit statement basis missing | PDF 133 full: title does not print standalone; no qualifying nearby auditor section supplied it. | No |
| ENRIN | Explicit statement basis missing | PDF 99 full: title does not print standalone; no qualifying nearby auditor section supplied it. | No |
| FACT | Explicit statement basis missing | PDF 125 full: title does not print standalone; no qualifying nearby auditor section supplied it. | No |
| FIVESTAR | Explicit statement basis missing | PDF 155 full: title does not print standalone; no qualifying nearby auditor section supplied it. | No |
| HDBFS | Explicit statement basis missing | PDF 227 full: title does not print standalone; no qualifying nearby auditor section supplied it. | No |
| HOMEFIRST | Explicit statement basis missing | PDF 358 full: title does not print standalone; no qualifying nearby auditor section supplied it. | No |
| MSUMI | Explicit statement basis missing | PDF 202 full: title does not print standalone; no qualifying nearby auditor section supplied it. | No |
| SBICARD | Explicit statement basis missing | PDF 339 full: title does not print standalone; no qualifying nearby auditor section supplied it. | No |
| VAML | Explicit statement basis missing | PDF 211 full: statement title lacks required explicit standalone basis; dates/scale cannot establish basis. VAML/VISL candidates are unaudited pro-forma adjustment tables. | No |
| VISL | Explicit statement basis missing | PDF 211 full: statement title lacks required explicit standalone basis; dates/scale cannot establish basis. VAML/VISL candidates are unaudited pro-forma adjustment tables. | No |
| TATAINVEST | All gates pass | BS 194: cash and equity anchors match; note 219: 139.78 lakh × 0.01 = 1.3978 crore. | Yes |
| HUDCO | Lease-note context | BS 209 has no explicit lease total. PDF 303 prints closing 1.47 but lacks a supported note heading/basis and its own complete unit context. | No |
| ATUL | Lease-note context | BS 254 prints non-current 3.26, no numeric current component. Note 30.12 PDF 312 prints closing 3.26, but no qualifying statement basis heading. | No |
| GLAXO | Two matching anchors required | PDF 181 cash matches; audited equity 2267.4171 vs baseline 2284.69 does not. Only one anchor. | No |
| GRAPHITE | Two matching anchors required | PDF 201 audited cash/equity/debt 22.28/5856.14/367.34 vs baseline 22/5856/367; none within unchanged tolerance. | No |
| ABREL | Numeric lease closing required | PDF 291 carrying-value rollforward closes with a dash after transfer to held-for-sale liabilities; dash is not an explicit zero. PDF 278 has discontinued/held-for-sale component figures. | No |
| CREDITACC | Two verified anchors required | PDF 146: cash 1151.58 matches. Consolidated equity has no printed total/NCI; debt includes a dashed subordinated row. Approved identity path does not apply to this layout. | No |
| RITES | Anchors / conflicting continuation basis | PDF 277 splits cash: owned 96.03 + clients 460.98 = 557.01; equity is on PDF 278. That continuation prints a consolidated title but a standalone-statements footer, so it cannot be joined under the current checks. | No |
| BSE | Numeric components or carrying total required | BS 163 prints current lease 2; non-current is a dash. Neither an explicit numeric total nor both numeric components is verified. | No |
| GALLANTT | Explicit liability balance required | No numeric lease total/both components or supported carrying-value note found in the scanned text candidates; policy/lessor/expense text does not prove nil. See saved page traces. | No |
| RECLTD | Explicit liability balance required | BS 177 has no lease total; PDF 220 gives short-term lease expense 16.41, which does not establish total lease liabilities or an explicit nil. | No |
| IFCI | Group liability evidence required | BS 209 has no lease total. The PDF 248 short-term-lease exemption concerns subsidiary IFCI Venture Capital; it cannot establish group nil. | No |
| IREDA | Validated carrying-value note required | BS 388 has no lease line. PDF 492 carrying amount 4.98 vs undiscounted 8.88; PDF 446 other-financial-liability line 4.99. No independently qualifying note accepted. | No |
| ITI | Explicit INR scale required | PDF 187 consolidated balance sheet has no explicit unambiguous currency scale in the supported statement context. | No |
| LODHA | Conflicting printed basis | PDF 229 header says “Standalone Balance Sheet and Profit & Loss”; title says “Consolidated Balance Sheet”. This is real printed conflict, not inactive navigation. | No |
| BDL | Numeric components or validated lease note required | BS 111: non-current dash, current 31.52 lakh. PDF 153 fair-value table also prints 31.52; it lacks the required explicit note basis. No dash-to-zero conversion. | No |
| TECHNOE | Explicit liability balance required | No numeric lease total/both components or supported carrying-value note found in the scanned text candidates; policy/lessor/expense text does not prove nil. See saved page traces. | No |
| SIEMENS | Comparable annual columns required | PDF 241 prints 31 March 2026 and 30 September 2024; not the required current/prior annual pair. | No |
| FORCEMOT | Explicit liability balance required | No numeric lease total/both components or supported carrying-value note found in the scanned text candidates; policy/lessor/expense text does not prove nil. See saved page traces. | No |
| GILLETTE | Company PDF identity | Official PDF HTTP 200, 164 pages rechecked; required ISIN INE322A01010 absent from extracted text. Prior exchange PDF had a corrupt page tree. | No |
| KPRMILL | Explicit liability balance required | No numeric lease total/both components or supported carrying-value note found in the scanned text candidates; policy/lessor/expense text does not prove nil. See saved page traces. | No |
| NCC | Explicit liability balance required | No numeric lease total/both components or supported carrying-value note found in the scanned text candidates; policy/lessor/expense text does not prove nil. See saved page traces. | No |
| OLECTRA | Explicit liability balance required | BS 196 has no total; PDF 229 short-term exemption/expense 138.78 lakh is not an explicit nil liability. | No |
| RPOWER | Explicit liability balance required | No numeric lease total/both components or supported carrying-value note found in the scanned text candidates; policy/lessor/expense text does not prove nil. See saved page traces. | No |

The compact table names the blocking evidence condition; raw parser output, page text, expected baselines and exact source URLs remain in [all-lease-results.json](all-lease-results.json), `traces/`, [lease-note-gates.json](lease-note-gates.json), and [broad-lease-review.json](broad-lease-review.json). No rejected stock was assigned a default zero.

## Additional source/page work

Actual PDF images inspected include TATAINVEST 194/219, ATUL 312, HUDCO 303, LODHA 229, CREDITACC 146 and RITES 277. The conflicting continuation text for RITES 278 is retained in its trace. FIVESTAR scanned auditor/annexure page headers 1–12 were visually inspected; this is not a full OCR review of all 82 pages.

SBI Card’s official Q4 filing returned HTTP 200 (11 pages); Five-Star’s official Q4 filing returned HTTP 200 (19 pages), and its annual financial-statements PDF returned HTTP 200 (82 pages). The annual Five-Star file has an empty text layer. These attempts did not provide qualifying explicit basis evidence. URLs, HTTP results, file sizes and hashes are in [alternate-source-attempts.json](alternate-source-attempts.json). Gillette’s exact ISIN was rechecked across all 164 readable PDF pages: [identity check](GILLETTE-identity-review.json).

CASTROLIND’s actual statement date is 31 December 2025 (comparative 31 December 2024), PDF 133. ENRIN’s is 30 September 2025 (comparative 30 September 2024), PDF 99. Their remaining issue is explicit basis, not the filename.

No report request is being sent back to the user: [source request status](source-request-by-stock.txt). The remaining cases are validation/document-identity blockers; they are not evidence that all public data sources are exhausted. [All 120 currently blocked stocks](BLOCKED_STOCKS.md) distinguishes this pass from carried-forward diagnostics and parked stocks.

## Live refresh interference and preservation checks

A pre-existing background price import overwrote VEDL’s adjusted pre-ex history during this run. The saved verified factor remains 289.5 / 773.6 = 0.37422440537745605. After verifying timestamps, volumes and factor arithmetic against the initial backup, 258 overwritten rows were restored exactly; 2 already-adjusted rows were retained. First post-ex open differs by 0% from adjusted pre-ex close, and close differs by −6.20%. See [restoration evidence](VEDL-restoration.json). Batch 1’s intermediate 489/379 score is therefore retained honestly in its log; batches 2 and 3 show 490/380.

A concurrent fundamentals refresh changed RITES during parsing. The optimistic write guard blocked the stale write. Batch 2 was retried with a fresh baseline immediately before each stock. The job also writes null annual-report metadata, exposing a repair-page HTTP 500. Display aggregation now treats null metadata as an unverified report, with a regression test. The cached report was normalized and the endpoint rechecked. The pre-existing refresh completed all 118 of its targets before the final UI check (HTTP 200, Investor 380). The running server retains previously imported Python code until a future normal restart; no automation was stopped or reconfigured.

The price importer itself was not modified. A later raw-history refresh can overwrite VEDL’s adjustment again; this restoration does not claim to fix that recurring importer behavior.

Backup: SQLite snapshot and modified source files saved under `work/followup10/` before changes. Ledger tables, portfolio/position/trade rows, auto-day/event records, depth usage, Claude portfolio records and automation configuration were compared with that backup and are unchanged. Only the natural `auto_status` heartbeat changed among automation state keys. Among audited financial numeric fields, the sole before/after change is TATAINVEST lease liabilities from missing to 1.3978. See [verification](verification.json).

**435 tests passed**, including selected-tab evidence, own note dates, wrong basis/currency, maturity/dash rejection, conflicting body basis, and the null-report display regression. [Test log](tests.log).

Too-new date labels/calendar behavior were not changed in this lease pass. Current UI still uses its existing “projected weekday” fallback where the saved session calendar ends; this report does not claim that the older holiday-calendar request is completed.

## Resume exactly

All 36 target rows are in `all-lease-results.json`; take the 35 with `accepted=false`. Download metadata and full-PDF symlinks are under `work/followup10/<symbol>/`. Do not repeat unchanged 35-row parses without new evidence or a tested parser change. Priority evidence needs: independent explicit-basis filings for the 12 basis blockers; correctly scoped lease notes for HUDCO/ATUL/IREDA; verified anchor source resolution for GLAXO/GRAPHITE; explicit identity for GILLETTE. RITES/LODHA contain real contradictory basis text; do not silently strip it. Banks/insurers/REITs stay parked; bootstrap for missing vendor baselines stays disabled.
