"""Corporate news for the two news baskets: collection, normalization, deduplication and rule-based analysis.

Source (verified 19 Oct 2026 from this Mac): NSE corporate announcements JSON
  https://www.nseindia.com/api/corporate-announcements?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
It is the exchange's own primary disclosure feed (SEBI LODR Reg. 30 filings), public, unauthenticated and
undocumented as an API: it can change shape or block clients without notice, so every field is validated and a
failed poll blocks new news-driven entries rather than inventing data. Each row carries the symbol, ISIN,
company, NSE subject (`desc`), a one-line summary (`attchmntText`), the filing PDF and the broadcast time
(`an_dt`, IST). PDFs of material categories are read (first pages only) to find disclosed amounts.

Not connected: BSE announcements (needs a separate scrip-code map), paid news wires, social media.
The optional LLM interpreter is an interface only (NullInterpreter); nothing here calls a model.

Analysis is deterministic. Missing inputs (order value, revenue, margins, price history) are listed in
`missing` and lower `confidence`; they are never filled with assumed values."""
import hashlib,json,math,re,time
from datetime import datetime,timedelta,timezone
import requests

IST=timezone(timedelta(hours=5,minutes=30))
SOURCE='nse_corporate_announcements'
ANNOUNCEMENTS='https://www.nseindia.com/api/corporate-announcements'
ATTACHMENT_HOSTS=('nsearchives.nseindia.com','archives.nseindia.com','www.nseindia.com')
ANALYSIS_VERSION='news-rules-v1'

# ---- collection -----------------------------------------------------------------------------------

class NseAnnouncements:
    """Rate-limited reader for the NSE announcement feed. One request per call; PDFs only on request."""
    def __init__(self,min_interval=1.):
        self.session=requests.Session();self.min_interval=min_interval;self.last=0;self.warmed=False
        self.session.headers.update({'User-Agent':'Mozilla/5.0','Accept':'application/json,text/html,*/*',
            'Referer':'https://www.nseindia.com/companies-listing/corporate-filings-announcements'})
    def _wait(self):
        delay=self.min_interval-(time.monotonic()-self.last)
        if delay>0:time.sleep(delay)
        self.last=time.monotonic()
    def fetch(self,start,end):
        """Raw rows for an inclusive IST date range. Raises ValueError on any malformed response."""
        if not self.warmed:
            try:self._wait();self.session.get('https://www.nseindia.com',timeout=(5,10))
            except requests.RequestException:pass
            self.warmed=True
        self._wait()
        try:r=self.session.get(ANNOUNCEMENTS,params={'index':'equities','from_date':start.strftime('%d-%m-%Y'),'to_date':end.strftime('%d-%m-%Y')},timeout=(5,30))
        except requests.RequestException as e:raise ValueError(f'NSE announcements unavailable: {e.__class__.__name__}') from None
        if r.status_code!=200:raise ValueError(f'NSE announcements HTTP {r.status_code}')
        try:rows=r.json()
        except ValueError:raise ValueError('NSE announcements returned non-JSON') from None
        if isinstance(rows,dict):rows=rows.get('data')
        if not isinstance(rows,list):raise ValueError('NSE announcements: unexpected payload shape')
        return rows
    def attachment_text(self,url,pages=3,max_bytes=4_000_000):
        """Text of the first pages of a filing PDF on an NSE host, or None."""
        from urllib.parse import urlsplit
        u=urlsplit(url or '')
        if u.scheme!='https' or u.hostname not in ATTACHMENT_HOSTS or not u.path.lower().endswith('.pdf'):return None
        self._wait()
        try:
            r=self.session.get(url,timeout=(5,30))
            if r.status_code!=200 or len(r.content)>max_bytes:return None
            import pymupdf
            with pymupdf.open(stream=r.content,filetype='pdf') as doc:
                return '\n'.join(doc[i].get_text() for i in range(min(pages,doc.page_count)))[:20000]
        except Exception:return None

class NullInterpreter:
    """Optional LLM hook. Returns None, so analysis stays rule-based; a real interpreter must return
    {'label','rationale','confidence'} and is recorded separately, never replacing missing facts."""
    name='none'
    def interpret(self,item,analysis):return None

def parse_time(text,fmt):
    try:return datetime.strptime(text.strip(),fmt).replace(tzinfo=IST)
    except (AttributeError,ValueError):return None

