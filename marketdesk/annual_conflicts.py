"""Evidence-bound company-report corrections, separate from missing-field fills.

Debt is normalized only when every NSE component reconciles to the explicitly
separate borrowing and lease rows. No global lease subtraction is permitted.
"""
import copy,hashlib,math,re
from datetime import datetime,timezone
import pymupdf
from .storage import ROOT,now
from .annual_reports import balance_text,parse_balance,NUM,amount
from .fallbacks import nse_annual
from .conflict_repair import context,same


def checked_bytes(path,digest):
    raw=(ROOT/path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Evidence hash mismatch')
    return raw

def report_text(report,symbol,isin):
    if report.get('status')!='verified' or (report.get('symbol'),report.get('isin'))!=(symbol,isin):raise ValueError('Report identity not verified')
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(report['observed_at'])).total_seconds()
    if not 0<=age<=30*86400:raise ValueError('Report observation expired')
    raw=checked_bytes(report['evidence_path'],report['excerpt_sha256']);e=report['evidence'];texts=[]
    inherited_basis=None
    with pymupdf.open(stream=raw,filetype='pdf') as doc:
        if e.get('basis_evidence_page'):
            if len(doc)!=len(e['pages'])+1:raise ValueError('Auditor evidence page missing')
            auditor=' '.join(doc[-1].get_text().lower().split())
            bases=re.findall(r'report on the audit of (?:the )?(standalone|consolidated) financial statements',auditor[:6000])
            inherited_basis=e.get('statement_type')
            if e.get('basis_method')=='sole_entity_no_subsidiary':
                from .annual_reports import SOLE_ENTITY_CLAUSE
                if inherited_basis!='standalone' or bases or not re.search(SOLE_ENTITY_CLAUSE,auditor):raise ValueError('Auditor no-subsidiary statement not verified')
            elif set(bases)!={inherited_basis}:
                from .annual_reports import audited_section_basis
                if (audited_section_basis(doc[-1].get_text()) or [None])[0]!=inherited_basis:raise ValueError('Auditor statement basis not verified')
        for index,p in enumerate(list(doc)[:len(e['pages'])]):
            if e['layout']=='joined_balance_spread':
                texts.extend(balance_text(p,clip) for clip in (pymupdf.Rect(0,0,p.rect.width/2,p.rect.height),pymupdf.Rect(p.rect.width/2,0,p.rect.width,p.rect.height)))
                continue
            if e['layout']=='right_then_left':
                clip=pymupdf.Rect(p.rect.width/2,0,p.rect.width,p.rect.height) if index==0 else pymupdf.Rect(0,0,p.rect.width/2,p.rect.height)
            elif e['layout']=='left_half':clip=pymupdf.Rect(0,0,p.rect.width/2,p.rect.height)
            elif e['layout']=='right_half':clip=pymupdf.Rect(p.rect.width/2,0,p.rect.width,p.rect.height)
            elif e['layout']=='full_page':clip=None
            else:raise ValueError('Unsupported correction layout')
            page_text=balance_text(p,clip)
            # Re-derive printed column order from this excerpt page's own word
            # coordinates, exactly as the original parse did; never trust a stored line.
            stored=e.get('column_order_evidence')
            if stored and index==0 and len(e['pages'])==1 and stored.get('page')==e['pages'][0]:
                from .annual_reports import verified_column_transcription
                context_row={k:e.get(k) for k in ('period','statement_type','units_in')}
                basis_text=(inherited_basis+'\n' if inherited_basis else '')+page_text
                derived,proof=verified_column_transcription(basis_text,p,context_row,clip)
                if not proof or proof['transcription']!=stored.get('transcription'):raise ValueError('Printed column order not re-verified')
                page_text=proof['transcription']+'\n'+page_text
            texts.append(page_text)
    text='\n'.join(texts)
    # Only a reviewed header line can be excluded (e.g. a navigation bar naming
    # both standalone and consolidated sections). Its exact text is hash-bound.
    for line in report.get('reviewed_navigation_lines',[]):
        if text.splitlines()[0]!=line:raise ValueError('Reviewed navigation header changed')
        text='\n'.join(text.splitlines()[1:])
    if inherited_basis:text=inherited_basis+'\n'+text
    return text

def quantum(line,scale):
    m=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
    if not m or amount(m[1]) is None:raise ValueError('Explicit numeric PDF component missing')
    token=m[1].replace(',','').strip('()')
    return scale*10**(-len(token.split('.')[1]) if '.' in token else 0)

