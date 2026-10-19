"""Insurer annual records from audited NSE insurance filings (LI and GI formats).

Listed insurers report under IRDAI's financial statement regulations (Indian GAAP).
Their NSE integrated filings print profit before tax, share capital, reserves,
borrowings and the cash-flow statement's closing cash and cash equivalents. The
format has no finance-cost line and no lease-liability line. A finance cost is
treated as not applicable only when the same audited balance sheet prints
borrowings of exactly zero; an insurer with borrowings stays incomplete until
its interest cost is found. Nothing is set to zero."""
import copy,json,re
from datetime import datetime,timezone
from .bank_inputs import INSURER_SOURCE as SOURCE,INSURER_FRAMEWORK as FRAMEWORK,SCALES,_index,_filing,saved_proof
from .storage import number

KINDS=('_LI_','_GI_')
REVENUE=('Gross premium income','Gross Premiums Written')
NET_PROFIT=('Profit/ (loss) after tax and Extraordinary Items','Profit / (loss) after tax')


def insurer_annual(html,symbol,isin,url,income_only=False):
    from .fallbacks import Tables
    if len(html.encode())>3_000_000:raise ValueError('NSE document exceeds parsing limit')
    parser=Tables();parser.feed(html);tables=parser.tables
    def matches(rows,label):return [row[i+1:] for row in rows for i,cell in enumerate(row) if cell.casefold()==label.casefold()]
    def starts(rows,prefix):return [row[1:] for row in rows if row and row[0].startswith(prefix)]
    def one(found,label):
        if len(found)!=1:raise ValueError('Missing/ambiguous NSE insurance field: '+label)
        return found[0]
    meta=[t for t in tables if matches(t,'NSE Symbol') and matches(t,'Type Of Insurance')]
    if len(meta)!=1:raise ValueError('Unsupported or ambiguous NSE insurance template')
    meta=meta[0];scalar=lambda label:one(matches(meta,label),label)[0]
    if scalar('NSE Symbol')!=symbol or scalar('ISIN')!=isin or scalar('Class of security')!='Equity':raise ValueError('NSE filing identity mismatch')
    currency=scalar('Description of presentation currency')
    if currency=='INR (in Actuals)' and not matches(meta,'Level of rounding used in financial results'):currency,unit='INR','Rupees'
    else:unit=scalar('Level of rounding used in financial results')
    if currency!='INR' or unit not in SCALES:raise ValueError('NSE currency/scale not verified')
    scale=SCALES[unit];basis=scalar('Nature of report standalone or consolidated').lower()
    if basis not in ('standalone','consolidated'):raise ValueError('Unknown NSE statement basis')
    def day(value):
        for form in ('%d-%m-%Y','%d-%b-%Y'):
            try:return datetime.strptime(value,form).date()
            except ValueError:pass
        raise ValueError('NSE reporting date not verified')
    start=day(scalar('Date of start of financial year'));end=day(scalar('Date of end of financial year'))
    if not 364<=(end-start).days<=366 or end>datetime.now(timezone.utc).date():raise ValueError('NSE annual period not verified')
    if one(starts(meta,'Whether results are audited or unaudited for the Year to date'),'year-to-date audit')[0]!='Audited':raise ValueError('NSE annual filing is not audited')
    def column(rows):
        s=one(matches(rows,'Date of start of reporting period'),'start');e=one(matches(rows,'Date of end of reporting period'),'end')
        # Older templates word the column audit row "Whether accounts are audited or unaudited".
        a=one(matches(rows,'Whether results are audited or unaudited')+matches(rows,'Whether accounts are audited or unaudited'),'audit');b=one(matches(rows,'Nature of report standalone or consolidated'),'basis')
        if len({len(s),len(e),len(a),len(b)})!=1:raise ValueError('NSE column dimensions mismatch')
        hits=[i for i,x in enumerate(s) if day(x)==start and day(e[i])==end and a[i]=='Audited' and b[i].lower()==basis]
        if len(hits)!=1:raise ValueError('NSE annual column not unique')
        return hits[0],len(s)
    sources={}
    def value(cells,col,key,label):
        if len(cells)!=col[1]:raise ValueError('NSE value column mismatch: '+label)
        text=cells[col[0]].replace(',','').strip()
        if re.fullmatch(r'\(\d+(?:\.\d+)?\)',text):text='-'+text[1:-1]
        v=number(text) if text else None
        if v is None:raise ValueError('NSE insurance value not numeric: '+label)
        sources[key]={'source':SOURCE,'source_url':url,'label':label,'reported_value':text,'source_units':unit,'conversion_to_crore':scale,
                      'rounding_quantum':scale*10**(-len(text.split('.')[1]) if '.' in text else 0),'period_end':end.isoformat(),'statement_type':basis}
        return v*scale
    # Results sections after the first dated table share its quarter / year-to-date columns.
    dated=[t for t in tables if matches(t,'Date of start of reporting period') and len(matches(t,'Date of start of reporting period')[0])==2]
    if not dated:raise ValueError('Dated NSE insurance results table unavailable')
    rc=column(dated[0])
    def results(labels,key):
        found=[(label,cells) for label in labels for t in tables for cells in matches(t,label) if len(cells)==rc[1] and any(c.strip() for c in cells)]
        label,cells=one(found,key)
        return value(cells,rc,key,label)
    out={'profit_before_tax':results(('Profit/ (loss) before tax',),'profit_before_tax'),'revenue':results(REVENUE,'revenue'),'net_profit':results(NET_PROFIT,'net_profit')}
    if income_only:
        # A comparative year supplies growth only: its results columns are verified, its balance sheet is not used.
        out.update(period=end.strftime('%b %Y'),period_end=end.isoformat(),statement_type=basis,units_in='crore',sources=sources,framework=FRAMEWORK,income_only=True)
        return out
    balance=[t for t in tables if all(matches(t,k) for k in ('Share capital','Reserves and surplus','Borrowings','Total Sources of Funds'))]
    if len(balance)!=1:raise ValueError('Unsupported or ambiguous NSE insurance balance sheet')
    balance=balance[0];bc=column(balance)
    for key,label in (('capital','Share capital'),('reserves','Reserves and surplus'),('borrowings','Borrowings'),('total_sources','Total Sources of Funds')):
        out[key]=value(one(matches(balance,label),label),bc,key,label)
    flows=[t for t in tables if matches(t,'Cash and cash equivalents cash flow statement at end of period')]
    if len(flows)!=1:raise ValueError('Audited closing cash unavailable')
    out['cash']=value(one(matches(flows[0],'Cash and cash equivalents cash flow statement at end of period'),'cash'),column(flows[0]),'cash','Cash and cash equivalents cash flow statement at end of period')
    if out['borrowings']<0 or out['cash']<0:raise ValueError('Negative insurer input requires review')
    captions=[c for row in balance for c in row if re.search(r'[a-z]',c,re.I)]
    out.update(period=end.strftime('%b %Y'),period_end=end.isoformat(),statement_type=basis,units_in='crore',sources=sources,balance_captions=captions,
               lease_caption_present=any(re.search(r'\blease',c,re.I) for c in captions),
               borrowings_printed_zero=sources['borrowings']['reported_value'] in ('0','0.00','0.0'),framework=FRAMEWORK)
    return out


