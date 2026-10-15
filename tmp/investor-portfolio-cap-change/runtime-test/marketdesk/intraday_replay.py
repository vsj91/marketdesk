"""Chronological paper-only minute replay; no broker API and no running-ledger writes."""
import collections, html, json, math, sqlite3
from datetime import datetime,timedelta,timezone
from pathlib import Path
from .autotrading import IST,RULES,entry_signal,exit_signal
from .execution import money,charges
from .intraday_data import atomic_json


class MinuteStore:
    """Load one session at a time so millions of candles do not occupy RAM at once."""
    def __init__(self,path,count,symbols):
        self.path=Path(path);self.minute_rows=count;self.symbols=set(symbols);self.day=None;self.rows={}
    def get(self,ts,default=None):
        day=ts[:10]
        if day!=self.day:
            self.rows=collections.defaultdict(dict);self.day=day
            end=(datetime.fromisoformat(day)+timedelta(days=1)).date().isoformat()
            with sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True) as db:
                for symbol,stamp,op,hi,lo,cl,vol in db.execute('SELECT * FROM bars WHERE ts>=? AND ts<? ORDER BY ts,symbol',(day,end)):
                    if symbol not in self.symbols:continue
                    self.rows[stamp][symbol]={'ts':stamp,'open':op,'high':hi,'low':lo,'close':cl,'volume':vol}
        return self.rows.get(ts,default)


