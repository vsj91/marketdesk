"""Exact printed identities: NCI, non-current debt, and nil borrowings across both liability sections."""
import re
from decimal import Decimal, InvalidOperation

def decimal_amount(token):
    token=token.strip().replace(',','')
    if token.lower()=='nil':return Decimal(0)
    if token in ('-','–','—',''):return None
    if token.startswith('(') and token.endswith(')'):token='-'+token[1:-1]
    try:return Decimal(token)
    except InvalidOperation:return None

def identity_evidence(text,scale):
    # Caller must first pass statement_context. No identity spans statements.
    from .annual_reports import NUM,NOTE_REF,caption
    rows={};noncurrent=[];in_noncurrent=False;closed=False;incomplete=False;has_borrow=False;has_nci=False
    for line in text.splitlines():
        if re.search(r'^(?:standalone |consolidated )?statement of (?:profit|cash|changes)',line.strip(),re.I):break
        clean=caption(line)
        if re.search(r'non[- ]controlling interest',clean):has_nci=True
        if clean=='non-current liabilities' or clean=='non current liabilities':in_noncurrent=True;continue
        if clean=='current liabilities':
            if in_noncurrent:incomplete=True
            in_noncurrent=False
        m=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if not m:
            if in_noncurrent and clean and clean not in ('financial liabilities',):incomplete=True
            if in_noncurrent and 'borrow' in clean:has_borrow=True
            continue
        label=caption(re.sub(r'\s{2,}'+NOTE_REF+r'\s*$','',line[:m.start()].strip(),flags=re.I))
        amount=decimal_amount(m[1]);prior=decimal_amount(m[2])
        if label in rows:rows[label]=None
        else:rows[label]={'amount':amount,'prior':prior,'token':m[1],'line':line.strip()}
        if in_noncurrent:
            if label in ('total non-current liabilities','total non current liabilities'):
                closed=True;in_noncurrent=False
            else:
                if 'borrow' in label:has_borrow=True
                if amount is None or amount<0:incomplete=True
                noncurrent.append({'label':label,'amount':amount,'line':line.strip()})
    def get(*labels):
        matches=[rows[k] for k in labels if k in rows]
        return matches[0] if len(matches)==1 and matches[0] and matches[0]['amount'] is not None else None
    owners=get('total equity attributable to owners of the company','total equity attributable to owners of the parent')
    nc=get('total non-current liabilities','total non current liabilities');cur=get('total current liabilities')
    total=get('total equity and liabilities','total equity & liabilities','total liabilities and equity')
    proofs={}
    if not has_nci and all((owners,nc,cur,total)) and all(r['amount']>=0 for r in (owners,nc,cur,total)) and owners['amount']+nc['amount']+cur['amount']==total['amount']:
        proofs['nci']={'value':0.,'method':'exact_printed_balance_identity','expression':'owners_equity + total_non_current_liabilities + total_current_liabilities = total_equity_and_liabilities',
            'operands':{k:str(r['amount']) for k,r in zip(('owners_equity','total_non_current_liabilities','total_current_liabilities','total_equity_and_liabilities'),(owners,nc,cur,total))},
            'lines':[r['line'] for r in (owners,nc,cur,total)],'conversion_to_crore':scale,'total_equity_crore':float(owners['amount']*Decimal(str(scale)))}
    absent=borrowings_absent_identity(text,scale)
    if absent:proofs['borrowings_absent']=absent
    no_lease=lease_absent_identity(text,scale)
    if no_lease:proofs['lease_absent']=no_lease
    if closed and nc and not incomplete and not has_borrow and noncurrent and sum((r['amount'] for r in noncurrent),Decimal(0))==nc['amount']:
        proofs['non_current_borrowings']={'value':0.,'method':'exact_exhaustive_non_current_liability_identity','expression':'sum(all printed non-current liability lines) = printed non-current liabilities total',
            'operands':[{'label':r['label'],'amount':str(r['amount']),'line':r['line']} for r in noncurrent],
            'printed_total':str(nc['amount']),'total_line':nc['line'],'conversion_to_crore':scale}
    return proofs

