"""Read-only entry-strategy attribution from the full paper ledger.

Sells inherit the buying cohort, including Claude exits of legacy holdings.
Mixed manual/automatic holdings use proportional average-cost allocation, matching
paper.py rather than silently assigning a mixed sale to the selling strategy.
"""
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from .execution import charges
from .storage import get_state
from .order_audit import annotate,legacy_identity

D = lambda x: Decimal(str(x))
CENT = Decimal('.01')
IST = timezone(timedelta(hours=5, minutes=30))
LABELS = {'claude': 'Claude entries', 'legacy': 'Before Claude · old strategy', 'other': 'Other / manual entries'}

def rounded(x):
    return x.quantize(CENT, rounding=ROUND_HALF_UP)

def attribution(trades, positions, selected_at=None, clock=None, by_version=False):
    clock = clock or datetime.now(timezone.utc)
    selected = datetime.fromisoformat(selected_at) if selected_at else None
    labels={t.get('strategy_version_id',legacy_identity(t)[0]):t.get('strategy_label',legacy_identity(t)[1]) for t in trades if t['side']=='BUY'} if by_version else LABELS
    groups = {k:dict(key=k,label=v,realized=D(0),unrealized=D(0),fees=D(0),exit_fees=D(0),buys=0,sells=0,open_symbols=[]) for k,v in labels.items()}
    pools = defaultdict(dict); cash_flow=D(0); sold_today=set(); day=clock.astimezone(IST).date()
    for t in trades:
        symbol=t['symbol'];q=D(t['quantity']);value=rounded(q*D(t['price']));fee=D(t['fee'])
        stamp=datetime.fromisoformat(t['created_at'])
        if t['side']=='BUY':
            is_claude=(t.get('source')=='automatic' and t.get('request_id','').startswith('claude-') and t.get('reason','').startswith(('Original Claude V2 completed breakout;','Claude V2 (Nifty 500 filter off) completed breakout;','Original Claude V2 live breakout above ','Claude V2 (Nifty 500 filter off) live breakout above ')))
            key=t.get('strategy_version_id',legacy_identity(t)[0]) if by_version else 'claude' if is_claude else 'legacy' if selected is None or stamp<selected else 'other'
            pool=pools[symbol].setdefault(key,dict(qty=D(0),cost=D(0)))
            pool['qty']+=q;pool['cost']+=value+fee
            groups[key]['fees']+=fee;groups[key]['buys']+=1;cash_flow-=value+fee
        elif t['side']=='SELL':
            held=sum((p['qty'] for p in pools[symbol].values()),D(0))
            basis=sum((p['cost'] for p in pools[symbol].values()),D(0))
            if q-held>D('0.00000001') or held<=0:raise ValueError('A sale has no matching purchase history.')
            if abs(q-held)<=D('0.00000001'):q=held
            remaining_cost=rounded(basis*(held-q)/held)
            removed_cost=basis-remaining_cost
            for key,pool in pools[symbol].items():
                if pool['qty']<=0:continue
                share=pool['qty']/held
                cost_share=pool['cost']/basis if basis else share
                allocated_cost=removed_cost*cost_share
                groups[key]['realized']+=(value-fee)*share-allocated_cost
                groups[key]['fees']+=fee*share;groups[key]['sells']+=1
                pool['qty']-=q*share;pool['cost']-=allocated_cost
            cash_flow+=value-fee
            if stamp.astimezone(IST).date()==day:sold_today.add(symbol)
        else:raise ValueError('Unknown ledger side.')
    marks={p['symbol']:p for p in positions}
    origins={};stale=[];mark_times=[]
    for symbol,cohorts in pools.items():
        qty=sum((p['qty'] for p in cohorts.values()),D(0))
        if qty<=D('0.00000001'):continue
        p=marks.get(symbol)
        if not p or abs(qty-D(p['quantity']))>D('0.00000001'):raise ValueError('Holdings and purchase history disagree.')
        basis=sum((x['cost'] for x in cohorts.values()),D(0))
        if abs(basis-D(p['cost']))>D('.011'):raise ValueError('Cost basis and paper ledger disagree.')
        active=[k for k,pool in cohorts.items() if pool['qty']>D('0.00000001')]
        origins[symbol]=' + '.join(labels[k] for k in active)
        mark=p.get('mark');timestamp=p.get('mark_time')
        if timestamp:mark_times.append(timestamp)
        try:fresh=mark is not None and p.get('mark_source')=='last_trade' and 0<=(clock-datetime.fromisoformat(timestamp)).total_seconds()<=30
        except (TypeError,ValueError):fresh=False
        if not fresh:stale.append(symbol)
        exit_fee=D(charges(float(rounded(qty*D(mark))),'SELL',symbol not in sold_today)['total']) if mark is not None else None
        for key in active:
            pool=cohorts[key];g=groups[key];g['open_symbols'].append(symbol)
            if mark is None:g['unrealized']=None;g['exit_fees']=None
            else:
                if g['unrealized'] is not None:g['unrealized']+=pool['qty']*D(mark)-pool['cost']
                if g['exit_fees'] is not None:g['exit_fees']+=exit_fee*pool['qty']/qty
    if set(marks)!=set(origins):raise ValueError('A holding has no matching purchase history.')
    result=[]
    for key,g in groups.items():
        net=g['realized']+g['unrealized'] if g['unrealized'] is not None else None
        result.append(dict(key=key,label=g['label'],buy_fills=g['buys'],sell_fills=g['sells'],open_symbols=g['open_symbols'],
            realized_net=float(rounded(g['realized'])),unrealized_net=float(rounded(g['unrealized'])) if g['unrealized'] is not None else None,
            net_after_charged_costs=float(rounded(net)) if net is not None else None,
            fees_charged=float(rounded(g['fees'])),estimated_exit_fees=float(rounded(g['exit_fees'])) if g['exit_fees'] is not None else None,
            net_after_estimated_exit_fees=float(rounded(net-g['exit_fees'])) if net is not None and g['exit_fees'] is not None else None))
    return dict(status='ok',selected_at=selected_at,calculated_at=clock.isoformat(),cohorts=result,holding_origins=origins,
        stale_symbols=stale,oldest_mark=min(mark_times) if mark_times else None,expected_cash_change=float(rounded(cash_flow)),
        attribution='By entry strategy. Partial or mixed sales use proportional average cost. Full recorded history, not the latest 200 fills.',
        cost_basis='Booked fees and execution slippage are already included. Estimated selling fees use current marks; future spread, slippage and partial-fill costs are not included.')

def trader_performance(db,account,clock=None):
    cfg=get_state(db,'auto_config',{}).get('trader',{})
    trades=annotate(db,[dict(r) for r in db.execute("SELECT * FROM trades WHERE portfolio='trader' ORDER BY rowid")])
    if cfg.get('strategy')!='claude-original-v2' and not any(t['request_id'].startswith('claude-') for t in trades):return None
    try:
        result=attribution(trades,account['positions'],cfg.get('strategy_selected_at'),clock,by_version=True)
        if abs(D(account['initial_cash'])+D(result['expected_cash_change'])-D(account['cash']))>D('.011'):
            raise ValueError('Cash does not reconcile to the recorded trade history.')
        return result
    except (ValueError,KeyError,TypeError,ArithmeticError) as error:
        return dict(status='unavailable',message='Strategy attribution unavailable: '+str(error))
