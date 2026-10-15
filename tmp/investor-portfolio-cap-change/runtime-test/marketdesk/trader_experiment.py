"""One predeclared, in-sample paper experiment using frozen local minute history."""
import collections,hashlib,html,json
from pathlib import Path
from datetime import datetime,timezone
from .intraday_data import atomic_json
from .intraday_replay import read_minutes,simulate,closed_positions
from .trader_candidate import NAME,SPEC


def digest(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def metrics(result):
    closed=closed_positions(result['trades']);wins=sum(p['pnl']>0 for p in closed);losses=sum(p['pnl']<0 for p in closed)
    return {**{k:result[k] for k in ('net_pnl','return_pct','realized_pnl','unrealized_pnl','fees','max_observed_drawdown_pct')},
            'buys':sum(t['side']=='BUY' for t in result['trades']),'closed':len(closed),'wins':wins,'losses':losses,
            'open':len(result['positions']),'win_rate_pct':round(wins/len(closed)*100,2) if closed else None,
            'mean_closing_exposure_pct':round(sum((d['equity']-d['cash'])/d['equity']*100 for d in result['daily'])/len(result['daily']),2)}


def audit_losses(original,plan):
    closed=closed_positions(original['trades']);entries={(t['symbol'],t['timestamp']):t for t in original['trades'] if t['side']=='BUY'}
    last_loss={};rows=[]
    for p in sorted(closed,key=lambda p:p['entry']):
        t=entries[p['symbol'],p['entry']];signal=plan['frames'][p['entry'][:10]]['signals'][p['symbol']]
        previous=last_loss.get(p['symbol']);row={**p,'pnl':round(p['pnl'],2),'entry_price':t['price'],
            'early_entry':p['entry'][11:16]<'09:30','same_day_exit':p['entry'][:10]==p['exit'][:10],
            'breakout_extension_pct':round((t['price']/signal['high20']-1)*100,3),
            'prior_loss_exit':previous if previous and previous<p['entry'] else None}
        rows.append(row)
        if p['pnl']<0:last_loss[p['symbol']]=p['exit']
    losers=[p for p in rows if p['pnl']<0];winners=[p for p in rows if p['pnl']>0]
    return {'losers':losers,'winners':winners,'early_losers':sum(p['early_entry'] for p in losers),
            'early_winners':sum(p['early_entry'] for p in winners),'same_day_losers':sum(p['same_day_exit'] for p in losers),
            'repeat_after_loss':sum(p['prior_loss_exit'] is not None for p in losers)}


def run(directory):
    directory=Path(directory);out=directory/'confirmed-breakout-v1';out.mkdir(exist_ok=True)
    plan=json.loads((directory/'plan.json').read_text());original=json.loads((directory/'result.json').read_text())
    hashes={name:digest(directory/name) for name in ('plan.json','minutes.sqlite3','result.json')}
    protocol={'candidate':NAME,'rules':SPEC,'created_at':datetime.now(timezone.utc).isoformat(),
              'policy_sha256':digest(Path(__file__).with_name('trader_candidate.py')),
              'replay_sha256':digest(Path(__file__).with_name('intraday_replay.py')),'inputs':hashes,
              'tuning':'One fixed candidate. Entire period already inspected: in-sample, no untouched holdout.',
              'deployment':'Offline paper experiment only. Running Trader and Investor rules/configurations unchanged.'}
    atomic_json(out/'protocol.json',protocol)
    minutes,metadata=read_minutes(directory,plan)
    if len(metadata)!=len(plan['keys']):raise ValueError('Missing matching cached instruments')
    report={'protocol':protocol,'days':plan['days'],'coverage':original['coverage'],'results':{},
            'loss_audit':audit_losses(original['results']['minute_close'],plan)}
    for mode in ('minute_close','range_touch'):
        report['results'][mode]={}
        for variant in ('baseline',NAME):
            print('Replaying '+mode+' / '+variant,flush=True)
            result=simulate(plan,minutes,exit_mode=mode,trader_variant=variant)
            if variant=='baseline':
                comparable={k:v for k,v in result.items() if k!='trader_variant'}
                prior={k:v for k,v in original['results'][mode].items() if k!='trader_variant'}
                if comparable!=prior:raise AssertionError('Original baseline did not reproduce exactly')
            report['results'][mode][variant]={'metrics':metrics(result),'ledger':result}
            print(json.dumps(report['results'][mode][variant]['metrics']),flush=True)
    if hashes!={name:digest(directory/name) for name in hashes}:raise AssertionError('Frozen input or baseline changed')
    report['baseline_reproduced']=True;report['input_files_unchanged']=True
    report['created_at']=datetime.now(timezone.utc).isoformat()
    atomic_json(out/'comparison.json',report)
    return report


def add_breadth_test(directory,report):
    from .trader_regime import NAME as REGIME,SPEC as REGIME_SPEC,breadth
    directory=Path(directory);folder=directory/'confirmed-breakout-v1'
    plan=json.loads((directory/'plan.json').read_text())
    protocol={'name':REGIME,'spec':REGIME_SPEC,'created_at':datetime.now(timezone.utc).isoformat(),
              'rationale':'Second hypothesis after inspecting the confirmation result and prior-day breadth. Risk abstention, not evidence of profitability.',
              'in_sample':True,'policy_sha256':digest(Path(__file__).with_name('trader_regime.py'))}
    atomic_json(folder/'breadth-protocol.json',protocol)
    report['breadth_protocol']=protocol;report['breadth_daily']={d:breadth(plan['frames'][d]['signals']) for d in plan['days']}
    minutes,_=read_minutes(directory,plan)
    for mode in ('minute_close','range_touch'):
        result=simulate(plan,minutes,exit_mode=mode,trader_variant=REGIME)
        report['results'][mode][REGIME]={'metrics':metrics(result),'ledger':result}
        print(REGIME+' '+mode+' '+json.dumps(metrics(result)),flush=True)
    if report['protocol']['inputs']!={name:digest(directory/name) for name in report['protocol']['inputs']}:raise AssertionError('Frozen input changed')
    atomic_json(folder/'comparison-with-breadth.json',report)
    return report


def render(report):
    e=html.escape
    def cash(v):return ('−' if v<0 else '')+f'₹{abs(v):,.2f}'
    def table(headers,rows):return '<div class="table-scroll"><table><thead><tr>'+''.join('<th>'+e(str(v))+'</th>' for v in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+e(str(v))+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'
    comparison=[];daily=[];details=[]
    for mode,variants in report['results'].items():
        label='Minute close' if mode=='minute_close' else 'Range touch'
        for variant,item in variants.items():
            m=item['metrics'];a=item['ledger'];version={'baseline':'Before','confirmed_breakout_v1':'After · confirmation','breadth_guard_v1':'Risk gate · cash abstention'}[variant]
            comparison.append([label,version,cash(m['net_pnl']),str(m['return_pct'])+'%',cash(m['fees']),f"{m['wins']} / {m['losses']}",m['buys'],m['open'],str(m['max_observed_drawdown_pct'])+'%',str(m['mean_closing_exposure_pct'])+'%'])
            fills=[[t['timestamp'],t['symbol'],t['side'],t['quantity'],t['unfilled'],cash(t['price']),cash(t['fee']),cash(t['realized_pnl']) if t['realized_pnl'] is not None else '—',t['reason']] for t in a['trades']]
            details.append('<details><summary>'+label+' · '+version+' · all fills</summary>'+table(['Time IST','Stock','Side','Filled','Cancelled','Price','Fees','Realized','Reason'],fills)+'</details>')
        for before,after in zip(variants['baseline']['ledger']['daily'],variants[NAME]['ledger']['daily']):
            daily.append([label,before['session'],cash(before['equity']),cash(after['equity']),cash(after['equity']-before['equity']),str(before['buy_halt']),str(after['buy_halt'])])
    base=report['results']['minute_close']['baseline']['metrics'];new=report['results']['minute_close'][NAME]['metrics'];delta=new['net_pnl']-base['net_pnl']
    audit=report['loss_audit'];loser_rows=[[p['symbol'],p['entry'],p['exit'],cash(p['pnl']),str(p['breakout_extension_pct'])+'%',p['prior_loss_exit'] or 'None',p['reason']] for p in sorted(audit['losers'],key=lambda p:p['pnl'])]
    after_closed=closed_positions(report['results']['minute_close'][NAME]['ledger']['trades'])
    after_rows=[[p['symbol'],p['entry'],p['exit'],cash(p['pnl']),p['reason']] for p in sorted(after_closed,key=lambda p:p['pnl'])]
    verdict=('The confirmation candidate reduced the loss' if new['net_pnl']<0 and delta>0 else 'The confirmation candidate improved the marked result' if delta>0 else 'The confirmation candidate worsened the marked result')
    cov=report['coverage']
    regime_section=''
    if report.get('breadth_daily'):
        regime=report['results']['minute_close']['breadth_guard_v1']['metrics']
        breadth_rows=[[d,b['stocks'],str(b['above50_pct'])+'%',str(b['above200_pct'])+'%','Allowed' if b['permits_buys'] else 'Blocked'] for d,b in report['breadth_daily'].items()]
        regime_section=f'''<h2>Second test: market-breadth risk gate</h2><p><strong>{cash(regime['net_pnl'])} · {regime['buys']} entries · {cash(regime['fees'])} fees.</strong> This separate variant starts from the original rules and permits buys only when more than half the valid prior-day universe is above each of its 50- and 200-day averages, with at least 500 valid stocks. Exits remain active. It does not combine the rejected confirmation rules.</p><div class="note">The filter blocked all 18 sessions and stayed entirely in cash. It avoided the sample loss by not trading; this is not a profitable trading result or evidence that 50% is an optimal threshold. This second hypothesis was chosen after the first failed and after inspecting this period's breadth, so it is also in-sample. Neither change has been applied to running automation.</div>{table(['Session','Valid stocks','Above 50-day','Above 200-day','New buys'],breadth_rows)}'''
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MarketDesk · Trader before and after</title><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/replay.css"></head><body><main>
<a href="/intraday-replay.html">← Original intraday replay</a> · <a href="/">Dashboard</a><p class="eyebrow">PAPER ONLY · SAME-CANDLE EXPERIMENT</p><h1>Trader: before and after</h1><p>{report['days'][0]} to {report['days'][-1]} · {len(report['days'])} sessions · ₹10 lakh starting cash per run</p>
<div class="note"><strong>{verdict} by {cash(abs(delta))}.</strong> Same-period research result: the losses were inspected before defining this candidate. It is not an independent validation. The candidate has not replaced either running paper strategy.</div>
<div class="replay-summary"><section><h2>Before · minute close</h2><strong class="replay-value {'loss' if base['net_pnl']<0 else ''}">{cash(base['net_pnl'])}</strong><p>{base['wins']} winners / {base['losses']} losers · {base['buys']} entries<br>Fees {cash(base['fees'])}<br>Observed drawdown {base['max_observed_drawdown_pct']:.2f}%</p></section><section><h2>After · confirmed breakout</h2><strong class="replay-value {'loss' if new['net_pnl']<0 else ''}">{cash(new['net_pnl'])}</strong><p>{new['wins']} winners / {new['losses']} losers · {new['buys']} entries<br>Fees {cash(new['fees'])}<br>Observed drawdown {new['max_observed_drawdown_pct']:.2f}%<br>Realized {cash(new['realized_pnl'])} · unrealized {cash(new['unrealized_pnl'])}</p></section></div>
<h2>What the 41 losses showed</h2><p>{audit['early_losers']} of {len(audit['losers'])} losing positions entered before 09:30; {audit['same_day_losers']} exited the same day; {audit['repeat_after_loss']} followed an earlier losing exit in the same stock. But {audit['early_winners']} of {len(audit['winners'])} winners also entered early. These are observations, not proof that a time filter alone works.</p>
<h2>The fixed candidate</h2><ol><li>Wait for all 15 opening minutes (09:15–09:29) to have traded candles and record their high.</li><li>Require three consecutive completed minute closes above that opening high, the prior 20-session high and a cumulative minute VWAP estimate. Earliest fill: 09:33. VWAP uses minute typical price × volume, not tick-level trade prices.</li><li>After a losing sell fill, block that stock for the next three observed sessions. Losing partial exits extend the cooldown.</li><li>Keep baseline ranking, position sizes, 3% stop, 6% target, daily limits, next-minute fills, liquidity caps, slippage and fees unchanged. Recheck confirmation at the execution open.</li></ol>
<p>One candidate was specified before running it; no parameter search or symbol blacklist. Both entry confirmation and cooldown changed together, so this comparison cannot attribute the effect to either one alone. Lower activity/exposure can explain part of any improvement. The earlier rejected price-cap rule is not used.</p>
{regime_section}
<h2>Before and after, including exit sensitivity</h2>{table(['Exit model','Version','Net P&L','Return','Fees','Wins / losses','Entries','Open','Observed drawdown','Mean closing exposure'],comparison)}
<p>Wins/losses count fully closed positions, grouping partial exits. Net P&L includes open marks. Mean closing exposure is the daily closing invested value divided by equity; it is not time-weighted intraday exposure.</p>
<h2>Verification and limits</h2><p>Both original exit-model baselines reproduced exactly. Frozen plan, candle database and original result hashes remained unchanged. {cov['downloaded_symbols']} stocks · {cov['minute_rows']:,} candles · {cov['eligible_minute_coverage_pct']}% eligible minute coverage; {len(cov['gaps'])} stock-sessions have gaps. Results remain provisional: minute bars do not reproduce live 15-second monitoring or historical order-book fills; zero-trade and missing minutes cannot fill. Current-universe bias, revisions and settlement assumptions remain. There is no untouched holdout in this report.</p>
<h2>Daily equity comparison</h2>{table(['Exit model','Session','Before','After','Difference','Before halt','After halt'],daily)}
<h2>All 41 original losing positions</h2>{table(['Stock','Entry IST','Final exit IST','Realized P&L','Entry breakout extension','Earlier losing exit','Final exit reason'],loser_rows)}
<h2>All closed candidate positions · minute close</h2>{table(['Stock','Entry IST','Final exit IST','Realized P&L','Final exit reason'],after_rows)}
<h2>Full simulated fill ledgers</h2>{''.join(details)}<p>Generated {e(report['created_at'])}. Local evidence: data/replays/intraday/confirmed-breakout-v1/comparison.json and protocol.json. Candidate {e(NAME)}. Both Trader and Investor remain paper-only; running ledgers and settings are unchanged.</p></main></body></html>'''


if __name__=='__main__':
    from .storage import ROOT
    directory=ROOT/'data/replays/intraday'
    report=add_breadth_test(directory,run(directory))
    page=render(report)
    (directory/'confirmed-breakout-v1/comparison.html').write_text(page)
    (ROOT/'web/trader-comparison.html').write_text(page)
    print('Saved /trader-comparison.html',flush=True)
