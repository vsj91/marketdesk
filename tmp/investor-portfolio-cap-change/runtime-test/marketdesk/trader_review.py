"""Local loss attribution and fixed-hypothesis research. No live settings or ledger writes."""
import collections
import html
import json
import statistics
from pathlib import Path
from .execution import money
from .replay import load_snapshot, replay


def analyze(report, snapshot):
    account=report['portfolios']['trader'];held={};exits=[]
    for trade in account['trades']:
        symbol=trade['symbol']
        if trade['side']=='BUY':
            held[symbol]={'entry':trade,'remaining':trade['quantity']}
            continue
        position=held[symbol];buy=position['entry']
        rows=snapshot['histories'][symbol]
        prior=[r for r in rows if r['ts'][:10]<buy['session']]
        during=[r for r in rows if buy['session']<=r['ts'][:10]<=trade['session']]
        open_price=buy['price']/(1+report['slippage_bps_each_side']/10000)
        average=buy['price']+buy['fee']/buy['quantity']
        stop=average*.97;target=average*1.06
        crosses=[{'session':r['ts'][:10],'stop':r['low']<=stop,'target':r['high']>=target}
                 for r in during if r['low']<=stop or r['high']>=target]
        atr=statistics.mean(max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close']))
                            for a,b in zip(prior[-15:-1],prior[-14:]))
        exit_cost=trade['fee']+buy['fee']*trade['quantity']/buy['quantity']
        exits.append({'symbol':symbol,'entry_session':buy['session'],'exit_session':trade['session'],
            'quantity':trade['quantity'],'net_pnl':trade['realized_pnl'],
            'net_return_pct':round(trade['realized_pnl']/(average*trade['quantity'])*100,3),
            'gap_pct':round((open_price/prior[-1]['close']-1)*100,3),
            'breakout_extension_pct':round((open_price/max(r['high'] for r in prior[-20:])-1)*100,3),
            'atr14_pct':round(atr/prior[-1]['close']*100,3),
            'first_range_cross':crosses[0] if crosses else None,
            'allocated_fees':money(exit_cost),'exit_reason':trade['reason']})
        position['remaining']-=trade['quantity']
        if not position['remaining']:del held[symbol]
    winners=[t for t in exits if t['net_pnl']>0];losers=[t for t in exits if t['net_pnl']<0]
    by_symbol=collections.Counter()
    for t in exits:by_symbol[t['symbol']]+=t['net_pnl']
    earlier=[t for t in exits if t['first_range_cross'] and t['first_range_cross']['session']<t['exit_session']]
    return {'exit_fills':len(exits),'wins':len(winners),'losses':len(losers),
        'win_rate_pct':round(len(winners)/len(exits)*100,2) if exits else 0,
        'average_win':money(statistics.mean(t['net_pnl'] for t in winners)) if winners else 0,
        'average_loss':money(statistics.mean(t['net_pnl'] for t in losers)) if losers else 0,
        'profit_factor':round(sum(t['net_pnl'] for t in winners)/abs(sum(t['net_pnl'] for t in losers)),3) if losers else None,
        'earlier_threshold_crosses':len(earlier),
        'ambiguous_first_crosses':sum(t['first_range_cross']['stop'] and t['first_range_cross']['target'] for t in exits if t['first_range_cross']),
        'losers_atr_above_stop':sum(t['atr14_pct']>3 for t in losers),
        'losers_gap_above_3pct':sum(t['gap_pct']>3 for t in losers),
        'winners_gap_above_3pct':sum(t['gap_pct']>3 for t in winners),
        'net_pnl':account['net_pnl'],'realized_pnl':account['realized_pnl'],'unrealized_pnl':account['unrealized_pnl'],
        'fees':account['fees'],'net_pnl_without_fees':money(account['net_pnl']+account['fees']),
        'per_symbol_realized':dict(by_symbol),'exits':sorted(exits,key=lambda t:t['net_pnl'])}