def insurer_xbrl(xml,symbol,isin,url,income_only=False):
    """The same verified values from an NSE insurance XBRL instance (facts in rupees)."""
    import xml.etree.ElementTree as ET
    if len(xml.encode())>3_000_000:raise ValueError('NSE document exceeds parsing limit')
    root=ET.fromstring(xml.encode('utf-8'));X='{http://www.xbrl.org/2003/instance}'
    contexts={}
    for c in root.iter(X+'context'):
        if c.find('.//'+X+'segment') is not None or c.find('.//'+X+'scenario') is not None:continue
        period=c.find(X+'period');inst=period.find(X+'instant')
        contexts[c.get('id')]=('instant',inst.text) if inst is not None else ('duration',period.find(X+'startDate').text,period.find(X+'endDate').text)
    facts={}
    for el in root:
        if el.get('contextRef') in contexts:facts.setdefault(el.tag.split('}')[-1],[]).append((contexts[el.get('contextRef')],(el.text or '').strip(),el.get('decimals')))
    def text(tag):
        values={v for _,v,_ in facts.get(tag,[])}
        if len(values)!=1:raise ValueError('Missing/ambiguous NSE XBRL field: '+tag)
        return values.pop()
    filed_isin=text('ISIN')
    # One filing types the letter O for zero in its ISIN; with the NSE symbol matching, that is the same security.
    if text('Symbol')!=symbol or (filed_isin!=isin and filed_isin.replace('O','0')!=isin.replace('O','0')):raise ValueError('NSE filing identity mismatch')
    if text('DescriptionOfPresentationCurrency') not in ('INR','INR (in Actuals)'):raise ValueError('NSE currency not verified')
    basis=text('NatureOfReportStandaloneConsolidated').lower()
    if basis not in ('standalone','consolidated'):raise ValueError('Unknown NSE statement basis')
    start=datetime.fromisoformat(text('DateOfStartOfFinancialYear')).date();end=datetime.fromisoformat(text('DateOfEndOfFinancialYear')).date()
    if not 364<=(end-start).days<=366 or end>datetime.now(timezone.utc).date():raise ValueError('NSE annual period not verified')
    year=('duration',start.isoformat(),end.isoformat());at=('instant',end.isoformat())
    if [v for ctx,v,_ in facts.get('WhetherResultsAreAuditedOrUnaudited',[]) if ctx==year]!=['Audited']:raise ValueError('NSE annual filing is not audited')
    sources={}
    def amount(tag,ctx,key):
        hits=[(v,d) for c,v,d in facts.get(tag,[]) if c==ctx]
        if len(hits)!=1 or number(hits[0][0]) is None:raise ValueError('Missing/ambiguous NSE XBRL value: '+tag)
        v,d=hits[0];quantum=10**(-int(d))*1e-7 if d not in (None,'INF') else 1e-7
        sources[key]={'source':SOURCE,'source_url':url,'label':tag,'reported_value':v,'source_units':'Rupees','conversion_to_crore':1e-7,'rounding_quantum':quantum,'period_end':end.isoformat(),'statement_type':basis}
        return number(v)*1e-7
    out={'profit_before_tax':amount('ProfitOrLossBeforeTax',year,'profit_before_tax'),'revenue':amount('GrossPremiumsWritten',year,'revenue'),'net_profit':amount('ProfitLossAfterTax',year,'net_profit')}
    out.update(period=end.strftime('%b %Y'),period_end=end.isoformat(),statement_type=basis,units_in='crore',sources=sources,framework=FRAMEWORK)
    if filed_isin!=isin:out['filed_isin']=filed_isin
    if income_only:out['income_only']=True;return out
    for key,tag in (('capital','ShareCapital'),('reserves','ReservesAndSurplus'),('borrowings','Borrowings'),('cash','CashAndCashEquivalentsCashFlowStatement')):out[key]=amount(tag,at,key)
    if out['borrowings']<0 or out['cash']<0:raise ValueError('Negative insurer input requires review')
    balance_tags=[t for t,v in facts.items() if any(c==at for c,_,_ in v)]
    out.update(balance_captions=balance_tags,lease_caption_present=any(re.search(r'^Lease',t) for t in balance_tags),
               borrowings_printed_zero=number(sources['borrowings']['reported_value'])==0)
    return out