def read_minutes(directory,plan):
    db=sqlite3.connect((Path(directory)/'minutes.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    try:
        metadata={s:json.loads(m) for s,m in db.execute('SELECT * FROM fetched')}
        metadata={s:m for s,m in metadata.items() if s in plan['keys'] and
                  (m['instrument_key'],m['start'],m['end'])==(plan['keys'][s],plan['start'],plan['end'])}
        count=sum(m['bars'] for m in metadata.values())
        return MinuteStore(Path(directory)/'minutes.sqlite3',count,metadata),metadata
    finally:db.close()


def coverage(plan,minutes,metadata):
    per_day=[];mismatches=[];missing=[];total_expected=0;total_present=0;zero_volume=0
    for day,frame in plan['frames'].items():
        selected=frame['eligible'];expected=len(selected)*375;present=0;gaps=0
        by_stock=collections.defaultdict(list)
        for n in range(375):
            ts=(datetime.fromisoformat(day+'T09:15:00').replace(tzinfo=IST)+timedelta(minutes=n)).isoformat()
            for s,b in minutes.get(ts,{}).items():by_stock[s].append(b)
        for s in selected:
            bars=by_stock[s];present+=len(bars)
            zero_volume+=sum(b['volume']==0 for b in bars)
            if len(bars)!=375:
                gaps+=1;missing.append({'session':day,'symbol':s,'minutes':len(bars),'missing':375-len(bars)})
            daily=frame['daily'].get(s)
            if daily and bars:
                # NSE official daily close can differ from the final minute close.
                actual={'open':bars[0]['open'],'high':max(b['high'] for b in bars),'low':min(b['low'] for b in bars)}
                diffs={k:round((actual[k]/daily[k]-1)*100,4) for k in actual if abs(actual[k]-daily[k])>max(.1,daily[k]*.01)}
                if diffs:mismatches.append({'session':day,'symbol':s,'difference_pct':diffs})
        total_expected+=expected;total_present+=present
        per_day.append({'session':day,'eligible':len(selected),'expected_minutes':expected,'present_minutes':present,'symbols_with_gaps':gaps})
    count=minutes.minute_rows if isinstance(minutes,MinuteStore) else sum(len(v) for v in minutes.values())
    return {'requested_symbols':len(plan['keys']),'downloaded_symbols':len(metadata),'minute_rows':count,
            'eligible_minute_coverage_pct':round(total_present/total_expected*100,3) if total_expected else 0,
            'eligible_zero_volume_minutes':zero_volume,
            'missing_downloads':sorted(set(plan['keys'])-set(metadata)),'gaps':missing,'daily_ohl_mismatches':mismatches,'daily':per_day}


def range_event(position,bar):
    average=position['cost']/position['quantity'];rules=RULES['trader']
    stop=bar['low']<=average*(1-rules['stop_pct']/100)
    target=bar['high']>=average*(1+rules['take_profit_pct']/100)
    return stop,target


def simulate(plan,minutes,exit_mode='minute_close',slippage_bps=10,capital=1_000_000,trader_variant='baseline'):
    if exit_mode not in ('minute_close','range_touch'):raise ValueError('Unknown exit model')
    if not 0<=slippage_bps<=200:raise ValueError('Invalid slippage')
    from .trader_candidate import ConfirmedBreakout,NAME
    from .trader_regime import breadth,NAME as REGIME_NAME
    if trader_variant not in ('baseline',NAME,REGIME_NAME):raise ValueError('Unknown Trader variant')
    candidate=ConfirmedBreakout() if trader_variant==NAME else None
    rules=RULES['trader'];cash=float(capital);positions={};trades=[];daily=[];events=collections.Counter()
    realized=0.;total_fees=0.;peak=capital;drawdown=0.;ambiguities=[]
    def mark_value():return cash+sum(p['quantity']*p['last_mark'] for p in positions.values())
    def record_drawdown():
        nonlocal peak,drawdown
        value=mark_value();peak=max(peak,value);drawdown=max(drawdown,(peak-value)/peak*100)
    for day_index,day in enumerate(plan['days']):
        frame=plan['frames'][day];signals=frame['signals'];eligible=frame['eligible']
        if candidate:candidate.start_day(day_index,eligible)
        regime_ok=trader_variant!=REGIME_NAME or breadth(signals)['permits_buys']
        if not regime_ok:events['breadth_blocked_sessions']+=1
        traded=set();buys=0;opening_equity=None;halted=False;pending=[];used=collections.Counter();dp_paid=set()
        for n in range(375):
            stamp=datetime.fromisoformat(day+'T09:15:00').replace(tzinfo=IST)+timedelta(minutes=n)
            ts=stamp.isoformat();bars=minutes.get(ts,{})
            previous_ts=(stamp-timedelta(minutes=1)).isoformat();previous=minutes.get(previous_ts,{}) if n else {}
            for symbol,pos in positions.items():
                if symbol in bars and bars[symbol]['volume']>0:pos['last_mark']=bars[symbol]['open']
                else:events['held_stock_minutes_without_trades']+=1
            # Evaluate the opening risk baseline before any queued fills; overnight positions included.
            if opening_equity is None and all(s in bars and bars[s]['volume']>0 for s in positions):opening_equity=mark_value()
            if opening_equity is not None and mark_value()<=opening_equity*(1-rules['daily_loss_pct']/100):halted=True
            record_drawdown()
            for order in sorted(pending,key=lambda x:(x['side']!='SELL',-x.get('score',0),x['symbol'])):
                symbol=order['symbol'];side=order['side'];bar=bars.get(symbol);prior=previous.get(symbol);sig=signals.get(symbol)
                # Missing/zero-trade minutes have no observable execution price. No forward filling.
                if not bar or not prior or bar['volume']<=0 or prior['volume']<=0:
                    events['cancelled_missing_execution_minute']+=1;continue
                if order['execute_at']!=ts:raise AssertionError('Orders must execute exactly one minute later')
                if sig and abs(bar['open']/sig['close']-1)>.25:
                    events['blocked_price_discontinuity']+=1;continue
                if side=='BUY':
                    if not (9,20)<=(stamp.hour,stamp.minute)<(15,0) or halted or opening_equity is None:continue
                    if any(s not in bars or bars[s]['volume']<=0 for s in positions):
                        events['buys_wait_stale_holding']+=1;continue
                    if symbol in positions or symbol in traded or buys>=rules['max_buys_per_day'] or len(positions)>=rules['max_positions']:continue
                    if not entry_signal('trader',sig,bar['open'],stamp):
                        events['entry_changed_before_fill']+=1;continue
                elif symbol not in positions:continue
                price=bar['open']*(1+slippage_bps/10000*(1 if side=='BUY' else -1))
                if side=='BUY' and not entry_signal('trader',sig,price,stamp):
                    events['entry_changed_after_slippage']+=1;continue
                if side=='BUY' and candidate and not candidate.permits(symbol,bar['open']):
                    events['confirmation_changed_before_fill']+=1;continue
                volume_reference=(sig or {}).get('average_volume20',(positions.get(symbol) or {}).get('average_volume20',0))
                day_limit=math.floor(volume_reference*.01)
                # Size uses already completed volume only. Current minute volume never increases capacity.
                capacity=min(math.floor(prior['volume']*.1),max(0,day_limit-used[symbol]))
                requested=order['quantity'] if side=='BUY' else min(order['quantity'],positions[symbol]['quantity'])
                quantity=min(requested,capacity)
                if quantity<=0:events['blocked_volume_capacity']+=1;continue
                dp=side=='SELL' and symbol not in dp_paid
                fee=charges(money(quantity*price),side,dp=dp)['total']
                if side=='BUY':
                    budget=min(cash,capital*rules['position_pct']/100)
                    while quantity and money(quantity*price)+fee>budget:
                        quantity-=1;fee=charges(money(quantity*price),side)['total']
                    if not quantity:continue
                    cost=money(quantity*price)+fee;cash=money(cash-cost)
                    positions[symbol]={'quantity':quantity,'cost':cost,'last_mark':price,'average_volume20':sig['average_volume20']};buys+=1;profit=None
                else:
                    pos=positions[symbol];basis=pos['cost']*quantity/pos['quantity'];proceeds=money(quantity*price)-fee
                    cash=money(cash+proceeds);profit=money(proceeds-basis);realized+=profit
                    if candidate:candidate.exited(symbol,profit)
                    pos['quantity']-=quantity;pos['cost']-=basis;dp_paid.add(symbol)
                    if not pos['quantity']:del positions[symbol]
                total_fees+=fee;used[symbol]+=quantity;traded.add(symbol)
                trades.append({'timestamp':ts,'observed_bar_start':order['observed_bar_start'],'observation_available_at':ts,
                    'symbol':symbol,'side':side,'quantity':quantity,'requested':requested,'unfilled':requested-quantity,
                    'price':round(price,6),'fee':fee,'realized_pnl':profit,'reason':order['reason'],'model':exit_mode,
                    'indicator_through':sig['as_of'] if sig else None,'prior_minute_volume':prior['volume']})
                if quantity<requested:events['partial_fill_cancelled_remainder']+=1
                if opening_equity is not None and mark_value()<=opening_equity*(1-rules['daily_loss_pct']/100):halted=True
                record_drawdown()
            pending=[]
            # The minute's OHLCV becomes observable only at its end. Decisions are queued for the next open.
            for symbol,pos in positions.items():
                if symbol in bars and bars[symbol]['volume']>0:pos['last_mark']=bars[symbol]['close']
            if opening_equity is not None and mark_value()<=opening_equity*(1-rules['daily_loss_pct']/100):halted=True
            record_drawdown()
            next_stamp=stamp+timedelta(minutes=1)
            if candidate:candidate.observe(n,bars,signals)
            for symbol,pos in positions.items():
                bar=bars.get(symbol)
                if not bar or bar['volume']<=0:continue
                stop,target=range_event(pos,bar)
                if stop and target:
                    ambiguities.append({'timestamp':ts,'symbol':symbol,'action':'Unknown within-minute order; no threshold-price fill'})
                reason=exit_signal('trader',signals.get(symbol),pos,bar['close'])
                if (stop or target) and not reason:events['range_touch_without_close_exit']+=1
                if exit_mode=='range_touch' and (stop or target):
                    reason='Completed minute range touched '+('both stop and target; order unknown' if stop and target else 'stop' if stop else 'target')
                if reason:
                    if n==374:events['exit_signal_at_close_waits_next_session']+=1
                    else:pending.append({'symbol':symbol,'side':'SELL','quantity':pos['quantity'],'reason':reason,'observed_bar_start':ts,'execute_at':next_stamp.isoformat()})
            if n==374:break
            if halted or opening_equity is None or not regime_ok:continue
            if any(s not in bars or bars[s]['volume']<=0 for s in positions):continue
            if not (9,20)<=(next_stamp.hour,next_stamp.minute)<(15,0):continue
            candidates=[]
            for symbol in eligible:
                if symbol in positions or symbol in traded:continue
                bar=bars.get(symbol)
                if not bar or bar['volume']<=0:continue
                result=entry_signal('trader',signals[symbol],bar['close'],next_stamp)
                if result and candidate and not candidate.permits(symbol,bar['close']):
                    events['unconfirmed_or_cooldown_entry']+=1;continue
                if result:candidates.append((result[1],symbol,result[0]))
            for score,symbol,reason in sorted(candidates,key=lambda x:(-x[0],x[1])):
                # Queue candidates; execution repeats all position, risk and price checks.
                budget=min(cash,capital*rules['position_pct']/100)
                quantity=max(0,math.floor((budget-25)/(bars[symbol]['close']*1.025)))
                if quantity:pending.append({'symbol':symbol,'side':'BUY','quantity':quantity,'score':score,'reason':reason,'observed_bar_start':ts,'execute_at':next_stamp.isoformat()})
            if buys>=rules['max_buys_per_day'] or len(positions)>=rules['max_positions']:
                pending=[p for p in pending if p['side']=='SELL']
        daily.append({'session':day,'equity':money(mark_value()),'cash':cash,'open_positions':len(positions),'buy_halt':halted,
                      'stale_closing_symbols':[s for s in positions if s not in bars or bars[s]['volume']<=0]})
    ending=money(mark_value())
    return {'model':'minute-next-open-v1','trader_variant':trader_variant,'exit_mode':exit_mode,'starting_cash':capital,'ending_equity':ending,'net_pnl':money(ending-capital),
            'return_pct':round((ending/capital-1)*100,4),'fees':money(total_fees),'realized_pnl':money(realized),
            'unrealized_pnl':money(sum(p['quantity']*p['last_mark']-p['cost'] for p in positions.values())),
            'max_observed_drawdown_pct':round(drawdown,4),'positions':positions,'trades':trades,'daily':daily,
            'events':dict(events),'ambiguous_minutes':ambiguities,'slippage_bps':slippage_bps}


def run(directory):
    directory=Path(directory);plan=json.loads((directory/'plan.json').read_text());minutes,metadata=read_minutes(directory,plan)
    cov=coverage(plan,minutes,metadata)
    result={'created_at':datetime.now(timezone.utc).isoformat(),'start':plan['start'],'end':plan['end'],'days':plan['days'],
            'daily_snapshot_sha256':plan['input_sha256'],'coverage':cov,'raw_sources':metadata,'results':{}}
    for mode in ('minute_close','range_touch'):
        print('Simulating '+mode,flush=True);result['results'][mode]=simulate(plan,minutes,mode)
        print(json.dumps({k:v for k,v in result['results'][mode].items() if k not in ('positions','trades','daily','ambiguous_minutes')}),flush=True)
    result['comparison_status']='PROVISIONAL: historical coverage limitations' if cov['missing_downloads'] or cov['gaps'] or cov['daily_ohl_mismatches'] else 'Complete one-minute coverage for the requested candidates'
    atomic_json(directory/'result.json',result)
    return result


def closed_positions(trades):
    active={};closed=[]
    for t in trades:
        symbol=t['symbol']
        if t['side']=='BUY':active[symbol]={'symbol':symbol,'entry':t['timestamp'],'remaining':t['quantity'],'pnl':0}
        else:
            position=active[symbol];position['remaining']-=t['quantity'];position['pnl']+=t['realized_pnl']
            position['exit']=t['timestamp'];position['reason']=t['reason']
            if not position['remaining']:closed.append(active.pop(symbol))
    return closed


def render(result):
    esc=html.escape;cov=result['coverage'];cards=[];trades=[];daily=[]
    diagnostics=[];closed=closed_positions(result['results']['minute_close']['trades'])
    winners=sum(p['pnl']>0 for p in closed);losers=sum(p['pnl']<0 for p in closed)
    loss_rows=''.join('<tr>'+''.join('<td>'+esc(str(v))+'</td>' for v in [p['symbol'],p['entry'],p['exit'],f"₹{p['pnl']:,.2f}",p['reason']])+'</tr>' for p in sorted(closed,key=lambda p:p['pnl'])[:5] if p['pnl']<0)
    for mode,a in result['results'].items():
        title='Minute-close observations' if mode=='minute_close' else 'Range-touch sensitivity'
        buys=sum(t['side']=='BUY' for t in a['trades']);sells=sum(t['side']=='SELL' for t in a['trades'])
        color=' loss' if a['net_pnl']<0 else ''
        cards.append(f"<section><h2>{title}</h2><strong class='replay-value{color}'>₹{a['net_pnl']:,.2f}</strong><p>{a['return_pct']:.2f}% after modeled costs<br>Realized ₹{a['realized_pnl']:,.2f}<br>Unrealized ₹{a['unrealized_pnl']:,.2f}<br>Fees ₹{a['fees']:,.2f}<br>{buys} buy fills · {sells} sell fills · {len(a['positions'])} open positions<br>Observed drawdown {a['max_observed_drawdown_pct']:.2f}%<br>{len(a['ambiguous_minutes'])} ambiguous stop/target minutes</p></section>")
        for d in a['daily']:daily.append(f"<tr><td>{title}</td><td>{d['session']}</td><td>₹{d['equity']:,.2f}</td><td>{d['open_positions']}</td><td>{d['buy_halt']}</td><td>{esc(', '.join(d['stale_closing_symbols']))}</td></tr>")
        for t in a['trades']:
            trades.append('<tr>'+''.join('<td>'+esc(str(v))+'</td>' for v in [title,t['timestamp'],t['symbol'],t['side'],t['quantity'],t['unfilled'],f"₹{t['price']:,.2f}",f"₹{t['fee']:,.2f}",f"₹{t['realized_pnl']:,.2f}" if t['realized_pnl'] is not None else '—',t['observed_bar_start'],t['reason']])+'</tr>')
        diagnostics.append('<h3>'+title+'</h3><ul>'+''.join('<li>'+esc(k.replace('_',' '))+': '+str(v)+'</li>' for k,v in a['events'].items())+'</ul>')
    gap_rows=''.join(f"<tr><td>{esc(g['symbol'])}</td><td>{g['session']}</td><td>{g['minutes']} / 375</td></tr>" for g in cov['gaps'])
    mismatch_rows=''.join(f"<li>{esc(g['symbol'])} · {g['session']}: {esc(str(g['difference_pct']))}%</li>" for g in cov['daily_ohl_mismatches'])
    baseline_note=('The earlier daily-open proxy showed −₹34,923.76 for these completed sessions. '
                   if len(result['days'])==18 and result['days'][0]=='2026-09-15' and result['days'][-1]=='2026-10-09' else '')
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MarketDesk · Intraday Trader replay</title><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/replay.css"></head><body><main>
<a href="/trader-review.html">← Trader loss review</a> · <a href="/trader-comparison.html">Trader before and after →</a> · <a href="/">Dashboard</a><p class="eyebrow">PAPER ONLY · UPSTOX ONE-MINUTE HISTORY</p><h1>Trader, replayed minute by minute</h1><p>{result['days'][0]} to {result['days'][-1]} · {len(result['days'])} sessions · ₹10 lakh starting cash per scenario</p>
<div class="note"><strong>{esc(result['comparison_status'])}.</strong> This improves time ordering, but one-minute candles still cannot reproduce 15-second monitoring, historical bid/ask depth or exact fills. Running Trader and Investor portfolios are untouched.</div>
<div class="replay-summary">{''.join(cards)}</div><h2>Data coverage</h2><p>{cov['downloaded_symbols']} / {cov['requested_symbols']} candidate instruments downloaded · {cov['minute_rows']:,} cached minute candles · {cov['eligible_minute_coverage_pct']}% of expected eligible minutes present.<br>{len(cov['gaps'])} eligible stock-sessions have missing minutes; {len(cov['daily_ohl_mismatches'])} have daily open/high/low differences above tolerance; {cov['eligible_zero_volume_minutes']:,} present minutes report zero volume and cannot fill. Missing prices are never interpolated.</p>
<details><summary>Inspect missing minutes and daily-price differences</summary><p>Missing downloads: {esc(', '.join(cov['missing_downloads']) or 'None')}. Missing bars can reflect no trades or unavailable provider history; we cannot distinguish those cases from the response alone.</p><div class="table-scroll"><table><thead><tr><th>Stock</th><th>Session</th><th>Minutes present</th></tr></thead><tbody>{gap_rows}</tbody></table></div><ul>{mismatch_rows}</ul></details>
<p>{baseline_note}This replay changes observation times, liquidity assumptions and closing marks together; any difference is not evidence that a new strategy is profitable.</p>
<h2>Minute-close trade outcomes</h2><p>{len(closed)} fully closed positions: {winners} profitable, {losers} losing, {len(closed)-winners-losers} flat. Partial sell fills are grouped back into their entry position. These are simulated outcomes after modeled costs; open positions remain marked separately.</p><div class="table-scroll"><table><thead><tr><th>Largest losses</th><th>Entry IST</th><th>Final exit IST</th><th>Net realized</th><th>Final exit reason</th></tr></thead><tbody>{loss_rows}</tbody></table></div>
<h2>What changed from the daily test</h2><ul><li>All stocks that could qualify from prior daily inputs are considered. Daily indicators use only completed earlier sessions. No selection based on the old replay's winners or losers.</li><li>A completed minute generates a signal. A fill is attempted at the next consecutive minute's open with 10 bps adverse slippage, entry revalidation and estimated delivery charges. No same-bar close fills or guaranteed stop prices.</li><li>Primary scenario observes minute closes. The sensitivity scenario reacts after a completed minute touches a stop or target. If both are touched, the order is explicitly unknown; the fill still uses the next open. These are alternative assumptions, not best/worst bounds.</li><li>Fill size is capped at 10% of the preceding completed minute's volume and 1% of prior 20-session average daily volume per stock/day. These are liquidity assumptions, not order-book evidence. Unfilled quantities are cancelled; held remainders are checked again.</li><li>Entries execute 09:20–14:59 IST. Exits operate through the normal session, with overnight holdings and gap exposure. A final-minute signal waits for a fresh observation next session. Stale held prices block buys; the 2% daily buy halt persists for the session.</li><li>Current-minute volume is used only to identify whether any trade occurred, never to increase fill capacity. Missing or zero-trade minutes cannot produce synthetic fills. Provider timestamps denote candle start; close/high/low become known one minute later.</li><li>Current universe, possible later historical revisions, corporate actions and unmodeled taxes/settlement remain limitations. This is an execution-model test, not proof of a profitable strategy.</li></ul>
<h2>Daily values</h2><div class="table-scroll"><table><thead><tr><th>Scenario</th><th>Session</th><th>Equity</th><th>Positions</th><th>Buy halt</th><th>Stale closing marks</th></tr></thead><tbody>{''.join(daily)}</tbody></table></div>
<details><summary>Replay execution diagnostics</summary>{''.join(diagnostics)}</details>
<h2>All simulated fills</h2><div class="table-scroll"><table><thead><tr><th>Scenario</th><th>Fill time IST</th><th>Stock</th><th>Side</th><th>Filled</th><th>Cancelled</th><th>Price</th><th>Fees</th><th>Realized P&amp;L</th><th>Signal candle start</th><th>Reason</th></tr></thead><tbody>{''.join(trades)}</tbody></table></div>
<p>Source: <a href="https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/">Upstox Historical Candle V3</a>. Raw responses, hashes, coverage details and ledgers remain locally in data/replays/intraday. Generated {esc(result['created_at'])}.</p></main></body></html>'''


if __name__=='__main__':
    import argparse
    from .storage import ROOT,DEFAULT_DB
    from .intraday_data import prepare,download
    parser=argparse.ArgumentParser(description='Isolated, paper-only Upstox one-minute replay')
    parser.add_argument('--fetch',action='store_true',help='Prepare a new snapshot and download missing cached raw responses')
    parser.add_argument('--start')
    parser.add_argument('--end')
    parser.add_argument('--directory',type=Path,default=ROOT/'data/replays/intraday')
    args=parser.parse_args()
    if args.fetch:
        if not args.start or not args.end:parser.error('--fetch requires --start and --end')
        download(prepare(DEFAULT_DB,args.start,args.end,args.directory),args.directory)
    elif args.start or args.end:parser.error('Dates require --fetch; otherwise the frozen saved plan is replayed')
    report=run(args.directory)
    (args.directory/'result.html').write_text(render(report))
    (ROOT/'web/intraday-replay.html').write_text(render(report))
    print('Saved local paper report: /intraday-replay.html')