def compact_result(report):
    a=report['portfolios']['trader']
    return {'start':report['sessions'][0],'end':report['sessions'][-1],'sessions':len(report['sessions']),
            'variant':report.get('trader_variant','baseline'),'net_pnl':a['net_pnl'],'return_pct':a['return_pct'],
            'drawdown_pct':a['max_daily_close_drawdown_pct'],'fees':a['fees'],
            'buys':sum(t['side']=='BUY' for t in a['trades']),'sells':sum(t['side']=='SELL' for t in a['trades']),
            'realized_pnl':a['realized_pnl'],'unrealized_pnl':a['unrealized_pnl']}


def run_review(path, original, output):
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    snapshot=load_snapshot(path,'2026-10-11')
    review={'snapshot_captured_at':snapshot['captured_at'],'input_sha256':snapshot['input_sha256'],
            'original_report':str(original),'deployment':'Research only; running Trader and Investor rules unchanged',
            'analysis':analyze(json.loads(Path(original).read_text()),snapshot),'experiments':[]}
    # Fixed before running results. No grid search or automatic promotion.
    for label,start,end in [('reviewed_month','2026-09-12','2026-10-11'),('earlier_cross_check','2026-08-12','2026-09-11')]:
        for variant in ('baseline','no_chase_v1'):
            print('Running '+label+' / '+variant,flush=True)
            result=replay(snapshot,start=start,end=end,fundamentals_mode='current',trader_variant=variant)
            destination=Path(output).parent/(label+'-'+variant+'.json')
            destination.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
            review['experiments'].append({'period':label,**compact_result(result)})
            print(json.dumps(review['experiments'][-1]),flush=True)
    Path(output).write_text(json.dumps(review,indent=2,allow_nan=False)+'\n')
    return review


def render_review(review):
    a=review['analysis'];esc=html.escape
    money_text=lambda v:('−' if v<0 else '')+f'₹{abs(v):,.2f}'
    month={e['variant']:e for e in review['experiments'] if e['period']=='reviewed_month'}
    delta=month['no_chase_v1']['net_pnl']-month['baseline']['net_pnl']
    verdict=('The entry cap worsened this month by '+money_text(abs(delta))+'. It has not been applied.' if delta<0 else
             'The entry cap improved this diagnostic month, but is not validated for deployment.')
    experiments=''.join('<tr>'+''.join('<td>'+esc(str(v))+'</td>' for v in [
        e['period'].replace('_',' '),e['variant'],e['start']+' – '+e['end'],money_text(e['net_pnl']),
        str(e['return_pct'])+'%',str(e['drawdown_pct'])+'%',str(e['buys'])+' / '+str(e['sells']),
        money_text(e['realized_pnl']),money_text(e['unrealized_pnl'])])+'</tr>' for e in review['experiments'])
    trades=[]
    for t in a['exits']:
        x=t['first_range_cross'];cross='None'
        if x:cross=x['session']+' · '+('both; order unknown' if x['stop'] and x['target'] else 'stop range' if x['stop'] else 'target range')
        trades.append('<tr>'+''.join('<td>'+esc(str(v))+'</td>' for v in [t['symbol'],t['entry_session'],t['exit_session'],money_text(t['net_pnl']),str(t['net_return_pct'])+'%',str(t['gap_pct'])+'%',str(t['breakout_extension_pct'])+'%',str(t['atr14_pct'])+'%',cross])+'</tr>')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MarketDesk · Trader loss review</title><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/replay.css"></head><body><main>
