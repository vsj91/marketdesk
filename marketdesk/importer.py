import csv, json, calendar, collections
from pathlib import Path
from datetime import datetime, timezone, timedelta
from .storage import connect, init, now, number, set_state, ROOT
from . import provider

ENDPOINTS=('profile','income_statement','balance_sheet','cash_flow','key_ratios')

def candle_row(row):
    if len(row)<6: raise ValueError('Fewer than six candle fields')
    ts=datetime.fromisoformat(str(row[0]).replace('Z','+00:00'))
    if ts.tzinfo is None:raise ValueError('Missing timezone')
    if ts>datetime.now(timezone.utc):raise ValueError('Future candle')
    ist=timezone(timedelta(hours=5,minutes=30))
    if ts.astimezone(ist).date()>=datetime.now(ist).date():raise ValueError('Current-session candle is not imported as a completed daily bar')
    values=[number(v) for v in row[1:6]]
    if any(v is None for v in values):raise ValueError('Non-numeric OHLCV')
    o,h,l,c,v=values
    if min(o,h,l,c)<=0 or v<0 or h<max(o,c,l) or l>min(o,c,h):raise ValueError('Invalid OHLCV')
    return (ts.isoformat(),o,h,l,c,v)

def summary_metrics(payloads):
    p=payloads.get('income_statement',{}).get('data',{})
    if not isinstance(p,dict):return {}
    series={x.get('category'):x.get('history',[]) for x in p.get('income_statement',[]) if isinstance(x,dict)}
    out={'statement_type':p.get('type'),'units':p.get('units_in'),'frequency':p.get('time_period')}
    for category in ('revenue','net_profit'):
        values=[]
        for x in series.get(category,[]):
            _,order=provider._fundamental_period(x.get('period'))
            val=number(x.get('value'))
            if order and val is not None:values.append((order,val,x.get('period')))
        values.sort(reverse=True)
        if values:
            order,value,period=values[0];out[category]=value;out[category+'_period']=period
            prev=next((v for o,v,p in values[1:] if o==(order[0]-1,order[1])),None)
            # Negative/zero comparison bases are not a meaningful growth percent.
            out[category+'_growth_yoy']=(value/prev-1)*100 if prev is not None and prev>0 else None
    return out

