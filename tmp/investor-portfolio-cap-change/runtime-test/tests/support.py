import json
from datetime import timedelta
from marketdesk.storage import connect
from marketdesk.execution import normalize

def seed_quality(path,symbol,clock,fundamentals=None):
    with connect(path) as db:
        db.execute('INSERT OR REPLACE INTO quality VALUES(?,?,?)',(symbol,json.dumps({'trader_ready':True,'investor_ready':True,'price_reasons':[],'financial_reasons':[]}),clock.isoformat()))
        for endpoint in ('income_statement','balance_sheet','cash_flow'):db.execute('INSERT OR REPLACE INTO payloads VALUES(?,?,?,?,?,?,?)',(symbol,endpoint,'{}',200,None,clock.isoformat(),'fixture'))
        if not db.execute('SELECT 1 FROM candles WHERE symbol=?',(symbol,)).fetchone():
            day=clock.date()-timedelta(days=1);count=0
            while count<201:
                if day.weekday()<5:
                    db.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',(symbol,day.isoformat()+'T00:00:00+05:30',100,101,99,100,100000,'fixture',clock.isoformat()));count+=1
                day-=timedelta(days=1)
        if fundamentals:db.execute('INSERT OR REPLACE INTO fundamentals VALUES(?,?,?)',(symbol,json.dumps(fundamentals),clock.isoformat()))

def fixture_book(path,symbol,clock=None):
    with connect(path) as db:
        q=dict(db.execute('SELECT * FROM quotes WHERE symbol=?',(symbol,)).fetchone());key=db.execute('SELECT instrument_key FROM instruments WHERE symbol=?',(symbol,)).fetchone()[0]
    from datetime import datetime
    clock=clock or datetime.fromisoformat(q['received_at']);p=q['price']
    return normalize({'instrument_token':key,'symbol':symbol,'timestamp':clock.isoformat(),'last_trade_time':q['trade_time'],'last_price':p,'volume':1000000,'lower_circuit_limit':p*.5,'upper_circuit_limit':p*1.5,'depth':{'buy':[{'price':round(p*.999,2),'quantity':1000000}],'sell':[{'price':round(p*1.001,2),'quantity':1000000}]}},key,symbol,clock)
