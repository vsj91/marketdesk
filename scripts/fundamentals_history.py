#!/usr/bin/env python3
"""Point-in-time annual fundamentals FY2018-FY2026 from NSE filings, for research backtests only (never the live app).

  fetch     For every non-financial stock in the research universe (top 500 by traded value at any month-end since
            mid-2019, scripts/research_baskets.py), cache NSE's annual-results listing, its audited annual XBRL files
            (FY2018-FY2024; consolidated, plus standalone when no consolidated file exists), the FY2025 integrated
            filing and the corporate-actions listing under data/backtests/fundamentals-history/. Reruns skip cached files.
  build     Parse the cache into data/backtests/fundamentals-history.json: one record per stock and fiscal year,
            consolidated preferred. FY2026 comes from the app's own audited latest record.
  coverage  Share of the universe with each field, by fiscal year.

Point in time: a fiscal year's figures count only from period end + 60 days (SEBI's deadline for audited annual
results). Share counts are converted to today's units with NSE's bonus and split ratios, because research prices
are split- and bonus-adjusted; a stock with an unreadable bonus/split action gets no share count before it.
Net profit is from continuing operations where filed, so a business sale does not read as earnings.
Missing values stay missing: nothing is read as zero. Balance-sheet lines (borrowings, cash, assets) exist in
NSE's results XBRL only from FY2023; earlier equity is paid-up capital plus reserves.

Usage: .venv/bin/python scripts/fundamentals_history.py fetch|build|coverage"""
import argparse,collections,json,math,re,sqlite3,statistics,sys,time
from datetime import date,datetime,timedelta
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from marketdesk.history_rules import is_financial
CACHE=ROOT/'data/backtests/fundamentals-history';OUT=ROOT/'data/backtests/fundamentals-history.json'
YEARS=range(2018,2025);LAG=60;NSE='https://www.nseindia.com'
FIELDS={'revenue':('RevenueFromOperations',),'other_income':('OtherIncome',),'finance_costs':('FinanceCosts',),'pbt':('ProfitBeforeTax',),
    'net_profit':('ProfitLossForPeriodFromContinuingOperations','ProfitLossForPeriod'),'net_profit_total':('ProfitLossForPeriod',),
    'np_owners':('ProfitOrLossAttributableToOwnersOfParent',),'depreciation':('DepreciationDepletionAndAmortisationExpense',),
    'paid_up':('PaidUpValueOfEquityShareCapital',),'reserves':('ReserveExcludingRevaluationReserves',),'equity':('Equity',),
    'borrowings_nc':('BorrowingsNoncurrent',),'borrowings_c':('BorrowingsCurrent',),'cash':('CashAndCashEquivalents',),'assets':('Assets',),
    'current_assets':('CurrentAssets',),'current_liabilities':('CurrentLiabilities',),'ocf':('CashFlowsFromUsedInOperatingActivities',)}
PER_SHARE={'face_value':('FaceValueOfEquityShareCapital',),'eps_cont':('BasicEarningsLossPerShareFromContinuingOperations',),
    'eps':('BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations','BasicEarningsLossPerShareFromContinuingOperations')}

def universe():
    import research_baskets as rb
    P=rb.load();pit=rb.indicators(P)['pit500']
    return sorted(c for c in pit.columns if pit[c].loc['2019-06-01':].any() and not is_financial(P['sector'].get(c) or ''))

class Client:
    def __init__(self):
        self.s=requests.Session();self.s.headers.update({'User-Agent':'Mozilla/5.0','Accept':'application/json,text/html,*/*',
            'Referer':NSE+'/companies-listing/corporate-filings-financial-results'});self.last=0;self.warm()
    def warm(self):
        try:self.s.get(NSE,timeout=(5,10))
        except requests.RequestException:pass
    def get(self,url,**kw):
        for attempt in range(4):
            time.sleep(max(0,1.-(time.monotonic()-self.last)));self.last=time.monotonic()   # one request a second
            try:
                r=self.s.get(url,timeout=(5,30),**kw)
                if r.status_code==200:return r
                if r.status_code in (401,403):self.warm()
                if r.status_code==429:time.sleep(30*(attempt+1))
                if r.status_code==404:return None
            except requests.RequestException:time.sleep(5)
        return None

