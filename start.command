#!/bin/zsh
cd "${0:A:h}"
# Reuse the login service or an existing foreground server instead of starting a duplicate.
if /usr/bin/curl --silent --fail --max-time 2 http://127.0.0.1:8765/api/health >/dev/null; then
  open http://127.0.0.1:8765
  exit 0
fi
if /bin/launchctl print "gui/$(id -u)/com.vinayjain.marketdesk-local" >/dev/null 2>&1; then
  /bin/launchctl kickstart "gui/$(id -u)/com.vinayjain.marketdesk-local"
  for attempt in {1..40}; do
    if /usr/bin/curl --silent --fail --max-time 1 http://127.0.0.1:8765/api/health >/dev/null; then
      open http://127.0.0.1:8765
      exit 0
    fi
    sleep 1
  done
  print 'The login service is starting or needs attention. Check data/logs/server.err.log.'
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || exit 1
fi
if ! .venv/bin/python -c 'import pandas, requests, upstox_client, yfinance' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt || exit 1
fi
if [[ ! -f data/marketdesk.sqlite3 && -d data/imports ]]; then
  .venv/bin/python app.py import || exit 1
fi
open http://127.0.0.1:8765
exec .venv/bin/python app.py serve --live
