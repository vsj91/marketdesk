from .working_universe import symbol_filter
from .history_rules import MIN_SESSIONS,tier,effective_rows,is_financial
"""Local, conservative eligibility audit. No interpolation or guessed financial values."""
import collections,json,math
from datetime import datetime,timedelta,timezone
from .storage import connect,get_state,set_state,number
from .liability_bound import attach,bounded_debt_pass
from .bank_inputs import not_applicable,SOURCE as BANK_SOURCE,INSURER_SOURCE
REIT_SOURCE='nse_audited_reit_filing'
IST=timezone(timedelta(hours=5,minutes=30))
FIELDS=('profit_before_tax','finance_costs','equity','total_borrowings','total_lease_liabilities','cash_and_cash_equivalents')

GROWTH_INPUT_SOURCES=('upstox','nse_audited_filing','company_annual_report',BANK_SOURCE,INSURER_SOURCE)

def undefined_growth(summary,key):
    """Both years' figures are on file but the prior base is zero or negative, so a growth % is undefined.

    The current value must be the selected summary value and the prior period exactly
    one year earlier, from one named source. The growth % itself stays None."""
    g=((summary or {}).get('growth_inputs') or {}).get(key) or {}
    a,b,selected=number(g.get('current')),number(g.get('prior')),number((summary or {}).get(key))
    if None in (a,b,selected) or b>0 or g.get('source') not in GROWTH_INPUT_SOURCES or abs(a-selected)>max(.015,abs(selected)*.00002):return False
    try:
        current=datetime.strptime(summary[key+'_period'],'%b %Y');prior=datetime.strptime(g['prior_period'],'%b %Y')
    except (KeyError,TypeError,ValueError):return False
    return (prior.year+1,prior.month)==(current.year,current.month)

def financial_reasons(f,observed,clock):
    reasons=[];annual=f.get('latest') or {};summary=f.get('summary') or {}
    # A line the audited filing's reporting framework does not carry (bank leases) is not missing.
    na=not_applicable(f)
    status_ok=f.get('status')=='COMPLETE' or bool(na and f.get('sector') and f.get('status')=='PARTIAL' and set(f.get('missing_fields') or [])<=na)
    if (not status_ok or any(number(annual.get(k)) is None and k not in na for k in FIELDS)) and not bounded_debt_pass(f):reasons.append('Annual financial inputs incomplete')
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
    if any(number(summary.get(k+'_growth_yoy')) is None and not undefined_growth(summary,k) for k in ('revenue','net_profit')):reasons.append('Comparable annual growth inputs missing')
    if any(number(annual.get(k)) is not None and number(annual[k])<0 for k in FIELDS[1:] if k!='equity'):reasons.append('Negative finance, debt or cash input needs review')
    return reasons

def source_reasons(f,times,clock):
    nse=f.get('selected_source')=='nse_audited_filing';summary=f.get('summary') or {}
    required=[] if nse else ['income_statement','balance_sheet','cash_flow']
    growth=[key for key in ('revenue','net_profit') if summary.get(key+'_growth_yoy') is not None]
    if nse and any(summary.get('growth_sources',{}).get(key) not in ('nse_audited_filing','company_annual_report') for key in growth):required.append('income_statement')
    # Undefined-growth inputs taken from the provider still need a fresh provider payload.
    if nse and any(((summary.get('growth_inputs') or {}).get(key) or {}).get('source')=='upstox' for key in ('revenue','net_profit') if key not in growth):required.append('income_statement')
    observations=[times.get(k) for k in required]
    for evidence in list((f.get('latest') or {}).get('sources',{}).values())+list(((f.get('latest') or {}).get('not_applicable') or {}).values()):
        if isinstance(evidence,dict) and evidence.get('source') in ('company_annual_report',BANK_SOURCE,INSURER_SOURCE,REIT_SOURCE):observations.append(evidence.get('observed_at'))
    if f.get('selected_source')=='nse_audited_filing':observations.append((f.get('latest') or {}).get('observed_at') or (f.get('fallbacks') or {}).get('nse',{}).get('observed_at'))
    if nse:
        for key in growth:
            if summary.get('growth_sources',{}).get(key) in ('nse_audited_filing','company_annual_report'):observations.extend(summary.get('growth_observed_at',{}).get(key) or [None])
    for key,entry in (summary.get('growth_inputs') or {}).items():
        if key not in growth and isinstance(entry,dict) and entry.get('source') in ('nse_audited_filing','company_annual_report'):observations.extend(entry.get('observed_at') or [None])
    for stamp in observations:
        try:
            if not 0<=(clock-datetime.fromisoformat(stamp)).total_seconds()<=30*86400:return ['Underlying financial evidence older than 30 days or future dated']
        except (TypeError,ValueError):return ['Underlying financial evidence timestamp missing']
    return []

