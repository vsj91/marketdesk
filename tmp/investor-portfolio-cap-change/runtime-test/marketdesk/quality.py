"""Local, conservative eligibility audit. No interpolation or guessed financial values."""
import collections,json,math
from datetime import datetime,timedelta,timezone
from .storage import connect,get_state,set_state,number
IST=timezone(timedelta(hours=5,minutes=30))
FIELDS=('profit_before_tax','finance_costs','equity','total_borrowings','total_lease_liabilities','cash_and_cash_equivalents')

def financial_reasons(f,observed,clock):
    reasons=[];annual=f.get('latest') or {};summary=f.get('summary') or {}
    if f.get('status')!='COMPLETE' or any(number(annual.get(k)) is None for k in FIELDS):reasons.append('Annual financial inputs incomplete')
    if f.get('source_conflicts'):reasons.append('Financial sources disagree')
    if f.get('annual_conflict_recheck_required'):reasons.append('Annual-report correction requires re-verification')
    try:
        period=datetime.strptime(annual['period'],'%b %Y').date()
        if not 0<=(clock.astimezone(IST).date()-period).days<=550:reasons.append('Annual period is stale or future dated')
    except (KeyError,ValueError,TypeError):reasons.append('Annual period unavailable')
    try:
        if not 0<=(clock-datetime.fromisoformat(observed)).total_seconds()<=30*86400:reasons.append('Financial observation older than 30 days or future dated')
    except (TypeError,ValueError):reasons.append('Financial observation time unavailable')
    if annual.get('statement_type') not in ('consolidated','standalone') or not annual.get('units_in'):reasons.append('Annual basis or units unavailable')
    if (any(summary.get(k)!=annual.get('period') for k in ('revenue_period','net_profit_period')) or summary.get('statement_type')!=annual.get('statement_type') or summary.get('units')!=annual.get('units_in') or summary.get('frequency') not in ('yearly','annual')):reasons.append('Growth inputs do not match annual period, basis and units')
    if any(number(summary.get(k)) is None for k in ('revenue_growth_yoy','net_profit_growth_yoy')):reasons.append('Comparable annual growth inputs missing')
    if any(number(annual.get(k)) is not None and number(annual[k])<0 for k in FIELDS[1:] if k!='equity'):reasons.append('Negative finance, debt or cash input needs review')
    return reasons

def source_reasons(f,times,clock):
    nse=f.get('selected_source')=='nse_audited_filing';summary=f.get('summary') or {}
    required=[] if nse else ['income_statement','balance_sheet','cash_flow']
    growth=[key for key in ('revenue','net_profit') if summary.get(key+'_growth_yoy') is not None]
    if nse and any(summary.get('growth_sources',{}).get(key) not in ('nse_audited_filing','company_annual_report') for key in growth):required.append('income_statement')
    observations=[times.get(k) for k in required]
    for evidence in (f.get('latest') or {}).get('sources',{}).values():
        if isinstance(evidence,dict) and evidence.get('source')=='company_annual_report':observations.append(evidence.get('observed_at'))
    if f.get('selected_source')=='nse_audited_filing':observations.append((f.get('latest') or {}).get('observed_at') or (f.get('fallbacks') or {}).get('nse',{}).get('observed_at'))
    if nse:
        for key in growth:
            if summary.get('growth_sources',{}).get(key) in ('nse_audited_filing','company_annual_report'):observations.extend(summary.get('growth_observed_at',{}).get(key) or [None])
    for stamp in observations:
        try:
            if not 0<=(clock-datetime.fromisoformat(stamp)).total_seconds()<=30*86400:return ['Underlying financial evidence older than 30 days or future dated']
        except (TypeError,ValueError):return ['Underlying financial evidence timestamp missing']
    return []

def inspect_history(rows,sessions,clock):
    reasons=[];dates=[r['ts'][:10] for r in rows];today=clock.astimezone(IST).date()
    if len(rows)<200:reasons.append('Fewer than 200 completed daily candles')
    if not rows:return reasons+['No daily candles']
    if len(set(dates))!=len(dates):reasons.append('Duplicate daily sessions')
    if any(d>=today.isoformat() for d in dates):reasons.append('Current or future daily candle')
    if not 1<=(today-datetime.fromisoformat(dates[-1]).date()).days<=7:reasons.append('Daily history is stale')
    if sessions and dates[-1]!=sessions[-1]:reasons.append('Latest observed market session missing')
    expected=[d for d in sessions[-200:] if dates[0]<=d<=dates[-1]]
    missing=sorted(set(expected)-set(dates))
    if missing:reasons.append(f'{len(missing)} gaps against locally observed market sessions (first: {missing[0]})')
    if any(not all(number(r[k]) is not None for k in ('open','high','low','close','volume')) or min(r['open'],r['high'],r['low'],r['close'])<=0 or r['volume']<0 or r['high']<max(r['open'],r['close'],r['low']) or r['low']>min(r['open'],r['close']) for r in rows):reasons.append('Invalid OHLCV values')
    jumps=[b['ts'][:10] for a,b in zip(rows,rows[1:]) if a['close']>0 and (abs(b['open']/a['close']-1)>.25 or abs(b['close']/a['close']-1)>.25)]
    if jumps:reasons.append(f'Unreconciled price discontinuity / possible corporate action ({jumps[-1]})')
    if any(r['volume']<=0 for r in rows[-20:]):reasons.append('Zero volume in recent 20 sessions')
    return reasons