def insurer_income(html,symbol,isin,url):
    return (insurer_xbrl if url.endswith('.xml') else insurer_annual)(html,symbol,isin,url,income_only=True)


def insurer_full(html,symbol,isin,url):
    return (insurer_xbrl if url.endswith('.xml') else insurer_annual)(html,symbol,isin,url)


def fetch_insurer_record(symbol,isin,details,client=None):
    """Newest audited annual insurance filing and the preceding year's, on one basis."""
    from .fallbacks import Fallbacks
    client=client or Fallbacks()
    rows=[r for r in _index(symbol,client,KINDS) if r['day'].month==3 and r['day'].day==31]
    if not rows:raise ValueError('No NSE insurance annual filing')
    newest=max(r['day'] for r in rows)
    def one(day,basis,parser=insurer_full):
        errors=[]
        for row in rows:
            if row['day']==day and row['basis']==basis:
                try:return _filing(symbol,isin,row['url'],client,parser)
                except ValueError as error:errors.append(str(error))
        raise ValueError('; '.join(dict.fromkeys(errors)) or 'No audited '+basis+' insurance filing for '+day.isoformat())
    current_basis=(details.get('latest') or {}).get('statement_type')
    order=[b for b in (current_basis,'consolidated','standalone') if b]
    errors=[];fallback=None
    # Prefer a basis with both audited years, so growth compares like with like.
    for basis in dict.fromkeys(order):
        try:current=one(newest,basis)
        except ValueError as error:errors.append(str(error));continue
        try:return {'current':current,'prior':one(newest.replace(year=newest.year-1),basis,insurer_income)}
        except ValueError as error:errors.append(str(error));fallback=fallback or {'current':current,'prior':None}
    if fallback:return fallback
    raise ValueError('; '.join(dict.fromkeys(errors))[:400])


