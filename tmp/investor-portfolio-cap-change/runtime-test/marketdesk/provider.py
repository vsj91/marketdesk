"""Collect PlanSIP equity candles and auditable Upstox company fundamentals.

Install: python3 -m pip install pandas requests
Run with UPSTOX_TOKEN set and VALID_STOCK_INPUT.csv in the working directory.
Set FUNDAMENTALS_ONLY=1 to repair fundamentals without downloading candles.
INPUT_CSV, OUTPUT_CSV, LOCAL_INSTRUMENT_MASTER and UPSTOX_STATEMENT_TYPE
(consolidated by default; standalone is also supported) are environment options.

Detailed statements are requested with fs=true and are ANNUAL. Their monetary
values are in the response's units_in (normally INR crore). Missing inputs remain
null/blank; total liabilities are never used as a substitute for borrowings.
Raw responses, statement histories and extraction provenance are retained.

API references verified 2026-10-11:
https://upstox.com/developer/api-documentation/get-company-profile/
https://upstox.com/developer/api-documentation/get-income-statement/
https://upstox.com/developer/api-documentation/get-balance-sheet/
https://upstox.com/developer/api-documentation/get-cash-flow/
"""

import copy
import csv
import json
import math
import os
import re
import sys
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pandas as pd
import requests

# --- CONFIGURATION ---
UPSTOX_TOKEN = os.getenv("UPSTOX_TOKEN", "YOUR_UPSTOX_ACCESS_TOKEN_HERE").strip()
INPUT_CSV = os.getenv("INPUT_CSV", "VALID_STOCK_INPUT.csv")
OUTPUT_CSV = os.getenv("OUTPUT_CSV", "STOCK_DATA_SUMMARY.csv")
RAW_DATA_DIR = Path(os.getenv("RAW_DATA_DIR", "RAW_CANDLES"))
RAW_JSON_DIR = RAW_DATA_DIR / "json"
RAW_CSV_DIR = RAW_DATA_DIR / "csv"
FUNDAMENTAL_DIR = Path(os.getenv("FUNDAMENTAL_DIR", "RAW_FUNDAMENTALS"))
LOCAL_INSTRUMENT_MASTER = Path(os.getenv(
    "LOCAL_INSTRUMENT_MASTER", "data/instruments.json"
)).expanduser()
FUNDAMENTAL_STATEMENT_TYPE = os.getenv("UPSTOX_STATEMENT_TYPE", "consolidated").strip().lower()
FUNDAMENTALS_ONLY = os.getenv("FUNDAMENTALS_ONLY", "0").strip().lower() in {"1", "true", "yes"}

headers = {
    "Accept": "application/json",
    "Authorization": f"Bearer {UPSTOX_TOKEN}",
}

INSTRUMENT_CACHE = {}
UPSTOX_AUTH_ERROR = None
LOCAL_INSTRUMENT_LOOKUP = None
LAST_REQUEST_TS = 0.0
MIN_REQUEST_GAP_SECONDS = 0.35
REQUEST_HISTORY = defaultdict(deque)
HTTP_SESSION = requests.Session()
# Official standard limits are per API, per user: 50/sec, 500/min, 2,000/30min.
# The global gap controls bursts; this per-endpoint window also covers retries.
MAX_REQUESTS_PER_30_MINUTES = 1900


def safe_print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    try:
        print(*args, **kwargs)
    except BrokenPipeError:
        sys.stdout = open(os.devnull, "w")


def safe_symbol_file_name(symbol):
    text = str(symbol or "UNKNOWN").strip()
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_")
    return value or "UNKNOWN"


def log_upstox_status(symbol, stage, status_code, detail="", failed=False):
    """Log API HTTP outcomes in a stable, machine-readable form without noisy tracebacks."""
    status_code = str(status_code or "UNKNOWN")
    status_text = "OK" if status_code == "200" else "RATE_LIMITED" if status_code == "429" else "AUTH_ERROR" if status_code in {"401", "403"} else "ERROR"
    if failed and status_text == "OK":
        status_text = "API_ERROR"
    message = detail.strip() if detail else "no detail"
    safe_print(f"HTTP_STATUS | symbol={symbol} | stage={stage} | status={status_code} | result={status_text} | detail={message[:180]}")


def throttled_requests_get(url, headers=None, timeout=30, params=None):
    """Pace all GETs, including retries, with per-API rolling-window accounting."""
    global LAST_REQUEST_TS
    path = urlsplit(url).path
    bucket = path.rsplit("/", 1)[-1] if "/fundamentals/" in path else (
        "historical-candle" if "/historical-candle/" in path else path
    )
    history = REQUEST_HISTORY[bucket]
    now = time.monotonic()
    while history and history[0] <= now - 1800:
        history.popleft()
    while len(history) >= MAX_REQUESTS_PER_30_MINUTES:
        delay = max(0.0, 1800 - (now - history[0])) + 0.1
        safe_print(f"RATE_LIMIT | {bucket} rolling window | waiting {delay:.0f}s")
        time.sleep(min(delay, 60))
        now = time.monotonic()
        while history and history[0] <= now - 1800:
            history.popleft()
    elapsed = now - LAST_REQUEST_TS
    if elapsed < MIN_REQUEST_GAP_SECONDS:
        time.sleep(MIN_REQUEST_GAP_SECONDS - elapsed)
    history.append(time.monotonic())
    try:
        return HTTP_SESSION.get(url, headers=headers, timeout=timeout, params=params)
    finally:
        LAST_REQUEST_TS = time.monotonic()


