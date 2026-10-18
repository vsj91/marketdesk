"""Read-only descriptive exposures, never an entry or exit filter."""
import math
from itertools import combinations


def sector_group(raw):
    raw=(raw or '').strip()
    return 'Financials' if raw.casefold() in ('bank','banks','finance','financial services') else raw or 'Unknown'


def exposure(db,positions,equity):
    sectors={};series={};pairs=[];gross=sum(p['value'] for p in positions) if all(p['value'] is not None for p in positions) else None
    for p in positions:
        row=db.execute('SELECT sector FROM instruments WHERE symbol=?',(p['symbol'],)).fetchone()
        raw=row[0] if row else None;name=sector_group(raw)
        g=sectors.setdefault(name,dict(sector=name,symbols=[],raw_sectors=[],cost=0,value=0))
        g['symbols'].append(p['symbol']);g['raw_sectors'].append(raw);g['cost']+=p['cost']
        g['value']=g['value']+p['value'] if g['value'] is not None and p['value'] is not None else None
        rows=list(reversed(db.execute('SELECT ts,close FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 61',(p['symbol'],)).fetchall()))
        # Match both endpoints, not merely the final date: no filling gaps in returns.
        series[p['symbol']]={(a['ts'][:10],b['ts'][:10]):b['close']/a['close']-1 for a,b in zip(rows,rows[1:]) if a['close']>0 and b['close']>0}
    for g in sectors.values():
        g['pct_invested']=g['value']/gross*100 if g['value'] is not None and gross else None
        g['pct_equity']=g['value']/equity*100 if g['value'] is not None and equity else None
    for a,b in combinations(sorted(series),2):
        dates=sorted(set(series[a])&set(series[b]));x=[series[a][d] for d in dates];y=[series[b][d] for d in dates];n=len(x);corr=None
        if n>=30:
            mx=sum(x)/n;my=sum(y)/n;den=math.sqrt(sum((v-mx)**2 for v in x)*sum((v-my)**2 for v in y))
            if den:corr=max(-1,min(1,sum((v-mx)*(w-my) for v,w in zip(x,y))/den))
        pairs.append(dict(a=a,b=b,observations=n,correlation=corr,first=dates[0][0] if dates else None,last=dates[-1][1] if dates else None))
    return dict(sectors=sorted(sectors.values(),key=lambda x:-(x['value'] or 0)),correlations=pairs,gross_value=gross,
       basis='Current marked holdings; Bank/Banks/Finance/Financial Services grouped as Financials. Pearson correlation over up to 60 common daily returns; minimum 30. Descriptive only; no sector or correlation limit is applied.')
