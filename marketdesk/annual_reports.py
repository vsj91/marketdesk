"""Company annual-report lease evidence, matched to an existing audited annual row.

PDF parsing runs in isolated subprocesses. Only relevant pages are retained: full
reports are temporary, bounded downloads. This module never infers zero from a dash.
"""
import calendar,copy,hashlib,json,math,re,shutil,subprocess,sys,tempfile,threading,unicodedata
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
from .storage import ROOT,now
from .intraday_data import atomic_json

REVISION=45
RULE_REVISION=2  # Explicit user approval: printed identities and debt/lease reconciliation.
INCOME_REVISION=5
# An auditor's basis heading covers the statements that follow it, up to this many
# pages (long qualified opinions and annexures), until any other audit heading.
BASIS_CONTEXT_PAGES=40
_pdf_slots=threading.BoundedSemaphore(6)
NUM=r'(?:\(?-?\d[\d,]*(?:\.\d+)?\)?|[-–—]|Nil|NIL)'
NOTE_REF=r'\d+[a-z]*(?:\s*\([a-z0-9]+\)|\.\d+[a-z]*)*'
FORMULA_REF=r'\s*\((?:[ivx0-9+\-\s]+|[a-z]\s*(?:[+\-]\s*[a-z]\s*)+)\)$'
LEASE_ROLLFORWARD=r'(?:carrying amounts of (?:the )?lease liabilities|reconciliation of (?:the )?lease liabilities|movement (?:in|of) (?:the )?(?:carrying value of (?:the )?)?lease liabilit(?:y|ies)|lease liabilit(?:y|ies) movement)'
LEASE_NOTE_HEADING=r'^\s*\d{1,3}\s+lease liabilit(?:y|ies)(?:\s*\(refer note\s+\d+\))?\s*$'

def amount(token):
    token=token.strip().replace(',','')
    # A printed dash is ambiguous, not a reported numeric zero.
    if token.lower()=='nil':return 0.
    if token in ('-','–','—'):return None
    if token.startswith('(') and token.endswith(')'):token='-'+token[1:-1]
    try:
        n=float(token)
        return n if math.isfinite(n) else None
    except ValueError:return None

def caption(value):
    value=re.sub(r'^\s*[ivx]+(?=\([a-z0-9]+\))','',value,flags=re.I)
    value=re.sub(r'\.{2,}\s*$','',value)
    value=re.sub(r'\s+\*\s*$','',value)
    value=re.sub(r'^\s*(?:(?:\([ivxa-z0-9]+\)|[ivx]+[a-z]?[.)]|[a-z][.)]|[0-9]+[.)]?)\s*)+','',value.lower())
    value=re.sub(r'^[ivx]+\s+','',value)
    value=re.sub(r'\s*-\s*','-',value.replace('–','-').replace('‑','-'))
    value=re.sub(r'^[-•●]+\s*','',value)
    return ' '.join(value.split()).strip(': ')

def balance_heading_text(text):
    text=re.sub(r'(?<=\d)(?=(?:(?:consolidated|standalone)\s+)?balance\s+sheet\b)',' ',text,flags=re.I)
    text=re.sub(r'\b(CONSOLIDATED|STANDALONE)(?=AS AT\b)',r'\1 ',text)
    return re.sub(r'\bbalance\s+sheetas\b','Balance Sheet as',text,flags=re.I)

def statement_context(text,latest,kind='balance'):
    text='\n'.join(line.strip() for line in unicodedata.normalize('NFKC',text).replace('\u00a0',' ').splitlines())
    text=balance_heading_text(text)
    text=re.sub(r'\bfinancial\s+statementsfor\b','financial statements for',text,flags=re.I)
    text=re.sub(r'\bIndian Rupees\b','INR',text,flags=re.I)
    text=re.sub(r'(₹|`)(?=(?:crore|lakh|million|thousand)s?\b)',r'\1 ',text,flags=re.I)
    # Annual reports also print "crores of Indian Rupees" or "Crore Rupees".
    # Normalize only an explicit Indian currency, never an unlabelled scale.
    text=re.sub(r'\b((?:crore|lakh|million|thousand)s?)\s+(?:of\s+)?(INR|rupees?)\b',r'\2 in \1',text,flags=re.I)
    text=re.sub(r'(₹|`|\bINR|\bRs\.?)\s*/\s*((?:crore|lakh|million|thousand)s?)\b',r'\1 in \2',text,flags=re.I)
    # The PDF layout engine can join the adjacent "No." column label to a date.
    text=re.sub(r'(\b\d{1,2}[./-]\d{1,2}[./-]20\d{2})(?=No\.)',r'\1 ',text)
    text=re.sub(r'(₹|`|\bINR|\bRs\.?|\brupees?)\s+((?:crore|lakh|million|thousand)s?)\b',r'\1 in \2',text,flags=re.I)
    text=re.sub(r'\blacs?\b','lakhs',text,flags=re.I)
    # Printed Indian currency unit abbreviation, only beside an explicit INR marker.
    text=re.sub(r'((?:\bRs\.?|\bINR|₹|`)\s+(?:in\s+)?)cr\b\.?',r'\1crore',text,flags=re.I)
    # Some PDF text layers concatenate a lower-case preposition with the title.
    text=re.sub(r'(?<![A-Z])(CONSOLIDATED|STANDALONE)(?![A-Z])',r' \1 ',text)
    low=text.lower()
    # A balance sheet's heading ends at its closing total; the footnote and
    # signature block printed after it are not the statement heading.
    closing=re.search(r'^total (?:equity (?:and|&) liabilities|liabilities (?:and|&) equity)\b.*$',low,re.M) if kind=='balance' else None
    head=re.sub(r'[ \t]+',' ',low[:closing.end()] if closing else low)[:1800]
    heading=r'\bbalance\s+sheet\b' if kind=='balance' else r'\bnotes\s+(?:to|forming\s+part\s+of)\s+(?:the\s+)?(?:standalone\s+|consolidated\s+)?financial\s+statements?\b' if kind=='lease_note' else r'\bstatement\s+of\s+(?:(?:standalone|consolidated)\s+)?profit\s+(?:and|or|&)\s+loss\b'
    numbered_lease=(kind=='lease_note' and re.search(r'\b(?:standalone|consolidated) financial statements\b',head) and re.search(r'^\d+\.\s+disclosures for leasing arrangements\s*$',head,re.M))
    if not re.search(heading,head) and not numbered_lease:raise ValueError('No '+kind+' statement heading')
    if kind=='income' and re.search(r'quarter|three months|six months',head):raise ValueError('Quarterly income columns are not annual inputs')
    basis=latest.get('statement_type')
    if basis not in ('standalone','consolidated') or not re.search(r'\b'+basis+r'\b',head):raise ValueError('Explicit statement basis missing')
    if re.search(r'\b'+('consolidated' if basis=='standalone' else 'standalone')+r'\b',head):raise ValueError('Mixed statement basis')
    if latest.get('units_in')!='crore':raise ValueError('Baseline unit not crore')
    date=datetime.strptime(latest['period'],'%b %Y');year=date.year
    # Normalize explicit dates, including numeric headings, without deriving a
    # period from a cover or treating quarterly dates as an annual balance date.
    day=calendar.monthrange(year,date.month)[1];month=date.strftime('%b').lower();full=date.strftime('%B').lower()
    date_head=re.sub(r'\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b',lambda m: f'{int(m[1])} {calendar.month_abbr[int(m[2])].lower()} {m[3]}' if 1<=int(m[2])<=12 else m[0],head)
    date_head=re.sub(r'(?<=\w)-(?=\w)',' ',date_head)
    month_pattern='(?:'+month+'|'+full+')';day_pattern=str(day)+r'(?:\s*(?:st|nd|rd|th))?'
    date_pattern=r'(?:'+day_pattern+r'\s*'+month_pattern+'|'+month_pattern+r'\s*'+day_pattern+r')\s*,?\s*'
    # Some tables put a shared day/month above two year columns. Require the
    # explicit "As at" heading and exactly the current/prior pair below it.
    shared_prefix=r'as at' if kind=='balance' else r'(?:for the )?year ended' if kind=='income' else r'as at'
    shared_date=re.search(r'\b'+shared_prefix+r'\s+'+date_pattern+r'\n(?:[^\n]*\n){0,3}[^\n]*?\b'+str(year)+r'\s+'+str(year-1)+r'\s*$',date_head,re.M)
    if not re.search(date_pattern+str(year),date_head) and not shared_date:raise ValueError('Annual balance date not matched')
    years=re.findall(r'\b20\d{2}\b',head)
    order=re.search(str(year)+r'\s+(?:as\s+at\s+)?(?:'+date_pattern+r')?\s*'+str(year-1),date_head)
    # A repeated month heading can sit above "31, 2026   31, 2025".
    split_order=re.search(r'(?:as at|ended)\s+'+month_pattern+r'\s+(?:as at|ended)\s+'+month_pattern+r'\s+'+day_pattern+r'\s*,?\s*'+str(year)+r'\s+'+day_pattern+r'\s*,?\s*'+str(year-1),date_head)
    if str(year-1) not in years or not (order or split_order):raise ValueError('Annual column order not verified')
    currency=r'(?:rs\.?|inr|₹|`|rupees?)'
    units=[(name,scale) for name,scale in [('crore',1),('lakh',.01),('million',.1),('thousand',.0001)] if re.search(r'(?:\bin\s+(?:'+currency+r'\s+)?|\(\s*'+currency+r'[^\n]{0,15})'+name+r's?\b',head)]
    if len(units)!=1:raise ValueError('PDF units not explicit or ambiguous')
    if re.search(r'\b(?:usd|eur|gbp|us dollars?)\b|us\$',head) or not re.search(r'(?:₹|`|\binr\b|\brs\.?|\brupees?)\s*[^\n]{0,35}\b(?:crore|lakh|million|thousand)s?\b',head):raise ValueError('PDF INR currency not explicit or ambiguous')
    return text,basis,units[0]

def parse_income(text,latest):
    """Finance costs require an annual income table and matching reported PBT."""
    text,basis,(unit,scale)=statement_context(text,latest,'income');found={};lines={}
    for line in text.splitlines():
        m=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if not m:continue
        label=re.sub(r'\s{2,}'+NOTE_REF+r'\s*$','',line[:m.start()].strip(),flags=re.I)
        label=caption(label);label=re.sub(r'^[ivx]+[.)]?\s+','',label);label=re.sub(FORMULA_REF,'',label).strip()
        key='finance_costs' if label in ('finance costs','finance cost','finance cost (interest)','finance costs (interest)') else 'profit_before_tax' if label in ('profit before tax','profit/(loss) before tax','profit / (loss) before tax','profit (loss) before tax','profit/(loss) before taxation','profit before taxation') else None
        if key:
            if key in found:raise ValueError('Duplicate annual income caption')
            value=amount(m[1]);found[key]=value*scale if value is not None else None;lines[key]=line.strip()
    pbt=latest.get('profit_before_tax');reported=found.get('profit_before_tax');finance=found.get('finance_costs')
    if pbt is None or reported is None or abs(pbt-reported)>max(.015,abs(pbt)*.00002):raise ValueError('Annual income PBT anchor does not match')
    if finance is None or finance<0:raise ValueError('Explicit annual finance costs unavailable')
    return {'value':finance,'matched_profit_before_tax':reported,'source_units':unit,'conversion_to_crore':scale,'lines':lines}

