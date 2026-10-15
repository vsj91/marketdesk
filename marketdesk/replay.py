from .working_universe import symbol_filter
"""Offline daily-open proxy. Opens the source database read-only; no paper ledger writes."""
import collections
import hashlib
import html
import json
import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from .autotrading import RULES, IST, entry_signal, exit_signal
from .execution import charges, money
from .quality import inspect_history, financial_reasons, source_reasons

LIMITATIONS = [
    'Daily-open proxy, not an exact replay of the live strategy: the 09:15 daily open substitutes for its first 09:20 observation. No intraday scans, stops, targets, continuous daily-loss monitoring or order-book fills can be reconstructed.',
    'Indicators use only candles before each simulated session. That session’s close is used only to mark end-of-day holdings. Orders never use that session’s high, low, close or volume.',
    'Fill prices assume adverse slippage on the open and estimated delivery charges. A shared limit of 1% of prior 20-session average volume caps daily simulated fills; this is an assumption, not historical bid/ask evidence. Unfilled remainders are cancelled.',
    'Investor requires financial snapshots and underlying evidence observed on or before the simulated session, within 30 days. Later snapshots are excluded; missing historical evidence means untestable, not a failed strategy.',
    'Uses the current instrument universe and currently saved/repaired price history. Survivorship, later price revisions, corporate actions and dividends are not fully modeled. This is not a point-in-time institutional backtest.',
    'This short testcase cannot establish profitability or real-money readiness. Open holdings are marked, not liquidated, and closing equity excludes hypothetical future exit costs.',
]