def import_exports(folder,db_path, symbols=None):
    init(db_path);folder=Path(folder);report=collections.Counter();missing=collections.Counter()
    candle_dir=folder/'RAW_CANDLES'/'csv';fund_dir=folder/'RAW_FUNDAMENTALS'
    universe={}
    master_file=ROOT/'data'/'instruments.json'
    master={r['instrument_key']:r for r in json.loads(master_file.read_text())} if master_file.exists() else {}
    input_file=folder/'VALID_STOCK_INPUT.csv'
    if input_file.exists():
        with input_file.open(newline='') as f:
            for row in csv.DictReader(f):universe[row['symbol']]=row
    names=set(universe)|{p.stem for p in candle_dir.glob('*.csv')}|{p.name for p in fund_dir.iterdir() if p.is_dir()} if fund_dir.exists() else set(universe)|{p.stem for p in candle_dir.glob('*.csv')}
    if symbols is not None:names &= set(symbols)
    with connect(db_path) as db:
        canonical={r.get('instrument_key'):sym for sym,r in universe.items() if r.get('instrument_key')}
        used={}
        for symbol in sorted(names):
            raw={};payloads={};source_times=[]
            for endpoint in ENDPOINTS:
                f=fund_dir/symbol/(endpoint+'.json')
                if f.exists():
                    try:raw[endpoint]=json.loads(f.read_text())
                    except (ValueError,OSError):report['invalid_json']+=1
            meta=next(iter(raw.values()),{})
            r=universe.get(symbol,{})
            key=r.get('instrument_key') or meta.get('instrument_key')
            if not key:
                jf=folder/'RAW_CANDLES'/'json'/(symbol+'.json')
                if jf.exists():key=json.loads(jf.read_text()).get('instrument_key')
            if not provider.is_cash_equity_instrument(key):report['invalid_instruments']+=1;continue
            if master and (key not in master or master[key].get('instrument_type')!='EQ' or master[key].get('segment')!='NSE_EQ'):
                report['excluded_non_equity_or_unknown']+=1
                db.execute('INSERT OR IGNORE INTO issues VALUES(?,?,?)',(symbol,'identity','Not a verified NSE common equity in the local instrument master'))
                continue
            if master and master[key].get('trading_symbol')!=symbol:
                report['quarantined_master_aliases']+=1
                db.execute('INSERT OR IGNORE INTO issues VALUES(?,?,?)',(symbol,'identity','Master symbol is '+master[key].get('trading_symbol','unknown')))
                continue
            if (key in canonical and canonical[key]!=symbol) or (key in used and used[key]!=symbol):
                report['quarantined_symbol_aliases']+=1
                db.execute('INSERT OR IGNORE INTO issues VALUES(?,?,?)',(symbol,'identity','Instrument maps to '+canonical.get(key,used.get(key,''))))
                continue
            used[key]=symbol
            isin=key.split('|')[1]
            db.execute('INSERT INTO instruments VALUES(?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET instrument_key=excluded.instrument_key,isin=excluded.isin', (symbol,key,isin,master.get(key,{}).get('name') or symbol,None))
            db.execute('DELETE FROM issues WHERE symbol=? AND kind IN (\'candle\',\'fundamentals\')',(symbol,))
            for endpoint,record in raw.items():
                if record.get('instrument_key')!=key or record.get('symbol')!=symbol:
                    report['identity_mismatches']+=1
                    db.execute('INSERT OR IGNORE INTO issues VALUES(?,?,?)',(symbol,'identity',endpoint+' identity mismatch; excluded'))
                    continue
                payload=record.get('payload') or {};error=record.get('request_error')
                stamp=record.get('response_received_at_utc') or record.get('requested_at_utc')
                if not stamp: stamp='1970-01-01T00:00:00+00:00'
                db.execute('INSERT INTO payloads VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol,endpoint) DO UPDATE SET payload=excluded.payload,http_status=excluded.http_status,error=excluded.error,observed_at=excluded.observed_at,source_url=excluded.source_url',
                    (symbol,endpoint,json.dumps(payload),record.get('http_status'),error,stamp,record.get('source_url')))
                if payload.get('status')=='success' and not error and record.get('http_status')==200:
                    payloads[endpoint]=payload;source_times.append(stamp)
                    report[endpoint+'_success']+=1
                else:report['endpoint_errors']+=1
            if raw:
                details=provider.extract_fundamental_details(payloads)
                details['summary']=summary_metrics(payloads)
                details['endpoints_present']=list(payloads)
                details['status']='COMPLETE' if not details['missing_fields'] and len(payloads)==5 else 'PARTIAL' if payloads else 'FAILED'
                report['fundamentals_'+details['status'].lower()]+=1
                missing.update(details['missing_fields'])
                for warning in details['warnings']:db.execute('INSERT OR IGNORE INTO issues VALUES(?,?,?)',(symbol,'fundamentals',warning))
                stamp=max(source_times) if source_times else '1970-01-01T00:00:00+00:00'
                fallback_file=ROOT/'data'/'fallbacks'/(symbol+'.json')
                if fallback_file.exists():
                    from .fallbacks import apply_fallback
                    details=apply_fallback(details,json.loads(fallback_file.read_text()))
                previous=db.execute('SELECT details FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()
                if previous:
                    from .annual_reports import retain_report
                    details=retain_report(details,json.loads(previous[0]),symbol,isin)
                db.execute('INSERT INTO fundamentals VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET details=excluded.details,observed_at=excluded.observed_at',(symbol,json.dumps(details,allow_nan=False),stamp))
                profile=payloads.get('profile',{}).get('data',{})
                db.execute('UPDATE instruments SET sector=?,name=? WHERE symbol=?',(details.get('sector'),profile.get('name') or profile.get('company_name') or master.get(key,{}).get('name') or symbol,symbol))
            f=candle_dir/(symbol+'.csv')
            if f.exists():
                jf=folder/'RAW_CANDLES'/'json'/(symbol+'.json');cmeta=json.loads(jf.read_text()) if jf.exists() else {}
                if cmeta.get('instrument_key')!=key or cmeta.get('symbol')!=symbol:
                    report['identity_mismatches']+=1
                    db.execute('INSERT OR IGNORE INTO issues VALUES(?,?,?)',(symbol,'identity','Candle identity mismatch; excluded'))
                    db.execute('DELETE FROM candles WHERE symbol=?',(symbol,))
                    continue
                observed=cmeta.get('requested_at_utc') or '1970-01-01T00:00:00+00:00'
                seen={};bad=set()
                with f.open(newline='') as handle:
                    for row in csv.reader(handle):
                        if row and row[0]=='timestamp':continue
                        try:
                            parsed=candle_row(row);session=datetime.fromisoformat(parsed[0]).astimezone(timezone(timedelta(hours=5,minutes=30))).date().isoformat()
                            if session in seen and seen[session]!=parsed:bad.add(session)
                            seen[session]=parsed
                        except (ValueError,TypeError) as e:
                            report['rejected_candles']+=1
                            db.execute('INSERT OR IGNORE INTO issues VALUES(?,?,?)',(symbol,'candle',str(e)))
                for session in bad:
                    seen.pop(session,None);report['conflicting_sessions']+=1
                    db.execute('INSERT OR IGNORE INTO issues VALUES(?,?,?)',(symbol,'candle','Conflicting daily session '+session))
                for parsed in seen.values():
                    db.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,ts) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,observed_at=excluded.observed_at', (symbol,*parsed,'upstox_daily_export',observed))
                report['imported_candles']+=len(seen);report['candle_symbols']+=1
            report['symbols']+=1
        result={'imported_at':now(),'scope':'full' if symbols is None else 'selected','counts':dict(report),'missing_fields':dict(missing),'note':'Saved provider responses verified for structure and extraction; not independently reconciled to exchange/company filings.'}
        set_state(db,'last_import',result)
    return result