def insurer_record(details,proofs,symbol,isin):
    current=proofs['current'];prior=proofs.get('prior')
    for proof in (current,prior):
        if proof and (proof.get('status')!='verified' or (proof.get('symbol'),proof.get('isin'))!=(symbol,isin)):raise ValueError('Insurance filing identity not verified')
    ins=current['bank'];out=copy.deepcopy(details);old=out.get('latest') or {}
    try:newer=datetime.strptime(old.get('period') or '','%b %Y')>=datetime.strptime(ins['period'],'%b %Y')
    except ValueError:newer=False
    if newer and old.get('source')!=SOURCE:raise ValueError('Provider record is not older than the audited insurance filing')
    if ins['lease_caption_present']:raise ValueError('Insurance balance sheet prints a lease caption; lease cannot be treated as not applicable')
    stamp={'source_url':current['source_url'],'raw_sha256':current['raw_sha256'],'observed_at':current['observed_at']}
    values={'profit_before_tax':ins['profit_before_tax'],'equity':ins['capital']+ins['reserves'],'total_borrowings':ins['borrowings'],'cash_and_cash_equivalents':ins['cash']}
    labels={'profit_before_tax':'Profit/ (loss) before tax','equity':'Share capital + Reserves and surplus','total_borrowings':'Borrowings',
            'cash_and_cash_equivalents':'Cash and cash equivalents cash flow statement at end of period'}
    na={'source':SOURCE,'framework':FRAMEWORK,'period':ins['period'],'statement_type':ins['statement_type'],'balance_captions':ins['balance_captions'],**stamp}
    not_applicable={'total_lease_liabilities':{**na,'reason':'The audited IRDAI-format balance sheet has no lease-liability line.'}}
    if ins['borrowings_printed_zero']:
        not_applicable['finance_costs']={**na,'reason':'The audited IRDAI-format results have no finance-cost line and the balance sheet prints Borrowings 0.'}
    latest={**values,'finance_costs':None,'total_lease_liabilities':None,'period':ins['period'],'period_end':ins['period_end'],'statement_type':ins['statement_type'],
            'units_in':'crore','reporting_frequency':'annual','source':SOURCE,**stamp,
            'missing_fields':[] if ins['borrowings_printed_zero'] else ['finance_costs'],
            'sources':{k:{'source':SOURCE,**stamp,'label':labels[k],'value':v} for k,v in values.items()},'not_applicable':not_applicable}
    summary={'statement_type':ins['statement_type'],'units':'crore','frequency':'annual','growth_sources':{},'growth_observed_at':{}}
    previous=(prior or {}).get('bank') or {}
    if previous and (previous.get('statement_type')!=ins['statement_type'] or datetime.fromisoformat(previous['period_end']).replace(year=datetime.fromisoformat(previous['period_end']).year+1)!=datetime.fromisoformat(ins['period_end'])):previous={}
    for key in ('revenue','net_profit'):
        a,b=ins.get(key),previous.get(key)
        summary[key]=a;summary[key+'_period']=ins['period']
        summary[key+'_growth_yoy']=(a/b-1)*100 if a is not None and b is not None and b>0 else None
        if b is not None:
            if b>0:summary['growth_sources'][key]=SOURCE;summary['growth_observed_at'][key]=[current['observed_at'],prior['observed_at']]
            summary.setdefault('growth_inputs',{})[key]={'current':a,'prior':b,'prior_period':previous['period'],'source':SOURCE,'label':current['bank']['sources'][key]['label']}
    missing=list(latest['missing_fields'])+([] if out.get('sector') else ['sector'])
    out.update(latest=latest,summary=summary,selected_source=SOURCE,missing_fields=missing,status='COMPLETE' if not missing else 'PARTIAL')
    out['insurer_record']={'current':{k:v for k,v in current.items() if k!='bank'},'prior':{k:v for k,v in prior.items() if k!='bank'} if prior else None,'replaced_provider_period':old.get('period') if old.get('source')!=SOURCE else (details.get('insurer_record') or {}).get('replaced_provider_period')}
    return out