def normalize(row,retrieved_at):
    """One validated news item, or None for rows without identity or timestamp."""
    if not isinstance(row,dict):return None
    symbol=(row.get('symbol') or '').strip().upper();isin=(row.get('sm_isin') or '').strip().upper()
    published=parse_time(row.get('an_dt'),'%d-%b-%Y %H:%M:%S') or parse_time(row.get('sort_date'),'%Y-%m-%d %H:%M:%S')
    if not symbol or not published or not row.get('seq_id'):return None
    headline=re.sub(r'\s+',' ',(row.get('attchmntText') or '').strip())
    subject=(row.get('desc') or '').strip()
    disseminated=parse_time(row.get('exchdisstime'),'%d-%b-%Y %H:%M:%S')
    return dict(source=SOURCE,source_id=str(row['seq_id']),symbol=symbol,isin=isin or None,company=(row.get('sm_name') or '').strip(),
        subject=subject,headline=headline,url=row.get('attchmntFile') or None,industry=row.get('smIndustry'),
        published_at=published.isoformat(),disseminated_at=disseminated.isoformat() if disseminated else None,
        retrieved_at=retrieved_at.isoformat(),
        # NSE gives broadcast time only; the underlying event time (board meeting, order date) is not a field.
        event_at=None,dedup_key=dedup_key(symbol,subject,headline))

def canonical(text):
    text=(text or '').lower();text=re.sub(r'https?://\S+','',text)
    text=re.sub(r'\b(limited|ltd|the|has|informed|exchange|about|regarding|intimation|disclosure|under|regulation|reg)\b','',text)
    return re.sub(r'[^a-z0-9]+',' ',text).strip()

def dedup_key(symbol,subject,headline):
    return hashlib.sha256(f'{symbol}|{canonical(subject)}|{canonical(headline)}'.encode()).hexdigest()[:24]

# ---- classification -------------------------------------------------------------------------------

LABELS=('Strongly Negative','Negative','Neutral','Positive','Strongly Positive')
ROUTINE_SUBJECTS=('certificate under sebi','newspaper publication','trading window','shareholders meeting','record date',
    'analysts/institutional','investor presentation','esop','esos','structural digital database','monitoring agency',
    'allotment of securities','loss of share certificate','duplicate share','spurt in volume','price movement','news verification',
    'clarification','corrigendum','statement of deviation','book closure','agm','postal ballot','compliance certificate')
# (category, subject regex, text regex, direction -2..2, momentum-eligible, catalyst-eligible, governance flag)
RULES=(
 ('insolvency_default',None,r'insolven|\bcirp\b|default(ed)? (on|in) (payment|repayment)|fraud|forensic audit|wilful defaulter',-2,False,False,True),
 ('auditor_resignation',r'auditor',r'resign',-2,False,False,True),
 ('regulatory_action',r'action\(s\)|litigation|dispute',r'penalt|show cause|sebi order|search|raid|investigation|demand notice|suspension|import alert|warning letter',-1,False,False,True),
 ('kmp_resignation',r'resignation|cessation',r'(chief executive|ceo|chief financial|cfo|managing director|whole[- ]time director)',-1,False,False,True),
 ('rating_downgrade',r'credit rating',r'downgrad|revised (to )?negative|watch with negative|outlook.{0,20}negative',-1,False,False,False),
 ('rating_upgrade',r'credit rating',r'upgrad|revised (to )?positive|outlook.{0,20}positive',1,True,True,False),
 ('order_win',r'bagging|orders?/contracts',r'(order|contract|letter of award|\bloa\b|letter of intent|\bloi\b|work order|purchase order)',1,True,True,False),
 ('order_win',None,r'(bag|bagg|receiv|secur|won|award|win)\w*.{0,60}(order|contract|letter of award|\bloa\b)',1,True,True,False),
 ('debt_reduction',None,r'prepa(id|y)|repaid|debt[- ]free|delever|reduc\w* (its )?(debt|borrowing)',1,False,True,False),
 ('capacity_expansion',None,r'capacity expansion|commission(ed|ing)|commercial (production|operation)|greenfield|brownfield|\bcapex\b|new (plant|facility|manufacturing)',1,True,True,False),
 ('regulatory_approval',None,r'usfda approval|final approval|tentative approval|\beir\b|establishment inspection report|approval (from|of) (the )?(dcgi|cdsco|usfda|ema)',1,True,True,False),
 ('product_launch',None,r'launch(ed|es)? (of )?(a |its )?(new )?(product|brand|drug|model|vehicle|service)',1,True,True,False),
 ('acquisition',r'acquisition|scheme of arrangement|amalgamation',r'acqui|merger|amalgamat|stake',1,True,True,False),
 ('buyback',r'buy ?back',r'buy ?back',1,True,False,False),
 ('insider_buy',r'takeover|insider|\bsast\b|\bpit\b',r'acqui|purchase|bought|creeping',1,True,False,False),
 ('insider_sell',r'takeover|insider|\bsast\b|\bpit\b',r'dispos|sale|sold|pledge|encumbr',-1,False,False,False),
 ('results',r'financial result|outcome of board meeting|integrated filing',r'financial result|unaudited|audited',0,False,True,False),
 ('policy',None,r'production linked incentive|\bpli\b|government (policy|notification)|tariff (order|revision)',1,False,True,False),
 ('fund_raise',None,r'\bqip\b|qualified institutions placement|preferential (issue|allotment)|rights issue|fund rais',0,False,False,False),
 ('rating_reaffirmed',r'credit rating',r'.',0,False,False,False),
 ('management_change',r'appointment|change in (management|director)',r'.',0,False,False,False),
)
STRENGTH={-2:'Strongly Negative',-1:'Negative',0:'Neutral',1:'Positive',2:'Strongly Positive'}