def fetch():
    c=Client();names=universe();isins=isin_map();CACHE.mkdir(parents=True,exist_ok=True)
    for i,sym in enumerate(names):
        d=CACHE/sym;d.mkdir(exist_ok=True)
        for name,url,params in (('listing.json',NSE+'/api/corporates-financial-results',{'index':'equities','symbol':sym,'period':'Annual'}),
                                ('actions.json',NSE+'/api/corporates-corporateActions',actions_params(sym)),
                                ('integrated.json',NSE+'/api/integrated-filing-results',{'symbol':sym,'size':100})):
            if not (d/name).exists():
                r=c.get(url,params=params)
                if r is not None:
                    try:r.json();(d/name).write_bytes(r.content)
                    except ValueError:pass
        try:rows=json.loads((d/'listing.json').read_text())
        except (OSError,ValueError):rows=[]
        for fy in YEARS:
            filed={x['consolidated']:x for x in rows if isinstance(x,dict) and x.get('toDate')==f'31-Mar-{fy}' and str(x.get('xbrl','')).endswith('.xml')}
            for kind in (['Consolidated'] if 'Consolidated' in filed else ['Non-Consolidated'] if 'Non-Consolidated' in filed else []):
                f=d/f'{fy}_{kind[0]}.xml'
                if not f.exists() and (r:=c.get(filed[kind]['xbrl'])) is not None:f.write_bytes(r.content)
        try:integ=json.loads((d/'integrated.json').read_text());integ=integ if isinstance(integ,list) else integ.get('data',[])
        except (OSError,ValueError):integ=[]
        annual={x.get('consolidated'):x for x in integ if isinstance(x,dict) and str(x.get('qe_Date','')).upper()=='31-MAR-2025' and str(x.get('ixbrl','')).startswith('https://nsearchives.nseindia.com/corporate/ixbrl/')}
        for kind in (['Consolidated'] if 'Consolidated' in annual else ['Standalone'] if 'Standalone' in annual else []):
            f=d/f'2025_{kind[0]}.html'
            if not f.exists() and (r:=c.get(annual[kind]['ixbrl'])) is not None:f.write_bytes(r.content)
        if i%25==0:print(i,len(names),sym,flush=True)
    print('fetched',len(names))

def actions_params(sym):
    # Without a date range NSE returns only the latest 20 actions, which drops older bonuses for frequent dividend payers.
    return {'index':'equities','symbol':sym,'from_date':'01-01-2017','to_date':date.today().strftime('%d-%m-%Y')}

def refetch_actions():
    """Re-download corporate actions cached before the date range was used (exactly 20 rows means truncated)."""
    c=Client();n=0
    for f in sorted(CACHE.glob('*/actions.json')):
        try:rows=json.loads(f.read_text())
        except ValueError:rows=[]
        if isinstance(rows,list) and len(rows)<20:continue
        r=c.get(NSE+'/api/corporates-corporateActions',params=actions_params(f.parent.name))
        if r is not None:
            try:r.json();f.write_bytes(r.content);n+=1
            except ValueError:pass
    print('refetched',n)

def isin_map():
    db=sqlite3.connect(f'file:{ROOT}/data/marketdesk.sqlite3?mode=ro',uri=True)
    return dict(db.execute('SELECT symbol,isin FROM instruments'))

