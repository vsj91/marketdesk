"""Bounded macOS idle-sleep protection; never changes global power settings."""
import math
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from .autotrading import IST


def remaining_seconds(clock):
    local = clock.astimezone(IST)
    start = local.replace(hour=9, minute=0, second=0, microsecond=0)
    end = local.replace(hour=15, minute=40, second=0, microsecond=0)
    if local.weekday() < 5 and start <= local < end:
        return math.ceil((end-local).total_seconds())
    return 0


class SleepGuard:
    def __init__(self):
        self.process = None
        self.thread = None
        self.stopped = threading.Event()
        self.state = {'status': 'starting', 'schedule': 'Monday–Friday 09:00–15:40 Asia/Kolkata',
                      'scope': 'Idle system sleep only; includes weekday exchange holidays'}

    def snapshot(self):
        return dict(self.state)

    def release(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.process = None
        self.state.pop('pid', None)

    def tick(self, clock=None):
        clock = clock or datetime.now(timezone.utc)
        seconds = remaining_seconds(clock)
        if sys.platform != 'darwin':
            self.state.update(status='unsupported', checked_at=clock.isoformat())
            return
        if not seconds:
            self.release()
            self.state.update(status='outside_hours', checked_at=clock.isoformat())
            return
        if self.process is None or self.process.poll() is not None:
            # Timeout bounds protection to this session; parent death releases it too.
            self.process = subprocess.Popen(
                ['/usr/bin/caffeinate', '-i', '-t', str(seconds), '-w', str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.state.pop('error', None)
        self.state.update(status='protecting', checked_at=clock.isoformat(), pid=self.process.pid)

    def run(self):
        try:
            while not self.stopped.is_set():
                try:
                    self.tick()
                except (OSError, subprocess.SubprocessError) as error:
                    self.state.update(status='error', error=type(error).__name__)
                self.stopped.wait(10)
        finally:
            self.release()
            self.state['status'] = 'stopped'

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run, name='market-hours-sleep-guard', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=5)