def retain_insurer_record(details,previous,symbol,isin):
    record=(previous or {}).get('insurer_record')
    if not record:return details
    try:
        proofs={'current':saved_proof(symbol,isin,record['current'],insurer_full),'prior':saved_proof(symbol,isin,record['prior'],insurer_income) if record.get('prior') else None}
        return insurer_record(details,proofs,symbol,isin)
    except (ValueError,KeyError,OSError,TypeError):return details


def insurer_refresh_due(details):
    if 'insurance' not in ((details or {}).get('sector') or '').lower():return False
    record=details.get('insurer_record') or {}
    if not record:return True
    try:return (datetime.now(timezone.utc)-datetime.fromisoformat(record['current']['observed_at'])).days>=20
    except (KeyError,TypeError,ValueError):return True


# ---- Company annual report: Form A-PL shareholders' account ----------------------------
REPORT_SOURCE='company_annual_report'
_UNITS=(("'000",1e-4),('thousand',1e-4),('lakh',.01),('crore',1.),('million',.1))


def form_apl(text,latest):
    """Interest on subordinated debt, PBT, PAT and premium from an IRDAI Form A-PL page (crore)."""
    from .annual_reports import NUM,amount
    low=' '.join(text.lower().replace('’',"'").split())
    # Life insurers print Form A-PL (shareholders' account); general insurers Form B-PL.
    if not re.search(r'form [ab]\s*-\s*(?:pl|ra)\b',low) or 'profit and loss account' not in low:raise ValueError('No IRDAI profit and loss account')
    units={scale for word,scale in _UNITS for _ in re.finditer(r"\((?:₹|`|rs\.?|inr)?\s*(?:in\s+)?"+re.escape(word)+r"s?\)",low)}
    if len(units)!=1:raise ValueError('Report unit not explicit or ambiguous')
    scale=units.pop();end=datetime.strptime(latest['period'],'%b %Y')
    import calendar
    day=calendar.monthrange(end.year,end.month)[1];month=end.strftime('%B').lower()
    if not re.search(r'year ended '+month+r' '+str(day)+r', '+str(end.year),low) or not re.search(month+r' '+str(day)+r', '+str(end.year-1),low):raise ValueError('Annual and comparative dates not matched')
    found=re.findall(r'(?<![a-z])(standalone|consolidated) profit and loss account',low)
    basis=found[0] if len(set(found))==1 else None
    if len(set(found))>1:raise ValueError('Mixed statement basis')
    rows={'finance_costs':r'(?:\([a-z]\)\s*)?interest on subordinated debt(?:\s*\(refer[^)]*\))?','profit_before_tax':r'profit\s*/?\s*\(\s*loss\s*\)\s*before tax(?:\s*\([a-z\-+ ]+\))?',
          'net_profit':r'profit\s*/?\s*\(\s*loss\s*\)\s*after tax','revenue':r'\(a\)\s*premium'}
    out={'scale':scale,'basis':basis,'lines':{}}
    for key,label in rows.items():
        hits=[m for m in re.finditer(r'^\s*'+label+r'(?:\s{2,}\d{1,2})?\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',text,re.I|re.M)]
        if len(hits)>1:raise ValueError('Ambiguous Form A-PL row: '+key)
        if hits:
            values=[amount(hits[0][i]) for i in (1,2)]
            if values[0] is not None:out[key]=[v*scale if v is not None else None for v in values];out['lines'][key]=hits[0][0].strip()
    return out