def bridge_components(latest,parsed,nse,reviewed_rounding=None):
    bridges=[];changes={};scale=parsed['conversion_to_crore']
    for term in ('current','non_current'):
        key=term+'_borrowings';leasekey=term+'_lease';borrow=parsed['borrowing_components'].get(key);lease=parsed['components'].get(leasekey);old=latest.get(key)
        if any(not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in (borrow,lease,old)):raise ValueError('Explicit debt components unavailable')
        if not same(old,nse.get(key)):raise ValueError('NSE component changed')
        tolerance=(nse['sources'][key]['rounding_quantum']+quantum(parsed['lines'][key],scale)+quantum(parsed['lines'][leasekey],scale))/2+1e-8
        if reviewed_rounding:
            # Two reviewed filings contain whole-lakh values despite .00 padding.
            # This explicit, hash-bound review is never inferred for other stocks.
            if reviewed_rounding.get('quantum_crore')!=.01 or abs(old*100-round(old*100))>1e-7:raise ValueError('Reviewed whole-lakh rounding does not apply')
            tolerance=max(tolerance,.00500001)
        # Otherwise tolerance comes only from the printed precision.
        if abs(old-borrow)<=tolerance:classification='excludes leases'
        elif abs(old-borrow-lease)<=tolerance:classification='includes leases'
        else:raise ValueError('Borrowing difference does not reconcile to explicit leases')
        changes[key]=borrow;bridges.append({'component':key,'nse_value':old,'report_borrowing':borrow,'report_lease':lease,'classification':classification,'rounding_tolerance':tolerance})
    if not any(b['classification']=='includes leases' for b in bridges):raise ValueError('No verified lease inclusion to correct')
    changes['total_borrowings']=sum(changes.values())
    if not same(latest.get('total_borrowings'),sum(b['nse_value'] for b in bridges)):raise ValueError('Selected debt total disagrees with components')
    leases=sum(parsed['components'][k] for k in ('current_lease','non_current_lease'))
    if not same(latest.get('total_lease_liabilities'),leases):raise ValueError('Selected leases differ from annual-report components')
    return changes,bridges

def verify_debt(details,report,symbol,isin):
    if details.get('selected_source')!='nse_audited_filing':raise ValueError('Selected source is not NSE')
    latest=details['latest'];text=report_text(report,symbol,isin);parsed=parse_balance(text,latest,allow_existing=True)
    if not {'equity','cash_and_cash_equivalents'}<=set(parsed['matched_fields']):raise ValueError('Debt correction requires equity and cash anchors')
    sha=latest['raw_sha256'];path='data/filings/'+symbol+'/'+sha+'.html';raw=checked_bytes(path,sha)
    nse=nse_annual(raw.decode(),symbol,isin,latest['source_url'])
    if any(latest.get(k)!=nse.get(k) for k in ('period','statement_type','units_in')):raise ValueError('NSE context changed')
    changes,bridges=bridge_components(latest,parsed,nse,report.get("reviewed_nse_rounding"))
    conflicts=[c for c in details.get('source_conflicts',[]) if c.get('field')=='total_borrowings' and same(c.get('selected_value'),latest['total_borrowings']) and same(c.get('annual_report'),changes['total_borrowings']) and c.get('source_url')==report['source_url'] and c.get('period')==latest['period'] and c.get('units')=='crore']
    if len(conflicts)!=1:raise ValueError('Exact annual-report borrowing conflict unavailable')
    return changes,conflicts,{'components':bridges,'parsed':parsed,'nse_evidence_path':path,'nse_raw_sha256':sha}

def reconcile_debt(details,report,symbol,isin):
    changes,conflicts,evidence=verify_debt(details,report,symbol,isin)
    record={'kind':'lease_classification','symbol':symbol,'isin':isin,'selected_context':context(details),'original_latest':copy.deepcopy(details['latest']),'changes':changes,'conflicts':copy.deepcopy(conflicts),'report':copy.deepcopy(report),'evidence':evidence,'resolved_at':now(),'reason':'Company annual report separates borrowings from leases; each NSE component reconciles within printed rounding.'}
    return apply_record(details,record),record

def apply_record(details,record):
    out=copy.deepcopy(details);latest=out['latest'];report=record['report']
    for field,value in record['changes'].items():
        latest[field]=value
        latest.setdefault('sources',{})[field]={'source':'company_annual_report','source_url':report['source_url'],'observed_at':report['observed_at'],'raw_sha256':report['raw_sha256'],'evidence_path':report['evidence_path'],'excerpt_sha256':report['excerpt_sha256'],'value':value,'correction':record['kind'],'original_value':record['original_latest'].get(field)}
    out['source_conflicts']=[c for c in out.get('source_conflicts',[]) if c not in record['conflicts']]
    out.pop('annual_conflict_recheck_required',None)
    history=out.setdefault('annual_conflict_resolutions',[])
    if not any(r['kind']==record['kind'] and r['selected_context']==record['selected_context'] for r in history):history.append(copy.deepcopy(record))
    return out