def inspect_history(rows,sessions,clock,confirmed_moves=()):
    rows=effective_rows(rows,confirmed_moves);reasons=[];dates=[r['ts'][:10] for r in rows];today=clock.astimezone(IST).date()
    if len(rows)<MIN_SESSIONS:reasons.append(f'Fewer than {MIN_SESSIONS} completed daily candles')
    if not rows:return reasons+['No daily candles']
    if len(set(dates))!=len(dates):reasons.append('Duplicate daily sessions')
    if any(d>=today.isoformat() for d in dates):reasons.append('Current or future daily candle')
    if not 1<=(today-datetime.fromisoformat(dates[-1]).date()).days<=7:reasons.append('Daily history is stale')
    if sessions and dates[-1]!=sessions[-1]:reasons.append('Latest observed market session missing')
    expected=[d for d in sessions[-200:] if dates[0]<=d<=dates[-1]]
    missing=sorted(set(expected)-set(dates))
    if missing:reasons.append(f'{len(missing)} gaps against locally observed market sessions (first: {missing[0]})')
    if any(not all(number(r[k]) is not None for k in ('open','high','low','close','volume')) or min(r['open'],r['high'],r['low'],r['close'])<=0 or r['volume']<0 or r['high']<max(r['open'],r['close'],r['low']) or r['low']>min(r['open'],r['close']) for r in rows):reasons.append('Invalid OHLCV values')
    jumps=[b['ts'][:10] for a,b in zip(rows,rows[1:]) if b['ts'][:10] not in confirmed_moves and a['close']>0 and (abs(b['open']/a['close']-1)>.25 or abs(b['close']/a['close']-1)>.25)]
    if jumps:reasons.append(f'Unreconciled price discontinuity / possible corporate action ({jumps[-1]})')
    if any(r['volume']<=0 for r in rows[-20:]):reasons.append('Zero volume in recent 20 sessions')
    return reasons

def init_quality(path):
    with connect(path) as db:db.execute('CREATE TABLE IF NOT EXISTS quality(symbol TEXT PRIMARY KEY,report TEXT NOT NULL,checked_at TEXT NOT NULL)')

