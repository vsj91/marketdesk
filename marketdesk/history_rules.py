"""Candle-history tiers. Established stocks use the full 200-session windows; recent listings use the
sessions they have, once they pass a floor that still covers the 63-session rank, 50-day average and
volume windows. Nothing is interpolated or back-filled."""
FULL_SESSIONS=200
MIN_SESSIONS=80

def long_window(sessions):
    """Length of the 'long' moving average: 200 when available, otherwise every prior session."""
    return min(FULL_SESSIONS,sessions)

FINANCIAL_SECTORS=('bank','finance','nbfc','insurance','asset management','stock broking')

def is_financial(sector):
    """Banks, insurers, NBFCs, AMCs and brokers: borrowings and finance cost are their business, so the
    industrial debt/coverage inputs do not apply and they need a separate capital/asset-quality model."""
    return any(k in (sector or '').lower() for k in FINANCIAL_SECTORS)

def tier(sessions):
    return 'full' if sessions>=FULL_SESSIONS else 'short' if sessions>=MIN_SESSIONS else 'insufficient'

def break_start(rows,confirmed=()):
    """Index of the first session after the latest raw-price discontinuity (>25% gap or move), else 0.
    Exchange-confirmed ordinary moves are not breaks. Pre-break prices are never adjusted or reused."""
    start=0
    for i in range(1,len(rows)):
        a,b=rows[i-1],rows[i]
        if b['ts'][:10] in confirmed or not a['close']>0:continue
        if abs(b['open']/a['close']-1)>.25 or abs(b['close']/a['close']-1)>.25:start=i
    return start

def effective_rows(rows,confirmed=()):
    """Rows after the latest break once they alone meet MIN_SESSIONS; otherwise unchanged (and still blocked)."""
    rows=list(rows);start=break_start(rows,confirmed)
    return rows[start:] if start and len(rows)-start>=MIN_SESSIONS else rows
