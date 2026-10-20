"""Local macOS notifications for problems that stop automatic paper trading; nothing leaves this Mac."""
import collections
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from .autotrading import IST
from .feed import load_token
from .storage import connect, get_state, set_state

FEED_GRACE = timedelta(minutes=3)
FEED_RESTART_AFTER = timedelta(minutes=1)
FEED_RESTART_GAP = timedelta(minutes=5)
ENGINE_SILENT = timedelta(minutes=5)
RESTART_WINDOW = timedelta(minutes=15)
RESTART_LIMIT = 3


def market_hours(clock):
    local = clock.astimezone(IST)
    open_ = local.replace(hour=9, minute=15, second=0, microsecond=0)
    close = local.replace(hour=15, minute=30, second=0, microsecond=0)
    return local.weekday() < 5 and open_ <= local < close


def mac_notify(title, message):
    if sys.platform != 'darwin':
        return
    # Text goes in as arguments, never into the script, so quotes in messages are harmless.
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"\nend run'
    try:
        subprocess.run(['/usr/bin/osascript', '-e', script, title, message],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def parse(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class Alerts:
    """Each problem notifies once when it starts and once when it clears."""

    def __init__(self, path, feed=None, notify=mac_notify, clock=None):
        self.path = path
        self.feed = feed
        self.notify = notify
        self.active = {}
        self.recent = collections.deque(maxlen=20)
        self.thread = None
        self.stopped = threading.Event()
        self.started = clock or datetime.now(timezone.utc)
        self.feed_down_since = None
        self.feed_restarted = None
        with connect(path) as db:
            self.last_error = db.execute("SELECT COALESCE(MAX(id),0) FROM auto_events WHERE kind='error'").fetchone()[0]
            starts = [s for s in get_state(db, 'service_starts', []) if (parse(s) or self.started) > self.started-RESTART_WINDOW]
            starts.append(self.started.isoformat())
            set_state(db, 'service_starts', starts)
        if len(starts) >= RESTART_LIMIT:
            self.send('MarketDesk keeps restarting',
                      f'The service started {len(starts)} times in 15 minutes. Check data/logs/server.err.log.')

    def send(self, title, message, clock=None):
        self.recent.appendleft({'at': (clock or datetime.now(timezone.utc)).isoformat(), 'title': title, 'message': message})
        self.notify(title, message)

    def raise_(self, key, title, message, clock):
        if key not in self.active:
            self.active[key] = message
            self.send(title, message, clock)

    def clear(self, key, title, message, clock, quiet=False):
        if self.active.pop(key, None) is not None and not quiet:
            self.send(title, message, clock)

    def snapshot(self):
        return {'active': dict(self.active), 'recent': list(self.recent)}

    def tick(self, clock=None):
        clock = clock or datetime.now(timezone.utc)
        with connect(self.path) as db:
            errors = db.execute("SELECT id,portfolio,reason FROM auto_events WHERE kind='error' AND id>? ORDER BY id",
                                (self.last_error,)).fetchall()
            stream = get_state(db, 'stream', {})
            statuses = get_state(db, 'auto_status', {})
            refresh = get_state(db, 'refresh', {})
        self.check_errors(errors, clock)
        self.check_feed(stream, clock)
        self.check_engine(statuses, clock)
        self.check_refresh(refresh, clock)

    def check_errors(self, errors, clock):
        if not errors:
            return
        self.last_error = errors[-1]['id']
        names = sorted({r['portfolio'] for r in errors})
        shown = ', '.join(names[:4]) + (f' and {len(names)-4} more' if len(names) > 4 else '')
        self.send('Paper trading paused', f'{shown}: {errors[-1]["reason"]} Turn it back on in Paper portfolios.', clock)

    def check_feed(self, stream, clock):
        status = stream.get('status')
        if status == 'auth_failed':
            self.raise_('token', 'Upstox token rejected', 'Save a fresh token under Connection; live prices have stopped.', clock)
        else:
            self.clear('token', 'Upstox token accepted', 'Live prices are flowing again.', clock)
        if status == 'connected' or not market_hours(clock):
            self.feed_down_since = None
            self.clear('feed', 'Live prices back', 'The market feed reconnected.', clock, quiet=not market_hours(clock))
            return
        self.feed_down_since = self.feed_down_since or clock
        down = clock-self.feed_down_since
        # The feed gives up after its reconnect attempts; during market hours start it again rather than wait for you.
        if (status == 'stopped' and self.feed and down >= FEED_RESTART_AFTER and load_token()
                and (self.feed_restarted is None or clock-self.feed_restarted >= FEED_RESTART_GAP)):
            self.feed_restarted = clock
            try:
                self.feed.start()
            except Exception as error:
                print('Feed restart failed (' + type(error).__name__ + ').', file=sys.stderr, flush=True)
        if down >= FEED_GRACE and status != 'auth_failed':
            minutes = int(down.total_seconds() // 60)
            self.raise_('feed', 'Live prices down', f'The market feed has been {status or "off"} for {minutes} minutes; trading waits for prices.', clock)

    def check_engine(self, statuses, clock):
        seen = [parse(s.get('checked_at')) for s in statuses.values() if isinstance(s, dict)]
        seen = [s for s in seen if s]
        last = max(seen) if seen else None
        if clock-self.started < ENGINE_SILENT:
            return
        if last is None or clock-last >= ENGINE_SILENT:
            self.raise_('engine', 'Trading engine not responding', 'No portfolio check for over 5 minutes. Restart MarketDesk if this continues.', clock)
        else:
            self.clear('engine', 'Trading engine running', 'Portfolio checks have resumed.', clock)

    def check_refresh(self, refresh, clock):
        if refresh.get('status') == 'failed':
            detail = refresh.get('stage') or refresh.get('error') or 'see Data health'
            self.raise_('refresh', 'Data refresh failed', f'{detail}. Stocks with old prices cannot be bought; resume it in Data health.', clock)
        else:
            self.clear('refresh', 'Data refresh recovered', 'The data refresh is no longer failing.', clock)

    def run(self):
        while not self.stopped.is_set():
            try:
                self.tick()
            except Exception as error:
                print('Alert check failed (' + type(error).__name__ + ').', file=sys.stderr, flush=True)
            self.stopped.wait(30)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run, name='local-alerts', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