def init_quality(path):
    with connect(path) as db:db.execute('CREATE TABLE IF NOT EXISTS quality(symbol TEXT PRIMARY KEY,report TEXT NOT NULL,checked_at TEXT NOT NULL)')

def audit(path,clock=None):
    clock=clock or datetime.now(timezone.utc);init_quality(path)
    with connect(path) as db:
        db.execute('BEGIN')
        instruments=[r['symbol'] for r in db.execute('SELECT symbol FROM instruments')]
        histories=collections.defaultdict(lambda:collections.deque(maxlen=201));counts=collections.Counter()
        for r in db.execute('SELECT symbol,ts,open,high,low,close,volume FROM candles ORDER BY symbol,ts'):
            histories[r['symbol']].append(dict(r));counts[r['ts'][:10]]+=1
        # A local consensus calendar, explicitly not an independently verified NSE calendar.
        threshold=max(1,len(instruments)//2);sessions=sorted(d for d,n in counts.items() if n>=threshold and d<clock.astimezone(IST).date().isoformat())
        funds={r['symbol']:(json.loads(r['details']),r['observed_at']) for r in db.execute('SELECT * FROM fundamentals')}
        evidence=collections.defaultdict(dict)
        for r in db.execute('SELECT symbol,endpoint,observed_at FROM payloads'):evidence[r['symbol']][r['endpoint']]=r['observed_at']
        bad={r[0] for r in db.execute("SELECT DISTINCT symbol FROM issues WHERE kind IN ('candle','identity')")}
    reports={};totals=collections.Counter()
    for symbol in instruments:
        reasons=inspect_history(list(histories[symbol]),sessions,clock)
        if symbol in bad:reasons.append('Unresolved source candle or identity issue')
        f,observed=funds.get(symbol,({},None));fr=financial_reasons(f,observed,clock)+source_reasons(f,evidence[symbol],clock)
        report={'trader_ready':not reasons,'investor_ready':not(reasons or fr),'price_reasons':reasons,'financial_reasons':fr,'as_of':histories[symbol][-1]['ts'] if histories[symbol] else None,'corporate_actions':'not independently reconciled'}
        reports[symbol]=report
        for r in set(reasons+fr):totals['Missing locally observed sessions' if 'gaps against' in r else 'Possible corporate action / price discontinuity' if 'discontinuity' in r else r]+=1
    summary={'checked_at':clock.isoformat(),'stocks':len(instruments),'trader_ready':sum(r['trader_ready'] for r in reports.values()),'investor_ready':sum(r['investor_ready'] for r in reports.values()),'latest_observed_session':sessions[-1] if sessions else None,'reasons':dict(totals),'calendar_basis':'Majority of local instrument histories; not an official exchange calendar','paper_validated':False,'live_execution_ready':False}
    with connect(path) as db:
        db.executemany('INSERT OR REPLACE INTO quality VALUES(?,?,?)',[(s,json.dumps(r),clock.isoformat()) for s,r in reports.items()]);set_state(db,'quality_summary',summary);set_state(db,'quality_sessions',sessions[-201:])
    return summary

def require_buy_quality(db,symbol,portfolio,clock):
    row=db.execute('SELECT * FROM quality WHERE symbol=?',(symbol,)).fetchone()
    if not row or not 0<=(clock-datetime.fromisoformat(row['checked_at'])).total_seconds()<=600:raise ValueError('Data check missing or stale; wait for the local audit.')
    report=json.loads(row['report']);reasons=list(report['price_reasons'])
    current=list(reversed([dict(r) for r in db.execute('SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 201',(symbol,))]))
    reasons+=inspect_history(current,get_state(db,'quality_sessions',[]),clock)
    # Re-check financial eligibility against the current record inside the fill transaction.
    if portfolio=='investor':
        f=db.execute('SELECT * FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()
        details=json.loads(f['details']) if f else {}
        reasons+=financial_reasons(details,f['observed_at'] if f else None,clock)
        times={r['endpoint']:r['observed_at'] for r in db.execute('SELECT endpoint,observed_at FROM payloads WHERE symbol=?',(symbol,))}
        reasons+=source_reasons(details,times,clock)
    if db.execute("SELECT 1 FROM issues WHERE symbol=? AND kind IN ('candle','identity')",(symbol,)).fetchone():reasons.append('Unresolved source validation issue')
    if reasons:raise ValueError('New paper buy blocked: '+'; '.join(dict.fromkeys(reasons)))
