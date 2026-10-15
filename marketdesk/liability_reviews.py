"""Narrow, evidence-bound liability classification reviews.

These reviews reconcile the exact NSE rows to a matching company balance sheet.
They neither prefer a source globally nor subtract leases without component proof.
"""
import copy,re,unicodedata
from datetime import datetime,timezone
from .storage import now
from .fallbacks import Tables,nse_annual
from .conflict_repair import context,same
from .annual_reports import parse_balance,caption,NOTE_REF,NUM
from .annual_conflicts import checked_bytes,report_text,reviewed_rows,apply_record


def verify_liability_review(details,report,symbol,isin):
    latest=details['latest'];review=report['liability_review'];proof=review['nse']
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(proof['observed_at'])).total_seconds()
    if not 0<=age<=30*86400:raise ValueError('NSE review expired')
    raw=checked_bytes(proof['evidence_path'],proof['raw_sha256']).decode()
    nse=nse_annual(raw,symbol,isin,proof['source_url'])
    if details.get('selected_source')!='nse_audited_filing' or latest.get('raw_sha256')!=proof['raw_sha256']:raise ValueError('Selected NSE evidence changed')
    if any(latest.get(k)!=nse.get(k) for k in ('period','statement_type','units_in')):raise ValueError('Annual context changed')
    text=report_text(report,symbol,isin);parsed=parse_balance(text,latest,allow_existing=True)
    if not {'equity','cash_and_cash_equivalents'}<=set(parsed['matched_fields']):raise ValueError('Cash and equity anchors required')
    if not all(same(latest[k],nse[k]) for k in ('equity','cash_and_cash_equivalents','profit_before_tax','finance_costs')):raise ValueError('NSE anchors changed')
    # A reviewed extra row must occur once, inside the stated liability section.
    line=review['extra_line'];lines=text.splitlines();idx=[i for i,l in enumerate(lines) if l.strip()==line]
    if len(idx)!=1:raise ValueError('Reviewed liability row missing or repeated')
    heads=[(i,caption(l.strip())) for i,l in enumerate(lines) if caption(l.strip()) in ('non-current liabilities','current liabilities')]
    section=next((h for i,h in reversed(heads) if i<idx[0]),None)
    match=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
    if not match:raise ValueError('Explicit comparative liability row required')
    label=re.sub(r'\s{2,}'+NOTE_REF+r'\s*$','',line[:match.start()].strip(),flags=re.I)
    label=caption(unicodedata.normalize('NFKC',label))
    extra=reviewed_rows(text,{'lines':{'extra':line}},parsed['conversion_to_crore'])['extra']
    if extra<0:raise ValueError('Negative liability component')
    bor=parsed['borrowing_components'];lease=parsed['components'];method=review['method']
    if method=='debt_with_trade_credit':
        if section!='current liabilities' or label not in ('trade credits & acceptances','trade credits and acceptances','trade credits'):raise ValueError('Explicit current trade-credit row required')
        for term in ('current','non_current'):
            b=bor.get(term+'_borrowings');l=lease.get(term+'_lease');selected=latest.get(term+'_borrowings')
            if b is None or l is None or b<0 or l<0:raise ValueError('Explicit borrowing and lease components required')
            expected=b+l+(extra if term=='current' else 0)
            if not same(expected,nse.get(term+'_borrowings')) or not same(selected,nse.get(term+'_borrowings')):raise ValueError('Borrowings do not reconcile to leases and trade credit')
        if not same(latest['total_borrowings'],nse['total_borrowings']) or not same(latest['total_lease_liabilities'],sum(lease.values())):raise ValueError('Selected liability totals changed')
        changes={'current_borrowings':bor['current_borrowings']+extra,'non_current_borrowings':bor['non_current_borrowings'],'current_lease_liabilities':lease['current_lease'],'non_current_lease_liabilities':lease['non_current_lease']}
        changes['total_borrowings']=changes['current_borrowings']+changes['non_current_borrowings']
        if not same(changes['total_borrowings']+latest['total_lease_liabilities'],nse['total_borrowings']):raise ValueError('Total debt conservation failed')
        field='total_borrowings'
        reason='NSE borrowing components include separately reported leases and current trade credits. Removed only leases; retained the explicit trade-credit debt.'
    elif method=='mislabelled_lease_component':
        if section!='non-current liabilities' or label!='other financial liabilities':raise ValueError('Explicit non-current other-financial-liability row required')
        parser=Tables();parser.feed(raw)
        tables=[t for t in parser.tables if any('Total equity' in r for r in t) and any('Cash and cash equivalents' in r for r in t)]
        if len(tables)!=1:raise ValueError('Ambiguous balance table')
        table=tables[0];start=next(i for i,r in enumerate(table) if 'Non-current liabilities' in r);end=next(i for i,r in enumerate(table) if 'Current liabilities' in r);rows=table[start:end]
        # This review supports a single independently verified annual balance column.
        scale=nse['sources']['equity']['conversion_to_crore']
        def value(label,ordinal):
            found=[r for r in rows if len(r)==3 and r[0]==ordinal and r[1]==label]
            if len(found)!=1:raise ValueError('NSE classification row missing or ambiguous')
            return float(found[0][2].replace(',',''))*scale
        if not same(value('Borrowings, non-current',''),bor.get('non_current_borrowings')):raise ValueError('Primary borrowing row mismatch')
        if not same(value('Borrowings, non-current','1'),lease.get('non_current_lease')):raise ValueError('Mislabelled lease row does not match report')
        if not same(value('Lease Liabilities','2'),extra) or not same(nse.get('non_current_lease_liabilities'),extra):raise ValueError('Mislabelled other liability does not match report')
        if not same(value('Total of other non-current financial liabilities',''),lease['non_current_lease']+extra):raise ValueError('Financial liability subtotal does not reconcile')
        if not same(nse.get('current_lease_liabilities'),lease.get('current_lease')):raise ValueError('Current lease component mismatch')
        if not same(latest['total_lease_liabilities'],nse['total_lease_liabilities']):raise ValueError('Selected lease total changed')
        if not same(latest['total_borrowings'],sum(bor.values())):raise ValueError('Borrowing total mismatch')
        changes={'total_lease_liabilities':sum(lease.values()),'non_current_lease_liabilities':lease['non_current_lease'],'non_current_borrowings':bor['non_current_borrowings']}
        field='total_lease_liabilities'
        reason='NSE non-current lease and other-financial-liability captions are misassigned. Company-report rows reconcile exactly to the NSE financial-liability subtotal; current leases and borrowing totals also match.'
    else:raise ValueError('Unsupported liability review')
    conflicts=[c for c in details.get('source_conflicts',[]) if c.get('field')==field and c.get('selected')=='nse_audited_filing' and c.get('period')==latest['period'] and c.get('units')=='crore' and c.get('source_url')==report['source_url'] and same(c.get('selected_value'),latest[field]) and same(c.get('annual_report'),parsed['values'][field])]
    if len(conflicts)!=1:raise ValueError('Exact reviewed conflict missing')
    return changes,conflicts,{'parsed':parsed,'review':copy.deepcopy(review),'extra_value':extra,'reason':reason}


def reconcile_liability_review(details,report,symbol,isin):
    changes,conflicts,evidence=verify_liability_review(details,report,symbol,isin)
    record={'kind':'reviewed_liability_classification','symbol':symbol,'isin':isin,'selected_context':context(details),'original_latest':copy.deepcopy(details['latest']),'changes':changes,'conflicts':copy.deepcopy(conflicts),'report':copy.deepcopy(report),'evidence':evidence,'resolved_at':now(),'reason':evidence['reason']}
    return apply_record(details,record),record