def parse_xbrl(text,fy):
    """Audited full-year (1 Apr-31 Mar) values of one results XBRL file, in crore (per-share values in rupees).
    Columns are identified by their DateOfStart/EndOfReportingPeriod facts, which NSE files carry even when the
    context element is missing or dated differently; balance-sheet columns by an instant context at the year end."""
    start,end=f'{fy-1}-04-01',f'{fy}-03-31'
    st=dict(re.findall(r'<in-bse-fin:DateOfStartOfReportingPeriod contextRef="([^"]+)"[^>]*>\s*([\d-]+)',text))
    en=dict(re.findall(r'<in-bse-fin:DateOfEndOfReportingPeriod contextRef="([^"]+)"[^>]*>\s*([\d-]+)',text))
    ctx={c for c in st if (st[c],en.get(c))==(start,end)}
    for cid,body in re.findall(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>',text,re.S):
        b=re.search(r'<xbrli:instant>([\d-]+)</xbrli:instant>',body)
        if b and b.group(1)==end and 'scenario' not in body and 'segment' not in body:ctx.add(cid)
    facts=collections.defaultdict(set)
    for tag,cid,value in re.findall(r'<in-bse-fin:(\w+) contextRef="([^"]+)"[^>]*>([^<]*)</in-bse-fin:\1>',text):
        if cid in ctx:facts[tag].add(value.strip())
    def one(tags,scale):
        for t in tags:
            vals={float(v) for v in facts.get(t,()) if re.fullmatch(r'-?\d+(\.\d+)?',v)}
            if len(vals)==1:return next(iter(vals))*scale
            if len(vals)>1:return None                                   # conflicting full-year values: unusable
        return None
    basis={v.lower() for v in facts.get('NatureOfReportStandaloneConsolidated',())}
    audited={v for v in facts.get('WhetherResultsAreAuditedOrUnaudited',())}
    out={k:one(v,1e-7) for k,v in FIELDS.items()};out.update({k:one(v,1.) for k,v in PER_SHARE.items()})
    out.update(fy=fy,basis=next(iter(basis)) if len(basis)==1 else None,audited=audited=={'Audited'})
    return out

SPLIT=re.compile(r'(?:split|sub-?division).*?r[se]\.?\s*([\d.]+).*?r[se]\.?\s*([\d.]+)',re.I);BONUS=re.compile(r'bonus\D{0,3}(\d+)\s*:\s*(\d+)',re.I)
NOT_EQUITY=re.compile(r'debenture|\bdeb\b|deb\d|ncrps|preference',re.I)   # bonus debentures / preference shares leave the equity count unchanged
def action_factors(rows):
    """[(ex_date, factor)] for bonuses and splits: shares then multiply by `factor`. None if any such action is unreadable."""
    out=[]
    for x in rows if isinstance(rows,list) else []:
        subject=' '.join(str(x.get('subject','')).split())
        if not re.search(r'bonus|split|sub-?division|consolidat',subject,re.I) or NOT_EQUITY.search(subject):continue
        try:ex=datetime.strptime(x.get('exDate',''),'%d-%b-%Y').date().isoformat()
        except ValueError:return None
        if (m:=BONUS.search(subject)):a,b=map(int,m.groups());out.append((ex,(a+b)/b));continue
        if (m:=SPLIT.search(subject)) and float(m.group(2))>0:out.append((ex,float(m.group(1))/float(m.group(2))));continue
        return None
    return out

def rupees(html):
    """FY2025 integrated filings state amounts in rupees ('INR (in Actuals)') without a rounding row, which the app's
    parser accepts only for income-only reads. Research-only shim: state that unit explicitly, change nothing else."""
    html=re.sub(r'<!--.*?-->','',html,flags=re.S)                       # the rounding row is often commented out
    if 'Level of rounding used in financial results' in html or len(re.findall(r'INR\s*\(in Actuals\)',html))!=1:return html
    return re.sub(r'INR\s*\(in Actuals\)','INR</td></tr><tr><td>Level of rounding used in financial results</td><td>Rupees',html,count=1)

def integrated_shares(html):
    """(crore shares at the year end, basic EPS) of an integrated filing: paid-up equity capital / face value in the
    filing's unit, and the annual-column EPS (continuing operations first, as for net profit)."""
    from marketdesk.fallbacks import Tables
    html=re.sub(r'<!--.*?-->','',html,flags=re.S);p=Tables();p.feed(html);rows=[r for t in p.tables for r in t]
    def last(label):
        found=[[c for c in r[r.index(label)+1:] if re.fullmatch(r'-?[\d,]+(\.\d+)?',c.strip())] for r in rows if label in r]
        vals={float(v[-1].replace(',','')) for v in found if v}
        return next(iter(vals)) if len(vals)==1 else None
    unit=next((r[r.index('Level of rounding used in financial results')+1] for r in rows if 'Level of rounding used in financial results' in r),None)
    actual=any(re.fullmatch(r'INR\s*\(in Actuals\)',c) for r in rows for c in r)
    scale={'Lakhs':1e5,'Crores':1e7,'Rupees':1.}.get(unit,1. if actual and unit is None else None)
    paid,face=last('Paid-up equity share capital'),last('Face value of equity share capital')
    eps=last('Basic earnings (loss) per share from continuing operations') or last('Basic earnings (loss) per share from continuing and discontinued operations')
    return (paid*scale/face/1e7 if paid and face and scale else None),eps

DIVIDEND=re.compile(r'dividend\D*?(?:rs\.?|re\.?|₹)\s*([\d.]+)',re.I)
def dividends(rows,factors):
    """[(ex_date, rupees per share in today's units)] from NSE corporate actions; unreadable amounts are skipped."""
    out=[]
    for x in rows if isinstance(rows,list) else []:
        subject=' '.join(str(x.get('subject','')).split())
        if not re.search(r'dividend',subject,re.I):continue
        try:ex=datetime.strptime(x.get('exDate',''),'%d-%b-%Y').date().isoformat()
        except ValueError:continue
        amounts=[float(a) for a in DIVIDEND.findall(subject) if a.replace('.','',1).isdigit()]
        if not amounts:continue
        later=1.
        for day,k in factors:
            if day>ex:later*=k
        out.append((ex,sum(amounts)/later))
    return sorted(out)

def build():
    from marketdesk.fallbacks import nse_annual
    isins=isin_map();db=sqlite3.connect(f'file:{ROOT}/data/marketdesk.sqlite3?mode=ro',uri=True)
    latest={s:json.loads(d) for s,d in db.execute('SELECT symbol,details FROM fundamentals')}
    records={};divs={};problems=collections.Counter()
    for d in sorted(p for p in CACHE.iterdir() if p.is_dir()):
        sym=d.name;years={}
        try:actions=json.loads((d/'actions.json').read_text())
        except (OSError,ValueError):actions=None
        factors=action_factors(actions) if actions is not None else None
        try:listing=json.loads((d/'listing.json').read_text())
        except (OSError,ValueError):listing=[]
        def filed(fy,kind):
            for x in listing if isinstance(listing,list) else []:
                if x.get('toDate')==f'31-Mar-{fy}' and x.get('consolidated')==kind:
                    try:return datetime.strptime(str(x.get('filingDate',''))[:11],'%d-%b-%Y').date().isoformat()
                    except ValueError:return None
        for fy in YEARS:
            f=next((f for f in (d/f'{fy}_C.xml',d/f'{fy}_N.xml') if f.exists()),None)
            if not f:continue
            r=parse_xbrl(f.read_text(errors='replace'),fy);r['filed']=filed(fy,'Consolidated' if f.name.endswith('_C.xml') else 'Non-Consolidated')
            if not r['audited'] or r['basis'] not in ('consolidated','standalone'):problems['unaudited or unknown basis']+=1;continue
            # Exactly zero reserves is how later files leave the line blank (they report equity directly), not a real value.
            if r['equity'] is None and r['paid_up'] is not None and r['reserves'] not in (None,0.):r['equity'],r['equity_method']=r['paid_up']+r['reserves'],'paid_up+reserves'
            years[fy]=r
        f=next((f for f in (d/'2025_C.html',d/'2025_S.html') if f.exists()),None)
        if f:
            try:
                a=nse_annual(rupees(f.read_text(errors='replace')),sym,isins.get(sym),'cached');years[2025]=dict(fy=2025,basis=a['statement_type'],audited=True,
                    revenue=a.get('revenue'),finance_costs=a['finance_costs'],pbt=a['profit_before_tax'],net_profit=a.get('net_profit'),equity=a['equity'],
                    cash=a['cash_and_cash_equivalents'],borrowings=a.get('total_borrowings'))
                years[2025]['shares_raw'],years[2025]['eps']=integrated_shares(rupees(f.read_text(errors='replace')))
                try:
                    integ=json.loads((d/'integrated.json').read_text());integ=integ if isinstance(integ,list) else integ.get('data',[])
                    when=[x.get('broadcast_Date','') for x in integ if str(x.get('qe_Date','')).upper()=='31-MAR-2025' and (x.get('consolidated') or '')[:1]==f.name[5]]
                    years[2025]['filed']=datetime.strptime(min(when)[:11],'%d-%b-%Y').date().isoformat() if when else None
                except (OSError,ValueError):pass
            except (ValueError,KeyError,TypeError):problems['FY2025 integrated filing unparsed']+=1
        l=(latest.get(sym) or {}).get('latest') or {};sm=(latest.get(sym) or {}).get('summary') or {}
        if l.get('period_end')=='2026-03-31' and l.get('units_in')=='crore' and l.get('source')=='nse_audited_filing':
            same=lambda k:sm.get(k) if sm.get(k+'_period')==l.get('period') and sm.get('statement_type')==l.get('statement_type') and sm.get('units')=='crore' else None
            years[2026]=dict(fy=2026,basis=l['statement_type'],audited=True,revenue=same('revenue'),finance_costs=l.get('finance_costs'),pbt=l.get('profit_before_tax'),
                net_profit=same('net_profit'),equity=l.get('equity'),cash=l.get('cash_and_cash_equivalents'),borrowings=l.get('total_borrowings'))
            for raw in sorted((ROOT/'data'/'filings'/sym).glob('*.html')):       # the app's cached filings; use the verified FY2026 one
                html=rupees(raw.read_text(errors='replace'))
                try:ok=(lambda a:a['period_end']=='2026-03-31' and a['statement_type']==l['statement_type'])(nse_annual(html,sym,isins.get(sym),'cached',income_only=True))
                except (ValueError,KeyError,TypeError):ok=False
                if ok:years[2026]['shares_raw'],years[2026]['eps']=integrated_shares(html);break
        # Unit slips (a whole file in lakhs tagged as rupees) shift every amount ~100x: drop such a year entirely.
        revs=[r['revenue'] for r in years.values() if r.get('revenue') and r['revenue']>0]
        if len(revs)>=3:
            mid=statistics.median(revs)
            for fy in [fy for fy,r in years.items() if r.get('revenue') and r['revenue']>0 and not 1/20<=r['revenue']/mid<=20]:
                del years[fy];problems['year dropped: amounts ~100x off the company norm']+=1
        # Shares at the results date: Ind AS 33 restates EPS for bonuses and splits up to approval, and the filed face value
        # is the one then in force, so only actions after min(filing date, period end + 60 days) convert to today's units.
        # The profit / EPS count is preferred; paid-up capital / face value is the fallback. Either must sit within 0.5-2x
        # of the company's median count, so a mistyped unit or face value leaves the year blank rather than wrong.
        def counts(r):
            pairs=((r.get('np_owners'),r.get('eps')),(r.get('net_profit_total'),r.get('eps')),(r.get('net_profit'),r.get('eps_cont') or r.get('eps')))
            out=[n/e for n,e in pairs if n and e and n/e>0]
            paid=r['paid_up']/r['face_value'] if r.get('paid_up') and r.get('face_value') else r.get('shares_raw')
            return [c for c in out+([paid] if paid else []) if c>0]
        def later(fy,r):
            cutoff=min(filter(None,(r.get('filed'),(date(fy,3,31)+timedelta(days=LAG)).isoformat())))
            return math.prod(k for ex,k in factors if ex>cutoff)
        # Every count a filing implies (profit / EPS variants; paid-up capital / face value) can be wrong in its own way:
        # minority interests, a mis-tagged EPS, a mistyped face value or unit. Keep, per year, the one nearest the
        # company's median count in today's units, and only within 0.5-2x of it.
        cand={fy:[c*later(fy,r) for c in counts(r)] for fy,r in years.items()} if factors is not None else {}
        allc=[c for v in cand.values() for c in v];median=statistics.median(allc) if allc else None
        for fy,v in cand.items():
            if not v:continue
            best=min(v,key=lambda c:abs(math.log(c/median)))
            if not .5<=best/median<=2:problems['share count outside 0.5-2x of the company median']+=1;continue
            r=years[fy];r['shares_now_units']=best
            # Owners' continuing earnings: continuing EPS x the count at the results date (excludes minority interests and
            # discontinued businesses); kept only when within 0.5-2x of reported net profit, else net profit stands.
            eps=r.get('eps_cont') if r.get('eps_cont') is not None else r.get('eps');raw=best/later(fy,r)
            est=eps*raw if eps is not None else None;ref=r.get('np_owners') or r.get('net_profit')
            r['earnings']=est if est is not None and ref and est*ref>0 and .5<=est/ref<=2 else r.get('net_profit')
        for fy,r in years.items():r.setdefault('earnings',r.get('net_profit'))
        for fy,r in years.items():
            if r.get('borrowings') is None and r.get('borrowings_nc') is not None and r.get('borrowings_c') is not None:r['borrowings']=r['borrowings_nc']+r['borrowings_c']
            r['available_from']=(date(fy,3,31)+timedelta(days=LAG)).isoformat()
            # Shares in today's units: raw count at the year end times every later bonus/split ratio.
        if years:records[sym]={str(k):v for k,v in sorted(years.items())}
        if factors is not None and actions:divs[sym]=dividends(actions,factors)
        if factors is None:problems['unreadable bonus/split action']+=1
    OUT.write_text(json.dumps(dict(created=time.strftime('%Y-%m-%d %H:%M'),lag_days=LAG,records=records,dividends=divs,problems=dict(problems)),indent=0))
    print('built',len(records),'stocks',dict(problems))

def coverage():
    names=universe();R=json.loads(OUT.read_text())['records'];keys=('revenue','finance_costs','pbt','net_profit','equity','borrowings','cash','assets','ocf','shares_now_units')
    print('universe',len(names))
    for fy in range(2018,2027):
        rows=[R[s][str(fy)] for s in names if s in R and str(fy) in R[s]]
        print(fy,'filers',f'{len(rows)/len(names):.0%}',' '.join(f"{k}={sum(r.get(k) is not None for r in rows)/len(names):.0%}" for k in keys))

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('command',choices=('fetch','refetch_actions','build','coverage'));globals()[a.parse_args().command]()