def classify(subject,text):
    """(category, direction, flags) from the NSE subject and the summary/attachment text."""
    s=(subject or '').lower();t=(text or '').lower()
    if 'incorporat' in t and ('subsidiar' in t or 'wholly owned' in t):return 'routine',0,dict(momentum=False,catalyst=False,governance=False)
    for category,sub,txt,direction,momentum,catalyst,gov in RULES:
        if sub and not re.search(sub,s):continue
        if txt and not re.search(txt,t+' '+s):continue
        return category,direction,dict(momentum=momentum,catalyst=catalyst,governance=gov)
    if any(r in s for r in ROUTINE_SUBJECTS):return 'routine',0,dict(momentum=False,catalyst=False,governance=False)
    return 'other',0,dict(momentum=False,catalyst=False,governance=False)

AMOUNT=re.compile(r'(?:(rs\.?|inr|₹|usd|us\s?\$|\$|eur|€)\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(crores?|crs?\.?|lakhs?|lacs?|million|mn|billion|bn)\b',re.I)
TO_CRORE={'crore':1.,'crores':1.,'cr':1.,'cr.':1.,'crs':1.,'crs.':1.,'lakh':.01,'lakhs':.01,'lac':.01,'lacs':.01,'million':.1,'mn':.1,'billion':100.,'bn':100.}

def amounts(text):
    """INR amounts in crore, plus foreign-currency mentions left unconverted (no FX rate is assumed)."""
    inr=[];foreign=[]
    for m in AMOUNT.finditer(text or ''):
        currency=(m.group(1) or '').lower().replace(' ','');unit=m.group(3).lower()
        try:value=float(m.group(2).replace(',',''))
        except ValueError:continue
        if value<=0:continue
        if currency in ('usd','us$','$','eur','€'):foreign.append(dict(currency=currency.upper(),value=value,unit=unit,text=m.group(0)))
        elif currency or unit.startswith(('cr','lakh','lac')):inr.append(dict(crore=value*TO_CRORE.get(unit.rstrip('.'),TO_CRORE.get(unit,0)),text=m.group(0)))
    return inr,foreign

def execution_years(text):
    m=re.search(r'(?:executed|execution|completed|delivered|period of|over|within)\D{0,25}([0-9]+(?:\.[0-9]+)?)\s*(months?|years?)',text or '',re.I)
    if not m:return None
    n=float(m.group(1));return n/12 if m.group(2).lower().startswith('month') else n

# ---- analysis -------------------------------------------------------------------------------------

WEIGHTS=dict(materiality=25,sentiment=20,novelty=15,credibility=10,freshness=10,surprise=10,run_up=5,profitability=5)

def band(x,steps):
    for limit,score in steps:
        if x>=limit:return score
    return steps[-1][1] if steps else 0