def load_snapshot(path, end=None):
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        captured_at = datetime.now(timezone.utc).isoformat()
        symbols = [r[0] for r in db.execute('SELECT symbol FROM instruments WHERE '+symbol_filter(db)+' ORDER BY symbol')]
        history = collections.defaultdict(list)
        counts = collections.Counter()
        digest = hashlib.sha256()
        for row in db.execute('SELECT symbol,ts,open,high,low,close,volume FROM candles WHERE '+symbol_filter(db)+' ORDER BY symbol,ts'):
            r = dict(row)
            if end and r['ts'][:10] > end:
                continue
            history[r['symbol']].append(r)
            counts[r['ts'][:10]] += 1
            digest.update(json.dumps(r, sort_keys=True).encode())
        funds = {r['symbol']:(json.loads(r['details']),r['observed_at']) for r in db.execute('SELECT * FROM fundamentals WHERE '+symbol_filter(db))}
        evidence = collections.defaultdict(dict)
        for r in db.execute('SELECT symbol,endpoint,observed_at FROM payloads WHERE '+symbol_filter(db)):
            evidence[r['symbol']][r['endpoint']] = r['observed_at']
        digest.update(json.dumps(funds,sort_keys=True).encode())
        digest.update(json.dumps(evidence,sort_keys=True).encode())
        # Static identity failures are retained; price eligibility is recomputed at each cutoff.
        bad = {r[0] for r in db.execute("SELECT DISTINCT symbol FROM issues WHERE kind='identity'")}
        digest.update(json.dumps([symbols,sorted(bad)]).encode())
        return {'symbols':symbols,'histories':history,'funds':funds,'evidence':evidence,'bad':bad,
                'sessions':sorted(d for d,n in counts.items() if n >= max(1,len(symbols)//2)),
                'input_sha256':digest.hexdigest(),'captured_at':captured_at}
    finally:
        db.close()


def signal_from(rows, fund):
    if len(rows)<200:
        return None
    closes=[r['close'] for r in rows]
    previous_volume=sum(r['volume'] for r in rows[-40:-10])/30
    return {'as_of':rows[-1]['ts'],'close':closes[-1],
            'dma50':sum(closes[-50:])/50,'dma200':sum(closes[-200:])/200,
            'high20':max(r['high'] for r in rows[-20:]),
            'volume_ratio':(sum(r['volume'] for r in rows[-10:])/10)/previous_volume if previous_volume else 0,
            'turnover20':sum(r['close']*r['volume'] for r in rows[-20:])/20,'fundamentals':fund}


def replay_entry(portfolio, signal, price, clock, trader_variant='baseline'):
    """Research-only entry variant; never used by the running automation engine."""
    result=entry_signal(portfolio,signal,price,clock)
    if result and portfolio=='trader' and trader_variant=='no_chase_v1':
        # Fixed hypothesis, not a parameter search: avoid buying far above the breakout.
        if price>signal['high20']*1.02 or price>signal['close']*1.03:return None
    return result


def replay(snapshot, sessions=5, end=None, slippage_bps=10, capital=1_000_000, fundamentals_mode='historical', start=None, trader_variant='baseline'):
    if trader_variant not in ('baseline','no_chase_v1'):
        raise ValueError('Unknown research-only Trader variant.')
    if fundamentals_mode not in ('historical','current'):
        raise ValueError('Choose historical or current fundamentals.')
    current_fundamentals = fundamentals_mode == 'current'
    captured_at = snapshot.get('captured_at') or datetime.now(timezone.utc).isoformat()
    snapshot_clock = datetime.fromisoformat(captured_at)
    if snapshot_clock.tzinfo is None:
        raise ValueError('Snapshot capture time requires a timezone.')
    if not 1<=sessions<=60 or not 0<=slippage_bps<=200 or not math.isfinite(capital) or capital<=0:
        raise ValueError('Use 1–60 sessions, 0–200 slippage basis points and positive capital.')
    today=datetime.now(IST).date().isoformat()
    if end:date.fromisoformat(end)
    if start:
        date.fromisoformat(start)
        if end and start>end:raise ValueError('Replay start must be on or before its end.')
    available=[d for d in snapshot['sessions'] if d<today and (not end or d<=end)]
    # Keep all preceding sessions for warm-up; the range limits execution only.
    days=[d for d in available if d>=start] if start else available[-sessions:]
    if not days or (not start and len(days)<sessions):
        raise ValueError('Not enough completed sessions in local history.')
    if len(days)>60:raise ValueError('Replay supports at most 60 completed sessions.')
    accounts={p:{'cash':capital,'positions':{},'trades':[],'daily':[],'realized_pnl':0.,'fees':0.,'rejections':collections.Counter()} for p in RULES}
    coverage=[]
    for day in days:
        clock=datetime.fromisoformat(day+'T09:20:00').replace(tzinfo=IST)
        previous=[d for d in available if d<day]
        signals={};bars={};investor_ok=set();caps={};excluded=collections.Counter()
        for symbol in snapshot['symbols']:
            all_rows=snapshot['histories'].get(symbol,[])
            rows=[r for r in all_rows if r['ts'][:10]<day][-201:]
            todays=[r for r in all_rows if r['ts'][:10]==day]
            if len(todays)==1 and math.isfinite(todays[0]['open']) and todays[0]['open']>0:
                bars[symbol]=todays[0]
            reasons=inspect_history(rows,previous,clock)
            if symbol in snapshot['bad']:
                reasons.append('Current identity issue')
            if reasons:
                excluded.update(reasons)
                continue
            fund,observed=snapshot['funds'].get(symbol,({},None))
            sig=signal_from(rows,fund)
            signals[symbol]=sig
            caps[symbol]=math.floor(sum(r['volume'] for r in rows[-20:])/20*.01)
            financial_clock = snapshot_clock if current_fundamentals else clock
            if not financial_reasons(fund,observed,financial_clock) and not source_reasons(fund,snapshot['evidence'].get(symbol,{}),financial_clock):
                investor_ok.add(symbol)
        coverage.append({'session':day,'price_eligible':len(signals),'investor_evidence_eligible':len(investor_ok),'open_bars':len(bars),'price_exclusions':dict(excluded)})
        used=collections.Counter()
        for portfolio, account in accounts.items():
            rules=RULES[portfolio];positions=account['positions'];traded=set();buys=0
            def mark(symbol,pos,field):
                bar=bars.get(symbol)
                value=bar[field] if bar else pos['last_mark']
                return value if math.isfinite(value) and value>0 else pos['last_mark']
            opening_equity=account['cash']+sum(pos['quantity']*mark(s,pos,'open') for s,pos in positions.items())
            stale=any(s not in bars for s in positions)
            def execute(symbol,side,requested,reason):
                bar=bars.get(symbol);sig=signals.get(symbol)
                if not bar:
                    account['rejections']['Missing session open']+=1;return
                if sig and abs(bar['open']/sig['close']-1)>.25:
                    account['rejections']['Opening discontinuity needs review']+=1;return
                # No recent valid history means there is no defensible historical volume cap.
                qty=min(requested,max(0,caps.get(symbol,0)-used[symbol]))
                if qty<=0:
                    account['rejections']['No modeled liquidity']+=1;return
                price=bar['open']*(1+slippage_bps/10000*(1 if side=='BUY' else -1))
                if side=='BUY' and not replay_entry(portfolio,sig,price,clock,trader_variant):
                    account['rejections']['Entry changed after slippage']+=1;return
                fee=charges(money(qty*price),side,dp=side=='SELL')['total']
                if side=='BUY':
                    budget=min(account['cash'],capital*rules['position_pct']/100)
                    while qty and money(qty*price)+fee>budget:
                        qty-=1;fee=charges(money(qty*price),side)['total']
                    if not qty:return
                    cost=money(qty*price)+fee
                    account['cash']=money(account['cash']-cost)
                    positions[symbol]={'quantity':qty,'cost':cost,'last_mark':price}
                    realized=None
                else:
                    pos=positions[symbol];basis=pos['cost']*qty/pos['quantity']
                    proceeds=money(qty*price)-fee
                    account['cash']=money(account['cash']+proceeds)
                    realized=money(proceeds-basis);account['realized_pnl']+=realized
                    pos['cost']-=basis;pos['quantity']-=qty
                    if not pos['quantity']:del positions[symbol]
                account['fees']+=fee;used[symbol]+=qty;traded.add(symbol)
                account['trades'].append({'session':day,'side':side,'symbol':symbol,'requested':requested,'quantity':qty,
                    'unfilled':requested-qty,'price':round(price,6),'fee':fee,'realized_pnl':realized,'reason':reason,
                    'indicator_through':sig['as_of'][:10] if sig else None,'model':'daily-open-proxy-v1',
                    'fundamentals_mode':fundamentals_mode if portfolio=='investor' else None,
                    'fundamentals_observed_at':snapshot['funds'].get(symbol,({},None))[1] if portfolio=='investor' else None})
            for symbol,pos in list(positions.items()):
                if symbol in bars:
                    reason=exit_signal(portfolio,signals.get(symbol),pos,bars[symbol]['open'])
                    if reason:execute(symbol,'SELL',pos['quantity'],reason)
            candidates=[]
            for symbol,sig in signals.items():
                if symbol not in bars or (portfolio=='investor' and symbol not in investor_ok):continue
                result=replay_entry(portfolio,sig,bars[symbol]['open'],clock,trader_variant)
                if result:candidates.append((result[1],symbol,result[0]))
            for _,symbol,reason in sorted(candidates,key=lambda item:(-item[0],item[1])):
                if stale or len(positions)>=rules['max_positions'] or buys>=rules['max_buys_per_day']:break
                value=account['cash']+sum(pos['quantity']*mark(s,pos,'open') for s,pos in positions.items())
                if value<=opening_equity*(1-rules['daily_loss_pct']/100):break
                if symbol in positions or symbol in traded:continue
                budget=min(account['cash'],capital*rules['position_pct']/100)
                requested=max(0,math.floor((budget-25)/(bars[symbol]['open']*1.025)))
                before=len(account['trades'])
                if requested:execute(symbol,'BUY',requested,reason)
                if len(account['trades'])>before:buys+=1
            for symbol,pos in positions.items():pos['last_mark']=mark(symbol,pos,'close')
            closing=money(account['cash']+sum(p['quantity']*p['last_mark'] for p in positions.values()))
            account['daily'].append({'session':day,'cash':account['cash'],'equity':closing,'positions':len(positions),
                                     'entry_candidates':len(candidates),'missing_held_bars':stale})
    for portfolio,account in accounts.items():
        final=account['daily'][-1]['equity'];peak=capital;drawdown=0
        for day in account['daily']:
            peak=max(peak,day['equity']);drawdown=max(drawdown,(peak-day['equity'])/peak*100)
        account.update(initial_cash=capital,ending_equity=final,net_pnl=money(final-capital),return_pct=round((final/capital-1)*100,4),
            max_daily_close_drawdown_pct=round(drawdown,4),fees=money(account['fees']),realized_pnl=money(account['realized_pnl']),
            unrealized_pnl=money(sum(p['quantity']*p['last_mark']-p['cost'] for p in account['positions'].values())))
        account['status']='UNTESTABLE: no eligible historical fundamentals' if portfolio=='investor' and not any(c['investor_evidence_eligible'] for c in coverage) else 'DAILY PROXY ONLY'
        if portfolio=='investor' and current_fundamentals:
            account['status']='HYPOTHETICAL · CURRENT FUNDAMENTALS' if any(c['investor_evidence_eligible'] for c in coverage) else 'UNTESTABLE: no qualified current fundamentals'
        account['rejections']=dict(account['rejections'])
    limitations=list(LIMITATIONS)
    if trader_variant!='baseline':
        limitations.append('Research-only Trader entry cap: at most 2% above the prior 20-session high and 3% above the previous close, checked again after modeled slippage. This variant is not deployed to running paper automation.')
    if current_fundamentals:
        limitations[3]='Investor intentionally uses current fundamental snapshots, frozen at '+captured_at+' throughout the replay window. Freshness is checked at capture time; completeness, financial consistency and strategy filters still apply. This introduces future information relative to the candle dates: a hypothetical paper testcase, not historical performance.'
    return {'model':'daily-open-proxy-v1','created_at':datetime.now(timezone.utc).isoformat(),'sessions':days,'capital_per_portfolio':capital,
            'requested_start':start,'requested_end':end,
            'trader_variant':trader_variant,
            'fundamentals_mode':fundamentals_mode,'snapshot_captured_at':captured_at,
            'slippage_bps_each_side':slippage_bps,'input_sha256':snapshot['input_sha256'],'coverage':coverage,
            'limitations':limitations,'portfolios':accounts,'source_database_access':'read-only; existing portfolios untouched'}


def render_report(report):
    esc=html.escape
    current = report.get('fundamentals_mode')=='current'
    investor_note=('Investor uses current fundamentals with the replay period’s candles. This is a hypothetical paper testcase, not historical performance.'
                   if current else 'Investor has no result where historical financial evidence is missing.')
    if current:
        captured=datetime.fromisoformat(report['snapshot_captured_at']).astimezone(IST)
        investor_note+=' Fundamentals frozen at '+captured.strftime('%d %b %Y, %H:%M IST')+'.'
    cards=[]
    for name in (('investor','trader') if current else ('trader','investor')):
        a=report['portfolios'][name]
        title=name.title()+(' · current fundamentals' if name=='investor' and current else ' · daily simulation')
        if a['status'].startswith('UNTESTABLE'):
            content=f"<strong>{esc(a['status'])}</strong>"
        else:
            buys=sum(t['side']=='BUY' for t in a['trades']);sells=sum(t['side']=='SELL' for t in a['trades'])
            value_class='replay-value loss' if a['net_pnl']<0 else 'replay-value'
            content=f"<strong class='{value_class}'>₹{a['net_pnl']:,.2f}</strong><p>Net marked P&amp;L · {a['return_pct']:.2f}% after modeled costs</p><p>Realized: ₹{a['realized_pnl']:,.2f}<br>Unrealized: ₹{a['unrealized_pnl']:,.2f}<br>{buys} buys · {sells} sells · {len(a['positions'])} open positions</p>"
        note=f'<p>{esc(investor_note)}</p>' if name=='investor' else ''
        cards.append(f'<section><h2>{esc(title)}</h2>{content}{note}</section>')
    rows=[]
    for name,a in report['portfolios'].items():
        if a['status'].startswith('UNTESTABLE'):
            rows.append(f"<tr><th>{esc(name.title())}</th><td>{esc(a['status'])}</td><td>0</td><td colspan=5>Performance not evaluated</td></tr>")
        else:
            rows.append(f"<tr><th>{esc(name.title())}</th><td>{esc(a['status'])}</td><td>{len(a['trades'])}</td><td>₹{a['ending_equity']:,.2f}</td><td>₹{a['net_pnl']:,.2f}</td><td>{a['return_pct']:.2f}%</td><td>₹{a['fees']:,.2f}</td><td>{a['max_daily_close_drawdown_pct']:.2f}%</td></tr>")
    trades=[]
    daily=[]
    for name,a in report['portfolios'].items():
        for t in a['trades']:
            trades.append('<tr>'+''.join('<td>'+esc(str(v))+'</td>' for v in [name,t['session'],t['side'],t['symbol'],t['quantity'],t['unfilled'],f"₹{t['price']:,.2f}",f"₹{t['fee']:,.2f}",t['indicator_through'],t['reason']])+'</tr>')
        for d in a['daily']:
            daily.append(f"<tr><td>{name}</td><td>{d['session']}</td><td>₹{d['equity']:,.2f}</td><td>{d['positions']}</td><td>{d['entry_candidates']}</td></tr>")
    coverage=''.join(f"<tr><td>{c['session']}</td><td>{c['price_eligible']}</td><td>{c['investor_evidence_eligible']}</td></tr>" for c in report['coverage'])
    window=(f"<p>Requested calendar window: {esc(report['requested_start'])} to {esc(report.get('requested_end') or report['sessions'][-1])}. Only completed sessions present in the local consensus calendar are replayed.</p>" if report.get('requested_start') else '')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MarketDesk · Candle replay</title><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/replay.css"></head><body><main>
<a href="/">← Back to MarketDesk</a><p class="eyebrow">ISOLATED PAPER TEST · DAILY CANDLE PROXY</p><h1>{len(report['sessions'])} sessions, replayed.</h1><p>{report['sessions'][0]} to {report['sessions'][-1]} · ₹{report['capital_per_portfolio']:,.0f} starting cash each · {report['slippage_bps_each_side']} bps adverse slippage each side</p>
{window}
<div class="note"><strong>This does not establish that the algorithm is profitable.</strong> Daily candles cannot reproduce the live intraday strategy or depth fills. {esc(investor_note)} Your running paper portfolios are unchanged.</div>
<div class="replay-summary">{''.join(cards)}</div><p><a href="/trader-review.html">Review Trader losses · September–October 2026 →</a> · <a href="/intraday-replay.html">One-minute Trader replay →</a></p><h2>Results after modeled costs</h2><div class="table-scroll"><table><thead><tr><th>Portfolio</th><th>Assessment</th><th>Fills</th><th>Ending equity</th><th>Net P&amp;L</th><th>Return</th><th>Fees</th><th>Daily close drawdown</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p>Trader realized P&amp;L: <strong>₹{report['portfolios']['trader']['realized_pnl']:,.2f}</strong>. Unrealized P&amp;L in {len(report['portfolios']['trader']['positions'])} open positions: <strong>₹{report['portfolios']['trader']['unrealized_pnl']:,.2f}</strong>. Equity includes those positions marked at the last close; gains are not locked in. Zero activity in an untestable portfolio is not a performance result.</p>
<h2>Daily account values</h2><div class="table-scroll"><table><thead><tr><th>Portfolio</th><th>Session</th><th>Equity</th><th>Open positions</th><th>Entry candidates</th></tr></thead><tbody>{''.join(daily)}</tbody></table></div>
<h2>Simulated fills</h2><div class="table-scroll"><table><thead><tr><th>Portfolio</th><th>Session</th><th>Side</th><th>Stock</th><th>Filled</th><th>Cancelled</th><th>Price</th><th>Fees</th><th>Indicators through</th><th>Reason</th></tr></thead><tbody>{''.join(trades) or '<tr><td colspan="10">No eligible fills.</td></tr>'}</tbody></table></div>
<h2>Input coverage · {'current fundamentals' if current else 'historical evidence'}</h2><div class="table-scroll"><table><thead><tr><th>Session</th><th>Pass price checks</th><th>Pass Investor evidence checks</th></tr></thead><tbody>{coverage}</tbody></table></div>
<h2>How to read this test</h2><ul>{''.join('<li>'+esc(item)+'</li>' for item in report['limitations'])}</ul>
<p class="muted">Generated {esc(report['created_at'])}. Model {report['model']}. Local input fingerprint: {report['input_sha256']}. Source database opened read-only.</p></main></body></html>'''


def save_report(report, destination):
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    destination.with_suffix('.html').write_text(render_report(report))
