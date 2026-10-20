"""Read-only adapter: MarketDesk database -> point-in-time snapshots for the quant baskets.

Sources and their limits (each is a known approximation, not hidden):
  candles            daily OHLCV for the working universe; quality-flagged symbols are excluded
  claude_benchmark   Nifty 500 index closes (regime detection and benchmark returns); closes only
  fundamentals       the latest annual statement per company (EBIT = PBT + finance costs, equity,
                     borrowings, leases, cash, net profit), used only from period end + FILING_LAG_DAYS
                     so a replay cannot see a report before it was filed; earlier sessions get none
  payloads/cash_flow operating cash flow for the same annual period (accruals filter)
  AMFI state         six-month average market cap and Large/Mid/Small label; the cap is rolled
                     forward/back by the stock's price against its average close over the AMFI window
  not available      dividend yield (the value sleeve uses earnings yield and book/price only),
                     sector indices (stat-arb Z-scores use price), VIX (realized index vol is used)
  price_reconciliation NSE-verified ordinary large moves (proofs last 24 hours and cover the latest
                     201 candles; an older or unverified >25% move still excludes the stock)"""
import bisect,collections,json,sqlite3
from datetime import date,datetime,timedelta,timezone
from ..history_rules import is_financial
from ..sectors import sector_group
from ..working_universe import symbol_filter
from ..price_reconciliation import confirmed,jumps
from .calc import finite

FILING_LAG_DAYS=60
LOOKBACK_BARS=320

def _ro(path):
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True,timeout=30);db.row_factory=sqlite3.Row;return db

def _operating_cash(payload,period):
    try:data=json.loads(payload)['data']
    except (ValueError,KeyError,TypeError):return None
    for row in data.get('cash_flow') or []:
        if row.get('category')=='operating':
            for h in row.get('history') or []:
                if h.get('period')==period and finite(h.get('value')):return float(h['value'])
    return None

def load_market(path):
    """Everything the snapshots need, loaded once."""
    db=_ro(path)
    try:
        bad={r[0] for r in db.execute("SELECT symbol FROM quality WHERE json_extract(report,'$.trader_ready')=0")}
        bad.update(r[0] for r in db.execute("SELECT DISTINCT symbol FROM issues WHERE kind IN ('candle','identity')"))
        bars=collections.defaultdict(list)
        for r in db.execute('SELECT symbol,ts,open,high,low,close,volume FROM candles WHERE '+symbol_filter(db)+' ORDER BY symbol,ts'):
            if r['symbol'] in bad:continue
            bars[r['symbol']].append(dict(day=r['ts'][:10],open=r['open'],high=r['high'],low=r['low'],close=r['close'],volume=r['volume']))
        inst={r['symbol']:dict(r) for r in db.execute('SELECT symbol,isin,sector FROM instruments')}
        amfi=json.loads((db.execute("SELECT value FROM state WHERE key='amfi_classification'").fetchone() or ['{}'])[0])
        by_isin=amfi.get('records',{})
        size={s:by_isin[i['isin']]['label'] for s,i in inst.items() if i.get('isin') in by_isin}
        caps={s:by_isin[i['isin']].get('average_market_cap_crore') for s,i in inst.items() if i.get('isin') in by_isin}
        cash_flow={r['symbol']:r['payload'] for r in db.execute("SELECT symbol,payload FROM payloads WHERE endpoint='cash_flow'")}
        funds={}
        for r in db.execute('SELECT symbol,details FROM fundamentals'):
            d=json.loads(r['details']);latest=d.get('latest') or {}
            if not latest.get('period_end'):continue
            sector=d.get('sector') or (inst.get(r['symbol']) or {}).get('sector')
            np_=latest.get('net_profit') if finite(latest.get('net_profit')) else (d.get('summary') or {}).get('net_profit')
            pbt,fc=latest.get('profit_before_tax'),latest.get('finance_costs')
            debt=sum(x for x in (latest.get('total_borrowings'),latest.get('total_lease_liabilities')) if finite(x)) if finite(latest.get('total_borrowings')) else None
            funds[r['symbol']]=dict(period=latest.get('period'),period_end=latest['period_end'],
                available_from=(date.fromisoformat(latest['period_end'])+timedelta(days=FILING_LAG_DAYS)).isoformat(),
                ebit=pbt+fc if finite(pbt) and finite(fc) else None,equity=latest.get('equity'),net_profit=np_,total_debt=debt,
                cash=latest.get('cash_and_cash_equivalents'),ocf=_operating_cash(cash_flow.get(r['symbol']),latest.get('period')),
                dividend_yield=None,financial=is_financial(sector or '',r['symbol']))
        bench=[(r['day'],r['close']) for r in db.execute('SELECT day,close FROM claude_benchmark ORDER BY day')]
        # Large moves NSE verified as ordinary market moves (the proofs the data audit accepts) are not breaks.
        clock=datetime.now(timezone.utc);moves={}
        for s,rows in bars.items():
            recent=[dict(b,ts=b['day']) for b in rows[-LOOKBACK_BARS:]]
            if jumps(recent):moves[s]=confirmed(db,s,recent,clock)
    finally:db.close()
    period=amfi.get('period')
    anchor={}
    if period:
        start=(date.fromisoformat(period)-timedelta(days=182)).isoformat()
        for s,rows in bars.items():
            w=[b['close'] for b in rows if start<=b['day']<=period]
            if len(w)>=60:anchor[s]=sum(w)/len(w)
    sectors={s:sector_group(i.get('sector'),symbol=s) for s,i in inst.items()}
    return dict(bars=dict(bars),days={s:[b['day'] for b in rows] for s,rows in bars.items()},sectors=sectors,size=size,caps=caps,
        cap_anchor=anchor,fundamentals=funds,benchmark=bench,amfi_period=period,confirmed=moves)

def sessions(market):
    return sorted({d for days in market['days'].values() for d in days})

def snapshot(market,day,lookback=LOOKBACK_BARS):
    """Completed data up to and including `day`; fundamentals only once filed."""
    bars={};funds={}
    for s,rows in market['bars'].items():
        n=bisect.bisect_right(market['days'][s],day)
        if n:bars[s]=rows[max(0,n-lookback):n]
    for s,f in market['fundamentals'].items():
        if s not in bars or day<f['available_from']:continue
        cap=market['caps'].get(s);anchor=market['cap_anchor'].get(s)
        if finite(cap) and anchor:cap=cap*bars[s][-1]['close']/anchor
        ev=cap+(f['total_debt'] or 0)-(f['cash'] or 0) if finite(cap) and f['total_debt'] is not None else None
        funds[s]=dict(f,market_cap=cap,ev=ev)
    bdays=[d for d,_ in market['benchmark']];n=bisect.bisect_right(bdays,day)
    bench=[c for _,c in market['benchmark'][max(0,n-lookback):n]]
    return dict(day=day,bars=bars,fundamentals=funds,sectors=market['sectors'],size=market['size'],
        benchmark=bench,benchmark_day=bdays[n-1] if n else None,confirmed=market.get('confirmed',{}))