def analyse(item,*,revenue=None,revenue_period=None,prior=(),bars=(),price=None,now,verification,interpreter=None,cfg=None):
    """News Impact Score 0-100 (strength of the event in the direction of `label`) with its components.

    revenue   latest annual revenue in crore (fundamentals summary) or None
    prior     earlier analysed items for the same symbol (dicts with category, amount_crore, published_at)
    bars      completed daily candles strictly before publication (oldest first)
    price     latest trade price observed at analysis time, or None"""
    cfg=cfg or {};text=' '.join(x for x in (item.get('headline'),item.get('attachment_text')) if x)
    category,direction,flags=classify(item.get('subject'),text)
    inr,foreign=amounts(text);amount=max((a['crore'] for a in inr),default=None)
    years=execution_years(text);missing=[];comp={};notes=[]
    # 1-2 materiality and revenue impact
    ratio=None
    if category in ('order_win','acquisition','capacity_expansion','debt_reduction','buyback'):
        if amount is None:missing.append('Event value not disclosed in the filing text'+(' (only foreign-currency amounts, not converted)' if foreign else ''))
        if not revenue or revenue<=0:missing.append('Annual revenue unavailable for materiality')
        if amount is not None and revenue and revenue>0:
            ratio=amount/revenue;annual=ratio/years if years and years>1 else ratio
            comp['materiality']=band(annual,((.25,100),(.10,85),(.05,65),(.02,45),(0,20)))
            notes.append(f'Value ₹{amount:,.0f} cr = {ratio*100:.1f}% of {revenue_period or "latest annual"} revenue'+(f'; spread over {years:g} years ({annual*100:.1f}% a year)' if years and years>1 else ''))
            if years is None and category=='order_win':missing.append('Execution period not disclosed; revenue recognition timing unknown')
    elif category in ('rating_upgrade','rating_downgrade','regulatory_approval','kmp_resignation','auditor_resignation','insolvency_default','regulatory_action'):
        comp['materiality']={'insolvency_default':100,'auditor_resignation':90,'regulatory_action':60,'kmp_resignation':55}.get(category,60)
    if category=='order_win':missing.append('Expected margins not disclosed')
    # 3 profitability (results only; uses reported growth, never an estimate)
    growth=cfg.get('profit_growth')
    if category=='results':
        if growth is None:missing.append('Reported profit growth unavailable')
        else:
            comp['profitability']=band(abs(growth),((50,100),(25,80),(10,60),(0,40)))
            direction=1 if growth>10 else -1 if growth<-10 else 0
            notes.append(f'Latest reported annual profit growth {growth:+.1f}%')
    # sentiment strength
    if direction>0 and comp.get('materiality',0)>=85:direction=2
    comp['sentiment']={2:100,1:65,0:15,-1:65,-2:100}[direction]
    # 4 credibility: primary exchange filing, identity-checked
    comp['credibility']=100 if verification=='verified' else 30
    # 5 novelty against earlier filings for the same company
    same=[p for p in prior if p.get('category')==category and category not in ('other','routine')]
    repeat=[p for p in same if amount and p.get('amount_crore') and abs(p['amount_crore']/amount-1)<=.02]
    comp['novelty']=10 if repeat else 50 if same else 100
    if repeat:notes.append('Same category and amount as an earlier filing: likely a repeat of previously announced information')
    # 9 freshness
    published=datetime.fromisoformat(item['published_at']);age=(now-published).total_seconds()/3600
    comp['freshness']=band(-age,((-2,100),(-6,80),(-24,50),(-72,20),(-1e9,0)))
    # 6/8 surprise and recent price reaction (completed sessions before the news; live price after)
    run_up=move=None
    closes=[b['close'] for b in bars]
    if len(closes)>=6:
        run_up=(closes[-1]/closes[-6]-1)*100;comp['run_up']=band(-abs(run_up),((-3,100),(-8,60),(-15,25),(-1e9,0)))
    else:missing.append('Fewer than 6 completed sessions before the news')
    if closes and price:
        move=(price/closes[-1]-1)*100;comp['surprise']=band(-max(0,move*(1 if direction>=0 else -1)),((-2,100),(-5,60),(-8,20),(-1e9,0)))
    else:missing.append('No live price after the news yet')
    missing.append('Sector-wide effect not modeled')
    have={k:w for k,w in WEIGHTS.items() if k in comp};cover=sum(have.values())/sum(WEIGHTS.values())
    score=round(sum(comp[k]*w for k,w in have.items())/sum(have.values()),1) if have else 0.
    confidence=round(cover*(1 if verification=='verified' else .4),2)
    llm=(interpreter or NullInterpreter()).interpret(item,None)
    return dict(version=ANALYSIS_VERSION,category=category,label=STRENGTH[direction],direction=direction,impact_score=score,
        confidence=confidence,components=comp,coverage=round(cover,2),missing=missing,notes=notes,amount_crore=amount,
        foreign_amounts=foreign,materiality_ratio=ratio,execution_years=years,run_up_pct=run_up,post_news_move_pct=move,
        age_hours=round(age,2),momentum_eligible=flags['momentum'],catalyst_eligible=flags['catalyst'],governance=flags['governance'],
        verification=verification,llm=llm,interpreter=(interpreter or NullInterpreter()).name)
