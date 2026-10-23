#!/usr/bin/env python3
"""One health check of the running MarketDesk app, run every 2 minutes by the watch LaunchAgent during market hours.

Checks the server, the live feed, the data audit's age, every basket's state, orders blocked over and over,
and new tracebacks in the server log. A problem not seen in the previous check is appended to
data/logs/health-watch.log with an IST time and raises a macOS notification; a CLEARED line marks when it goes away. Read-only against the app."""
import json,os,sqlite3,sys,urllib.request
from datetime import datetime,timedelta,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from marketdesk.alerts import mac_notify

IST=timezone(timedelta(hours=5,minutes=30))
LOGS=ROOT/'data/logs';LOG=LOGS/'health-watch.log';STATE=LOGS/'health-watch.state.json'
QUANT=('smart_beta','risk_parity','stat_arb','regime')

def in_hours(now):
    local=now.astimezone(IST)
    return local.weekday()<5 and (9,10)<=(local.hour,local.minute)<=(15,35)

def get(path):return json.load(urllib.request.urlopen('http://127.0.0.1:8765'+path,timeout=20))

def problems(now,state):
    try:health,paper=get('/api/health'),get('/api/paper')
    except Exception as error:return [f'APP DOWN: {error}']
    out=[];stream=health.get('stream') or {};quality=health.get('quality') or {}
    if stream.get('status')!='connected':out.append(f"FEED {stream.get('status')} market={stream.get('market_status')}")
    try:
        age=(now-datetime.fromisoformat(quality['checked_at'])).total_seconds()
        if age>900:out.append(f'DATA CHECK stale: last audit {age/60:.0f} min ago')
    except (KeyError,TypeError,ValueError):out.append('DATA CHECK time missing')
    for name,s in paper['automation']['status'].items():
        if isinstance(s,dict) and s.get('state') not in ('monitoring','market_wait','waiting'):
            out.append(f"BASKET {name}: {s.get('state')} - {str(s.get('message'))[:140]}")
    db=sqlite3.connect((ROOT/'data/marketdesk.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
    try:
        since=(now-timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M')
        for p,sym,reason,n in db.execute("SELECT portfolio,symbol,substr(reason,1,90),count(*) FROM auto_events WHERE kind IN ('blocked','error') AND created_at>=? GROUP BY 1,2,3 HAVING count(*)>=8",(since,)):
            # A depth-limited fill too small for the fee floor can still fill later in a rule basket; quant buys should be cancelled.
            if reason.startswith('Buy too small for fee floor') and p not in QUANT:continue
            out.append(f'REPEATED {p} {sym} x{n}: {reason}')
    finally:db.close()
    err=LOGS/'server.err.log'
    if err.exists():
        count=sum(1 for line in err.open(errors='ignore') if line.startswith('Traceback'))
        if count>state.get('tracebacks',count):out.append(f"SERVER ERRORS: {count-state['tracebacks']} new traceback(s) in server.err.log")
        state['tracebacks']=count
    return out

def main():
    now=datetime.now(timezone.utc)
    if not in_hours(now) and '--force' not in sys.argv:return
    LOGS.mkdir(parents=True,exist_ok=True)
    try:state=json.loads(STATE.read_text())
    except (OSError,ValueError):state={}
    found=problems(now,state);stamp=now.astimezone(IST).strftime('%Y-%m-%d %H:%M')
    new=[line for line in found if line.split(':')[0] not in state.get('open',[])]
    cleared=[key for key in state.get('open',[]) if key not in {line.split(':')[0] for line in found}]
    with LOG.open('a') as log:
        for line in new:log.write(f'{stamp} {line}\n')                                   # each problem once, when it starts
        for key in cleared:log.write(f'{stamp} CLEARED {key}\n')
        if not found and now.astimezone(IST).minute<2:log.write(f'{stamp} OK\n')      # one heartbeat an hour
    if new:mac_notify('MarketDesk health',new[0][:200]+(f' (+{len(new)-1} more)' if len(new)>1 else ''))
    state['open']=[line.split(':')[0] for line in found];state['checked_at']=now.isoformat()
    STATE.write_text(json.dumps(state))
    for line in found:print(stamp,line)

if __name__=='__main__':main()