<a href="/replay.html">← Monthly replay</a> · <a href="/intraday-replay.html">New: one-minute Trader replay →</a><p class="eyebrow">PAPER ONLY · LOSS REVIEW</p><h1>Why Trader lost in this replay</h1>
<p>Monthly baseline: 15 September–9 October 2026. Starting cash ₹10 lakh. Running automation rules and portfolios are unchanged.</p>
<div class="note"><strong>{esc(verdict)}</strong> First improve intraday execution evidence, then validate strategy changes in separate paper tests.</div>
<div class="replay-summary"><section><h2>Net marked result</h2><strong class="replay-value loss">{money_text(a['net_pnl'])}</strong><p>Realized: {money_text(a['realized_pnl'])}<br>Unrealized: {money_text(a['unrealized_pnl'])}<br>Fees: {money_text(a['fees'])}</p></section><section><h2>Completed exits</h2><strong class="replay-value">{a['wins']} winners / {a['losses']} losers</strong><p>Win rate {a['win_rate_pct']}% · profit factor {a['profit_factor']}<br>Average winner {money_text(a['average_win'])}<br>Average loser {money_text(a['average_loss'])}</p></section></div>
<h2>What the evidence says</h2><ul>
<li><strong>Exit timing is the biggest measurement problem.</strong> {a['earlier_threshold_crosses']} of {a['exit_fills']} exits had an earlier daily candle reaching the modeled stop or target. {a['ambiguous_first_crosses']} first-crossing bars touched both. Daily opens miss intraday events and cannot establish execution order or achievable fills. Winners can also become losers under different intraday paths.</li>
<li><strong>The current entries can chase jumps.</strong> {a['losers_gap_above_3pct']} losing exits followed entries over 3% above the previous close, but so did {a['winners_gap_above_3pct']} of the {a['wins']} winners. A cap may remove winners too. The current rule permits up to 15% and ranks candidates by largest extension above the 20-session high. Historical volume confirmation is a 10/30-session ratio, not that day's buying volume.</li>
<li><strong>The fixed 3% stop is small relative to selected stocks' movement.</strong> {a['losers_atr_above_stop']} of {a['losses']} losers had a prior 14-session average true range above 3% of price. This suggests testing volatility-aware sizing/stops; it does not prove wider stops improve returns.</li>
<li><strong>Costs add to losses but do not explain them.</strong> Removing estimated fees alone still leaves {money_text(a['net_pnl_without_fees'])}. The calculation still includes assumed slippage.</li></ul>
<h2>One fixed entry hypothesis, tested separately</h2><p><strong>no_chase_v1:</strong> require entry ≤2% above the previous 20-session high and ≤3% above the previous close, including after modeled slippage. All other rules and costs are unchanged. These thresholds were fixed before running the comparison; there was no parameter search.</p>
<div class="table-scroll"><table><thead><tr><th>Period</th><th>Variant</th><th>Dates</th><th>Net P&amp;L</th><th>Return</th><th>Daily close drawdown</th><th>Buys / sells</th><th>Realized</th><th>Unrealized</th></tr></thead><tbody>{experiments}</tbody></table></div>
<p>The earlier window is a cross-check, not proof of forward performance. Both comparisons retain the same daily-open limitations and current-universe bias. Investor uses the same frozen current fundamentals; this review evaluates Trader only.</p>
<h2>Fix order</h2><ol><li><strong>Validate execution first:</strong> collect/replay timestamped intraday candles or ticks and depth evidence, including gap handling, stale quotes and partial fills. Do not replace missing paths with guaranteed 3% exits.</li><li><strong>Keep the capped-entry candidate in research:</strong> require evidence from independent periods and forward paper runs before promoting it. Do not tune to erase this month's losses.</li><li><strong>Then test risk per trade:</strong> volatility-based sizing, stop distance and a portfolio risk cap together. Increasing stop distance alone increases the loss budget.</li><li><strong>Test confirmation and re-entry separately:</strong> current-session breakout confirmation and a cooldown after a stop. Historical volume and next-day re-entry currently permit repeated failed breakouts.</li></ol>
<h2>Every baseline exit, worst first</h2><div class="table-scroll"><table><thead><tr><th>Stock</th><th>Bought</th><th>Sold</th><th>Net P&amp;L</th><th>Net return</th><th>Entry gap</th><th>Breakout extension</th><th>Prior ATR14 / price</th><th>First daily range touch</th></tr></thead><tbody>{''.join(trades)}</tbody></table></div>
<p>Range touches are diagnostic observations, not executable orders. Entry-day high/low may precede the actual 09:20 entry; the replay uses the daily open as a proxy. Current saved prices may contain later revisions. The original monthly result is preserved.</p>
<p>References: <a href="https://www.investor.gov/introduction-investing/investing-basics/how-stock-markets-work/types-orders">Order execution and stop-price limits</a> · <a href="https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/research-guide">Avoiding overfitting in strategy research</a>.</p>
<p class="muted">Local input fingerprint {review['input_sha256']}. Snapshot {esc(review['snapshot_captured_at'])}. {esc(review['deployment'])}.</p></main></body></html>'''
