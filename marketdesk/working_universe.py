"""One research/data scope. Saved history and the paper ledger are never deleted."""
from datetime import datetime, timezone, timedelta
from .storage import get_state
from .buy_universe import KEY, SOURCE, validate_members


def enabled(db):
    return get_state(db,'working_universe','all') != 'all'


def research_symbols(db):
    if not enabled(db):return None
    if get_state(db,'working_universe')!='nifty500':return set()
    snapshot=get_state(db,KEY,{})
    try:
        members=snapshot['members'];validate_members(members)
        if snapshot['source']!=SOURCE or datetime.fromisoformat(snapshot['fetched_at'])>datetime.now(timezone.utc):raise ValueError()
    except (ValueError,KeyError,TypeError):return set()
    # Cached verified membership keeps research available during a source outage.
    # Buy authorization separately requires verification today.
    return {r[0] for r in db.execute('SELECT symbol,isin FROM instruments') if members.get(r[0])==r[1]}


def held_symbols(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='positions'").fetchone():return set()
    return {r[0] for r in db.execute('SELECT DISTINCT symbol FROM positions WHERE quantity>0')}


def symbol_filter(db,column='symbol',include_holdings=False):
    """SQL predicate for internal column names; quote data as SQLite literals."""
    symbols=research_symbols(db)
    if symbols is None:return '1'
    if include_holdings:symbols |= held_symbols(db)
    return column+' IN ('+','.join("'"+s.replace("'","''")+"'" for s in sorted(symbols))+')' if symbols else '0'


def status(db):
    symbols=research_symbols(db)
    if symbols is None:return {'name':'All stocks','enabled':False}
    snapshot=get_state(db,KEY,{})
    members=snapshot.get('members',{})
    return dict(name='Nifty 500',enabled=True,matched=len(symbols),constituents=len(members),
                unmatched=sorted(set(members)-symbols),fetched_at=snapshot.get('fetched_at'),
                pending_exit_holdings=len(held_symbols(db)-symbols))


def scope_report(db,report):
    """Display only in-scope rows; historical all-stock baselines are not comparable."""
    symbols=research_symbols(db)
    if symbols is None:return report
    import collections,json
    rows=[r for r in report.get('results',[]) if r.get('symbol') in symbols]
    # Keep the repair page complete: every currently blocked constituent gets
    # a plain-language category and its exact quality reasons, even when it was
    # not part of the historical repair queue.
    quality_rows={r['symbol']:json.loads(r['report']) for r in db.execute('SELECT symbol,report FROM quality WHERE '+symbol_filter(db))}
    existing={r.get('symbol') for r in rows}
    for symbol,q in quality_rows.items():
        if q.get('investor_ready'):continue
        reasons=list(q.get('price_reasons',[]))+list(q.get('financial_reasons',[]))
        if any('Fewer than 200' in x for x in q.get('price_reasons',[])):
            candles=[r[0] for r in db.execute('SELECT ts FROM candles WHERE symbol=? ORDER BY ts',(symbol,))]
            sessions=get_state(db,'quality_sessions',[])
            eligible='unknown date'
            if candles and sessions:
                try:
                    need=max(0,200-len(candles));pos=sessions.index(candles[-1][:10]);eligible=sessions[pos+need] if pos+need<len(sessions) else None
                except (ValueError,IndexError):pass
            if eligible is None and candles:
                # A projected weekday date is clearly labelled; exchange
                # holidays can move it later when fresh sessions arrive.
                day=datetime.fromisoformat(candles[-1]).date();remaining=max(0,200-len(candles))
                while remaining:
                    day+=timedelta(days=1)
                    if day.weekday()<5:remaining-=1
                eligible=day.isoformat()+' (projected weekday)'
            category='Too new, eligible from '+eligible
        elif any('Financial sources disagree' in x for x in q.get('financial_reasons',[])):
            category='Sources disagree'
        elif q.get('price_reasons') or q.get('financial_reasons'):
            category='Fixable' if any('Annual' in x or 'Growth' in x for x in reasons) else 'Needs source data'
        else:
            category='Needs source data'
        if symbol not in existing:
            rows.append({'symbol':symbol,'status':'unresolved','before_missing':[],
                         'after_missing':[],'source':None,'period':None,'basis':None,
                         'source_conflicts':int(any('Financial sources disagree' in x for x in reasons)),
                         'blocker_category':category,'blocker_reason':'; '.join(reasons) or 'Data-validity check failed'})
        else:
            for row in rows:
                if row.get('symbol')==symbol:
                    row['blocker_category']=category;row['blocker_reason']='; '.join(reasons) or 'Data-validity check failed'
    if report.get('working_universe')=='nifty500' and len(rows)==len(report.get('results',[])):
        return report
    quality=get_state(db,'quality_summary',{})
    complete=db.execute("SELECT COUNT(*) FROM fundamentals WHERE "+symbol_filter(db)+" AND json_extract(details,'$.status')='COMPLETE'").fetchone()[0]
    out={k:report[k] for k in ('status','started_at','updated_at','finished_at','method') if k in report}
    out.update(results=rows,total=len(rows),done=len(rows),quality=quality,quality_before={},
               scoped=True,complete_now=complete,incomplete_now=len(symbols)-complete,
               newly_eligible=[s for s in report.get('newly_eligible',[]) if s in symbols],
               newly_complete=sum(bool(r.get('before_missing')) and not r.get('after_missing') for r in rows if 'after_missing' in r),
               recovered_fields=sum(len(r.get('recovered',[])) for r in rows),
               improved_stocks=sum(bool(r.get('recovered')) for r in rows),errors=sum(r.get('status')=='error' for r in rows),
               investor_after=quality.get('investor_ready'),resolved_fields=sum(len(r.get('resolved',[])) for r in rows),
               cleared_stocks=sum(r.get('status')=='resolved' for r in rows),unresolved_stocks=sum(r.get('status')!='resolved' for r in rows),
               remaining_conflict_stocks=quality.get('reasons',{}).get('Financial sources disagree',0),
               annual_reports_verified=sum(r.get('annual_report',{}).get('status')=='verified' for r in rows),
               note='Nifty 500 rows only. Current coverage uses the active universe; earlier all-stock baselines are not comparable.')
    return out


def sync_instruments(path,fetch_missing=False):
    """Register only official constituents with matching Upstox symbol + ISIN.

    BE and RR constituents are data coverage, not exemptions from buy-quality checks.
    """
    import json,gzip,io
    from .storage import ROOT,connect,set_state,now
    with connect(path) as db:
        if not enabled(db):return
        snapshot=get_state(db,KEY,{})
        try:
            members=snapshot['members'];validate_members(members)
            if snapshot['source']!=SOURCE:raise ValueError()
        except (KeyError,ValueError,TypeError):return
    file=ROOT/'data/instruments.json'
    master=json.loads(file.read_text()) if file.exists() else []
    def verified(rows):
        return {r['trading_symbol']:r for r in rows if isinstance(r,dict) and r.get('segment')=='NSE_EQ'
                and r.get('instrument_type') in ('EQ','BE','RR') and members.get(r.get('trading_symbol'))==r.get('isin')
                and r.get('instrument_key')=='NSE_EQ|'+str(r.get('isin'))}
    records=verified(master)
    if fetch_missing and set(members)-set(records):
        import requests
        try:
            response=requests.get('https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz',timeout=(3,15));response.raise_for_status()
            if len(response.content)>20_000_000:raise ValueError('Instrument master too large')
            with gzip.GzipFile(fileobj=io.BytesIO(response.content)) as handle:raw=handle.read(60_000_001)
            if len(raw)>60_000_000:raise ValueError('Expanded instrument master too large')
            incoming=json.loads(raw)
            if not isinstance(incoming,list):raise ValueError('Invalid instrument master')
            records=verified(incoming)
            if len(records)>=len(master and verified(master) or {}):
                temp=file.with_suffix('.json.tmp');temp.write_text(json.dumps(incoming));temp.replace(file)
        except (requests.RequestException,ValueError,OSError):pass
    with connect(path) as db:
        for s,r in records.items():
            old=db.execute('SELECT isin FROM instruments WHERE symbol=?',(s,)).fetchone()
            # Corporate-action identity changes need review; never relabel old candles.
            if old:continue
            db.execute('INSERT OR IGNORE INTO instruments VALUES(?,?,?,?,?)',(s,r['instrument_key'],r['isin'],r.get('name') or s,None))
        set_state(db,'working_universe_sync',{'checked_at':now(),'missing':sorted(set(members)-research_symbols(db))})