def retain_annual_resolutions(details,previous,symbol,isin):
    out=copy.deepcopy(details)
    for record in previous.get('annual_conflict_resolutions',[]):
        if (record.get('symbol'),record.get('isin'))!=(symbol,isin):continue
        out['annual_conflict_recheck_required']=True
        if record.get('selected_context')!=context(out):continue
        try:
            if record['kind']=='lease_classification':changes,conflicts,_=verify_debt(out,record['report'],symbol,isin)
            elif record['kind']=='reviewed_liability_classification':
                from .liability_reviews import verify_liability_review
                changes,conflicts,_=verify_liability_review(out,record['report'],symbol,isin)
            else:changes,conflicts,_=verify_reviewed(out,record['report'],symbol,isin)
            if changes!=record['changes'] or conflicts!=record['conflicts']:continue
            out=apply_record(out,record)
        except (ValueError,KeyError,OSError,TypeError):continue
    for record in previous.get('annual_conflict_resolutions',[]):
        if record not in out.setdefault('annual_conflict_resolutions',[]):out['annual_conflict_resolutions'].append(copy.deepcopy(record))
    return out

def reviewed_rows(text,review,scale):
    values={}
    for field,line in review['lines'].items():
        if sum(l.strip()==line for l in text.splitlines())!=1:raise ValueError('Reviewed PDF row missing or ambiguous: '+field)
        m=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if not m or amount(m[1]) is None:raise ValueError('Reviewed PDF number unavailable')
        values[field]=amount(m[1])*scale
    return values

def verify_reviewed(details,report,symbol,isin):
    from .annual_reports import statement_context
    text=report_text(report,symbol,isin);review=report['review'];latest=details['latest']
    for original,replacement in report.get('reviewed_glyphs',[]):
        if text.count(original)!=1:raise ValueError('Reviewed currency glyph changed')
        text=text.replace(original,replacement)
    _,_,(_,scale)=statement_context(text,latest,review['statement'])
    values=reviewed_rows(text,review,scale);field=review['field']
    conflicts=[c for c in details.get('source_conflicts',[]) if c.get('field')==field and c.get('selected')=='nse_audited_filing' and same(c.get('nse'),latest.get(field)) and c.get('period')==latest['period'] and c.get('units')=='crore']
    if len(conflicts)!=1:raise ValueError('Exact reviewed vendor conflict unavailable')
    # Vendor values serve only as a rounded corroboration; the primary PDF is
    # the selected value and has independent official/accounting checks below.
    if abs(conflicts[0]['upstox']-values[field])>.00500001:raise ValueError('Vendor corroboration failed')
    if field=='equity':
        proof=review['nse'];raw=checked_bytes(proof['evidence_path'],proof['raw_sha256']);nse=nse_annual(raw.decode(),symbol,isin,proof['source_url'])
        if any(nse.get(k)!=latest.get(k) for k in ('period','statement_type','units_in')):raise ValueError('Corroborating NSE context mismatch')
        if not same(nse['equity'],values['equity']) or not same(values['cash_and_cash_equivalents'],latest['cash_and_cash_equivalents']):raise ValueError('Equity or cash corroboration failed')
        if not all(same(nse[k],latest[k]) for k in ('total_borrowings','cash_and_cash_equivalents','profit_before_tax')):raise ValueError('Corroborating NSE anchors disagree')
    elif field=='profit_before_tax':
        if not same(values['net_profit'],latest['net_profit']) or abs(values['finance_costs']-latest['finance_costs'])>.00015001:raise ValueError('Income anchors disagree')
        if not same(values[field],values['net_profit']+values['tax_expense']):raise ValueError('PBT does not reconcile to profit plus tax')
        if not same(values[field],values['before_exceptional']-values['exceptional_items']):raise ValueError('PBT does not reconcile to exceptional items')
    else:raise ValueError('Unsupported reviewed correction')
    return {field:values[field]},conflicts,{'review':copy.deepcopy(review),'parsed_values':values}

def reconcile_reviewed(details,report,symbol,isin):
    changes,conflicts,evidence=verify_reviewed(details,report,symbol,isin)
    record={'kind':'reviewed_primary_statement','symbol':symbol,'isin':isin,'selected_context':context(details),'original_latest':copy.deepcopy(details['latest']),'changes':changes,'conflicts':copy.deepcopy(conflicts),'report':copy.deepcopy(report),'evidence':evidence,'resolved_at':now(),'reason':report['review']['reason']}
    return apply_record(details,record),record