def active_financial_navigation(page,clip=None):
    """Identify a visibly selected statement tab; never ignore body conflicts."""
    import pymupdf
    if clip is not None:return None
    labels={'Notice','Board’s Report','Report on Corporate Governance','Standalone Financial Statements','Consolidated Financial Statements'}
    spans=[s for b in page.get_text('dict')['blocks'] for line in b.get('lines',[]) for s in line['spans'] if s.get('text','').strip() in labels and s['bbox'][1]<page.rect.height*.18]
    if len(spans)!=5 or {s['text'].strip() for s in spans}!=labels:return None
    if max(s['bbox'][1] for s in spans)-min(s['bbox'][1] for s in spans)>2:return None
    selected=[s for s in spans if s.get('color')==0xffffff]
    if len(selected)!=1 or selected[0]['text'] not in ('Standalone Financial Statements','Consolidated Financial Statements'):return None
    selected=selected[0];box=pymupdf.Rect(selected['bbox'])
    fills=[d for d in page.get_drawings() if d.get('fill') and len(d['fill'])==3 and d['fill'][2]>.4 and d['fill'][2]>d['fill'][0]+.2 and d['fill'][1]>d['fill'][0]+.1 and d['rect'].contains(box)]
    if len(fills)!=1:return None
    fill=fills[0]
    if any(fill['rect'].intersects(pymupdf.Rect(s['bbox'])) for s in spans if s is not selected):return None
    bottom=fill['rect'].y1
    body=balance_text(page,pymupdf.Rect(0,bottom+.1,page.rect.width,page.rect.height))
    return selected['text']+'\n'+body,{'method':'printed_active_navigation_tab','title':selected['text'],'text_bbox':list(box),'text_color':selected['color'],'highlight_bbox':list(fill['rect']),'highlight_fill':list(fill['fill']),'inactive_titles':[s['text'] for s in spans if s is not selected]}

def parse_numbered_lease_disclosure(text,latest):
    """Read the closing value in a numbered lease note with its own table header."""
    title=re.search(r'^\s*(\d+\.\s+Disclosures for leasing arrangements)\s*$',text,re.I|re.M)
    basis=re.search(r'^(Standalone|Consolidated) Financial Statements\s*$',text,re.M)
    if not title or not basis:raise ValueError('Explicit numbered lease disclosure and statement section required')
    if re.search(r'\b'+('standalone' if basis[1]=='Consolidated' else 'consolidated')+r'\b',text,re.I):raise ValueError('Mixed statement basis')
    scope=text[title.end():]
    if not re.search(r'^\s*\(A\)\s+Company as a Lessee\s*$',scope,re.I|re.M):raise ValueError('Lessee subsection required')
    start=re.search(LEASE_ROLLFORWARD,scope,re.I)
    if not start:raise ValueError('Explicit lease carrying-value reconciliation unavailable')
    prefix=scope[:start.start()]
    header=re.search(r'\(b\)\s+Amount recognised during the year(?P<header>.*?)\ba\)\s+Depreciation on ROU Asset',prefix,re.I|re.S)
    if not header:raise ValueError('Lease note own dated table header unavailable')
    # Dotted leaders are typography. Remove only runs between a row caption
    # and its two printed cells; never decimal points or missing value cells.
    table=re.sub(r'\.{3,}(?=\s+)', '  ',scope[start.start():])
    head=basis[0]+'\n'+title[1]+'\n'+header['header']
    result=parse_lease_note(head+'\n'+table,latest)
    result['numbered_note_title']=title[1];result['own_table_header']=header['header'].strip()
    return result

def parse_lease_note(text,latest):
    """Read an explicit carrying-value closing balance, never a maturity total."""
    normalized='\n'.join(line.strip() for line in text.splitlines())
    start=re.search(LEASE_ROLLFORWARD,normalized,re.I)
    classified=False
    if not start:
        start=re.search(LEASE_NOTE_HEADING,normalized,re.I|re.M);classified=bool(start)
    if not start:raise ValueError('Explicit lease carrying-value reconciliation unavailable')
    scope=re.split(r'\n\s*(?:\d{1,3}[.)]|[A-Z]\))\s+',normalized[start.start():3000+start.start()])[0]
    closing=re.search(r'^\s*(?:closing balance|balance as of closing of the year|balance at the end(?: of (?:the )?year)?|lease liabilit(?:y|ies) as at (?:the )?end of (?:the )?year)(?:\s*\([a-z](?:[+\-][a-z])+\))?\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',scope,re.I|re.M)
    if not closing:raise ValueError('Explicit lease closing balance unavailable')
    scope=scope[:closing.end()]
    if classified and not all(re.search(r'^\s*'+term+r'\s{2,}'+NUM+r'\s{2,}'+NUM+r'\s*$',scope,re.I|re.M) for term in ('current','non[- ]current')):raise ValueError('Classified lease note needs both component rows')
    if re.search(r'undiscounted|maturity analysis|contractual maturities',normalized[max(0,start.start()-100):start.start()]+scope,re.I):raise ValueError('Lease maturity amounts are not carrying balances')
    # Keep the actual note heading and its own table dates/units. Do not borrow
    # the balance sheet's basis or unit for an unlabelled note.
    head='\n'.join(line for line in normalized[:start.start()].splitlines() if line)[:600]
    # A single lease table can share its dated header across A (ROU assets)
    # and B (lease liabilities). Retain that table's header, not dates from
    # an unrelated note or the preceding commitments table.
    prefix=normalized[:start.start()];shared_header=None
    shared=re.search(r'^\s*[A-Z]\.\s+Leases\s*\n(?P<header>(?:[^\n]*\n){1,5}?)\s*A\.\s+Movement in the Right to use assets\b',prefix,re.I|re.M)
    if shared and re.search(r'\nB\.\s*$',prefix[shared.end():],re.I):
        shared_header=shared['header'].strip();head+='\n'+shared_header
    _,_,(unit,scale)=statement_context(head+'\n'+scope,latest,'lease_note')
    value=amount(closing[1]);value=value*scale if value is not None else None
    if value is None or value<0:raise ValueError('Lease closing balance is not numeric')
    if classified:
        parts=[amount(re.search(r'^\s*'+term+r'\s{2,}('+NUM+r')\s{2,}'+NUM+r'\s*$',scope,re.I|re.M)[1]) for term in ('current','non[- ]current')]
        known_parts=[p*scale for p in parts if p is not None]
        if any(p<0 for p in known_parts) or sum(known_parts)>value+scale*.015 or (len(known_parts)==2 and abs(sum(known_parts)-value)>scale*.015):raise ValueError('Lease note components contradict closing balance')
    known=[latest.get(k) for k in ('current_lease_liabilities','non_current_lease_liabilities')]
    if sum(v for v in known if isinstance(v,(int,float)) and v>=0)>value+.015:raise ValueError('Lease closing balance contradicts known components')
    result={'value':value,'source_units':unit,'conversion_to_crore':scale,'line':closing[0].strip(),'method':'explicit_carrying_value_closing_balance'}
    if shared_header:result['shared_table_header']=shared_header
    return result

def parse_lease_rollforward(text,latest):
    """A lease-liability roll-forward whose printed arithmetic reconciles exactly.

    opening + printed movements = closing must hold to the paisa (Decimal) in the
    current-year column, and in the prior-year column where it is printed; the prior
    closing must equal the current opening. Unsigned outflows (payments, deletions) are
    subtracted only when that is the one reading that reconciles. A dash counts as nil
    only inside an identity that then holds exactly. The page must state the unit, both
    year-end dates in the table header and the selected statement basis."""
    from decimal import Decimal
    from .balance_identity import decimal_amount
    lines=[line.rstrip() for line in text.splitlines()]
    heads=[i for i,line in enumerate(lines) if re.search(r'lease\s+liabilit(?:y|ies)',line,re.I) and not re.search(r'undiscounted|maturit|right[- ]of[- ]use|interest on|payment of|\s{2,}'+NUM+r'\s*$',line,re.I)
           and re.fullmatch(r'\s*(?:\(?[a-z0-9]{1,4}[.)]\s*)*(?:the following is the |the table below shows the |details of |movement (?:in|of) (?:the )?)?(?:carrying (?:value|amount) of )?(?:the )?(?:movement (?:in|of) )?lease\s+liabilit(?:y|ies)(?: during the year)?\s*:?\s*',line,re.I)]
    if len(heads)!=1:raise ValueError('Lease roll-forward heading absent or ambiguous')
    start=heads[0]
    date=datetime.strptime(latest['period'],'%b %Y');day=calendar.monthrange(date.year,date.month)[1]
    def dated(line,year):
        compact=re.sub(r'[-./]',' ',line.lower())
        return re.search(r'\b'+str(day)+r'(?:st|nd|rd|th)?\s+(?:'+date.strftime('%B').lower()+'|'+date.strftime('%b').lower()+'|0?'+str(date.month)+r')\s+'+str(year)+r'\b',compact) or re.search(r'(?:'+date.strftime('%B').lower()+'|'+date.strftime('%b').lower()+r')\s+'+str(day)+r',?\s+'+str(year)+r'\b',compact)
    header=[i for i in range(start+1,min(start+5,len(lines))) if dated(lines[i],date.year) and dated(lines[i],date.year-1)]
    if len(header)!=1:raise ValueError('Lease roll-forward needs both year-end dates in its own header')
    rows=[];closing=None
    for line in lines[header[0]+1:header[0]+16]:
        m=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if not m:
            if line.strip() and re.search(r'right[- ]of[- ]use|depreciation|amorti[sz]ation|undiscounted|maturit',line,re.I):raise ValueError('Lease roll-forward mixes another table')
            continue
        label=' '.join(line[:m.start()].lower().split())
        values=[decimal_amount(m[1]),decimal_amount(m[2])]
        if re.search(r'right[- ]of[- ]use|depreciation|amorti[sz]ation',label):raise ValueError('Lease roll-forward mixes another table')
        rows.append({'label':label,'values':values,'line':line.strip()})
        if re.match(r'(?:closing balance|balance as at 3[01]|balance at the end of the (?:year|period)|as at 3[01])',label):closing=rows.pop();break
    if closing is None or len(rows)<2:raise ValueError('Lease roll-forward closing balance unavailable')
    if not re.match(r'(?:opening balance|balance as at (?:1st |1 )?(?:april|apr)|balance at the beginning of the (?:year|period)|as at (?:1st |1 )?(?:april|apr))',rows[0]['label']):raise ValueError('Lease roll-forward opening balance unavailable')
    outflow=r'payment|paid|deletion|deduction|termination|reduction|repaid|derecogni|less'
    def reconciles(col):
        values=[r['values'][col] for r in rows];total=closing['values'][col]
        if total is None:total=Decimal(0)
        for flip in (False,True):
            amount=Decimal(0)
            for r,v in zip(rows,values):
                v=v if v is not None else Decimal(0)
                if flip and v>0 and re.search(outflow,r['label']) and r is not rows[0]:v=-v
                amount+=v
            if amount==total:return 'unsigned_outflows_subtracted' if flip else 'printed_signs'
        return None
    current=reconciles(0)
    if not current or closing['values'][0] is None or closing['values'][0]<0:raise ValueError('Lease roll-forward arithmetic does not reconcile')
    prior=reconciles(1)
    if any(r['values'][1] is not None for r in rows+[closing]) and not prior:raise ValueError('Prior-year lease roll-forward does not reconcile')
    if (closing['values'][1] or Decimal(0))!=(rows[0]['values'][0] or Decimal(0)):raise ValueError('Prior closing lease balance differs from current opening')
    before='\n'.join(lines[:start]).lower()
    units=[(m.start(),m[1]) for m in re.finditer(r'(?:₹|`|\binr\b|\brs\.?)\s*(?:in\s+)?(crores?|lakhs?|millions?|thousands?)\b|all amounts (?:are )?in (?:₹|inr|rs\.?)?\s*(crores?|lakhs?|millions?)',before)]
    if not units:raise ValueError('Lease roll-forward unit not explicit')
    unit=(units[-1][1] or '').rstrip('s') or re.search(r'(crore|lakh|million)',before[units[-1][0]:]).group(1)
    scale={'crore':1.,'lakh':.01,'million':.1,'thousand':1e-4}[unit]
    titles=set(re.findall(r'notes\s+(?:to|forming\s+part\s+of)\s+(?:the\s+)?(standalone|consolidated)\s+financial\s+statements',' '.join(text.lower().split())))
    if titles!={latest.get('statement_type')}:raise ValueError('Lease roll-forward basis not explicit or differs')
    value=float(closing['values'][0])*scale
    known=[latest.get(k) for k in ('current_lease_liabilities','non_current_lease_liabilities')]
    if sum(v for v in known if isinstance(v,(int,float)) and v>=0)>value+.015:raise ValueError('Lease closing balance contradicts known components')
    return {'value':value,'source_units':unit,'conversion_to_crore':scale,'line':closing['line'],'method':'reconciled_lease_rollforward','sign_reading':current,
            'table':[r['line'] for r in rows]+[closing['line']],'header':lines[header[0]].strip()}

