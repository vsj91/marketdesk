#!/usr/bin/env python3
import argparse,json
from pathlib import Path
from marketdesk.storage import DEFAULT_DB,ROOT

def main():
    parser=argparse.ArgumentParser(description='Independent local stock research and paper trading')
    parser.add_argument('command',choices=['serve','import','audit','replay'],nargs='?',default='serve')
    parser.add_argument('--db',type=Path,default=DEFAULT_DB)
    parser.add_argument('--source',type=Path,default=ROOT/'data'/'imports')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--live',action='store_true')
    period=parser.add_mutually_exclusive_group()
    period.add_argument('--sessions',type=int,default=5)
    period.add_argument('--start',help='Inclusive calendar start YYYY-MM-DD; uses all completed sessions through --end')
    parser.add_argument('--end',help='Replay end date, YYYY-MM-DD; defaults to latest completed session')
    parser.add_argument('--slippage-bps',type=float,default=10)
    parser.add_argument('--fundamentals-mode',choices=['historical','current'],default='historical',help='Current uses today’s fundamentals in an explicitly hypothetical testcase')
    parser.add_argument('--output',type=Path,default=ROOT/'data/replays/last-week.json')
    args=parser.parse_args()
    if args.command=='import':
        from marketdesk.importer import import_exports
        print(json.dumps(import_exports(args.source,args.db),indent=2))
    elif args.command=='audit':
        from marketdesk.server import App
        health=App(args.db,read_only=True).health();health.pop('csrf',None)
        print(json.dumps(health,indent=2))
    elif args.command=='replay':
        from marketdesk.replay import load_snapshot,replay,save_report,render_report
        if args.end:
            from datetime import date
            date.fromisoformat(args.end)
        report=replay(load_snapshot(args.db,args.end),args.sessions,args.end,args.slippage_bps,fundamentals_mode=args.fundamentals_mode,start=args.start)
        save_report(report,args.output)
        (ROOT/'web/replay.html').write_text(render_report(report))
        print(json.dumps({'sessions':report['sessions'],'report':str(args.output),
            'portfolios':{p:{k:a[k] for k in ('status','ending_equity','net_pnl','fees','return_pct','max_daily_close_drawdown_pct')} for p,a in report['portfolios'].items()}},indent=2))
    else:
        from marketdesk.server import serve
        serve(args.db,args.port,args.live)
if __name__=='__main__':main()