def wait_for_rate_limit(response, attempt, fallback_seconds=60):
    """Honor Upstox 429 cooldowns and log the delay before retrying."""
    retry_after = response.headers.get("Retry-After") if hasattr(response, "headers") else None
    try:
        wait_seconds = float(retry_after) if retry_after is not None else fallback_seconds
    except (TypeError, ValueError):
        try:
            retry_time = parsedate_to_datetime(retry_after)
            if retry_time.tzinfo is None:
                retry_time = retry_time.replace(tzinfo=timezone.utc)
            wait_seconds = (retry_time - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            wait_seconds = fallback_seconds

    if not math.isfinite(wait_seconds):
        wait_seconds = fallback_seconds
    wait_seconds = max(0, wait_seconds)

    if attempt > 1:
        wait_seconds = max(wait_seconds, min(2 ** (attempt - 1), 30))

    safe_print(f"RATE_LIMIT | retrying in {wait_seconds:.0f}s after attempt {attempt}")
    while wait_seconds > 0:
        pause = min(wait_seconds, 60)
        time.sleep(pause)
        wait_seconds -= pause


def sanitize_for_json(value):
    """Convert non-standard float NaN/Inf values to JSON-safe nulls recursively."""
    if isinstance(value, dict):
        return {str(k): sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_for_json(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_json(item) for item in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
    return value


def parse_missing_dates(raw_value):
    """Parse dates such as '2026-10-05;2026-10-06' or '2026-10-08' into a clean list."""
    if raw_value is None or (isinstance(raw_value, float) and pd.isna(raw_value)):
        return []

    text = str(raw_value).strip()
    if not text:
        return []

    values = re.split(r"[;\s,]+", text)
    dates = []
    for part in values:
        clean = part.strip()
        if not clean:
            continue
        try:
            dt = datetime.strptime(clean, "%Y-%m-%d").date()
        except ValueError:
            continue
        dates.append(dt)
    return sorted(set(dates))


def build_fetch_windows(row):
    """Return a list of (from_date, to_date) windows to recover missing or invalid histories.

    PlanSIP requires a full-close history window larger than 253 trading sessions, so the default
    recovery window deliberately exceeds a 12-month range to absorb weekends and market holidays.
    """
    # Explicit known missing session dates from the diagnostic sheet should be repaired first.
    missing_dates = parse_missing_dates(row.get("knownMissingSessions"))
    if missing_dates:
        return [(d, d) for d in missing_dates]

    # If this row has a last completed date, backfill from the last good date to the current day.
    last_completed = row.get("historyLastCompleted")
    if last_completed:
        try:
            last_day = datetime.strptime(str(last_completed).split("T")[0], "%Y-%m-%d").date()
            today = date.today()
            if last_day < today:
                return [(last_day + timedelta(days=1), today)]
        except ValueError:
            pass

    # Fallback: fetch a longer history window so the final set exceeds the 253-close requirement
    # after weekends/holidays are removed.
    today = date.today()
    start = today - timedelta(days=520)
    return [(start, today)]


def clean_text(value):
    """Treat empty CSV cells/pandas NA values as missing, never the string 'NAN'."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none", "null", "<na>"} else text


def normalize_isin(value):
    """Validate the 12-character Indian ISIN shape (the API validates existence)."""
    text = clean_text(value).upper()
    return text if re.fullmatch(r"IN[A-Z0-9]{9}[0-9]", text) else None


def is_cash_equity_instrument(instrument_key):
    text = clean_text(instrument_key).upper()
    prefix, separator, suffix = text.partition("|")
    return bool(separator and prefix in {"NSE_EQ", "BSE_EQ"} and normalize_isin(suffix))


def normalize_upstox_instrument_key(value):
    """Return a canonical Upstox instrument key in the form SEGMENT|ISIN."""
    text = clean_text(value).upper()
    if not text:
        return None
    if "|" in text:
        prefix, suffix = text.split("|", 1)
        prefix = prefix.strip()
        suffix = suffix.strip()
        if prefix in {"NSE_EQ", "BSE_EQ"} and normalize_isin(suffix):
            return f"{prefix}|{suffix}"
        return None
    if normalize_isin(text):
        return f"NSE_EQ|{text}"
    return None


def lookup_instrument_key_from_master(symbol=None, isin=None):
    """Reuse one equity-only master index across the entire stock universe."""
    lookup = load_local_instrument_lookup()
    if normalize_isin(isin):
        return lookup.get(normalize_isin(isin))
    return lookup.get(clean_text(symbol).upper())


def resolve_instrument_key(symbol, row=None):
    """Accept a valid Upstox instrument_key or derive one from a symbol or ISIN."""
    if row is not None:
        for key_name in ("instrument_key", "instrumentKey", "upstox_instrument_key"):
            value = clean_text(row.get(key_name))
            if value:
                # An explicit non-equity key must never fall through to a fuzzy lookup.
                candidate = normalize_upstox_instrument_key(value)
                if LOCAL_INSTRUMENT_MASTER.exists() and candidate not in set(load_local_instrument_lookup().values()):
                    return None
                return candidate

        for key_name in ("isin", "ISIN", "isin_code"):
            value = normalize_isin(row.get(key_name))
            if value:
                master_match = lookup_instrument_key_from_master(symbol=symbol, isin=value)
                if master_match:
                    return master_match
                return f"NSE_EQ|{value}"

    value = clean_text(symbol)
    if not value:
        return None

    normalized = normalize_upstox_instrument_key(value)
    if normalized:
        return normalized
    if "|" in value:
        return None

    master_match = lookup_instrument_key_from_master(symbol=value)
    if master_match:
        return master_match

    return search_instrument_by_symbol(value)


def load_local_instrument_lookup():
    """Load the downloaded Upstox instrument JSON into a symbol/ISIN lookup map."""
    global LOCAL_INSTRUMENT_LOOKUP
    if LOCAL_INSTRUMENT_LOOKUP is not None:
        return LOCAL_INSTRUMENT_LOOKUP

    LOCAL_INSTRUMENT_LOOKUP = {}
    master_path = LOCAL_INSTRUMENT_MASTER
    if not master_path.exists():
        return LOCAL_INSTRUMENT_LOOKUP

    try:
        with master_path.open("r", encoding="utf-8") as handle:
            records = json.load(handle)
    except Exception:
        return LOCAL_INSTRUMENT_LOOKUP

    if not isinstance(records, list):
        return LOCAL_INSTRUMENT_LOOKUP

    # Prefer NSE on duplicate cash-equity records; derivatives/indexes are excluded.
    records = [item for item in records if isinstance(item, dict)]
    records.sort(key=lambda item: not clean_text(item.get("instrument_key")).upper().startswith("NSE_EQ|"))
    for item in records:
        if not isinstance(item, dict):
            continue
        # NSE_EQ also contains government/corporate debt. Require common-equity type.
        if item.get("segment") != "NSE_EQ" or item.get("instrument_type") != "EQ":
            continue
        instrument_key = normalize_upstox_instrument_key(item.get("instrument_key"))
        if not is_cash_equity_instrument(instrument_key):
            continue
        for field in ("trading_symbol", "short_name", "name"):
            value = str(item.get(field) or "").strip()
            if value:
                LOCAL_INSTRUMENT_LOOKUP.setdefault(value.upper(), instrument_key)
        isin = str(item.get("isin") or "").strip()
        if isin:
            LOCAL_INSTRUMENT_LOOKUP.setdefault(isin.upper(), instrument_key)
        LOCAL_INSTRUMENT_LOOKUP.setdefault(instrument_key.split("|", 1)[1], instrument_key)

    return LOCAL_INSTRUMENT_LOOKUP


def search_instrument_by_symbol(symbol):
    """Map a ticker or ISIN symbol to a valid Upstox instrument key."""
    global UPSTOX_AUTH_ERROR

    value = clean_text(symbol)
    if not value:
        return None

    normalized = value.upper()
    if normalized in INSTRUMENT_CACHE:
        return INSTRUMENT_CACHE[normalized]

    local_lookup = load_local_instrument_lookup()
    if normalized in local_lookup:
        INSTRUMENT_CACHE[normalized] = local_lookup[normalized]
        return local_lookup[normalized]

    if value.startswith("INE"):
        candidate = f"NSE_EQ|{value.upper()}"
        if candidate in {str(v).upper() for v in local_lookup.values()}:
            INSTRUMENT_CACHE[normalized] = candidate
            return candidate

    if UPSTOX_AUTH_ERROR:
        return None
    if UPSTOX_TOKEN in ("", "YOUR_UPSTOX_ACCESS_TOKEN_HERE"):
        UPSTOX_AUTH_ERROR = "UPSTOX_TOKEN is missing or still set to the placeholder."
        INSTRUMENT_CACHE[normalized] = None
        return None

    url = "https://api.upstox.com/v2/instruments/search"
    candidate_queries = [value, value.upper(), value.lower(), value.replace(" ", "")]
    seen = set()
    for query in candidate_queries:
        if not query or query in seen:
            continue
        seen.add(query)
        params = {
            "query": query,
            "exchanges": "NSE,BSE",
            "segments": "EQ",
            "page_number": 1,
            "records": 20,
        }

        try:
            response = throttled_requests_get(url, params=params, headers=headers, timeout=30)
            if response.status_code in {401, 403}:
                UPSTOX_AUTH_ERROR = (
                    "Upstox rejected the bearer token: HTTP "
                    f"{response.status_code}. Check that UPSTOX_TOKEN is valid and active."
                )
                INSTRUMENT_CACHE[normalized] = None
                return None

            if response.status_code != 200:
                continue

            data = response.json().get("data", [])
            if isinstance(data, dict):
                data = data.get("instruments", [])
            if not data:
                continue

            preferred = []
            for item in data:
                if not isinstance(item, dict) or not is_cash_equity_instrument(item.get("instrument_key")):
                    continue
                trading_symbol = str(item.get("trading_symbol", "")).upper()
                short_name = str(item.get("short_name", "")).upper()
                name = str(item.get("name", "")).upper()
                if trading_symbol == normalized or short_name == normalized or name == normalized:
                    preferred.append(item)

            if not preferred:
                continue
            preferred.sort(key=lambda item: not item["instrument_key"].upper().startswith("NSE_EQ|"))
            chosen = preferred[0]
            instrument_key = chosen.get("instrument_key")
            INSTRUMENT_CACHE[normalized] = instrument_key
            return instrument_key
        except Exception:
            continue

    INSTRUMENT_CACHE[normalized] = None
    return None


def resolve_isin_for_symbol(symbol, row=None, instrument_key=None):
    """Use validated equity key/ISIN identity; reject conflicting explicit inputs."""
    candidates = set()
    key_values = [instrument_key]
    if row is not None:
        key_values.extend(row.get(name) for name in (
            "instrument_key", "instrumentKey", "upstox_instrument_key"
        ))
        for name in ("isin", "ISIN", "isin_code"):
            isin = normalize_isin(row.get(name))
            if isin:
                candidates.add(isin)
    if "|" in clean_text(symbol):
        key_values.append(symbol)
    for value in key_values:
        text = clean_text(value)
        if not text:
            continue
        key = normalize_upstox_instrument_key(text)
        if not key or not is_cash_equity_instrument(key):
            return None
        candidates.add(key.split("|", 1)[1])
    symbol_isin = normalize_isin(symbol)
    if symbol_isin:
        candidates.add(symbol_isin)
    if len(candidates) > 1:
        raise ValueError("Conflicting ISIN/instrument_key values in input row; correct the stock identity.")
    if candidates:
        return candidates.pop()
    key = lookup_instrument_key_from_master(symbol=symbol)
    return key.split("|", 1)[1] if is_cash_equity_instrument(key) else None


def fetch_historical_candles(instrument_key, from_date, to_date, max_retries=3):
    """Fetch a range of daily candles from Upstox with retry/backoff for rate-limited responses."""
    global UPSTOX_AUTH_ERROR

    if UPSTOX_AUTH_ERROR:
        return {"status": "http_401", "instrument_key": instrument_key,
                "from_date": str(from_date), "to_date": str(to_date),
                "error": UPSTOX_AUTH_ERROR, "candles": []}
    if UPSTOX_TOKEN in ("", "YOUR_UPSTOX_ACCESS_TOKEN_HERE"):
        UPSTOX_AUTH_ERROR = "UPSTOX_TOKEN is missing or still set to the placeholder."
        return {
            "status": "missing_token",
            "instrument_key": instrument_key,
            "from_date": str(from_date),
            "to_date": str(to_date),
            "error": "UPSTOX_TOKEN is missing or still set to the placeholder.",
            "candles": [],
        }

    if instrument_key is None:
        return {
            "status": "invalid_instrument",
            "instrument_key": instrument_key,
            "from_date": str(from_date),
            "to_date": str(to_date),
            "error": "No instrument key was resolved.",
            "candles": [],
        }

    url = f"https://api.upstox.com/v3/historical-candle/{instrument_key}/days/1/{to_date}/{from_date}"
    for attempt in range(1, max_retries + 1):
        try:
            response = throttled_requests_get(url, headers=headers, timeout=30)
        except requests.RequestException as exc:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            return {
                "status": "request_error",
                "instrument_key": instrument_key,
                "from_date": str(from_date),
                "to_date": str(to_date),
                "error": str(exc),
                "candles": [],
                "source_url": url,
            }

        if response.status_code in {401, 403}:
            UPSTOX_AUTH_ERROR = (
                "Upstox rejected the bearer token: HTTP "
                f"{response.status_code}. Check that UPSTOX_TOKEN is valid and active."
            )
            return {
                "status": f"http_{response.status_code}",
                "instrument_key": instrument_key,
                "from_date": str(from_date),
                "to_date": str(to_date),
                "error": UPSTOX_AUTH_ERROR,
                "candles": [],
                "source_url": url,
            }

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                return {"status": "invalid_json", "candles": [], "from_date": str(from_date), "to_date": str(to_date), "error": "Upstox returned invalid JSON", "source_url": url}
            if not isinstance(payload, dict) or payload.get("status") != "success" or not isinstance(payload.get("data"), dict) or not isinstance(payload["data"].get("candles"), list):
                return {"status": "api_error", "candles": [], "from_date": str(from_date), "to_date": str(to_date), "error": "Upstox did not return a successful candle array", "source_url": url}
            candles = payload["data"]["candles"]
            return {
                "status": "success",
                "instrument_key": instrument_key,
                "from_date": str(from_date),
                "to_date": str(to_date),
                "source_url": url,
                "candles": candles,
                "raw_response": payload,
            }

        if response.status_code in {429, 500, 502, 503, 504} and attempt < max_retries:
            if response.status_code == 429:
                wait_for_rate_limit(response, attempt)
            else:
                time.sleep(min(2 ** attempt, 30))
            continue

        return {
            "status": f"http_{response.status_code}",
            "instrument_key": instrument_key,
            "from_date": str(from_date),
            "to_date": str(to_date),
            "error": response.text[:500],
            "candles": [],
            "source_url": url,
        }

    return {
        "status": "unknown_error",
        "instrument_key": instrument_key,
        "from_date": str(from_date),
        "to_date": str(to_date),
        "error": "No valid response was produced.",
        "candles": [],
        "source_url": url,
    }


def save_raw_candle_exports(symbol, instrument_key, sector, result, outcome_label):
    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)
    RAW_CSV_DIR.mkdir(parents=True, exist_ok=True)

    base_name = safe_symbol_file_name(symbol)
    json_path = RAW_JSON_DIR / f"{base_name}.json"
    csv_path = RAW_CSV_DIR / f"{base_name}.csv"

    candles = result.get("candles", []) if isinstance(result, dict) else []
    export_record = {
        "symbol": symbol,
        "instrument_key": instrument_key,
        "sector": sector,
        "requested_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": outcome_label,
        "source_url": result.get("source_url"),
        "from_date": result.get("from_date"),
        "to_date": result.get("to_date"),
        "response_status": result.get("status"),
        "error": result.get("error"),
        "raw_response": result.get("raw_response"),
        "fetch_windows": result.get("fetch_windows", []),
        "candle_count": len(candles),
        "candles": candles,
    }
    write_json_atomic(json_path, export_record)

    temporary_csv = csv_path.with_suffix(".csv.tmp")
    with open(temporary_csv, mode="w", newline="", encoding="utf-8") as out_file:
        writer = csv.writer(out_file)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume", "open_interest"])
        for candle in candles:
            if isinstance(candle, (list, tuple)):
                writer.writerow(candle[:7])
            else:
                writer.writerow([candle])

    temporary_csv.replace(csv_path)
    return str(json_path), str(csv_path)


FUNDAMENTAL_DETAIL_FIELDS = (
    "profit_before_tax", "finance_costs", "equity", "current_borrowings",
    "non_current_borrowings", "total_borrowings", "current_lease_liabilities",
    "non_current_lease_liabilities", "total_lease_liabilities",
    "cash_and_cash_equivalents",
)
FUNDAMENTAL_REQUIRED_FIELDS = (
    "profit_before_tax", "finance_costs", "equity", "total_borrowings",
    "total_lease_liabilities", "cash_and_cash_equivalents",
)


def _fundamental_label(value):
    """Normalize punctuation, never perform substring-based financial matching."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold().replace("&", " and ")).strip()


def _fundamental_number(value):
    """Preserve zero; reject missing, boolean, NaN, and infinite amounts."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value.startswith("(") and value.endswith(")"):
            value = "-" + value[1:-1].strip()
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _fundamental_period(value):
    """Return an explicit fiscal month, without guessing ambiguous FY labels."""
    text = str(value or "").strip().replace("’", "'")
    for pattern in ("%b %Y", "%B %Y", "%b-%Y", "%B-%Y", "%b'%y",
                    "%b %y", "%b-%y", "%Y-%m-%d", "%d-%m-%Y",
                    "%d %b %Y", "%d %B %Y", "%Y-%m"):
        try:
            parsed = datetime.strptime(text, pattern)
            return parsed.strftime("%b %Y"), (parsed.year, parsed.month)
        except ValueError:
            pass
    return None, None


_FUNDAMENTAL_LABEL_ALIASES = {
    "income_statement": {
        "profit_before_tax": ("Profit Before Tax", "Profit Before Taxation",
                              "Profit/(Loss) Before Tax", "Profit or Loss Before Tax", "PBT"),
        "finance_costs": ("Finance Costs", "Finance Cost", "Finance Charges",
                          "Interest and Finance Costs", "Interest and Finance Charges"),
    },
    "balance_sheet": {
        "equity": ("Total Equity", "Shareholders' Equity", "Total Shareholders' Equity",
                   "Shareholders' Funds", "Total Shareholders' Funds", "Equity"),
        "current_borrowings": ("Current Borrowings", "Short Term Borrowings",
                               "Borrowings (Current)", "Borrowings (Short Term)"),
        "non_current_borrowings": ("Non Current Borrowings", "Long Term Borrowings",
                                   "Borrowings (Non Current)", "Borrowings (Long Term)"),
        "total_borrowings": ("Total Borrowings",),
        "_current_maturities": ("Current Maturities of Long Term Debt",
                                "Current Maturities of Long Term Borrowings"),
        "current_lease_liabilities": ("Current Lease Liabilities", "Short Term Lease Liabilities",
                                      "Lease Liabilities (Current)", "Lease Liabilities (Short Term)"),
        "non_current_lease_liabilities": ("Non Current Lease Liabilities", "Long Term Lease Liabilities",
                                          "Lease Liabilities (Non Current)", "Lease Liabilities (Long Term)"),
        "total_lease_liabilities": ("Total Lease Liabilities",),
        "cash_and_cash_equivalents": ("Cash and Cash Equivalents", "Cash & Cash Equivalents"),
        "_total_assets": ("Total Assets",),
        "_total_liabilities": ("Total Liabilities",),
        "_equity_capital": ("Equity Capital",),
    },
    "cash_flow": {
        "_closing_cash": ("Cash (End of the year)", "Cash at End of the Year",
                          "Cash and Cash Equivalents at End of the Year",
                          "Cash and Cash Equivalents at the End of the Year",
                          "Closing Cash and Cash Equivalents"),
    },
}


def extract_fundamental_details(payloads):
    """Extract annual inputs from raw Upstox responses, retaining provenance.

    Monetary amounts keep the API's units; they are never silently rescaled.
    `full_statement` is ANNUAL even when its wrapper's `time_period` is quarterly.
    Align only the same fiscal month, statement type and units. Income summaries
    do not determine the latest annual statement. Missing or ambiguous amounts
    remain None; neither missing leases nor missing borrowings are assumed zero.
    No ROCE or debt ratio is calculated, and leases are not added to borrowings.
    """
    warnings, rows, candidates, metadata = [], {}, {}, {}

    def warn(message):
        if message not in warnings:
            warnings.append(message)

    def row_for(endpoint, period_value, is_main):
        period, order = _fundamental_period(period_value)
        if period is None:
            warn(f"{endpoint}: unrecognized fiscal period {period_value!r}; not combined.")
            return None
        info = metadata[endpoint]
        # Missing identity information must not enable cross-endpoint matching.
        isolation = endpoint if not info["statement_type"] or not info["units_in"] else ""
        key = (order, info["statement_type"], info["units_in"], isolation)
        if key not in rows:
            rows[key] = dict.fromkeys(FUNDAMENTAL_DETAIL_FIELDS)
            rows[key].update(period=period, statement_type=info["statement_type"],
                             units_in=info["units_in"], reporting_frequency="annual",
                             sources={}, endpoint_metadata={}, _order=order, _main=False)
            candidates[key] = {}
        rows[key]["_main"] = rows[key]["_main"] or is_main
        rows[key]["endpoint_metadata"][endpoint] = dict(info)
        return key

    def add(key, metric, value, endpoint, particular, location="full_statement"):
        amount = _fundamental_number(value)
        if amount is None:
            return
        source = {"endpoint": endpoint, "particular": particular, "location": location}
        candidates[key].setdefault(metric, []).append((amount, source))

    sector = None
    profile = payloads.get("profile")
    if isinstance(profile, dict) and profile.get("status") == "success":
        profile_data = profile.get("data")
        value = profile_data.get("sector") if isinstance(profile_data, dict) else None
        if isinstance(value, str) and value.strip() and value.strip().casefold() not in {
                "unknown", "n/a", "na", "null", "none", "-"}:
            sector = value.strip()

    unit_aliases = {"crore": "crore", "crores": "crore", "inr crore": "crore",
                    "inr crores": "crore", "lakh": "lakh", "lakhs": "lakh",
                    "million": "million", "millions": "million", "billion": "billion",
                    "billions": "billion", "inr": "INR", "rupees": "INR"}
    for endpoint, aliases in _FUNDAMENTAL_LABEL_ALIASES.items():
        payload = payloads.get(endpoint)
        if not isinstance(payload, dict) or payload.get("status") != "success":
            if payload is not None:
                warn(f"{endpoint}: no successful API response; details remain missing.")
            continue
        data = payload.get("data")
        if not isinstance(data, dict):
            warn(f"{endpoint}: expected an object in data; details remain missing.")
            continue
        statement_type = str(data.get("type") or "").strip().casefold()
        statement_type = statement_type if statement_type in {"consolidated", "standalone"} else None
        units = unit_aliases.get(_fundamental_label(data.get("units_in")))
        metadata[endpoint] = {"statement_type": statement_type, "units_in": units,
                              "source_type": data.get("type"), "source_units_in": data.get("units_in"),
                              "summary_time_period": data.get("time_period"),
                              "full_statement_frequency": "annual"}
        if statement_type is None or units is None:
            warn(f"{endpoint}: missing or unrecognized type/units; kept separate from other endpoints.")
        alias_index = {_fundamental_label(label): metric for metric, labels in aliases.items() for label in labels}
        statements = data.get("full_statement")
        if statements is None:
            warn(f"{endpoint}: full_statement is missing; request fs=true for detailed annual fields.")
            statements = []
        elif not isinstance(statements, list):
            warn(f"{endpoint}: unsupported full_statement shape; expected a particular/history array.")
            statements = []
        for item in statements:
            if not isinstance(item, dict) or not isinstance(item.get("history"), list):
                continue
            particular = item.get("particular")
            metric = alias_index.get(_fundamental_label(particular))
            for observation in item["history"]:
                if not isinstance(observation, dict):
                    continue
                key = row_for(endpoint, observation.get("period"), endpoint != "cash_flow")
                if key is not None and metric:
                    if metric == "_current_maturities":
                        rows[key]["_current_maturities_seen"] = True
                    add(key, metric, observation.get("value"), endpoint, particular)
        # Upstox's BS summary separates liabilities from equity. Use only annual
        # summary totals, because quarterly summaries can accompany annual fs.
        if endpoint == "balance_sheet" and str(data.get("time_period") or "").casefold() in {"yearly", "annual"}:
            history = data.get("history")
            for observation in history if isinstance(history, list) else []:
                if not isinstance(observation, dict):
                    continue
                key = row_for(endpoint, observation.get("period"), True)
                if key is not None:
                    for metric, field in (("_total_assets", "total_asset"), ("_total_liabilities", "total_liability")):
                        add(key, metric, observation.get(field), endpoint, field, "history")

    def agrees(left, right):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=0.01)

    for key, row in rows.items():
        values, resolved_sources, conflicts = {}, {}, set()
        for metric, observations in candidates[key].items():
            value, source = observations[0]
            if any(value != other for other, _ in observations[1:]):
                conflicts.add(metric)
                labels = sorted({str(item[1]["particular"]) for item in observations})
                warn(f"{row['period']} ({row['statement_type']}, {row['units_in']}): conflicting {metric} values for {labels}; left missing.")
                continue
            values[metric], resolved_sources[metric] = value, dict(source)
            if len(observations) > 1:
                resolved_sources[metric]["corroborating_sources"] = [item[1] for item in observations[1:]]
        for metric in FUNDAMENTAL_DETAIL_FIELDS:
            if metric in values:
                row[metric], row["sources"][metric] = values[metric], resolved_sources[metric]

        def derived(metric, amount, formula, input_names):
            if not math.isfinite(amount):
                warn(f"{row['period']}: non-finite derived {metric}; left missing.")
                return
            row[metric] = amount
            row["sources"][metric] = {"endpoint": "balance_sheet", "formula": formula,
                                       "inputs": {name: {"value": values[name], "source": resolved_sources[name]}
                                                  for name in input_names}}

        assets, liabilities = values.get("_total_assets"), values.get("_total_liabilities")
        if assets is not None and liabilities is not None:
            implied_equity = assets - liabilities
            if row["equity"] is not None and not agrees(row["equity"], implied_equity):
                row["equity"] = None
                row["sources"].pop("equity", None)
                conflicts.add("equity")
                warn(f"{row['period']}: total equity does not reconcile to total assets minus total liabilities; left missing.")
            if row["equity"] is None and "equity" not in conflicts:
                derived("equity", implied_equity, "total_assets - total_liabilities", ("_total_assets", "_total_liabilities"))
                capital = values.get("_equity_capital")
                if capital is not None and agrees(capital, implied_equity):
                    row["sources"]["equity"]["reconciled_equity_capital"] = resolved_sources["_equity_capital"]
        elif row["equity"] is None and "_equity_capital" in values:
            warn(f"{row['period']}: Equity Capital alone does not establish total equity; left missing.")

        for total, current, non_current in (
            ("total_borrowings", "current_borrowings", "non_current_borrowings"),
            ("total_lease_liabilities", "current_lease_liabilities", "non_current_lease_liabilities"),
        ):
            if total == "total_borrowings" and row.get("_current_maturities_seen") and values.get("_current_maturities") != 0:
                # A separate maturities line may overlap with (or be omitted from)
                # current borrowings. Preserve a reported total; do not guess a sum.
                if row[total] is None:
                    warn(f"{row['period']}: separate current maturities disclosed; total borrowings cannot be inferred from two components alone.")
                continue
            if current not in values or non_current not in values:
                continue
            amount = values[current] + values[non_current]
            if row[total] is not None and not agrees(row[total], amount):
                row[total] = None
                row["sources"].pop(total, None)
                conflicts.add(total)
                warn(f"{row['period']}: {total} conflicts with current plus non-current components; left missing.")
            elif row[total] is None and total not in conflicts:
                derived(total, amount, f"{current} + {non_current}", (current, non_current))

        if row["cash_and_cash_equivalents"] is None and "cash_and_cash_equivalents" not in conflicts and "_closing_cash" in values:
            row["cash_and_cash_equivalents"] = values["_closing_cash"]
            row["sources"]["cash_and_cash_equivalents"] = dict(resolved_sources["_closing_cash"], fallback=True)
        row["missing_fields"] = [field for field in FUNDAMENTAL_REQUIRED_FIELDS if row[field] is None]

    if len({(row["statement_type"], row["units_in"]) for row in rows.values()}) > 1:
        warn("Different statement types or units were returned; separate annual rows are retained without cross-combining values.")

    def row_order(row):
        return (row["_order"], row["_main"], row["statement_type"] == "consolidated",
                bool(row["statement_type"] and row["units_in"]), -len(row["missing_fields"]))

    annual = sorted(rows.values(), key=row_order, reverse=True)
    main_rows = [row for row in annual if row["_main"]]
    latest = max(main_rows or annual, key=row_order) if annual else {}
    missing_fields = list(latest.get("missing_fields", FUNDAMENTAL_REQUIRED_FIELDS))
    if sector is None:
        missing_fields.insert(0, "sector")
    for row in annual:
        row.pop("_order")
        row.pop("_main")
        row.pop("_current_maturities_seen", None)
    return {"sector": sector, "annual": annual, "latest": latest,
            "warnings": warnings, "missing_fields": missing_fields, "endpoint_metadata": metadata}


def normalize_fundamental_payload(name, payload):
    """Preserve legacy compact keys AND the full annual statement and metadata."""
    empty = {name: {} if name == "profile" else []}
    if not isinstance(payload, dict) or payload.get("status") != "success" or payload.get("error"):
        return empty
    data = copy.deepcopy(payload.get("data"))
    if name == "profile":
        return {name: data if isinstance(data, dict) else {}}
    if isinstance(data, list):
        return {name: data}
    if not isinstance(data, dict):
        return empty
    compact = data.get(name)
    if compact is None:
        compact = data.get("history", data.get("data", []))
    result = {name: compact if isinstance(compact, list) else [compact] if compact else []}
    result.update({
        "type": data.get("type"),
        "time_period": data.get("time_period"),
        "units_in": data.get("units_in"),
        "full_statement_period": "yearly",
        "full_statement": data.get("full_statement"),
    })
    return result


def api_error_details(payload, fallback=""):
    errors = payload.get("errors", []) if isinstance(payload, dict) else []
    if not isinstance(errors, list):
        errors = []
    codes = {str(item.get("error_code") or item.get("errorCode") or "")
             for item in errors if isinstance(item, dict)}
    messages = [f"{item.get('error_code') or item.get('errorCode') or 'API_ERROR'}: {item.get('message', '')}"
                for item in errors if isinstance(item, dict)]
    return codes, "; ".join(messages)[:500] or fallback


def fetch_fundamental_payload(url, params, max_retries=4):
    """Retry transient failures; recognize HTTP and JSON-level API errors."""
    global UPSTOX_AUTH_ERROR
    used_url = url + ("?" + urlencode(params) if params else "")
    if UPSTOX_AUTH_ERROR or UPSTOX_TOKEN in ("", "YOUR_UPSTOX_ACCESS_TOKEN_HERE"):
        UPSTOX_AUTH_ERROR = UPSTOX_AUTH_ERROR or "UPSTOX_TOKEN is missing or still set to the placeholder."
        return None, None, used_url, UPSTOX_AUTH_ERROR
    for attempt in range(1, max_retries + 1):
        try:
            response = throttled_requests_get(url, headers=headers, timeout=30, params=params)
        except requests.RequestException as exc:
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            return None, None, used_url, f"{type(exc).__name__}: {exc}"
        try:
            payload = response.json()
        except ValueError:
            payload = None
        codes, message = api_error_details(payload, str(response.text)[:500])
        if response.status_code in {401, 403} or codes & {"UDAPI100050", "UDAPI100016", "UDAPI100067"}:
            UPSTOX_AUTH_ERROR = f"Upstox authentication/access error (HTTP {response.status_code}): {message}"
            return payload, response.status_code, used_url, UPSTOX_AUTH_ERROR
        is_api_error = not isinstance(payload, dict) or payload.get("status") != "success"
        if response.status_code == 200 and not is_api_error and isinstance(payload.get("data"), (dict, list)):
            return payload, response.status_code, used_url, None
        rate_limited = response.status_code == 429 or "UDAPI10005" in codes
        if (rate_limited or response.status_code in {500, 502, 503, 504} or
                (response.status_code == 200 and payload is None)) and attempt < max_retries:
            if rate_limited:
                wait_for_rate_limit(response, attempt)
            else:
                time.sleep(min(2 ** attempt, 30))
            continue
        error = message or "Response is missing a successful JSON data object."
        return payload, response.status_code, used_url, error
    return None, None, used_url, "Retry limit reached."


def write_json_atomic(path, value):
    """Keep the previous export intact if a JSON write is interrupted."""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(sanitize_for_json(value), indent=2, default=str, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def debt_inputs_ready(row):
    return bool(row) and all(row.get(key) is not None for key in (
        "total_borrowings", "total_lease_liabilities", "cash_and_cash_equivalents"
    ))


def fetch_and_save_fundamental_jsons(symbol, instrument_key, row=None):
    """Fetch all five endpoints for a validated ISIN and save auditable inputs."""
    saved = {"details": extract_fundamental_details({}), "status": "INVALID_ISIN", "errors": []}
    try:
        isin = resolve_isin_for_symbol(symbol, row=row, instrument_key=instrument_key)
    except ValueError as exc:
        saved["errors"].append(str(exc))
        return saved
    if not isin:
        saved["errors"].append("No valid cash-equity ISIN was resolved.")
        return saved
    saved["isin"] = isin
    if FUNDAMENTAL_STATEMENT_TYPE not in {"consolidated", "standalone"}:
        raise ValueError("UPSTOX_STATEMENT_TYPE must be consolidated or standalone.")

    stock_dir = FUNDAMENTAL_DIR / safe_symbol_file_name(symbol)
    stock_dir.mkdir(parents=True, exist_ok=True)
    base = f"https://api.upstox.com/v2/fundamentals/{isin}"
    statement_params = {"type": FUNDAMENTAL_STATEMENT_TYPE, "fs": "true"}
    endpoint_specs = {
        "profile": (f"{base}/profile", {}),
        "income_statement": (f"{base}/income-statement", {**statement_params, "time_period": "yearly"}),
        "balance_sheet": (f"{base}/balance-sheet", dict(statement_params)),
        "cash_flow": (f"{base}/cash-flow", dict(statement_params)),
        "key_ratios": (f"{base}/key-ratios", {}),
    }
    payloads = {}
    endpoint_statuses = {}
    for name, (url, params) in endpoint_specs.items():
        # Reuse recent successful evidence; repeated calls cannot manufacture
        # fields the provider omitted. NSE/Yahoo handle unresolved inputs.
        cache_path = stock_dir / f"{name}.json"
        if os.getenv("FORCE_REFRESH", "0") != "1" and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached.get("response_received_at_utc") or cached.get("requested_at_utc"))).total_seconds()
                cached_payload = cached.get("payload") or {}
                data = cached_payload.get("data") or {}
                detailed = name not in {"income_statement", "balance_sheet", "cash_flow"} or (
                    isinstance(data, dict) and isinstance(data.get("full_statement"), list) and bool(data["full_statement"])
                    and str(data.get("type", "")).lower() == FUNDAMENTAL_STATEMENT_TYPE)
                if 0 <= age < 86400 and cached.get("isin") == isin and cached.get("symbol") == clean_text(symbol) and cached.get("http_status") == 200 and cached_payload.get("status") == "success" and not cached.get("request_error") and detailed:
                    payloads[name] = cached_payload
                    endpoint_statuses[name] = {"http_status": 200, "error": None, "cached": True}
                    saved[name] = str(cache_path)
                    continue
            except (ValueError, TypeError, KeyError):
                pass
        req_time = datetime.now(timezone.utc).isoformat()
        payload, status, source_url, error = fetch_fundamental_payload(url, params)
        endpoint_statuses[name] = {"http_status": status, "error": error}
        if error:
            saved["errors"].append(f"{name}: {error}")
            log_upstox_status(symbol, name, status, error, failed=True)
        else:
            payloads[name] = payload
        normalized = normalize_fundamental_payload(name, payload if not error else None)
        full_statement = normalized.get("full_statement")
        has_full_statement = isinstance(full_statement, list) and any(
            isinstance(item, dict) and isinstance(item.get("history"), list) and any(
                isinstance(observation, dict) and _fundamental_number(observation.get("value")) is not None
                for observation in item["history"]
            )
            for item in full_statement
        )
        note = "Total liabilities are not borrowings. Use explicitly extracted borrowing and lease inputs, with their sources and periods."
        record = {
            "symbol": clean_text(symbol), "instrument_key": instrument_key, "isin": isin,
            "requested_at_utc": req_time, "http_status": status, "source_url": source_url,
            "request_params": params, "response_received_at_utc": datetime.now(timezone.utc).isoformat(),
            "request_error": error,
            "payload": payload if payload is not None else {"error": error or "No response."},
            "normalized_payload": normalized, "full_statement_available": has_full_statement,
            "liability_is_debt_eligible": False, "debt_check_note": note,
        }
        if name == "balance_sheet":
            for entry in normalized.get("balance_sheet", []):
                if isinstance(entry, dict):
                    entry.update({"full_statement_available": has_full_statement,
                                  "liability_is_debt_eligible": False, "debt_check_note": note})
        out_path = stock_dir / f"{name}.json"
        # Retain usable historical evidence on a transient/authentication failure.
        previous = None
        if error and out_path.exists():
            try:
                previous = json.loads(out_path.read_text())
            except (ValueError, OSError):
                pass
        if previous and previous.get("symbol") == clean_text(symbol) and previous.get("isin") == isin and previous.get("http_status") == 200 and (previous.get("payload") or {}).get("status") == "success" and not previous.get("request_error"):
            write_json_atomic(stock_dir / f"{name}.last_error.json", record)
            payloads[name] = previous["payload"]
        else:
            write_json_atomic(out_path, record)
        saved[name] = str(out_path)
        if UPSTOX_AUTH_ERROR:
            # Remaining endpoints are omitted from this result; existing files are
            # not advertised as fresh data. Stop the universe in main as well.
            break

    details = extract_fundamental_details(payloads)
    saved["details"] = details
    latest = details.get("latest") or {}
    if UPSTOX_AUTH_ERROR:
        saved["status"] = "AUTH_FAILED"
    elif not payloads:
        saved["status"] = "FAILED"
    elif not details.get("sector") and not details.get("annual"):
        saved["status"] = "NO_DATA"
    elif saved["errors"] or not details.get("sector") or not latest or latest.get("missing_fields"):
        saved["status"] = "PARTIAL"
    else:
        saved["status"] = "COMPLETE"
    out_path = stock_dir / "extracted_fundamentals.json"
    write_json_atomic(out_path, {
        "symbol": clean_text(symbol), "instrument_key": instrument_key, "isin": isin,
        "requested_statement_type": FUNDAMENTAL_STATEMENT_TYPE,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": saved["status"], "endpoint_statuses": endpoint_statuses,
        "errors": saved["errors"], "debt_inputs_ready": debt_inputs_ready(latest),
        **details,
    })
    saved["extracted"] = str(out_path)
    return saved


def decide_stock_outcome(fetch_results, total_candles):
    """Convert API outcomes to a meaningful status.

    A successful API call with zero candles is not a true failure; it usually means the symbol
    simply has no traded data for the queried session/date. The script should record that as
    NO_DATA rather than FAILED so missing-date cleanups do not look like missing code/data.
    """
    if total_candles > 0:
        return "SUCCESS"

    if any(result.get("status") == "success" for result in fetch_results):
        return "NO_DATA"

    if any(result.get("status") in {"http_401", "http_403"} for result in fetch_results):
        return "AUTH_FAILED"

    if any(result.get("status") == "invalid_instrument" for result in fetch_results):
        return "SKIPPED"

    return "FAILED"


SUMMARY_METRICS = {
    "PBT": "profit_before_tax", "Finance_Costs": "finance_costs", "Equity": "equity",
    "Current_Borrowings": "current_borrowings", "Non_Current_Borrowings": "non_current_borrowings",
    "Total_Borrowings": "total_borrowings", "Current_Lease_Liabilities": "current_lease_liabilities",
    "Non_Current_Lease_Liabilities": "non_current_lease_liabilities",
    "Total_Lease_Liabilities": "total_lease_liabilities", "Cash_And_Cash_Equivalents": "cash_and_cash_equivalents",
}
SUMMARY_COLUMNS = [
    "Symbol", "Sector", "Status", "Fetched_Candles", "Latest_Candle_Date", "Raw_JSON", "Raw_CSV",
    "Source_URL", "Profile_JSON", "Income_JSON", "Balance_JSON", "Ratios_JSON",
    "ISIN", "Instrument_Key", "Input_Sector", "Sector_Source", "Fundamentals_Status", "Fundamentals_Period",
    "Statement_Type", "Financial_Units", *SUMMARY_METRICS,
    "Debt_Inputs_Ready", "Missing_Fundamental_Fields", "Fundamentals_Error", "Fundamentals_Warnings",
    "CashFlow_JSON", "Fundamentals_JSON",
]


def empty_summary_row(row):
    result = dict.fromkeys(SUMMARY_COLUMNS, "")
    result.update({"Symbol": clean_text(row.get("symbol")) or "UNKNOWN",
                   "Sector": clean_text(row.get("sectorClass")) or "unknown",
                   "Input_Sector": clean_text(row.get("sectorClass")), "Fetched_Candles": 0})
    return result


def process_stock_row(row, index, total):
    summary = empty_summary_row(row)
    symbol = summary["Symbol"]
    current = index + 1
    safe_print(f"PROGRESS {current}/{total} | {symbol} | start")
    instrument_key = resolve_instrument_key(symbol, row)
    summary["Instrument_Key"] = instrument_key or ""
    if not is_cash_equity_instrument(instrument_key):
        summary.update({"Status": "AUTH_FAILED" if UPSTOX_AUTH_ERROR else "SKIPPED",
                        "Fundamentals_Status": "AUTH_FAILED" if UPSTOX_AUTH_ERROR else "INVALID_ISIN",
                        "Fundamentals_Error": UPSTOX_AUTH_ERROR or "No valid cash-equity instrument key."})
        return summary

    # Fetch profile first so sector reaches the summary and candle metadata.
    fundamentals = fetch_and_save_fundamental_jsons(symbol, instrument_key, row=row)
    details = fundamentals.get("details") or {}
    latest = details.get("latest") or {}
    api_sector = clean_text(details.get("sector"))
    summary.update({
        "Sector": api_sector or summary["Sector"],
        "Sector_Source": "upstox_profile" if api_sector else "input" if summary["Input_Sector"] else "missing",
        "ISIN": fundamentals.get("isin", ""), "Fundamentals_Status": fundamentals.get("status", "FAILED"),
        "Fundamentals_Period": latest.get("period", ""), "Statement_Type": latest.get("statement_type", ""),
        "Financial_Units": latest.get("units_in", ""),
        "Missing_Fundamental_Fields": ";".join(details.get("missing_fields", [])),
        "Fundamentals_Error": "; ".join(fundamentals.get("errors", [])),
        "Fundamentals_Warnings": "; ".join(details.get("warnings", [])),
        "Debt_Inputs_Ready": debt_inputs_ready(latest),
        "Profile_JSON": fundamentals.get("profile", ""), "Income_JSON": fundamentals.get("income_statement", ""),
        "Balance_JSON": fundamentals.get("balance_sheet", ""), "Ratios_JSON": fundamentals.get("key_ratios", ""),
        "CashFlow_JSON": fundamentals.get("cash_flow", ""), "Fundamentals_JSON": fundamentals.get("extracted", ""),
    })
    summary.update({column: latest.get(metric) for column, metric in SUMMARY_METRICS.items()})
    safe_print(f"PROGRESS {current}/{total} | {symbol} | fundamentals={summary['Fundamentals_Status']} | period={summary['Fundamentals_Period'] or 'N/A'} | missing={summary['Missing_Fundamental_Fields'] or 'none'}")
    if UPSTOX_AUTH_ERROR or fundamentals.get("status") == "INVALID_ISIN":
        summary["Status"] = "AUTH_FAILED" if UPSTOX_AUTH_ERROR else "SKIPPED"
        return summary
    if FUNDAMENTALS_ONLY:
        summary["Status"] = "FUNDAMENTALS_ONLY"
        return summary

    fetch_results = []
    candles_by_timestamp = {}
    for from_date, to_date in build_fetch_windows(row):
        result = fetch_historical_candles(instrument_key, from_date, to_date)
        fetch_results.append(result)
        for candle in result.get("candles", []):
            if isinstance(candle, (list, tuple)) and candle:
                candles_by_timestamp[str(candle[0])] = candle
        if UPSTOX_AUTH_ERROR:
            break
        time.sleep(0.4)
    candles = [candles_by_timestamp[key] for key in sorted(candles_by_timestamp, reverse=True)]
    outcome = decide_stock_outcome(fetch_results, len(candles))
    summary.update({"Status": outcome, "Fetched_Candles": len(candles),
                    "Latest_Candle_Date": candles[0][0] if candles else ""})
    if fetch_results:
        combined = dict(fetch_results[-1])
        combined["candles"] = candles
        combined["from_date"] = min(result["from_date"] for result in fetch_results)
        combined["to_date"] = max(result["to_date"] for result in fetch_results)
        combined["fetch_windows"] = [{key: value for key, value in result.items() if key != "candles"}
                                    for result in fetch_results]
        summary["Raw_JSON"], summary["Raw_CSV"] = save_raw_candle_exports(
            symbol, instrument_key, summary["Sector"], combined, outcome
        )
        summary["Source_URL"] = combined.get("source_url", "")
    safe_print(f"PROGRESS {current}/{total} | {symbol} | candles={len(candles)} | outcome={outcome}")
    return summary


def is_equity_symbol(value):
    """Exclude F&O derivative contract rows and only process real cash-market equity symbols."""
    text = clean_text(value)
    if not text:
        return False
    if any(token in text.upper() for token in [" CE ", " PE ", " FUT", " OPT", " MIDCPNIFTY", " SENSEX "]):
        return False
    if re.search(r"\b\d{2}\s+[A-Z]{3}\s+\d{2}\b", text, flags=re.I):
        return False
    return True


def main():
    safe_print("Loading stock universe...")
    try:
        df = pd.read_csv(INPUT_CSV)
    except FileNotFoundError:
        safe_print(f"Error: {INPUT_CSV} not found. Please ensure it is in the same folder.")
        return 1
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        safe_print(f"Error: could not read {INPUT_CSV}: {exc}")
        return 1
    if "symbol" not in df.columns:
        safe_print("Error: input CSV needs a symbol column and an instrument_key or ISIN (or a matching instrument master).")
        return 1
    if Path(INPUT_CSV).resolve() == Path(OUTPUT_CSV).resolve():
        safe_print("Error: INPUT_CSV and OUTPUT_CSV must be different files.")
        return 1

    safe_print(f"Input rows detected: {len(df)}")
    if UPSTOX_TOKEN in ("", "YOUR_UPSTOX_ACCESS_TOKEN_HERE"):
        safe_print("Upstream configuration error: UPSTOX_TOKEN is missing or still set to the placeholder value.")
        return 1
    if FUNDAMENTAL_STATEMENT_TYPE not in {"consolidated", "standalone"}:
        safe_print("Error: UPSTOX_STATEMENT_TYPE must be consolidated or standalone.")
        return 1

    filtered_df = df[df["symbol"].map(is_equity_symbol)].copy().reset_index(drop=True)
    safe_print(f"Filtered input universe from {len(df)} rows to {len(filtered_df)} equity rows.")

    if not FUNDAMENTALS_ONLY:
        RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
        RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)
        RAW_CSV_DIR.mkdir(parents=True, exist_ok=True)
    Path(OUTPUT_CSV).parent.mkdir(parents=True, exist_ok=True)
    counts = defaultdict(int)
    rows_written = 0
    interrupted = False
    safe_print(f"STARTING RUN: {len(filtered_df)} equity stocks")
    safe_print(f"Mode: {'fundamentals only' if FUNDAMENTALS_ONLY else 'fundamentals and candles'} | statement_type={FUNDAMENTAL_STATEMENT_TYPE}")
    # Flush after each stock: a long universe run retains its completed CSV rows.
    with open(OUTPUT_CSV, mode="w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        file.flush()
        try:
            for idx, row in filtered_df.iterrows():
                try:
                    item = process_stock_row(row, idx, len(filtered_df))
                except Exception as exc:
                    symbol = clean_text(row.get("symbol")) or "UNKNOWN"
                    message = f"{type(exc).__name__}: {exc}"
                    safe_print(f"ERROR | symbol={symbol} | row_index={idx} | {message}")
                    item = empty_summary_row(row)
                    item.update({"Status": "FAILED", "Fundamentals_Status": "FAILED", "Fundamentals_Error": message})
                writer.writerow(item)
                file.flush()
                rows_written += 1
                counts[item.get("Fundamentals_Status", "FAILED")] += 1
                if UPSTOX_AUTH_ERROR:
                    safe_print(f"STOPPED: {UPSTOX_AUTH_ERROR}")
                    break
        except KeyboardInterrupt:
            interrupted = True
            safe_print("Interrupted; completed stock exports and summary rows have been retained.")

    safe_print(f"\nProcessed {rows_written}/{len(filtered_df)} rows. Summary saved as {OUTPUT_CSV}")
    safe_print("Fundamentals results: " + ", ".join(f"{name}={count}" for name, count in sorted(counts.items())))
    if rows_written < len(filtered_df):
        safe_print(f"Unprocessed rows: {len(filtered_df) - rows_written}; fix the reported issue before rerunning those rows.")
    if not FUNDAMENTALS_ONLY:
        safe_print(f"Raw candle payloads saved under {RAW_DATA_DIR}")
    safe_print(f"Fundamental payloads saved under {FUNDAMENTAL_DIR}")
    return 130 if interrupted else 1 if UPSTOX_AUTH_ERROR else 0


if __name__ == "__main__":
    sys.exit(main())