def parse_classified_lease_note(text,latest):
    """A lease note's explicit total, reconciled to both printed components."""
    rows=r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*'
    block=re.search(r'^\s*(?:\(?[a-z]\)\s*)?(?:lease liabilities|carrying amount of lease liability:)(?:\s{2,}[a-z]+\s+\d{1,2},?\s+20\d{2}\s{2,}[a-z]+\s+\d{1,2},?\s+20\d{2})?\s*\n'
                    r'\s*current'+rows+r'\n\s*non[- ]current'+rows+
                    r'\n\s*total(?: lease liabilities)?'+rows+r'$',text,re.I|re.M)
    if not block:raise ValueError('Explicit classified lease total unavailable')
    # The note's own dates, currency and statement basis must be visible. Stop
    # at this table, before later cash flows or undiscounted commitments.
    context=text[:block.end()]
    if re.search(r'undiscounted|maturity analysis|contractual maturities',context[max(0,block.start()-400):],re.I):raise ValueError('Lease maturity amounts are not carrying balances')
    _,_,(unit,scale)=statement_context(context,latest,'lease_note')
    current,noncurrent,total=[amount(block[i]) for i in (1,3,5)]
    if any(v is None or v<0 for v in (current,noncurrent,total)):raise ValueError('Explicit lease components and total required')
    if abs(current+noncurrent-total)>.015:raise ValueError('Classified lease total does not reconcile')
    for key,value in (('current_lease_liabilities',current),('non_current_lease_liabilities',noncurrent)):
        known=latest.get(key)
        if known is not None and abs(known-value*scale)>max(.015,abs(known)*.00002):raise ValueError('Lease note contradicts known components')
    return {'value':total*scale,'source_units':unit,'conversion_to_crore':scale,'line':block[0].strip(),'method':'explicit_classified_lease_total',
            'components':{'current':current*scale,'non_current':noncurrent*scale}}

