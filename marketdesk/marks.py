"""Display marks only. Trading quote freshness and sizing are unchanged."""
from datetime import datetime,time,timedelta,timezone
import math
IST=timezone(timedelta(hours=5,minutes=30))


def display_mark(quote,candle,clock):
    q=dict(quote) if quote else None;c=dict(candle) if candle else None
    last=q['price'] if q and math.isfinite(q['price']) and q['price']>0 else None
    stamp=datetime.fromtimestamp(q['trade_time']/1000,timezone.utc) if last is not None else None
    official=None;close_time=None;close_day=None
    if c and str(c.get('source','')).startswith('upstox_daily'):
        try:
            day=datetime.fromisoformat(c['ts']).astimezone(IST).date()
            end=datetime.combine(day,time(15,30),tzinfo=IST)
            observed=datetime.fromisoformat(c['observed_at'])
            if end<=clock and end<=observed<=clock and math.isfinite(c['close']) and c['close']>0:
                official=c['close'];close_time=end.isoformat();close_day=day.isoformat()
        except (ValueError,KeyError,TypeError):pass
    use_close=official is not None and (stamp is None or close_day>=stamp.astimezone(IST).date().isoformat())
    # Unverified or pre-close daily bars never acquire an official-close label.
    mark=official if use_close else last
    return dict(mark=mark,mark_time=close_time if use_close else stamp.isoformat() if stamp else None,
      mark_source='official_daily_close' if use_close else 'last_trade' if last is not None else 'missing',
      last_trade_mark=last,last_trade_time=stamp.isoformat() if stamp else None,
      official_close=official,official_close_session=close_day,official_close_observed_at=c.get('observed_at') if official is not None else None)