def _report_pages(path):
    import pymupdf
    from .annual_reports import balance_text
    with pymupdf.open(path) as doc:
        if len(doc)>1200:raise ValueError('Annual report exceeds page limit')
        for i,page in enumerate(doc):
            plain=' '.join(page.get_text().lower().split())
            if not re.search(r'form [ab]\s*-\s*(?:pl|ra)\b',plain) or 'profit and loss account' not in plain:continue
            clips=[None]
            if page.rect.width>page.rect.height*1.15:
                w,h=page.rect.width,page.rect.height;clips+= [pymupdf.Rect(0,0,w/2,h),pymupdf.Rect(w/2,0,w,h)]
            for clip in clips:yield i,clip,balance_text(page,clip)
            if len(clips)==3:
                # A spread prints the revenue account beside the P&L; read both halves together.
                yield i,'joined_halves',balance_text(page,clips[1])+'\n'+balance_text(page,clips[2])


def insurer_report_check(path,details):
    """Verified Form A-PL values: page PBT must match the selected audited PBT."""
    from .annual_reports import sole_entity_basis
    import pymupdf
    latest=details['latest'];candidates=[];issues=[]
    with pymupdf.open(path) as doc:sole=sole_entity_basis([p.get_text() for p in doc]) if latest.get('statement_type')=='standalone' else None
    for i,clip,text in _report_pages(path):
        try:
            v=form_apl(text,latest)
            if v['basis'] is None and not (sole and latest.get('statement_type')=='standalone'):raise ValueError('Explicit statement basis missing')
            if v['basis'] not in (None,latest.get('statement_type')):raise ValueError('Statement basis differs')
            pbt=(v.get('profit_before_tax') or [None])[0];step=v['scale']
            if pbt is None or abs(pbt-latest['profit_before_tax'])>max(.015,step/2+1e-9):raise ValueError('Form A-PL PBT does not match the audited filing')
            layout=clip if isinstance(clip,str) else 'full_page' if clip is None else 'left_half' if clip.x0==0 else 'right_half'
            candidates.append({**v,'page':i+1,'layout':layout,'basis_evidence':'page' if v['basis'] else 'sole_entity_no_subsidiary'})
        except ValueError as error:issues.append(str(error))
    if not candidates:raise ValueError('; '.join(dict.fromkeys(issues))[:400] or 'No Form A-PL page')
    # Every candidate must agree on each value it prints; the most complete one is used.
    for key in ('finance_costs','net_profit','revenue','profit_before_tax'):
        if len({json.dumps(c[key]) for c in candidates if c.get(key)})>1:raise ValueError('Conflicting Form A-PL values: '+key)
    return max(candidates,key=lambda c:sum(bool(c.get(k)) for k in ('finance_costs','net_profit','revenue')))


def fetch_insurer_report(symbol,isin,details):
    """NSE-archived annual report for the selected year; values must anchor on audited PBT."""
    import tempfile,hashlib
    from pathlib import Path
    from .storage import ROOT,now
    from .financial_sources import request
    from .fallbacks import Fallbacks
    from .report_sources import download_report,fresh
    from .intraday_data import atomic_json
    latest=details['latest'];year=datetime.strptime(latest['period'],'%b %Y').year
    folder=ROOT/'data/annual-reports'/symbol;folder.mkdir(parents=True,exist_ok=True)
    client=Fallbacks();index_file=folder/'index.json';index=json.loads(index_file.read_text()) if index_file.exists() else {}
    if index.get('symbol')!=symbol or not fresh(index):
        response=request(client.session,'https://www.nseindia.com/api/annual-reports',params={'index':'equities','symbol':symbol})
        index={'symbol':symbol,'observed_at':now(),'payload':response.json()};atomic_json(index_file,index)
    rows=[r for r in index.get('payload',{}).get('data',[]) if str(r.get('toYr'))==str(year) and '_'+symbol+'_' in (r.get('fileName') or '') and r['fileName'].startswith('https://nsearchives.nseindia.com/annual_reports/')]
    if not rows:raise ValueError('Matching annual report absent from NSE archive')
    url=rows[0]['fileName']
    with tempfile.TemporaryDirectory(dir=folder,prefix='insurer-') as tmp:
        pdf=Path(tmp)/'report.pdf';sha,size=download_report(request,client.session,url,pdf)
        check=insurer_report_check(pdf,details)
        import pymupdf
        excerpt=pymupdf.open()
        with pymupdf.open(pdf) as doc:excerpt.insert_pdf(doc,from_page=check['page']-1,to_page=check['page']-1,widgets=False)
        out=Path(tmp)/'excerpt.pdf';excerpt.save(out,garbage=4,deflate=True,no_new_id=True);excerpt.close()
        from .annual_reports import save_excerpt
        target,digest=save_excerpt(folder,out,sha)
    return {'status':'verified','symbol':symbol,'isin':isin,'source':REPORT_SOURCE,'source_url':url,'raw_sha256':sha,'observed_at':now(),
            'evidence_path':str(target.relative_to(ROOT)),'excerpt_sha256':digest,'check':check}