def audit(path,clock=None):
    clock=clock or datetime.now(timezone.utc);init_quality(path)
    with connect(path) as db:
        db.execute('BEGIN')
        sectors={r['symbol']:r['sector'] for r in db.execute('SELECT symbol,sector FROM instruments WHERE '+symbol_filter(db))};instruments=list(sectors)
        histories=collections.defaultdict(lambda:collections.deque(maxlen=201));counts=collections.Counter()
        for r in db.execute('SELECT symbol,ts,open,high,low,close,volume FROM candles WHERE '+symbol_filter(db)+' ORDER BY symbol,ts'):
            histories[r['symbol']].append(dict(r));counts[r['ts'][:10]]+=1
        # A local consensus calendar, explicitly not an independently verified NSE calendar.
        threshold=max(1,len(instruments)//2);sessions=sorted(d for d,n in counts.items() if n>=threshold and d<clock.astimezone(IST).date().isoformat())
        funds={r['symbol']:(json.loads(r['details']),r['observed_at']) for r in db.execute('SELECT * FROM fundamentals WHERE '+symbol_filter(db))}
        attach(db,{s:d for s,(d,_) in funds.items()})
        evidence=collections.defaultdict(dict)
        for r in db.execute('SELECT symbol,endpoint,observed_at FROM payloads WHERE '+symbol_filter(db)):evidence[r['symbol']][r['endpoint']]=r['observed_at']
        bad={r[0] for r in db.execute("SELECT DISTINCT symbol FROM issues WHERE "+symbol_filter(db)+" AND kind IN ('candle','identity')")}
        from .price_reconciliation import confirmed
        confirmed_moves={symbol:confirmed(db,symbol,list(histories[symbol]),clock) for symbol in instruments}
    reports={};totals=collections.Counter()
    for symbol in instruments:
        reasons=inspect_history(list(histories[symbol]),sessions,clock,confirmed_moves[symbol])
        if symbol in bad:reasons.append('Unresolved source candle or identity issue')
        f,observed=funds.get(symbol,({},None));fr=financial_reasons(f,observed,clock)+source_reasons(f,evidence[symbol],clock)
        report={'trader_ready':not reasons,'financial_sector':is_financial(sectors[symbol]),'history_tier':tier(len(effective_rows(histories[symbol],confirmed_moves[symbol]))),'investor_ready':not(reasons or fr),'price_reasons':reasons,'financial_reasons':fr,'as_of':histories[symbol][-1]['ts'] if histories[symbol] else None,'corporate_actions':'not independently reconciled','exchange_verified_market_moves':sorted(confirmed_moves[symbol])}
        reports[symbol]=report
        for r in set(reasons+fr):totals['Missing locally observed sessions' if 'gaps against' in r else 'Possible corporate action / price discontinuity' if 'discontinuity' in r else r]+=1
    summary={'checked_at':clock.isoformat(),'stocks':len(instruments),'trader_ready':sum(r['trader_ready'] for r in reports.values()),'investor_ready':sum(r['investor_ready'] for r in reports.values()),'latest_observed_session':sessions[-1] if sessions else None,'reasons':dict(totals),'calendar_basis':'Majority of local instrument histories; not an official exchange calendar','financial_sector_stocks':sum(r['financial_sector'] for r in reports.values()),'investor_ready_non_financial':sum(r['investor_ready'] and not r['financial_sector'] for r in reports.values()),'paper_validated':False,'live_execution_ready':False}
    with connect(path) as db:
        db.executemany('INSERT OR REPLACE INTO quality VALUES(?,?,?)',[(s,json.dumps(r),clock.isoformat()) for s,r in reports.items()]);set_state(db,'quality_summary',summary);set_state(db,'quality_sessions',sessions[-201:])
    return summary

def require_buy_quality(db,symbol,portfolio,clock):
    row=db.execute('SELECT * FROM quality WHERE symbol=?',(symbol,)).fetchone()
    if not row or not 0<=(clock-datetime.fromisoformat(row['checked_at'])).total_seconds()<=600:raise ValueError('Data check missing or stale; wait for the local audit.')
    report=json.loads(row['report']);reasons=list(report['price_reasons'])
    current=list(reversed([dict(r) for r in db.execute('SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 201',(symbol,))]))
    from .price_reconciliation import confirmed
    reasons+=inspect_history(current,get_state(db,'quality_sessions',[]),clock,confirmed(db,symbol,current,clock))
    # Re-check financial eligibility against the current record inside the fill transaction.
    if portfolio=='investor':
        f=db.execute('SELECT * FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()
        details=attach(db,{symbol:json.loads(f['details']) if f else {}})[symbol]
        reasons+=financial_reasons(details,f['observed_at'] if f else None,clock)
        times={r['endpoint']:r['observed_at'] for r in db.execute('SELECT endpoint,observed_at FROM payloads WHERE symbol=?',(symbol,))}
        reasons+=source_reasons(details,times,clock)
    if db.execute("SELECT 1 FROM issues WHERE symbol=? AND kind IN ('candle','identity')",(symbol,)).fetchone():reasons.append('Unresolved source validation issue')
    if reasons:raise ValueError('New paper buy blocked: '+'; '.join(dict.fromkeys(reasons)))