def _liability_sections(text):
    """Non-current and current liability sections: every printed line and the printed total.

    A section closes at its own "Total ... liabilities" line, or at an unlabelled pair of
    amounts printed directly before the next section heading / total liabilities line.
    Returns None when the layout is not exactly two closed sections."""
    from .annual_reports import NUM,caption
    lines={'non_current':[],'current':[]};totals={};section=None;texts=[]
    raw_lines=[l for l in text.splitlines() if l.strip()]
    for index,raw in enumerate(raw_lines):
        if re.search(r'^(?:standalone |consolidated )?statement of (?:profit|cash|changes)',raw.strip(),re.I):break
        head=re.sub(r'^(?:\(?[ivx]+[.)]|\(?[a-z][.)])\s*','',raw.strip().lower())
        if re.fullmatch(r'non[- ]current liabilities',head):
            if section or 'non_current' in totals:return None
            section='non_current';continue
        if re.fullmatch(r'current liabilities',head):
            if section or 'current' in totals:return None
            section='current';continue
        if section is None:continue
        texts.append(head)
        m=re.fullmatch(r'\s*('+NUM+r')\s{2,}('+NUM+r')\s*',raw) or re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',raw)
        if not m:continue
        label=caption(raw[:m.start(1)]).lower() if m.start(1)>0 else ''
        values=[decimal_amount(m[1]),decimal_amount(m[2])]
        following=' '.join(raw_lines[index+1].lower().split()) if index+1<len(raw_lines) else ''
        own_total=(section=='non_current' and re.fullmatch(r'total non[- ]current liabilities',label)) or (section=='current' and label=='total current liabilities')
        unlabelled_total=not label.strip() and (re.match(r'(?:\(?[ivx]+[.)]\s*)?current liabilities$',following) if section=='non_current' else re.match(r'total liabilities\b',following))
        if own_total or unlabelled_total:
            if any(v is None or v<0 for v in values):return None
            totals[section]={'values':values,'line':raw.strip()};section=None;continue
        if any(v is not None and v<0 for v in values):return None
        lines[section].append({'values':values,'line':raw.strip()})
    if set(totals)!={'non_current','current'}:return None
    for key in ('non_current','current'):
        for col in (0,1):
            if sum((r['values'][col] for r in lines[key] if r['values'][col] is not None),Decimal(0))!=totals[key]['values'][col]:return None
    return lines,totals,texts


def _absent(text,scale,pattern,expression,method):
    found=_liability_sections(text)
    if not found:return None
    lines,totals,texts=found
    if any(re.search(pattern,t) for t in texts):return None
    return {'value':0.,'method':method,'expression':expression,
            'non_current':{'lines':[r['line'] for r in lines['non_current']],'total_line':totals['non_current']['line']},
            'current':{'lines':[r['line'] for r in lines['current']],'total_line':totals['current']['line']},'conversion_to_crore':scale}


def borrowings_absent_identity(text,scale):
    """Prove borrowings are nil from the audited balance sheet itself, never from a blank.

    Every printed line in both the non-current and current liability sections must sum
    exactly (both years, Decimal) to that section's printed total, and no borrowing,
    debt-security or loan caption may appear in either section. A dashed line is then
    proven nil by the arithmetic. Any omitted or double-counted line breaks the sum."""
    return _absent(text,scale,r'borrow|debt securit|\bloans?\b|debenture','sum(all printed liability lines) = printed section total, for both sections and years; no borrowing caption','exact_exhaustive_liability_identity')


def lease_absent_identity(text,scale):
    """The same exhaustive identity with no lease caption in either liability section.

    Schedule III (Division II, as amended in 2021) requires lease liabilities as their own
    line under financial liabilities, so an exactly reconciling balance sheet without one
    carries none. The caller must also confirm no lease-liability amount is printed anywhere
    else in the report."""
    return _absent(text,scale,r'\blease','sum(all printed liability lines) = printed section total, for both sections and years; no lease caption','exact_exhaustive_liability_identity_no_lease_line')


def borrowings_reconciliation(existing,audited,leases,lines,scale):
    if not all(isinstance(v,(int,float)) for v in (existing,audited,leases)) or leases<=0:return None
    from .annual_reports import NUM
    tokens=[]
    for line in lines.values():
        if not re.search('borrow|lease',line,re.I):continue
        m=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if m and m[1].lower()!='nil' and decimal_amount(m[1]) is not None:tokens.append(decimal_amount(m[1]))
    if not tokens:return None
    # Half a unit at the least precise printed decimal place; no broad anchor tolerance.
    quantum=max(Decimal(1).scaleb(v.as_tuple().exponent) for v in tokens)*Decimal(str(scale))
    residual=Decimal(str(existing))-Decimal(str(audited))-Decimal(str(leases))
    if abs(residual)>quantum/2:return None
    return {'reason':'vendor figure includes leases','vendor_value':existing,'audited_borrowings':audited,'audited_lease_liabilities':leases,
            'expression':'vendor total_borrowings - audited borrowings = audited lease liabilities','residual':str(residual),'rounding_bound_crore':str(quantum/2)}
