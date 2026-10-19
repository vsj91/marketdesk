#!/usr/bin/env python3
"""Rebalance cron handler for the quant paper baskets.

  init    create the five default baskets in the ledger (existing names are left alone)
  run     end-of-day pipeline for one completed session (default: the latest session in the database);
          fills yesterday's orders at today's typical price, runs due strategies, queues tomorrow's orders,
          snapshots NAV/weights. Safe to rerun: a finished (basket, session) is skipped.
  replay  run every session in a date range in order (builds history for a fresh ledger)
  report  write the JSON telemetry payload for one or all baskets

The MarketDesk database is opened read-only; all writes go to the ledger file.
The local app (app.py serve) already runs this daily through marketdesk.quant.runner; use this script
for manual runs, replays into a separate --ledger, or reports."""
import argparse,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import DEFAULT_DB
from marketdesk.quant import BasketEngine
from marketdesk.quant.data import load_market,sessions,snapshot
from marketdesk.quant.runner import DEFAULT_BASKETS,ledger_path

DEFAULT_LEDGER=ledger_path(DEFAULT_DB)

def main():
    a=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument('command',choices=('init','run','replay','report'))
    a.add_argument('--database',default=str(DEFAULT_DB));a.add_argument('--ledger',default=str(DEFAULT_LEDGER))
    a.add_argument('--capital',type=float,default=100_000);a.add_argument('--start',help='basket start session for init')
    a.add_argument('--day');a.add_argument('--from',dest='first');a.add_argument('--to',dest='last')
    a.add_argument('--basket');a.add_argument('--output')
    args=a.parse_args();engine=BasketEngine(args.ledger)
    if args.command=='init':
        for name,strategy in DEFAULT_BASKETS.items():
            print(name,'created' if engine.create(name,strategy,args.capital,day=args.start) else 'exists')
        return
    if args.command=='report':
        names=[args.basket] if args.basket else engine.names()
        out={n:engine.payload(n) for n in names}
        text=json.dumps(out if len(out)>1 else out[names[0]],indent=1,default=str)
        Path(args.output).write_text(text) if args.output else print(text)
        return
    if not engine.names():sys.exit('No baskets in the ledger; run init first.')
    t=time.monotonic();market=load_market(args.database);days=sessions(market)
    print(f'loaded {len(market["bars"])} symbols, {len(days)} sessions in {time.monotonic()-t:.1f}s',file=sys.stderr)
    if args.command=='run':todo=[args.day or days[-1]]
    else:todo=[d for d in days if (not args.first or d>=args.first) and (not args.last or d<=args.last)]
    for day in todo:
        if day not in days:sys.exit(f'{day} is not a session in the database.')
        result=engine.run_day(snapshot(market,day))
        print(json.dumps({'day':day,**result},default=str),flush=True)

if __name__=='__main__':main()