def parse_dated_lease_rollforward(text,latest):
    """An explicitly dated closing carrying balance in a lease movement table."""
    normalized='\n'.join(line.strip() for line in text.splitlines())
    start=re.search(r'^\s*(?:\([A-Z]\)|\d{1,3}[.)]?)\s+lease liabilit(?:y|ies)(?:\s{2,}[^\n]*)?$',normalized,re.I|re.M)
    if not start:raise ValueError('Dated lease movement heading unavailable')
    scope=normalized[start.start():start.start()+3000]
    scope=re.split(r'\n\s*(?:\([A-Z]\)|\d{1,3}[.)])\s+',scope)[0]
    date=datetime.strptime(latest['period'],'%b %Y');day=calendar.monthrange(date.year,date.month)[1]
    closing=re.search(r'^balance as at '+str(day)+r'(?:st|nd|rd|th)?\s+'+date.strftime('%B')+r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',scope,re.I|re.M)
    if not closing or not re.search(r'^balance as at 1(?:st)?\s+[A-Za-z]+\s{2,}'+NUM+r'\s{2,}'+NUM+r'\s*$',scope,re.I|re.M):raise ValueError('Explicit opening and dated closing balances required')
    scope=scope[:closing.end()]
    if re.search(r'undiscounted|maturity analysis|contractual maturities',scope,re.I):raise ValueError('Lease maturity amounts are not carrying balances')
    movements=[line for line in scope.splitlines() if re.search(r'^(?:additions|adjustments|add: interest|less: payment)',line,re.I) and re.search(r'\s{2,}'+NUM+r'\s{2,}'+NUM+r'\s*$',line)]
    if len(movements)<2:raise ValueError('Lease movement rows unavailable')
    head='\n'.join(line for line in normalized[:start.start()].splitlines() if line)[:600]
    _,_,(unit,scale)=statement_context(head+'\n'+scope,latest,'lease_note')
    value=amount(closing[1])
    if value is None or value<0:raise ValueError('Lease closing balance is not numeric')
    known=[latest.get(k) for k in ('current_lease_liabilities','non_current_lease_liabilities')]
    if sum(v for v in known if isinstance(v,(int,float)) and v>=0)>value*scale+.015:raise ValueError('Lease closing balance contradicts known components')
    return {'value':value*scale,'source_units':unit,'conversion_to_crore':scale,'line':closing[0].strip(),'table':scope,'method':'explicit_dated_carrying_value_closing_balance'}

def parse_unclassified_lease_note(text,latest):
    """An explicit lease carrying amount within a fully reconciled OFL note.

    The caller must also match the note number and both total columns to an
    unclassified balance sheet. This cannot supply a missing classified part.
    """
    title=re.search(r'^[ \t]*Note (\d+): Other Financial Liabilities[^\n]*$',text,re.I|re.M)
    if not title:raise ValueError('Unclassified other-financial-liabilities note title unavailable')
    scope=re.split(r'\n\s*Note \d+:',text[title.start():],maxsplit=1,flags=re.I)[0]
    if re.search(r'\b(?:current|non.current|undiscounted|maturit(?:y|ies))\b',scope,re.I):raise ValueError('Classified/maturity note cannot establish an unclassified lease total')
    # Retain the note's own page title and its own table header, never the dates
    # or scale of an earlier note printed on the same page.
    head=re.search(r'notes\s+to\s+the\s+(?:standalone|consolidated)\s+financial\s+statements[^\n]*',text,re.I)
    if not head:raise ValueError('Explicit note statement title required')
    _,_,(unit,scale)=statement_context(head[0]+'\n'+scope,latest,'lease_note')
    total=None;parts=[];lease=None
    for line in scope.splitlines()[1:]:
        row=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if not row:continue
        label=caption(line[:row.start()]);values=[amount(row[1]),amount(row[2])]
        if any(v is None or v<0 for v in values):raise ValueError('Every OFL note amount must be explicitly numeric')
        item={'label':label,'printed_values':values,'line':line.strip()}
        if label=='total':total=item;break
        if label in ('lease liability','lease liabilities'):
            if lease is not None:raise ValueError('Duplicate lease line in OFL note')
            lease=item
        parts.append(item)
    if total is None or lease is None or len(parts)<2:raise ValueError('Complete OFL lease table unavailable')
    if any(abs(sum(r['printed_values'][j] for r in parts)-total['printed_values'][j])>.015 for j in (0,1)):raise ValueError('OFL note rows do not sum to printed total')
    known=[latest.get(k) for k in ('current_lease_liabilities','non_current_lease_liabilities')]
    if sum(v for v in known if isinstance(v,(int,float)) and v>=0)>lease['printed_values'][0]*scale+.015:raise ValueError('Lease note contradicts known components')
    return {'value':lease['printed_values'][0]*scale,'source_units':unit,'conversion_to_crore':scale,'line':lease['line'],'method':'explicit_lease_row_in_reconciled_unclassified_note',
            'parent_balance_requirement':{'note':title[1],'current':total['printed_values'][0]*scale,'prior':total['printed_values'][1]*scale},
            'arithmetic':{'rows':parts,'printed_total':total,'summed_columns':[sum(r['printed_values'][j] for r in parts) for j in (0,1)]}}

def parse_numbered_ofl_note(text):
    """Printed lease row in a numbered, fully summing "Other financial liabilities" note.

    The note need not print its own unit: it is accepted only when the balance
    sheet prints an unclassified "Other financial liabilities <note>" row with
    exactly the same two printed totals (see bound_ofl_lease), so the note shares
    that statement's basis, dates and unit. No dash is read as zero."""
    title=re.search(r'^[ \t]*(?:Note\s+)?(\d{1,3})(?:[.:]\s*|\s*[-–]\s*|\s+)Other financial liabilities(?:\s{2,}[^\n]*\b20\d{2}\b[^\n]*)?\s*$',text,re.I|re.M)
    if not title:raise ValueError('Numbered other-financial-liabilities note unavailable')
    scope=re.split(r'\n[ \t]*(?:Note\s+)?\d{1,3}(?:[.:]\s*|\s+)[A-Z]',text[title.end():],maxsplit=1)[0]
    # Only the table up to its total row; footnotes after it are not part of the classification.
    closing=re.search(r'^[ \t]*(?:\(?[a-z]\)\s*)?total\b[^\n]*\d\s*$',scope,re.I|re.M)
    if closing:scope=scope[:closing.end()]
    if re.search(r'\b(?:current|non.current|undiscounted|maturit(?:y|ies))\b',scope,re.I):raise ValueError('Classified/maturity note cannot establish an unclassified lease total')
    # Carrying amounts may be grouped as payable within / after twelve months;
    # one lease row under each group is summed. Any other text line is rejected.
    groups=('payable within twelve months? after (?:the )?reporting (?:period|date)','payable after twelve months? after (?:the )?reporting (?:period|date)')
    rows=[];total=None;lease=None;group=None;grouped_leases={};pending=None
    for line in scope.splitlines():
        row=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if not row:
            # Column headers (dates, "As at", "Particulars") only before the first row.
            if not line.strip() or (not rows and pending is None and re.search(r'\bas at\b|\bparticulars\b|\b20\d{2}\b|\bin\s+(?:lakhs?|crores?|millions?)\b',line,re.I)):continue
            heading=[i for i,g in enumerate(groups) if re.fullmatch(r'\s*'+g+r'\s*',line,re.I)]
            if heading and pending is None and (group is None or heading[0]>group):group=heading[0];continue
            # One wrapped caption line continues on the next numeric row.
            if pending is not None or heading:raise ValueError('Unparsed line inside other-financial-liabilities note')
            pending=line.strip();continue
        label=' '.join((((pending+' ') if pending else '')+line[:row.start()]).lower().split());pending=None
        # Row markers such as "(iv)" or "(B)" are not part of the caption.
        label=re.sub(r'^(?:sr\.?\s*)?\(?(?:[ivx]{1,4}|[a-z]|\d{1,2})\)\s*','',label)
        values=[amount(row[1]),amount(row[2])]
        if values[0] is None or values[0]<0:raise ValueError('Every current OFL amount must be explicitly numeric')
        item={'label':label,'printed_values':values,'line':line.strip()}
        # The closing total may be printed with or without a "Total" caption.
        if label in ('total',''):total=item;break
        if re.fullmatch(r'lease liabilit(?:y|ies)\*?(?:\s*\(refer note:?\s*\d+\))?\*?',label):
            if group is not None:
                if group in grouped_leases:raise ValueError('Duplicate lease line in OFL group')
                grouped_leases[group]=item
            else:
                if lease is not None:raise ValueError('Duplicate lease line in OFL note')
                lease=item
        rows.append(item)
    if grouped_leases:
        if lease is not None or len(grouped_leases)!=2:raise ValueError('Grouped OFL note needs one lease row in each group')
        parts=[grouped_leases[0]['printed_values'][0],grouped_leases[1]['printed_values'][0]]
        lease={'label':'lease liabilities (within + after twelve months)','printed_values':[round(sum(parts),6),None],'line':' + '.join(grouped_leases[g]['line'] for g in (0,1)),'parts':parts}
    if pending is not None or total is None or lease is None or len(rows)<2:raise ValueError('Complete OFL lease table unavailable')
    if abs(sum(r['printed_values'][0] for r in rows)-total['printed_values'][0])>.015:raise ValueError('OFL note rows do not sum to printed total')
    prior=[r['printed_values'][1] for r in rows]
    if total['printed_values'][1] is None:raise ValueError('Prior OFL total must be explicitly numeric')
    if all(v is not None for v in prior) and abs(sum(prior)-total['printed_values'][1])>.015:raise ValueError('Prior OFL rows do not sum to printed total')
    return {'printed_value':lease['printed_values'][0],'note':title[1],'printed_totals':total['printed_values'],'line':lease['line'],'rows':rows,'printed_total':total,
            'method':'lease_row_in_summing_ofl_note_bound_to_balance_row'}

def bound_ofl_lease(note,text,scale):
    """Crore value of a numbered OFL lease row whose note totals equal the balance row exactly."""
    if re.search(r'^\s*(?:non[- ]current|current) liabilities\s*$',text,re.I|re.M):return None
    rows=list(re.finditer(r'^\s*(?:\([a-z]\)\s+)?Other financial liabilities\s{2,}'+re.escape(note['note'])+r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',text,re.I|re.M))
    if len(rows)!=1 or [amount(rows[0][1]),amount(rows[0][2])]!=note['printed_totals']:return None
    return {'value':note['printed_value']*scale,'conversion_to_crore':scale,'balance_row':rows[0][0].strip(),**note}

def matching_unclassified_note(note,text,balance_scale):
    requirement=note.get('parent_balance_requirement')
    if not requirement:return True
    if re.search(r'^\s*(?:non[- ]current|current) liabilities\s*$',text,re.I|re.M):return False
    rows=list(re.finditer(r'^\s*Other financial liabilities\s{2,}'+re.escape(requirement['note'])+r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',text,re.I|re.M))
    if len(rows)!=1:return False
    values=[amount(rows[0][1]),amount(rows[0][2])];scale=balance_scale
    return all(v is not None and abs(v*scale-requirement[k])<=scale*.015 for v,k in zip(values,('current','prior')))

def parse_financing_lease_reconciliation(text,latest):
    """Read the printed closing cell of two horizontally arranged rollforwards.

    Each table must explicitly date its opening and closing columns. Dashes in
    movement cells are retained as unknown; no missing movement is made zero.
    """
    normalized='\n'.join(line.strip() for line in text.splitlines())
    title=re.search(r'reconciliation of movement of liabilities to cash flows arising from financing activities',normalized,re.I)
    if not title:raise ValueError('Financing reconciliation title unavailable')
    date=datetime.strptime(latest['period'],'%b %Y')
    if date.month!=3:raise ValueError('Unsupported financing reconciliation date layout')
    scope=normalized[title.end():]
    tables=[]
    for year in (date.year,date.year-1):
        start=re.search(r'^Year ended March 31, '+str(year)+r'\s*$',scope,re.I|re.M)
        if not start:raise ValueError('Both annual financing reconciliations required')
        block=re.split(r'\nYear ended ',scope[start.end():],maxsplit=1,flags=re.I)[0]
        header=re.search(r'Particulars\s+March 31,\s+Business\s+Cash flows\s+Exchange\s+Others\s+March 31,\s*\n'+str(year-1)+r'\s+Combination\s+\(net\)\s+difference\s+'+str(year)+r'\s*$',block,re.I|re.M)
        if not header:raise ValueError('Opening/movement/closing financing columns not verified')
        rows=list(re.finditer(r'^Lease liabilities'+(r'\s{2,}('+NUM+r')')*6+r'\s*$',block,re.I|re.M))
        if len(rows)!=1:raise ValueError('Exactly one complete six-column lease row required')
        tokens=list(rows[0].groups());opening,closing=amount(tokens[0]),amount(tokens[-1])
        if opening is None or closing is None or min(opening,closing)<0:raise ValueError('Numeric opening and closing lease carrying values required')
        tables.append({'year':year,'header':header[0],'row':rows[0][0],'printed_cells':tokens,'opening':opening,'closing':closing})
    if tables[0]['opening']!=tables[1]['closing']:raise ValueError('Lease prior closing does not match current opening')
    # The closing dates are printed in separate tables, current table first.
    # Supply their verified order to the unchanged period/basis/unit checker.
    context=normalized[:title.start()]+'\n'+f'March 31, {date.year}    March 31, {date.year-1}'+'\n'+scope[:scope.find('Particulars')]
    _,_,(unit,scale)=statement_context(context,latest,'lease_note')
    value=tables[0]['closing']*scale
    known=[latest.get(k) for k in ('current_lease_liabilities','non_current_lease_liabilities')]
    if sum(v for v in known if isinstance(v,(int,float)) and v>=0)>value+.015:raise ValueError('Lease closing balance contradicts known components')
    return {'value':value,'source_units':unit,'conversion_to_crore':scale,'line':tables[0]['row'],'method':'explicit_horizontal_financing_lease_closing_balance','tables':tables,'prior_year_bridge':{'current_opening':tables[0]['opening'],'prior_closing':tables[1]['closing']},'movement_dashes':'retained as unknown; no arithmetic or zero inference'}

def parse_capital_note(text,latest):
    """Explicit lease total in a capital note, with three anchors and net-debt proof.

    This is a carrying balance, not a lease commitment/maturity schedule. A dash
    never means zero. All amounts come from the same two-column annual table.
    """
    normalized='\n'.join(line.strip() for line in text.splitlines())
    heading=re.search(r'^\s*(?:[A-Z0-9]+[.)]?\s+)?capital management\s*$',normalized,re.I|re.M)
    if not heading:raise ValueError('Capital management heading unavailable')
    scope=normalized[heading.start():heading.start()+5000]
    # Keep the financial note title, but require dates and units in this table.
    title=re.search(r'^.*notes to (?:the )?(?:consolidated|standalone) financial statements.*$',normalized,re.I|re.M)
    if not title:raise ValueError('Explicit financial note basis unavailable')
    table=re.search(r'^.*total borrowings\s{2,}'+NUM+r'\s{2,}'+NUM+r'\s*$',scope,re.I|re.M)
    if not table:raise ValueError('Capital note borrowings table unavailable')
    header=scope[max(0,table.start()-500):table.start()]
    _,basis,(unit,scale)=statement_context(title[0]+'\n'+header,latest,'lease_note')
    found={};lines={}
    labels={'total borrowings':'total_borrowings','lease liabilities':'total_lease_liabilities',
            'lease liabilitites':'total_lease_liabilities', # Printed Acutaas report caption.
            'less: cash and cash equivalents':'cash_and_cash_equivalents',
            'net debt':'net_debt','net debts':'net_debt','total equity':'equity'}
    for line in scope[table.start():].splitlines():
        if re.match(r'\s*note\s*:',line,re.I):break
        match=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if not match:continue
        label=re.sub(r'\s*\([a-z]\)\s*$','',caption(line[:match.start()]))
        key=labels.get(label)
        if not key:continue
        if key in found:raise ValueError('Duplicate capital note caption')
        value=amount(match[1])
        if value is None:raise ValueError('Capital note amount is not explicit')
        # Parentheses following "Less:" express a deduction, not negative cash.
        if key=='cash_and_cash_equivalents':value=abs(value)
        found[key]=value*scale;lines[key]=line
    required={'total_borrowings','total_lease_liabilities','cash_and_cash_equivalents','net_debt','equity'}
    if set(found)!=required:raise ValueError('Capital note proof is incomplete')
    if any(found[k]<0 for k in required-{'net_debt'}):raise ValueError('Negative capital note input')
    anchors=['total_borrowings','cash_and_cash_equivalents','equity']
    if any(not isinstance(latest.get(k),(int,float)) or abs(found[k]-latest[k])>max(.015,abs(latest[k])*.00002) for k in anchors):raise ValueError('Capital note anchors do not match')
    if abs(found['total_borrowings']+found['total_lease_liabilities']-found['cash_and_cash_equivalents']-found['net_debt'])>max(.000001,scale*.03):raise ValueError('Capital note net-debt arithmetic does not reconcile')
    known=[latest.get(k) for k in ('current_lease_liabilities','non_current_lease_liabilities')]
    if sum(v for v in known if isinstance(v,(int,float)) and v>=0)>found['total_lease_liabilities']+.015:raise ValueError('Capital note contradicts known lease components')
    value=found['total_lease_liabilities']
    return {'value':value,'values':{'total_lease_liabilities':value},'period':latest['period'],
            'statement_type':basis,'units_in':'crore','source_units':unit,'conversion_to_crore':scale,
            'source_currency':'INR','matched_fields':anchors,'lines':lines,'capital_note_values':found,
            'method':'explicit_capital_note_with_reconciled_net_debt'}

def explicit_equity_section_subtotal(text,basis,scale):
    """A lettered Equity section with its explicitly matching printed subtotal."""
    if basis!='standalone':return None
    heading=re.search(r'^\s*([A-Z])\s+Equity\s*$',text,re.M)
    if not heading:return None
    block=text[heading.end():]
    subtotal=re.search(r'^\s*Sub Total \('+heading[1]+r'\)\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',block,re.I|re.M)
    if not subtotal:return None
    parts=[]
    for line in block[:subtotal.start()].splitlines():
        if not line.strip():continue
        row=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if not row:return None
        label=caption(re.sub(r'\s{2,}'+NOTE_REF+r'\s*$','',line[:row.start()],flags=re.I))
        if label not in ('equity share capital','instruments entirely equity in nature','other equity'):return None
        value=amount(row[1])
        if value is None or value<0:return None
        parts.append({'label':label,'printed_value':value,'line':line.strip()})
    if len(parts)!=3 or len({r['label'] for r in parts})!=3:return None
    value=amount(subtotal[1])
    if value is None or abs(sum(r['printed_value'] for r in parts)-value)>.015:return None
    return {'value':value*scale,'heading':heading[0].strip(),'subtotal':subtotal[0].strip(),'components':parts,'sum':sum(r['printed_value'] for r in parts),'source_scale':scale,'comparative':'printed only; no value inferred for dash'}

def parse_balance(text,latest,allow_existing=False,allow_identity=True,current_nci=None):
    """Two-column annual balance only; period, basis, scale and 2 anchors required."""
    text,basis,(unit,scale)=statement_context(text,latest)
    from .balance_identity import identity_evidence,borrowings_reconciliation
    identities=identity_evidence(text,scale) if allow_identity else {}
    section=None;found={};evidence={};equity_section=False;previous_key=None;subtotal_equity=None;cash_parts=[]
    # Some audited tables have separate Note and Page reference columns.
    # Remove both only when that exact header is present; numeric financial
    # columns must never be mistaken for references.
    note_and_page=bool(re.search(r'\bNote\s+Page\b',text[:1800],re.I))
    def record(key,value,line):
        nonlocal previous_key
        previous_key=key
        if key in found:raise ValueError('Duplicate PDF balance caption: '+key)
        found[key]=None if value is None else value*scale;evidence[key]=line.strip()
    for line in text.splitlines():
        # Stop at the next financial statement; don't read cash-flow lease payments.
        line=re.sub(r'(?<=[0-9–—-])\*(?=\s|$)','',line)
        if re.search(r'^(?:standalone |consolidated )?statement of (?:profit|cash|changes)',line.strip().lower()):break
        clean=caption(line)
        if clean=='equity':equity_section=True
        if 'liabilities' in clean and not re.search(r'\d',clean):equity_section=False
        # Some reports print the equity subtotal without a label. Accept only
        # the row directly after its explicit components, with exact arithmetic.
        subtotal=re.fullmatch('('+NUM+r')\s{2,}('+NUM+')',line.strip())
        if equity_section and previous_key in ('other_equity','nci') and subtotal and found.get('share_capital') is not None and found.get('other_equity') is not None:
            value=amount(subtotal[1]);total=found['share_capital']+found['other_equity']+(found.get('nci') or 0)
            if value is not None and abs(value*scale-total)<=scale*.015:
                subtotal_equity=(value*scale,'nci' in found);equity_section=False
        if section=='current' and re.match(r'^total (?:equity (?:and|&) liabilities|liabilities (?:and|&) equity|liabilities)\s{2,}',line.strip(),re.I):break
        if re.fullmatch(r'non[- ]current liabilities',clean):section='non_current'
        elif re.fullmatch(r'current liabilities',clean):section='current'
        m=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if not m:continue
        label=line[:m.start()].strip()
        # Drop a separate note-number column, never a value column.
        if note_and_page:
            label=re.sub(r'\s{2,}'+NOTE_REF+r'\s{2,}F?\d{1,4}\s*$','',label,flags=re.I)
        else:label=re.sub(r'\s{2,}'+NOTE_REF+r'\s*$','',label,flags=re.I)
        # A separate unpunctuated alphabetic row marker is not part of a caption.
        label=re.sub(r'^[a-z]\s{2,}','',label,flags=re.I)
        label=caption(label);value=amount(m.group(1))
        if label in ('cash and cash equivalents','cash & cash equivalents','cash and cash equivalent','cash & cash equivalent'):record('cash',value,line)
        # Some balance sheets print cash only as explicitly captioned parts
        # (e.g. "Cash and Cash Equivalents-Owned Fund" / "-Clients Fund").
        part=re.fullmatch(r'cash (?:and|&) cash equivalents?\s*[-–]\s*([a-z][a-z ]{2,40})',label)
        if part and value is not None:cash_parts.append((part[1],value*scale,line.strip()))
        if label=='sub-total-equity' or re.fullmatch(r'total[ -]equity(?: \([a-z0-9]+\))?',label):record('equity',value,line)
        if label in ('equity attributable to owners of the company','equity attributable to owners of the parent','total equity attributable to owners of the company','total equity attributable to owners of the parent'):record('owners_equity',value,line)
        if label in ('equity share capital','share capital'):record('share_capital',value,line)
        if label=='other equity':record('other_equity',value,line)
        if label in ('non controlling interest','non controlling interests','non-controlling interest','non-controlling interests'):record('nci',value,line)
        if label in ('borrowings','borrowings, current','borrowings, non-current') and section:record(section+'_borrowings',value,line)
        if section is None and label in ('debt securities','borrowings (other than debt securities)','subordinated liabilities'):
            record({'debt securities':'debt_securities','borrowings (other than debt securities)':'other_borrowings','subordinated liabilities':'subordinated_liabilities'}[label],value,line)
        if label in ('lease liabilities','lease liability','lease obligations','financial liabilities-lease liabilities','net liability in leased assets','current maturity of leased liability'):
            record((section+'_lease') if section else 'total_lease',value,line)
    if 'cash' not in found and len(cash_parts)>=2 and len({name for name,_,_ in cash_parts})==len(cash_parts):
        found['cash']=sum(v for _,v,_ in cash_parts);evidence['cash']=' + '.join(line for _,_,line in cash_parts)
    equity_subtotal=explicit_equity_section_subtotal(text,basis,scale) if found.get('equity') is None else None
    if equity_subtotal:found['equity']=equity_subtotal['value'];evidence['equity']=equity_subtotal['subtotal']
    if 'nci' not in found and current_nci is not None:
        record('nci',current_nci['printed_value'],current_nci['row_text'])
    if 'nci' not in found and 'nci' in identities:found['nci']=0.
    if 'non_current_borrowings' not in found and 'non_current_borrowings' in identities:found['non_current_borrowings']=0.
    if 'borrowings_absent' in identities and not any(k.endswith('_borrowings') and found.get(k) for k in found):
        found['current_borrowings']=found.get('current_borrowings') or 0.;found['non_current_borrowings']=found.get('non_current_borrowings') or 0.
    matched=[]
    def same(a,b):return a is not None and b is not None and abs(a-b)<=max(.015,abs(b)*.00002)
    if same(found.get('cash'),latest.get('cash_and_cash_equivalents')):matched.append('cash_and_cash_equivalents')
    equity=found.get('equity')
    if equity is None and subtotal_equity is not None:
        equity=subtotal_equity[0]+(0 if subtotal_equity[1] else found.get('nci') or 0)
    derived_equity=equity is None
    if derived_equity:
        # Explicit components can substantiate the equity anchor when a printed
        # subtotal has no caption. Consolidated totals must include explicit NCI.
        owners=found.get('owners_equity')
        if owners is None and all(found.get(k) is not None for k in ('share_capital','other_equity')):owners=found['share_capital']+found['other_equity']
        if owners is not None and (basis=='standalone' or found.get('nci') is not None):equity=owners+(found.get('nci') or 0)
    if same(equity,latest.get('equity')) or (not derived_equity and equity is not None and found.get('nci') is not None and same(equity+found['nci'],latest.get('equity'))):matched.append('equity')
    borrow=[found.get(k+'_borrowings') for k in ('current','non_current')]
    if all(v is not None for v in borrow) and same(sum(borrow),latest.get('total_borrowings')):matched.append('total_borrowings')
    # The selected NBFC filing defines debt as these three explicit components.
    # Deposits and other liabilities are not substituted; all three printed
    # numeric rows and the existing definition must agree independently.
    nbfc_keys=('debt_securities','other_borrowings','subordinated_liabilities')
    nbfc_anchor=None
    definition=latest.get('sources',{}).get('total_borrowings',{}).get('formula')
    # A component printed as a dash agrees with an audited NSE value of exactly 0.
    nbfc_values={k:(0. if k in found and found[k] is None and latest.get(k)==0 else found.get(k)) for k in nbfc_keys}
    if section is None and definition=='debt_securities + other_borrowings + subordinated_liabilities' and all(nbfc_values[k] is not None and nbfc_values[k]>=0 and same(nbfc_values[k],latest.get(k)) for k in nbfc_keys):
        total=sum(nbfc_values[k] for k in nbfc_keys)
        if same(total,latest.get('total_borrowings')):
            if 'total_borrowings' not in matched:matched.append('total_borrowings')
            nbfc_anchor={'formula':definition,'operands':nbfc_values,'result':total,'lines':{k:evidence[k] for k in nbfc_keys}}
    if len(matched)<2:raise ValueError('Fewer than two matching balance-sheet anchors')
    parts=[found.get(k+'_lease') for k in ('current','non_current')]
    if all(v is not None and v>=0 for v in parts):value=sum(parts)
    elif not any(k.endswith('_lease') and k!='total_lease' for k in found) and found.get('total_lease') is not None and found['total_lease']>=0:value=found['total_lease']
    else:value=None
    values={}
    if value is not None:values['total_lease_liabilities']=value
    if all(v is not None and v>=0 for v in borrow):values['total_borrowings']=sum(borrow)
    if found.get('cash') is not None and found['cash']>=0:values['cash_and_cash_equivalents']=found['cash']
    reconciliation=borrowings_reconciliation(latest.get('total_borrowings'),values.get('total_borrowings'),value,evidence,scale)
    if not allow_existing and not reconciliation and not any(latest.get(k) is None for k in values):raise ValueError('No missing balance input safely recovered; explicit lease components may be unavailable')
    lease_calculation=({'expression':'current_lease + non_current_lease','operands':dict(zip(('current_lease','non_current_lease'),parts)),'result':value,'units':'crore'} if all(v is not None and v>=0 for v in parts) else None)
    date=datetime.strptime(latest['period'],'%b %Y')
    context_evidence={'balance_date':date.replace(day=calendar.monthrange(date.year,date.month)[1]).date().isoformat(),
        'statement_title_lines':[line for line in text[:1800].splitlines() if re.search(r'balance\s+sheet',line,re.I)],
        'column_header_lines':[line for line in text[:1800].splitlines() if re.search(r'\b20\d{2}\b|\bas at\b',line,re.I)],
        'units_lines':[line for line in text[:1800].splitlines() if re.search(r'\b(?:crore|lakh|million|thousand)s?\b',line,re.I)],
        'column_order':'current_year_then_prior_year'}
    return {'value':value,'values':values,'period':latest['period'],'statement_type':basis,'units_in':'crore','source_units':unit,'lease_calculation':lease_calculation,'context_evidence':context_evidence,'identity_evidence':identities,'borrowings_reconciliation':reconciliation,
            'conversion_to_crore':scale,'source_currency':'INR','matched_fields':matched,'balance_equity':equity,'nbfc_borrowings_anchor':nbfc_anchor,'equity_subtotal_evidence':equity_subtotal,'borrowing_components':{k:v for k,v in found.items() if k.endswith('_borrowings')},'components':{k:v for k,v in found.items() if k.endswith('_lease')},'lines':evidence}

def balance_text(page,clip=None):
    """Resolve only documented rupee-font glyphs next to an explicit unit label."""
    kwargs={'clip':clip} if clip is not None else {}
    text=page.get_text(sort=True,**kwargs)
    blocks=page.get_text('dict',**kwargs)['blocks']
    lines=[line for block in blocks for line in block.get('lines',[])]
    rotated=[line for line in lines if tuple(line.get('dir',(1.,0.)))!=(1.,0.)]
    horizontal=[line for line in lines if tuple(line.get('dir',(1.,0.)))==(1.,0.)]
    # Rotated navigation in the outside margin can make MuPDF join financial
    # rows. Exclude only these known labels, wholly beyond every horizontal
    # text box. Never crop overlapping or rotated financial table headings.
    labels={'financial statements','statutory reports','corporate overview','consolidated financial statements'}
    def navigation_label(line):
        label=' '.join(''.join(s.get('text','') for s in line['spans']).lower().split())
        return label in labels or bool(re.fullmatch(r'[a-z .&]+\s*\|\s*annual report 20\d{2}[-–]\d{2}',label))
    if rotated and horizontal and all(navigation_label(line) for line in rotated):
        edge=min(line['bbox'][0] for line in rotated)
        # A one- or two-glyph decorative icon (no digits) in the outer margin is
        # not table text; it must not keep the navigation rail inside the crop.
        def margin_icon(line):
            text=''.join(s.get('text','') for s in line['spans']).strip()
            return len(text)<=2 and not re.search(r'\d',text) and line['bbox'][0]>=page.rect.width*.9
        table=[line for line in horizontal if not margin_icon(line)] or horizontal
        if edge>max(line['bbox'][2] for line in table) and edge>page.rect.width*.9:
            import pymupdf
            bounds=pymupdf.Rect(clip or page.rect);bounds.x1=min(bounds.x1,edge-.1)
            kwargs={'clip':bounds}
            text=page.get_text(sort=True,**kwargs)
        else:
            edge=max(line['bbox'][2] for line in rotated)
            # A page folio can sit to the left of a vertical navigation rail.
            # It is not a financial column: only a short integer outside the
            # rail's vertical span, wholly in the margin, is excluded here.
            top=min(line['bbox'][1] for line in rotated);bottom=max(line['bbox'][3] for line in rotated)
            financial=[line for line in horizontal if not (
                re.fullmatch(r'\d{1,4}',''.join(s.get('text','') for s in line['spans']).strip())
                and line['bbox'][2]<edge+10 and (line['bbox'][3]<top or line['bbox'][1]>bottom))]
            if financial and edge<min(line['bbox'][0] for line in financial) and edge<page.rect.width*.1:
                import pymupdf
                bounds=pymupdf.Rect(clip or page.rect);bounds.x0=max(bounds.x0,edge+.1)
                text=page.get_text(sort=True,clip=bounds)
    for block in blocks:
        for line in block.get('lines',[]):
            spans=line['spans'];joined=''.join(span.get('text','') for span in spans)
            if not re.search(r'\b(?:(?:lakh|crore|million|thousand)s?|cr)\b',joined,re.I):continue
            for span in spans:
                glyph=span.get('text','').strip()
                # Glyphs confirmed by rendering to print ₹ in ITFRupee (R: MMTC FY2026 p131).
                if span.get('font')=='ITFRupee' and glyph in ('H','C','I','D','J','R'):
                    text=re.sub(r'(?<!\w)'+glyph+r'\s+(?=\(?(?:in\s+)?(?:(?:lakh|crore|million|thousand)s?|cr)\b)','₹ ',text,flags=re.I)
    return text

def complete_candidate(candidates):
    """Use one self-contained proof that agrees with every partial match."""
    for candidate in candidates:
        values=candidate[0]['values']
        if all(all(key in values and values[key]==value for key,value in other['values'].items()) for other,_ in candidates):return candidate
    raise ValueError('Annual report has conflicting or complementary matching balance sheets')

def audited_section_basis(text):
    """Read an explicit description of the statements audited, never infer basis."""
    compact=' '.join(text.split())[:6000]
    matches=list(re.finditer(r'report on the audit of (?:the )?(standalone|consolidated) (?:Ind[- ]AS )?financial statements',compact,re.I))
    # An auditor's annexure can name the same audited statements in its opening
    # paragraph rather than its title. Require the explicit own-audit clause,
    # not a passing mention of standalone/consolidated elsewhere in the report.
    if re.search(r'independent auditor(?:s[’\x27]?|[’\x27]s)? report',compact[:600],re.I) and re.search(r'we have audited',compact[:2000],re.I):
        matches+=list(re.finditer(r'in conjunction with our audit of the (standalone|consolidated) financial statements of the Company for the year ended on that date',compact,re.I))
    if re.search(r'auditor[’\x27]s responsibilit(?:y|ies) for the audit of the financial statements',compact,re.I):
        matches+=list(re.finditer(r'in connection with our audit of the (standalone|consolidated) financial statements',compact,re.I))
    # CARO annexure: "...not applicable in respect of audit of standalone financial statements of the Company".
    if re.search(r'clause 3\(xxi\) of the order',compact,re.I):
        matches+=list(re.finditer(r'in respect of (?:the )?audit of (?:the )?(standalone|consolidated) financial statements of the company',compact,re.I))
    if matches and len({m[1].lower() for m in matches})==1:
        return matches[0][1].lower(),matches[0][0]
    return None

SOLE_ENTITY_CLAUSE=(r'company (?:do(?:es)?n[’\']t|did ?n[’\']t|(?:did|does) not) have any subsidiar(?:y|ies) and consolidated financial statements|'
                    r'company (?:(?:did|does) not have any|has no|(?:did|does) not hold any investments? in any) subsidiar(?:y|ies)\s*(?:,|or|and)\s*(?:any )?(?:associates?|joint ventures?)(?: companies| company)?'
                    r'\s*(?:,|or|and)\s*(?:or |and )?(?:any )?(?:associates?|joint ventures?)')

def sole_entity_basis(pages):
    """Standalone basis for a company that prepares only one set of statements.

    Ind AS 110/28 require consolidated statements whenever a company has a
    subsidiary, associate or joint venture, and listed companies then label both
    sets. A company with none prints a single, unlabelled set. Accept that set as
    standalone only when the auditor's report is titled for unqualified
    "financial statements", no consolidated or standalone audit report or
    consolidated statement heading appears anywhere, and the auditor's own
    report states the company has no subsidiary, associate or joint venture.
    Returns (clause page index, printed clause) or None."""
    # Some text layers use control characters (e.g. \x03) as spaces.
    compact=[' '.join(re.sub(r'[\x00-\x1f]',' ',unicodedata.normalize('NFKC',p)).lower().split()) for p in pages]
    auditor_title=r"(?:report on (?:the )?audit of (?:the )?|independent auditors?['’]?s?['’]? report on (?:the )?)"
    if any(re.search(auditor_title+r'(?:standalone|consolidated)',c) or
           re.search(r'consolidated (?:balance sheet|statement of profit|statement of cash flows?)\b',c[:600]) for c in compact):return None
    # The title must be on the auditor's opinion page itself, not cited in a contents list.
    opinion=r'wehaveaudited(?:the)?(?:accompanying)?(?:ind-?as)?financialstatements'
    headings=[i for i,c in enumerate(compact) if re.search(auditor_title+r'(?:ind[- ]as )?financial statements',c[:6000]) and re.search(opinion,c.replace(' ',''))]
    for h in headings:
        for i in range(h,min(h+21,len(compact))):
            m=re.search(SOLE_ENTITY_CLAUSE,compact[i])
            if m:return i,m[0]
    return None

def verified_column_transcription(text,page,latest,clip=None):
    """Recover reading order only from two explicit dated PDF column headers."""
    try:statement_context(text,latest);return text,None
    except ValueError as error:
        if str(error)!='Annual column order not verified':return text,None
    bounds=clip or page.rect
    words=page.get_text('words',clip=clip) if clip is not None else page.get_text('words')
    date=datetime.strptime(latest['period'],'%b %Y');day=calendar.monthrange(date.year,date.month)[1]
    month='(?:'+date.strftime('%b')+'|'+date.strftime('%B')+')'
    pattern=re.compile(r'\b'+str(day)+r'(?:st|nd|rd|th)?\s+'+month+r'\s*,?\s*(20\d{2})\b',re.I)
    dates=[]
    for word in words:
        if not re.fullmatch(r'20\d{2}',word[4]) or word[0]<bounds.x0+bounds.width*.5 or word[1]>bounds.y0+bounds.height*.35:continue
        row=sorted([w for w in words if abs((w[1]+w[3])-(word[1]+word[3]))<6 and word[2]-115<w[0]<=word[0]],key=lambda w:w[0])
        raw=' '.join(w[4] for w in row);m=pattern.search(raw)
        if m and m[1]==word[4]:dates.append({'text':m[0],'year':int(m[1]),'bbox':list(word[:4]),'raw_words':[list(w[:5]) for w in row]})
    dates.sort(key=lambda d:d['bbox'][0])
    if len(dates)!=2 or [d['year'] for d in dates]!=[date.year,date.year-1]:return text,None
    if dates[1]['bbox'][0]-dates[0]['bbox'][0]<20 or abs(dates[1]['bbox'][1]-dates[0]['bbox'][1])>24:return text,None
    line='PDF column headers (left to right): '+dates[0]['text']+'    '+dates[1]['text']
    return line+'\n'+text,{'method':'printed_header_coordinates','headers':dates,'transcription':line}

def verified_current_nci(text,page,latest,clip=None):
    """Read a printed current NCI cell with a blank comparative, never fill it.

    This narrowly handles right-aligned dated columns. Both complete annual
    dates must already pass; the amount must align with the current header and
    the comparative cell must contain no text. The blank remains unvalued.
    """
    try:statement_context(text,latest)
    except ValueError:return None
    bounds=clip or page.rect;words=page.get_text('words',clip=clip) if clip else page.get_text('words')
    year=datetime.strptime(latest['period'],'%b %Y').year
    headers=[w for w in words if w[4] in (str(year),str(year-1)) and w[0]>bounds.x0+bounds.width*.5 and w[1]<bounds.y0+bounds.height*.35]
    headers.sort(key=lambda w:w[0])
    if len(headers)!=2 or [w[4] for w in headers]!=[str(year),str(year-1)] or abs(headers[0][1]-headers[1][1])>6:return None
    xcurrent,xprior=headers[0][2],headers[1][2]
    if xprior-xcurrent<30:return None
    rows=[]
    for word in words:
        if word[1]<=headers[0][3] or not re.fullmatch(r'Non[-–]?|Non-controlling',word[4],re.I):continue
        row=sorted([w for w in words if abs((w[1]+w[3])-(word[1]+word[3]))<4],key=lambda w:w[0])
        label=' '.join(w[4] for w in row if w[2]<xcurrent-30)
        if caption(label) not in ('non controlling interest','non controlling interests','non-controlling interest','non-controlling interests'):continue
        cells=[w for w in row if w[0]>=xcurrent-30]
        if len(cells)!=1 or not re.fullmatch(r'\(?-?\d[\d,]*\.\d+\)?',cells[0][4]) or abs(cells[0][2]-xcurrent)>3:continue
        value=amount(cells[0][4])
        if value is None:continue
        rows.append({'printed_value':value,'row_text':' '.join(w[4] for w in row),'cell_bbox':list(cells[0][:4]),'column_header_words':[list(w[:5]) for w in headers],'comparative_cell':'blank; no value inferred','method':'printed_current_nci_cell_coordinates'})
    return rows[0] if len(rows)==1 else None

def pdf_worker(pdf_path,context_path,output_path):
    import pymupdf
    context=json.loads(Path(context_path).read_text());latest=context['latest'];doc=pymupdf.open(pdf_path)
    if len(doc)>1200:raise ValueError('Annual report exceeds page limit')
    if context.get('require_pdf_isin') and not any(context['isin'] in re.sub(r'\s+','',p.get_text()) for p in doc):
        Path(output_path).write_text(json.dumps({'status':'unresolved','reason':'Company PDF does not contain the expected ISIN'}));return
    candidates=[];issues=[];diagnostics=[];basis_context=None;lease_notes=[];ofl_notes=[]
    plain_pages=[p.get_text() for p in doc]
    sole=sole_entity_basis(plain_pages) if latest.get('statement_type')=='standalone' else None
    # Any printed lease-liability or lease-payment amount anywhere rules out a nil lease identity.
    lease_amount_pages=[j+1 for j,plain in enumerate(plain_pages) if re.search(r'(?:lease\s+liabilit(?:y|ies)|lease\s+obligations?|payments?\s+of\s+lease)[^\n]{0,80}\d',' '.join(plain.split()),re.I)]
    if latest.get('total_lease_liabilities') is None:
        for j,plain in enumerate(plain_pages):
            if re.search(r'capital\s+management',plain,re.I):
                page=doc[j];clips=[None]
                if page.rect.width>page.rect.height*1.15:
                    w,h=page.rect.width,page.rect.height;clips.extend([pymupdf.Rect(0,0,w/2,h),pymupdf.Rect(w/2,0,w,h)])
                for clip in clips:
                    try:
                        capital=parse_capital_note(balance_text(page,clip),latest);capital['pages']=[j+1]
                        candidates.append((capital,[j]))
                    except ValueError:pass
            if not re.search(LEASE_ROLLFORWARD,' '.join(plain.split()),re.I) and not re.search(r'lease liabilit(?:y|ies)\s*\(refer note|total\s+lease\s+liabilities|carrying amount of lease liability',plain,re.I) and not (re.search(r'lease liabilit',plain,re.I) and re.search(r'balance as at|closing balance|reconciliation of movement of liabilities|other financial liabilities',plain,re.I)):continue
            page=doc[j];clips=[None]
            if page.rect.width>page.rect.height*1.15:
                w,h=page.rect.width,page.rect.height;clips.extend([pymupdf.Rect(0,0,w/2,h),pymupdf.Rect(w/2,0,w,h)])
            for clip in clips:
                text=balance_text(page,clip)
                # In a proven single-entity report every note belongs to the standalone statements.
                if sole and not re.search(r'\b(?:standalone|consolidated)\b',' '.join(text.lower().split())[:1800]):text='standalone\n'+text
                if re.search(r'Disclosures for leasing arrangements',text,re.I):
                    active=active_financial_navigation(page,clip)
                    if active:
                        try:
                            note=parse_numbered_lease_disclosure(active[0],latest);note['basis_evidence']={**active[1],'page':j+1};note['pages']=[j+1];lease_notes.append((note,j))
                        except ValueError:pass
                try:ofl_notes.append((parse_numbered_ofl_note(text),j))
                except ValueError:pass
                for parser in (parse_lease_note,parse_classified_lease_note,parse_dated_lease_rollforward,parse_financing_lease_reconciliation,parse_unclassified_lease_note,parse_lease_rollforward):
                    try:
                        note=parser(text,latest);note['pages']=[j+1];lease_notes.append((note,j))
                    except ValueError:pass
    for i,page in enumerate(doc):
        plain=plain_pages[i];low=balance_heading_text(' '.join(plain.lower().split())).lower()
        audit=audited_section_basis(plain)
        if audit:basis_context=(audit[0],i,audit[1])
        elif sole and i==sole[0]:basis_context=('standalone',i,sole[1])
        elif re.search(r'report on the audit of (?:the )?(?:standalone|consolidated)',low):basis_context=None
        # Cheap text pass; expensive layout text only for actual statement pages.
        if not re.search(r'\bbalance\s+sheet\b',low) or not ('cash and cash equivalents' in low or 'equity and liabilities' in low):continue
        clips=[None]
        # Printed two-page spreads must not interleave balance and income columns.
        if page.rect.width>page.rect.height*1.15:
            w,h=page.rect.width,page.rect.height
            clips.extend([pymupdf.Rect(0,0,w/2,h),pymupdf.Rect(w/2,0,w,h)])
        for clip in clips:
            selected=[i];text=balance_text(page,clip);compact=balance_heading_text(' '.join(text.lower().split())).lower()
            # An auditor-derived basis must also apply when checking a continuation
            # page; an explicit contrary basis on either page still fails.
            inherited=basis_context[0]+'\n' if not re.search(r'\b(?:standalone|consolidated)\b',compact[:1800]) and basis_context and 0<i-basis_context[1]<=BASIS_CONTEXT_PAGES else ''
            if not re.search(r'\bbalance\s+sheet\b',compact[:1000]):continue
            # A landscape sheet can contain two consecutive printed balance
            # pages, rather than a balance sheet beside an income statement.
            joined_spread=False
            if clip is not None and clip.x0==0:
                right=balance_text(page,pymupdf.Rect(page.rect.width/2,0,page.rect.width,page.rect.height))
                right_head=' '.join(right.lower().split())[:700]
                continued=re.search(r'balance\s+sheet\s*\(?cont',right_head)
                right_captions={caption(line) for line in right.splitlines()}
                repeated_header=(re.search(r'balance\s+sheet',right_head) and 'current liabilities' in right_captions and re.search(r'total\s+(?:equity and liabilities|liabilities and equity)',right,re.I) and not {'assets','non-current assets','current assets'} & right_captions)
                if (continued or repeated_header) and not re.search(r'total\s+(?:equity and liabilities|liabilities and equity)',compact):
                    # Both halves must state the same annual financial context.
                    try:
                        statement_context(inherited+text,latest);statement_context(inherited+right,latest)
                        text+='\n'+right;compact=' '.join(text.lower().split());joined_spread=True
                    except ValueError:pass
            cross_spread=False
            if i+1<len(doc) and not re.search(r'total equity and liabilities|total liabilities and equity',compact):
                next_clip=clip
                # Printed right-hand pages continue on the next spread's left.
                if clip is not None and clip.x0>0 and doc[i+1].rect.width>doc[i+1].rect.height*1.15:
                    nxt=doc[i+1];next_clip=pymupdf.Rect(0,0,nxt.rect.width/2,nxt.rect.height)
                    following=balance_text(nxt,next_clip)
                    try:
                        statement_context(inherited+text,latest);statement_context(inherited+following,latest)
                        if not re.search(r'\bbalance\s+sheet\b',' '.join(following.lower().split())[:700]):raise ValueError('No continuation heading')
                        text+='\n'+following;selected.append(i+1);cross_spread=True
                    except ValueError:pass
                else:
                    following=balance_text(doc[i+1],next_clip)
                    try:
                        statement_context(inherited+text,latest);statement_context(inherited+following,latest)
                        text+='\n'+following;selected.append(i+1)
                    except ValueError:pass
            contextual=False
            if not re.search(r'\b(?:standalone|consolidated)\b',compact[:1800]) and basis_context and 0<i-basis_context[1]<=BASIS_CONTEXT_PAGES:
                text=basis_context[0]+'\n'+text;contextual=True
            text,column_evidence=verified_column_transcription(text,page,latest,clip)
            try:
                income_needed=latest.get('finance_costs') is None and latest.get('profit_before_tax') is not None
                current_nci=verified_current_nci(text,page,latest,clip) if len(selected)==1 and not joined_spread else None
                evidence=parse_balance(text,latest,allow_existing=income_needed or bool(lease_notes or ofl_notes) or (latest.get("total_lease_liabilities") is None and not lease_amount_pages),allow_identity=len(selected)==1,current_nci=current_nci);evidence['pages']=[n+1 for n in selected]
                if current_nci:evidence['printed_cell_evidence']={'nci':{**current_nci,'page':i+1}}
                if column_evidence:evidence['column_order_evidence']={**column_evidence,'page':i+1}
                for identity in evidence.get('identity_evidence',{}).values():identity['pages']=list(evidence['pages'])
                matching_notes=[(note,j) for note,j in lease_notes if matching_unclassified_note(note,text,evidence['conversion_to_crore'])]
                if 'total_lease_liabilities' not in evidence['values'] and matching_notes and len({round(v['value'],6) for v,_ in matching_notes})==1 and sum(v for v in evidence['components'].values() if v is not None)<=matching_notes[0][0]['value']+.015:
                    note,j=matching_notes[0];evidence['values']['total_lease_liabilities']=note['value'];evidence['value']=note['value'];evidence['lease_note_evidence']=note
                    if j not in selected:selected.append(j);evidence['pages'].append(j+1)
                no_lease=evidence.get('identity_evidence',{}).get('lease_absent')
                if 'total_lease_liabilities' not in evidence['values'] and no_lease and not lease_notes and not ofl_notes and not lease_amount_pages and not any(v is not None for v in evidence['components'].values()):
                    evidence['values']['total_lease_liabilities']=0.;evidence['value']=0.;evidence['lease_absent_evidence']={**no_lease,'report_wide_lease_amounts':'none printed'}
                if 'total_lease_liabilities' not in evidence['values'] and not any(v is not None for v in evidence['components'].values()):
                    bound=[(b,j) for b,j in ((bound_ofl_lease(note,text,evidence['conversion_to_crore']),j) for note,j in ofl_notes) if b]
                    if len(bound)==1 or (bound and len({round(b['value'],6) for b,_ in bound})==1):
                        note,j=bound[0];evidence['values']['total_lease_liabilities']=note['value'];evidence['value']=note['value'];evidence['lease_note_evidence']=note
                        if j not in selected:selected.append(j);evidence['pages'].append(j+1)
                if income_needed:
                    incomes=[];income_issues=[]
                    for j in range(i,min(i+3,len(doc))):
                        income_page=doc[j];income_clips=[None]
                        if income_page.rect.width>income_page.rect.height*1.15:
                            w,h=income_page.rect.width,income_page.rect.height
                            income_clips.extend([pymupdf.Rect(0,0,w/2,h),pymupdf.Rect(w/2,0,w,h)])
                        for income_clip in income_clips:
                            income_text=balance_text(income_page,income_clip)
                            if not re.search(r'statement\s+of\s+(?:(?:standalone|consolidated)\s+)?profit',' '.join(unicodedata.normalize('NFKC',income_text).lower().split())[:1000]):continue
                            # The auditor-derived basis covering the balance sheet covers the adjacent P&L too.
                            if inherited and not re.search(r'\b(?:standalone|consolidated)\b',' '.join(income_text.lower().split())[:1800]):income_text=inherited+income_text
                            try:
                                income=parse_income(income_text,latest)
                                income['pages']=[j+1];incomes.append((income,j))
                            except ValueError as error:income_issues.append({'page':j+1,'reason':str(error)})
                    evidence['income_diagnostics']=income_issues
                    if incomes and len({round(v['value'],6) for v,_ in incomes})==1:
                        income,j=incomes[0];evidence['values']['finance_costs']=income['value'];evidence['income_evidence']=income
                        if j not in selected:selected.append(j);evidence['pages'].append(j+1)
                if not evidence.get('borrowings_reconciliation') and not any(latest.get(k) is None for k in evidence['values']):
                    reasons='; '.join(dict.fromkeys(x['reason'] for x in evidence.get('income_diagnostics',[])))
                    raise ValueError('No missing annual input safely recovered'+(': '+reasons if reasons else ''))
                if contextual:
                    evidence['basis_evidence_page']=basis_context[1]+1
                    evidence['basis_evidence']=basis_context[2]
                    if sole and basis_context[1]==sole[0]:evidence['basis_method']='sole_entity_no_subsidiary'
                    selected.append(basis_context[1])
                evidence['layout']='right_then_left' if cross_spread else 'joined_balance_spread' if joined_spread else 'full_page' if clip is None else 'left_half' if clip.x0==0 else 'right_half'
                candidates.append((evidence,selected))
            except ValueError as error:
                issues.append(str(error));diagnostics.append({'page':i+1,'reason':str(error),'layout':'full_page' if clip is None else 'half_page'})
    # On a landscape two-page spread the full-page text interleaves both pages; where a
    # half-page reading of the same page succeeded, it is the faithful one.
    halves={c[0]['pages'][0] for c in candidates if c[0].get('layout') in ('left_half','right_half','joined_balance_spread','right_then_left')}
    candidates=[c for c in candidates if not (c[0].get('layout')=='full_page' and c[0]['pages'][0] in halves)]
    if not candidates:result={'status':'unresolved','reason':'; '.join(dict.fromkeys(issues))[:400] or 'No supported balance sheet with explicit lease amounts'}
    else:
        try:
            evidence,selected=complete_candidate(candidates);excerpt=pymupdf.open()
            # Evidence needs the printed page content, not interactive forms.
            # Some public reports contain cyclic widget trees that make grafting
            # fail even though their financial table text parses correctly.
            for n in selected:excerpt.insert_pdf(doc,from_page=n,to_page=n,widgets=False)
            evidence_path=Path(output_path).with_suffix('.pdf');excerpt.save(evidence_path,garbage=4,deflate=True,no_new_id=True)
            result={'status':'verified','evidence':evidence,'excerpt':str(evidence_path)}
        except ValueError as error:result={'status':'unresolved','reason':str(error)}
    result['diagnostics']=diagnostics[:60]
    Path(output_path).write_text(json.dumps(result))

def save_excerpt(folder,source,raw_hash):
    excerpt_hash=hashlib.sha256(Path(source).read_bytes()).hexdigest()
    target=folder/(raw_hash[:16]+'-'+excerpt_hash[:16]+'.pdf')
    if not target.exists():shutil.copy2(source,target)
    if hashlib.sha256(target.read_bytes()).hexdigest()!=excerpt_hash:raise ValueError('Saved evidence hash mismatch')
    return target,excerpt_hash

def known_archive_duplicate(row,previous,seen_hashes):
    """Reuse today's exact archive/hash observation only after a fresh copy matched.

    Company URLs can change in place, so they are never deduplicated this way.
    This avoids downloading the same NSE/BSE filing twice without changing the
    evidence used for recovered fields or increasing the provider request rate.
    """
    from .report_sources import fresh
    if row.get('provider') not in ('NSE','BSE') or not fresh(previous):return None
    if previous.get('revision')!=REVISION or previous.get('rule_revision')!=RULE_REVISION or previous.get('status')!='verified':return None
    for attempt in previous.get('attempts',[]):
        if attempt.get('source_url')==row['fileName'] and attempt.get('provider')==row['provider'] and (attempt.get('raw_sha256'),REVISION,RULE_REVISION) in seen_hashes:
            return {'raw_sha256':attempt['raw_sha256'],'previous_observed_at':previous['observed_at']}
    return None

class AnnualReportSources:
    def fetch(self,symbol,isin,details):
        from .financial_sources import request
        from .fallbacks import Fallbacks
        from .report_sources import bse_reports,company_reports,public_request,fresh,COMPANY_PAGES,download_report
        latest=details.get('latest') or {}
        income_needed=latest.get('finance_costs') is None and latest.get('profit_before_tax') is not None
        folder=ROOT/'data/annual-reports'/symbol;folder.mkdir(parents=True,exist_ok=True)
        key=hashlib.sha256(json.dumps(latest,sort_keys=True).encode()).hexdigest()[:16]
        cache=folder/(key+'.json');old={}
        if cache.exists():
            old=json.loads(cache.read_text())
            if old.get('revision')==REVISION and old.get('rule_revision')==RULE_REVISION and (not income_needed or old.get('income_revision')==INCOME_REVISION) and old.get('symbol')==symbol and old.get('isin')==isin and fresh(old):
                evidence=ROOT/old.get('evidence_path','missing')
                if old.get('status')!='verified' or (evidence.is_file() and hashlib.sha256(evidence.read_bytes()).hexdigest()==old.get('excerpt_sha256')):return old
        result={'revision':REVISION,'rule_revision':RULE_REVISION,'income_revision':INCOME_REVISION,'symbol':symbol,'isin':isin,'observed_at':now(),'status':'unresolved','source_checks':[],'attempts':[]}
        client=Fallbacks();year=None;seen_hashes=set();seen_parses={}
        try:year=datetime.strptime(latest.get('period',''),'%b %Y').year
        except ValueError:pass
        def discover(provider):
            check={'provider':provider};result['source_checks'].append(check)
            try:
                if provider=='BSE':choices=bse_reports(client.session,isin,year,folder)
                elif provider=='Company website':choices=company_reports(client.session,isin,year,folder)
                else:
                    path=folder/'index.json';index=json.loads(path.read_text()) if path.exists() else {}
                    if index.get('symbol')!=symbol or not fresh(index):
                        response=request(client.session,'https://www.nseindia.com/api/annual-reports',params={'index':'equities','symbol':symbol})
                        try:index={'symbol':symbol,'observed_at':now(),'payload':response.json()}
                        finally:response.close()
                        atomic_json(path,index)
                    choices=[]
                    for row in index.get('payload',{}).get('data',[]):
                        url=row.get('fileName','');u=urlsplit(url)
                        if year and str(row.get('toYr'))!=str(year):continue
                        if u.scheme!='https' or u.hostname!='nsearchives.nseindia.com' or not u.path.startswith('/annual_reports/') or not u.path.lower().endswith('.pdf') or '_'+symbol+'_' not in u.path:continue
                        choices.append({**row,'provider':'NSE'})
                        if len(choices)>=2:break
                check.update(status='available' if choices else 'unavailable',reports=len(choices))
                return choices
            except Exception as error:
                check.update(status='unavailable',reason=str(error)[:220] if isinstance(error,ValueError) else type(error).__name__)
                return []
        def parse(row):
            url=row['fileName'];attempt={'provider':row['provider'],'source_url':url};result['attempts'].append(attempt)
            if (old.get('symbol'),old.get('isin'))==(symbol,isin):
                duplicate=known_archive_duplicate(row,old,seen_hashes)
                if duplicate:
                    attempt.update(duplicate,status='unresolved',reason='Identical archived report already parsed; reused earlier SHA-256 check')
                    return False
            try:
                with _pdf_slots:
                    if shutil.disk_usage(ROOT).free<1_000_000_000:raise ValueError('Less than 1 GB free; PDF fetch deferred')
                    with tempfile.TemporaryDirectory(prefix='report-',dir=ROOT/'data/annual-reports') as tmp:
                        tmp=Path(tmp);pdf=tmp/'report.pdf'
                        raw_hash,size=download_report(request if row['provider']=='NSE' else public_request,client.session,url,pdf)
                        attempt['raw_sha256']=raw_hash
                        parse_key=(raw_hash,REVISION,RULE_REVISION)
                        attempt['parse_key']=list(parse_key)
                        # Reuse only a parse actually performed in this fetch.
                        # Preserve its validation failure and page diagnostics,
                        # rather than reporting duplicate content as a blocker.
                        if parse_key in seen_parses:
                            attempt.update(copy.deepcopy(seen_parses[parse_key]),reused_current_parse=True)
                            return False
                        seen_hashes.add(parse_key)
                        context=tmp/'context.json';context.write_text(json.dumps({'latest':latest,'symbol':symbol,'isin':isin,'require_pdf_isin':row.get('require_pdf_isin',False)}))
                        output=tmp/'result.json'
                        subprocess.run([sys.executable,'-m','marketdesk.annual_reports',str(pdf),str(context),str(output)],cwd=ROOT,timeout=90,check=True,capture_output=True)
                        parsed=json.loads(output.read_text());attempt.update({k:v for k,v in parsed.items() if k not in ('excerpt','evidence')})
                        if parsed['status']!='verified':seen_parses[parse_key]={k:v for k,v in parsed.items() if k not in ('excerpt','evidence')}
                        if parsed['status']=='verified':
                            target,excerpt_hash=save_excerpt(folder,parsed['excerpt'],raw_hash)
                            result.update({k:v for k,v in parsed.items() if k!='excerpt'},source_url=url,provider=row['provider'],
                                index_url=row.get('index_url'),identity=row.get('identity'),raw_sha256=raw_hash,download_bytes=size,
                                evidence_path=str(target.relative_to(ROOT)),excerpt_sha256=excerpt_hash)
                            return True
            except Exception as error:attempt.update(status='unresolved',reason=str(error)[:250] if isinstance(error,ValueError) else type(error).__name__)
            return False
        providers=['NSE']+(['Company website'] if isin in COMPANY_PAGES else [])+['BSE']
        for provider in providers:
            choices=discover(provider)
            # Discover alternate sources even for records lacking a safe baseline,
            # but don't merge figures whose annual period or basis is unknown.
            if not year or not latest.get('statement_type'):continue
            if all(latest.get(k) is not None for k in ('total_lease_liabilities','total_borrowings','cash_and_cash_equivalents','finance_costs')):continue
            if any(parse(row) for row in choices):break
        if result['status']!='verified':
            if not year or not latest.get('statement_type'):reason='No verified baseline annual period/basis; alternate report sources checked'
            elif all(latest.get(k) is not None for k in ('total_lease_liabilities','total_borrowings','cash_and_cash_equivalents','finance_costs')):reason='Supported annual inputs present; other fields remain missing'
            elif result['attempts']:reason='; '.join(dict.fromkeys(a.get('reason','Unsupported report') for a in result['attempts']))
            else:reason='Matching annual-report PDF unavailable from checked sources'
            result['reason']=reason[:650]
        atomic_json(cache,result);return result

def apply_report(details,report):
    previous=details.get('annual_report_check') or {}
    if report.get('status')!='verified':
        details['annual_report_attempt']=report
        if previous.get('status')!='verified':details['annual_report_check']=report
        return details
    evidence=report.get('evidence',{});latest=details.get('latest') or {}
    if any(latest.get(k)!=evidence.get(k) for k in ('period','statement_type','units_in')):return details
    if len(set(evidence.get('matched_fields',[])))<2:return details
    if previous.get('status')=='verified' and previous.get('excerpt_sha256')!=report.get('excerpt_sha256'):
        history=details.setdefault('annual_report_history',[])
        if not any(r.get('excerpt_sha256')==previous.get('excerpt_sha256') for r in history):history.append(copy.deepcopy(previous))
    details['annual_report_check']=report
    latest=copy.deepcopy(latest);filled=[]
    values=evidence.get('values',{'total_lease_liabilities':evidence.get('value')})
    for key,value in values.items():
        if key not in ('total_lease_liabilities','total_borrowings','cash_and_cash_equivalents','finance_costs'):continue
        if not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:continue
        if key=='finance_costs':
            anchor=evidence.get('income_evidence',{}).get('matched_profit_before_tax');pbt=latest.get('profit_before_tax')
            if anchor is None or pbt is None or abs(anchor-pbt)>max(.015,abs(pbt)*.00002):continue
            if evidence['income_evidence'].get('value')!=value:continue
        if latest.get(key) is not None:
            existing=latest[key]
            reconciliation=evidence.get('borrowings_reconciliation') if key=='total_borrowings' else None
            if reconciliation and existing==reconciliation.get('vendor_value') and value==reconciliation.get('audited_borrowings'):
                from .balance_identity import borrowings_reconciliation
                verified=borrowings_reconciliation(existing,value,evidence.get('values',{}).get('total_lease_liabilities'),evidence.get('lines',{}),evidence.get('conversion_to_crore',1))
                if verified:
                    details.setdefault('borrowings_reconciliation_log',[]).append({**verified,'source_url':report['source_url'],'pages':evidence.get('pages',[]),'observed_at':report['observed_at'],'previous_source':latest.get('sources',{}).get(key)})
                    retained=[]
                    for conflict in details.get('source_conflicts',[]):
                        if conflict.get('field')==key and conflict.get('selected_value')==existing and conflict.get('annual_report')==value:
                            details.setdefault('reconciled_source_conflicts',[]).append(conflict)
                        else:retained.append(conflict)
                    details['source_conflicts']=retained
                    latest[key]=None
            if latest.get(key) is not None:
                if abs(existing-value)>max(.015,abs(existing)*.00002):
                    conflict={'field':key,'selected_value':existing,'annual_report':value,'selected':details.get('selected_source','upstox'),
                              'period':latest['period'],'units':'crore','source_url':report['source_url']}
                    conflicts=details.setdefault('source_conflicts',[])
                    if conflict not in conflicts:conflicts.append(conflict)
                continue
        latest[key]=value;filled.append(key)
        latest.setdefault('sources',{})[key]={'source':'company_annual_report','source_url':report['source_url'],
           'observed_at':report['observed_at'],'raw_sha256':report['raw_sha256'],'evidence_path':report['evidence_path'],**evidence,'value':value}
        if key=='finance_costs':latest['sources'][key].update(evidence['income_evidence'])
        if key=='total_lease_liabilities' and evidence.get('lease_note_evidence'):latest['sources'][key].update(evidence['lease_note_evidence'])
    latest['annual_report_anchor_evidence']={'source_url':report['source_url'],'pages':evidence.get('pages',[]),'equity':evidence.get('balance_equity'),'matched_fields':evidence.get('matched_fields',[]),'identity_evidence':evidence.get('identity_evidence',{}),'evidence_path':report['evidence_path']}
    latest['missing_fields']=[k for k in latest.get('missing_fields',[]) if k not in filled];details['latest']=latest
    details['missing_fields']=[k for k in details.get('missing_fields',[]) if k not in filled]
    from .provider import FUNDAMENTAL_REQUIRED_FIELDS
    from .bank_inputs import not_applicable
    na=not_applicable(details)
    details['status']='COMPLETE' if details.get('sector') and all(latest.get(k) is not None or k in na for k in FUNDAMENTAL_REQUIRED_FIELDS) else 'PARTIAL'
    return details

def retain_report(details,previous,symbol,isin):
    """Keep verified supplements across raw imports, without refreshing their age.

    A changed financial context or changed anchors requires a fresh source check.
    The saved excerpt must still match its recorded SHA-256.
    """
    reports=previous.get('annual_report_history',[])+[previous.get('annual_report_check') or {}]
    for report in reports:details=_retain_one_report(details,previous,report,symbol,isin)
    return details

def _retain_one_report(details,previous,report,symbol,isin):
    if report.get('status')!='verified' or (report.get('symbol'),report.get('isin'))!=(symbol,isin):return details
    latest=details.get('latest') or {};old=previous.get('latest') or {};evidence=report.get('evidence') or {}
    if any(latest.get(k)!=evidence.get(k) for k in ('period','statement_type','units_in')):return details
    anchors=evidence.get('matched_fields',[])
    same=[k for k in set(anchors) if k in ('cash_and_cash_equivalents','equity','total_borrowings') and
          isinstance(latest.get(k),(int,float)) and isinstance(old.get(k),(int,float)) and
          abs(latest[k]-old[k])<=max(.015,abs(old[k])*.00002)]
    if len(same)<2:return details
    path=ROOT/report.get('evidence_path','missing')
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=report.get('excerpt_sha256'):return details
    # Preserve relevant unresolved conflicts too; an import is not reconciliation.
    for conflict in previous.get('source_conflicts',[]):
        conflicts=details.setdefault('source_conflicts',[])
        if conflict not in conflicts:conflicts.append(copy.deepcopy(conflict))
    return apply_report(details,copy.deepcopy(report))

if __name__=='__main__':pdf_worker(*sys.argv[1:])