def apply_insurer_report(details,proof,symbol,isin):
    """Fill an insurer's finance cost, and missing growth, from a verified Form A-PL page."""
    if proof.get('status')!='verified' or (proof.get('symbol'),proof.get('isin'))!=(symbol,isin):raise ValueError('Insurer report identity not verified')
    out=copy.deepcopy(details);latest=out['latest'];c=proof['check'];summary=out.setdefault('summary',{})
    pbt=c['profit_before_tax'][0]
    if abs(pbt-latest['profit_before_tax'])>max(.015,c['scale']/2+1e-9):raise ValueError('Form A-PL PBT does not match the audited filing')
    stamp={'source':REPORT_SOURCE,'source_url':proof['source_url'],'raw_sha256':proof['raw_sha256'],'observed_at':proof['observed_at'],
           'evidence_path':proof['evidence_path'],'excerpt_sha256':proof['excerpt_sha256'],'page':c['page'],'layout':c['layout']}
    if latest.get('finance_costs') is None and 'finance_costs' not in (latest.get('not_applicable') or {}):
        if not c.get('finance_costs'):raise ValueError('Form A-PL finance cost row unavailable')
        latest['finance_costs']=c['finance_costs'][0]
        latest.setdefault('sources',{})['finance_costs']={**stamp,'label':'Interest on subordinated debt','line':c['lines']['finance_costs'],'value':latest['finance_costs']}
        latest['missing_fields']=[k for k in latest.get('missing_fields',[]) if k!='finance_costs']
        out['missing_fields']=[k for k in out.get('missing_fields',[]) if k!='finance_costs']
    # Growth from the report's comparative columns, only where the audited filings gave none
    # and the current-year report values equal the audited filing's.
    for key in ('revenue','net_profit'):
        if summary.get(key+'_growth_yoy') is not None or key in (summary.get('growth_inputs') or {}) or not c.get(key):continue
        a,b=c[key]
        if b is None or summary.get(key) is None or abs(a-summary[key])>max(.015,c['scale']/2+1e-9):continue
        prior=datetime.strptime(latest['period'],'%b %Y');prior_period=prior.replace(year=prior.year-1).strftime('%b %Y')
        summary[key+'_growth_yoy']=(a/b-1)*100 if b>0 else None
        if b>0:summary.setdefault('growth_sources',{})[key]=REPORT_SOURCE;summary.setdefault('growth_observed_at',{})[key]=[proof['observed_at'],proof['observed_at']]
        summary.setdefault('growth_inputs',{})[key]={'current':summary[key],'prior':b,'prior_period':prior_period,'source':REPORT_SOURCE,'observed_at':[proof['observed_at'],proof['observed_at']],'line':c['lines'][key]}
    from .bank_inputs import not_applicable
    from .provider import FUNDAMENTAL_REQUIRED_FIELDS
    na=not_applicable(out)
    if out.get('sector') and all(latest.get(k) is not None or k in na for k in FUNDAMENTAL_REQUIRED_FIELDS):out['status']='COMPLETE'
    out['insurer_report']={k:v for k,v in proof.items()}
    return out


def retain_insurer_report(details,previous,symbol,isin):
    import hashlib
    from .storage import ROOT
    proof=(previous or {}).get('insurer_report')
    if not proof:return details
    try:
        raw=(ROOT/proof['evidence_path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=proof['excerpt_sha256']:return details
        return apply_insurer_report(details,proof,symbol,isin)
    except (ValueError,KeyError,OSError,TypeError):return details
