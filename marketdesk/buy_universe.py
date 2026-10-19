"""Official Nifty 500 membership for new buys; never filters held-position exits."""
import csv
import hashlib
import io
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from .storage import connect, get_state, set_state

SOURCE = 'https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv'
KEY = 'nifty500_membership'
IST = timezone(timedelta(hours=5, minutes=30))


def validate_members(members):
    # Corporate actions can temporarily take the official list above 500.
    if not isinstance(members, dict) or not 500 <= len(members) <= 510:
        raise ValueError('Unexpected Nifty 500 constituent count')
    if any(not isinstance(s, str) or not re.fullmatch(r'[A-Z0-9&_.-]+', s) or
           not isinstance(i, str) or not re.fullmatch(r'IN[A-Z0-9]{10}', i)
           for s, i in members.items()) or len(set(members.values())) != len(members):
        raise ValueError('Invalid or duplicate Nifty 500 identity')


def parse_constituents(raw):
    if len(raw) > 1_000_000:raise ValueError('Constituent file too large')
    rows = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    if not {'Symbol', 'ISIN Code', 'Series'} <= set(rows.fieldnames or []):
        raise ValueError('Unexpected Nifty 500 CSV columns')
    members = {}
    for row in rows:
        symbol = (row.get('Symbol') or '').strip()
        isin = (row.get('ISIN Code') or '').strip()
        if symbol in members or (row.get('Series') or '').strip() not in {'EQ', 'BE', 'RR'}:
            raise ValueError('Duplicate constituent or unsupported series')
        # The official file can carry a temporary dummy corporate-action line.
        # It has no real ISIN and must never authorize a purchase.
        if symbol.startswith('DUMMY') and re.fullmatch(r'DUM[A-Z0-9]{9}', isin):continue
        members[symbol] = isin
    validate_members(members)
    return members


def membership(db, clock):
    snapshot = get_state(db, KEY, {})
    try:
        fetched = datetime.fromisoformat(snapshot['fetched_at'])
        if snapshot['source'] != SOURCE or fetched > clock or fetched.astimezone(IST).date() != clock.astimezone(IST).date():
            raise ValueError('Membership is not verified today')
        members = snapshot['members'];validate_members(members)
        return members
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('Nifty 500 membership unavailable or not verified today; new buys wait. Existing exits remain active.') from None


def allowed_symbols(db, portfolio, clock):
    choice = get_state(db, 'auto_config', {}).get(portfolio, {}).get('buy_universe', 'all')
    if choice == 'all':return None
    if choice != 'nifty500':raise ValueError('Unknown buy universe; new buys wait.')
    members = membership(db, clock)
    return {r['symbol'] for r in db.execute('SELECT symbol,isin FROM instruments')
            if r['symbol'] in members and members[r['symbol']] == r['isin']}


def require_buy_universe(db, portfolio, symbol, clock):
    allowed = allowed_symbols(db, portfolio, clock)
    if allowed is not None and symbol not in allowed:
        raise ValueError(symbol + ' is outside the verified Nifty 500 buy universe; new buys blocked.')


def universe_status(db, portfolio, clock=None):
    clock = clock or datetime.now(timezone.utc)
    choice = get_state(db, 'auto_config', {}).get(portfolio, {}).get('buy_universe', 'all')
    if choice == 'all':return dict(name='All database stocks', status='ok')
    snapshot = get_state(db, KEY, {})
    result = dict(name='Nifty 500', fetched_at=snapshot.get('fetched_at'), source=SOURCE)
    try:
        symbols = allowed_symbols(db, portfolio, clock)
        # Only Investor needs financial inputs; every other basket uses the price-data check.
        check = 'investor_ready' if portfolio == 'investor' else 'trader_ready'
        ready = sum(bool(json.loads(r['report']).get(check))
                    for r in db.execute('SELECT symbol,report FROM quality') if r['symbol'] in symbols)
        result.update(status='ok', constituents=len(snapshot['members']), matched=len(symbols), data_ready=ready,
                      source_rows=snapshot.get('source_rows',len(snapshot['members'])))
    except ValueError as error:result.update(status='waiting', message=str(error))
    return result


def refresh_membership(path):
    """Fetch outside ledger transactions; replace only a complete validated list."""
    try:
        with requests.get(SOURCE, headers={'User-Agent':'Mozilla/5.0', 'Referer':'https://www.niftyindices.com/',
                          'Cache-Control':'no-cache'}, timeout=(3, 8), stream=True) as response:
            response.raise_for_status()
            chunks=[];size=0;started=time.monotonic()
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 1_000_000 or time.monotonic()-started > 20:raise ValueError('Constituent download limit exceeded')
                chunks.append(chunk)
        raw=b''.join(chunks);members=parse_constituents(raw)
        snapshot=dict(source=SOURCE, fetched_at=datetime.now(timezone.utc).isoformat(),
                      sha256=hashlib.sha256(raw).hexdigest(), members=members,
                      source_rows=len(list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))))
        with connect(path) as db:
            set_state(db, KEY, snapshot)
            set_state(db, KEY+'_refresh', dict(status='ok', checked_at=snapshot['fetched_at']))
        from .working_universe import sync_instruments
        sync_instruments(path,fetch_missing=True)
        return True
    except (requests.RequestException, ValueError, UnicodeError) as error:
        with connect(path) as db:
            set_state(db, KEY+'_refresh', dict(status='unavailable', checked_at=datetime.now(timezone.utc).isoformat(), error=type(error).__name__))
        return False


class MembershipRefresh:
    """Daily verification, bounded retries in a worker so exits never wait on HTTP."""
    def __init__(self, path):self.path=path;self.worker=None;self.attempt=None

    def start_if_due(self):
        if self.worker and self.worker.is_alive():return
        if self.attempt is not None and time.monotonic()-self.attempt < 900:return
        with connect(self.path) as db:
            if not any(c.get('buy_universe') == 'nifty500' for c in get_state(db, 'auto_config', {}).values()):return
            try:membership(db, datetime.now(timezone.utc));return
            except ValueError:pass
        self.attempt=time.monotonic()
        self.worker=threading.Thread(target=refresh_membership, args=(self.path,), daemon=True)
        self.worker.start()
