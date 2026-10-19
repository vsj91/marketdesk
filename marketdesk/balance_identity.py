"""User-approved exact printed identities, limited to NCI and non-current debt."""
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
    if closed and nc and not incomplete and not has_borrow and noncurrent and sum((r['amount'] for r in noncurrent),Decimal(0))==nc['amount']:
        proofs['non_current_borrowings']={'value':0.,'method':'exact_exhaustive_non_current_liability_identity','expression':'sum(all printed non-current liability lines) = printed non-current liabilities total',
            'operands':[{'label':r['label'],'amount':str(r['amount']),'line':r['line']} for r in noncurrent],
            'printed_total':str(nc['amount']),'total_line':nc['line'],'conversion_to_crore':scale}
    return proofs

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
