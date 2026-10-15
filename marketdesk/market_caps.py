"""AMFI size labels, joined by ISIN. Display metadata only, never a trading signal."""
import collections
import hashlib
import io
import re
import threading
import zipfile
from datetime import date, datetime, timezone
from xml.etree import ElementTree as ET
from urllib.parse import urlsplit

import requests
from .storage import connect, get_state, set_state, number

PAGE='https://www.amfiindia.com/otherdata/categorisation-of-stocks'
KEY='amfi_classification'
NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
LABELS={'Large Cap':'Large cap','Mid Cap':'Mid cap','Small Cap':'Small cap'}


def workbook_records(raw, period):
    """Read values from AMFI's published workbook; no formula execution."""
    if len(raw)>10_000_000:raise ValueError('AMFI workbook too large')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sum(f.file_size for f in archive.infolist())>40_000_000:raise ValueError('Expanded workbook too large')
        strings=[]
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings=[''.join(t.text or '' for t in item.findall('.//s:t',NS)) for item in ET.fromstring(archive.read('xl/sharedStrings.xml'))]
        sheet=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        rows=[]
        for row in sheet.findall('.//s:sheetData/s:row',NS):
            values={}
            for cell in row.findall('s:c',NS):
                column=re.sub(r'\d','',cell.attrib['r']);value=cell.find('s:v',NS)
                text=value.text if value is not None else ''
                if cell.attrib.get('t')=='s':text=strings[int(text)]
                elif cell.attrib.get('t')=='inlineStr':text=''.join(t.text or '' for t in cell.findall('.//s:t',NS))
                values[column]=text
            rows.append(values)
    title=rows[0].get('A','') if rows else ''
    expected=date.fromisoformat(period).strftime('%d %B %Y').lstrip('0')
    if expected not in title or 'Average Market Capitalization' not in title:raise ValueError('AMFI period heading does not match')
    if len(rows)<3 or rows[1].get('C')!='ISIN' or rows[1].get('F')!='NSE Symbol':raise ValueError('Unexpected AMFI columns')
    records={};counts=collections.Counter();ranks=set()
    for row in rows[2:]:
        if not row.get('A'):continue
        isin=row.get('C','').strip();category=row.get('K','').strip();rank=int(row['A']);cap=number(row.get('J'))
        if not re.fullmatch(r'IN[A-Z0-9]{10}',isin) or isin in records or rank in ranks or category not in LABELS or cap is None or cap<0:
            raise ValueError('Invalid or duplicate AMFI record')
        expected_category='Large Cap' if rank<=100 else 'Mid Cap' if rank<=250 else 'Small Cap'
        if category!=expected_category:raise ValueError('AMFI category and rank disagree')
        records[isin]=dict(label=LABELS[category],rank=rank,average_market_cap_crore=cap,nse_symbol=row.get('F','').strip())
        counts[category]+=1;ranks.add(rank)
    if counts['Large Cap']!=100 or counts['Mid Cap']!=150 or len(records)<1000 or ranks!=set(range(1,len(records)+1)):
        raise ValueError('Incomplete AMFI classification')
    return records


def save_workbook(path,raw,source,period):
    if urlsplit(source).hostname not in ('www.amfiindia.com','portal.amfiindia.com'):raise ValueError('Official AMFI source required')
    records=workbook_records(raw,period)
    snapshot=dict(source=source,period=period,fetched_at=datetime.now(timezone.utc).isoformat(),sha256=hashlib.sha256(raw).hexdigest(),records=records)
    with connect(path) as db:
        previous=get_state(db,KEY,{})
        if previous.get('period','')>period:raise ValueError('Refusing older AMFI classification')
        set_state(db,KEY,snapshot)
    return len(records)


def refresh(path):
    try:
        headers={'User-Agent':'Mozilla/5.0'}
        response=requests.get(PAGE,headers=headers,timeout=(3,10));response.raise_for_status()
        choices=[]
        for url in set(re.findall(r'href="(https://[^"<>]+AverageMarketCapitalization[^"<>]+\.xlsx)"',response.text)):
            match=re.search(r'(\d{1,2}[A-Za-z]{3}\d{4})\.xlsx$',url)
            if match and urlsplit(url).hostname in ('www.amfiindia.com','portal.amfiindia.com'):
                period=datetime.strptime(match[1],'%d%b%Y').date()
                if period<=date.today():choices.append((period.isoformat(),url))
        if not choices:raise ValueError('No official AMFI workbook link found')
        period,url=max(choices)
        with connect(path) as db:previous=get_state(db,KEY,{})
        if previous.get('period')==period:return True
        response=requests.get(url,headers=headers,timeout=(3,15));response.raise_for_status()
        save_workbook(path,response.content,url,period)
        return True
    except (requests.RequestException,ValueError,KeyError,IndexError,zipfile.BadZipFile,ET.ParseError):return False


def classifications(path):
    with connect(path) as db:
        snapshot=get_state(db,KEY,{})
        instruments=[dict(r) for r in db.execute('SELECT symbol,isin,name FROM instruments')]
    records=snapshot.get('records',{});stocks={};counts=collections.Counter()
    for row in instruments:
        item=records.get(row['isin'],{});label=item.get('label','Unclassified')
        stocks[row['symbol']]=dict(label=label,rank=item.get('rank'),name=row['name'])
        counts[label]+=1
    return dict(source=snapshot.get('source'),period=snapshot.get('period'),fetched_at=snapshot.get('fetched_at'),
                stocks=stocks,counts=dict(counts),basis='Latest published AMFI classification; historical reports show this classification, not the category at trade time.')


class ClassificationRefresh:
    def __init__(self,path):self.path=path;self.stop_event=threading.Event();self.thread=None
    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def run(self):
        while not self.stop_event.is_set():
            refresh(self.path)
            self.stop_event.wait(86400)
    def stop(self):self.stop_event.set()


def decorate_html(body):
    if b'/stock-labels.js' in body:return body
    extra=b'<link rel="stylesheet" href="/stock-labels.css"><script src="/stock-labels.js" defer></script>'
    return body.replace(b'</head>',extra+b'</head>',1)
