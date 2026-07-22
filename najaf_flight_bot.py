"""
Najaf Flight Availability Bot
------------------------------
Checks soltantravel.net every run for one-way flights on the configured
routes/dates. Every run sends one Telegram message per route+date with
the FULL current list of available flights (not just deltas), tagged:
  - 🆕 a flight appearing for the first time
  - 🔄 a previously-seen flight whose departure/arrival time changed
  - 🌙 a flight whose departure falls within
    HIGHLIGHT_WINDOW_START..HIGHLIGHT_WINDOW_END
A trailing "No longer available" section lists previously-seen flights
that dropped out of this run's results (cancelled/sold out). Price is
shown per flight but is not tracked or compared.

Run this on a schedule (cron, Task Scheduler, GitHub Actions, etc.) - see
the bottom of this file for a sample cron line.

API SHAPE (soltantravel.net migrated to a shared "Tourscope" white-label
platform on 2026-07-22; reverse-engineered from the new JS bundle and
confirmed against the live backend):
  1. POST https://api.tourscope.site/public/v1/flights/search
     JSON body: {"adults":1,"children":0,"infant":0,"cabin":"Economy",
     "tripType":"oneWay","legs":[{"origin":"NJF","destination":"MHD",
     "date":"2026-07-23"}]}  - origin/destination are IATA airport codes,
     not the old numeric buffer IDs. -> {"payload":{"searchId":"..."}}
  2. GET  https://api.tourscope.site/public/v1/flights/search/{searchId}/results?page=1
     Poll until payload.isComplete is true (has been observed completing
     on the very first poll). payload.flights is the result list;
     payload.pagination.totalPages tells you how many pages to fetch.
     Tenant (which site's inventory) is resolved server-side from the
     Origin/Referer header - no tenant ID needs to be sent explicitly.

SETUP REQUIRED BEFORE RUNNING:
1. Create a Telegram bot:
   - Open Telegram, search for "BotFather", send /newbot, follow prompts.
   - It gives you a token like "123456789:ABCdefGhIJKlmNoPQRstuVwxYZ"
   - Paste it into TELEGRAM_BOT_TOKEN below.
2. Get your chat ID:
   - Send any message to your new bot first (e.g. "hi").
   - Then visit this URL in your browser (replace TOKEN):
     https://api.telegram.org/botTOKEN/getUpdates
   - Look for "chat":{"id": 123456789 ...} in the JSON - that number is
     your TELEGRAM_CHAT_ID below.
3. Install the one dependency:  pip install requests --break-system-packages
"""

import requests
import json
import os
import sys
import time
from datetime import datetime

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ── CONFIG ────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

# Routes to check: each has its own list of dates to search. origin/
# destination are IATA airport codes (e.g. NJF = Al Najaf, MHD = Mashhad).
ROUTES = [
    {"label": "Mashhad → Al Najaf", "origin": "MHD", "destination": "NJF", "dates": ["2026-07-30", "2026-07-31", "2026-08-01"]},
]

# Departures inside this window get flagged/highlighted in the alert.
HIGHLIGHT_WINDOW_START = datetime(2026, 7, 30, 0, 0)
HIGHLIGHT_WINDOW_END = datetime(2026, 8, 1, 23, 59)

BASE_URL = "https://api.tourscope.site"
SEARCH_URL = f"{BASE_URL}/public/v1/flights/search"

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_flights.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_log.txt")

# How long to wait for a search to finish assembling results.
PROGRESS_MAX_ATTEMPTS = 30
PROGRESS_POLL_SECONDS = 2

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://soltantravel.net",
    "Referer": "https://soltantravel.net/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
}


# ── HELPERS ───────────────────────────────────────────────────────────────

def log(msg):
    # Network exceptions from the Telegram send can include the request
    # URL (bot<TOKEN>/sendMessage) in their string representation - never
    # let that reach the log file, which gets committed to the repo.
    if TELEGRAM_BOT_TOKEN and "PUT_YOUR" not in TELEGRAM_BOT_TOKEN:
        msg = msg.replace(TELEGRAM_BOT_TOKEN, "<redacted>")
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_seen():
    """Returns {flight_identity_key: {"departure": raw_time, "arrival": raw_time}}.
    Entries from an incompatible/older schema are dropped rather than
    migrated - those flights just get re-reported as new on the next run."""
    if not os.path.exists(SEEN_FILE):
        return {}
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and "departure" in v}


def save_seen(seen_dict):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_dict, f, ensure_ascii=False, indent=2, sort_keys=True)


def send_telegram(text):
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log("Telegram not configured yet - skipping send. Message would have been:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=30)
            if resp.status_code == 200:
                break
            log(f"Telegram send failed (attempt {attempt}): {resp.status_code} {resp.text}")
        except requests.RequestException as e:
            log(f"Telegram send error (attempt {attempt}): {e}")
        time.sleep(3)
    # Stay under Telegram's ~1 msg/sec per-chat rate limit so a burst of
    # alerts (e.g. the first run seeding many flights at once) doesn't
    # cause the connection issues that dropped messages in testing.
    time.sleep(1.5)


def wait_for_results(search_id):
    """Poll the results endpoint until payload.isComplete, then fetch every
    page and return the combined list of flight items."""
    url = f"{SEARCH_URL}/{search_id}/results"
    for attempt in range(PROGRESS_MAX_ATTEMPTS):
        try:
            resp = requests.get(url, params={"page": 1}, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            payload = resp.json().get("payload", {})
        except (requests.RequestException, ValueError) as e:
            log(f"  results poll failed (attempt {attempt}): {e}")
            payload = {}

        if payload.get("isComplete"):
            break
        time.sleep(PROGRESS_POLL_SECONDS)
    else:
        log("  gave up waiting for search results to finish assembling")
        return []

    all_flights = list(payload.get("flights", []))
    total_pages = payload.get("pagination", {}).get("totalPages", 1) or 1
    for page in range(2, total_pages + 1):
        try:
            resp = requests.get(url, params={"page": page}, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            all_flights.extend(resp.json().get("payload", {}).get("flights", []))
        except (requests.RequestException, ValueError) as e:
            log(f"  results fetch failed on page {page}: {e}")
            break

    return all_flights


def search_flights(origin, destination, date):
    """Runs a full search (initial POST, then polling) and returns the
    list of flight result items for this route/date."""
    body = {
        "adults": 1,
        "children": 0,
        "infant": 0,
        "cabin": "Economy",
        "tripType": "oneWay",
        "legs": [{"origin": origin, "destination": destination, "date": date}],
    }
    try:
        resp = requests.post(SEARCH_URL, json=body, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        result = resp.json()
    except requests.RequestException as e:
        log(f"Request failed for origin={origin} dest={destination} date={date}: {e}")
        return []
    except ValueError:
        log(f"Non-JSON response for origin={origin} dest={destination} date={date}")
        return []

    search_id = result.get("payload", {}).get("searchId")
    if not search_id:
        log(f"No searchId in response for origin={origin} dest={destination} date={date}: {result}")
        return []

    return wait_for_results(search_id)


def leg_info(item):
    return item["legs"][0]


def flight_identity_key(route_label, date, item):
    """Identifies a specific flight (route + date + airline + flight
    number), deliberately excluding departure/arrival time so a schedule
    change is detected as 'this flight changed time' rather than looking
    like an unrelated new flight. The result "id" field is scoped to the
    search itself and changes on every run even for the exact same
    flight, so it can't be used here."""
    info = leg_info(item)
    airline_code = info["airline"]["code"]
    flight_number = info["segments"][0]["flightNumber"]
    return "|".join([route_label, date, airline_code, flight_number])


def flight_times(item):
    info = leg_info(item)
    return {
        "departure": info["departure"],
        "arrival": info["arrival"],
    }


def parse_raw_time(raw_time):
    """Handle both the current ISO format ("2026-07-23T07:00:00") and an
    older space-separated one, in case the API is inconsistent again."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw_time, fmt)
        except ValueError:
            continue
    return None


def is_in_highlight_window(item):
    dt = parse_raw_time(leg_info(item)["departure"])
    if dt is None:
        return False
    return HIGHLIGHT_WINDOW_START <= dt <= HIGHLIGHT_WINDOW_END


def format_time(raw_time):
    dt = parse_raw_time(raw_time)
    return dt.strftime("%H:%M") if dt else raw_time


def flight_sort_key(item):
    dt = parse_raw_time(leg_info(item)["departure"])
    return dt or datetime.max


def flight_summary_line(item, status, old_times=None):
    """One compact line per flight for the full-listing message. status is
    "new", "changed", or "unchanged"."""
    info = leg_info(item)
    price = item["pricing"]["totalPrice"]
    currency = item["pricing"]["currency"]["symbol"]
    stops_text = info.get("stopsSummary") or ("Direct" if info.get("stops", 0) == 0 else f"{info['stops']} stop(s)")
    dep = format_time(info["departure"])
    arr = format_time(info["arrival"])
    airline = info["airline"]["code"]
    flight_no = info["segments"][0]["flightNumber"]

    tag = {"new": "🆕", "changed": "🔄"}.get(status, "")
    window = "🌙" if is_in_highlight_window(item) else ""
    prefix = "".join(p for p in (tag, window) if p)
    prefix = f"{prefix} " if prefix else ""

    changed_note = ""
    if status == "changed" and old_times:
        changed_note = f" (was {format_time(old_times['departure'])}→{format_time(old_times['arrival'])})"

    return f"{prefix}{dep}→{arr}{changed_note} | {airline} {flight_no} | {stops_text} | {currency}{price}"


def build_summary_message(route_label, date, entries, cancelled):
    """entries: list of (item, status, old_times). cancelled: list of
    (identity, old_times) for flights that dropped out of this run."""
    lines = [f"📋 {route_label} — {date} ({len(entries)} flight{'s' if len(entries) != 1 else ''})"]
    if not entries:
        lines.append("(none currently available)")
    for item, status, old_times in entries:
        try:
            lines.append(flight_summary_line(item, status, old_times))
        except (KeyError, IndexError, TypeError):
            lines.append(f"(couldn't parse a result: {json.dumps(item, ensure_ascii=False)[:200]})")

    if cancelled:
        lines.append("")
        lines.append("❌ No longer available:")
        for identity, old_times in cancelled:
            parts = identity.split("|", 3)
            airline_code, flight_number = parts[2], parts[3]
            lines.append(f"  {airline_code} {flight_number} (was {format_time(old_times['departure'])}→{format_time(old_times['arrival'])})")

    return "\n".join(lines)


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    seen = load_seen()
    updated_seen = dict(seen)

    for route in ROUTES:
        for date in route["dates"]:
            log(f"Checking {route['label']} on {date}...")
            data = search_flights(route["origin"], route["destination"], date)
            log(f"  -> items returned={len(data)}")

            entries = []
            seen_this_run = set()
            for item in data:
                try:
                    identity = flight_identity_key(route["label"], date, item)
                    times = flight_times(item)
                except (KeyError, IndexError, TypeError):
                    continue

                seen_this_run.add(identity)

                if identity not in seen:
                    status, old_times = "new", None
                    updated_seen[identity] = times
                elif seen[identity] != times:
                    status, old_times = "changed", seen[identity]
                    updated_seen[identity] = times
                else:
                    status, old_times = "unchanged", None

                entries.append((item, status, old_times))

            entries.sort(key=lambda e: flight_sort_key(e[0]))

            # Anything previously tracked for this exact route+date that
            # didn't show up in this run's results is gone - cancelled or
            # sold out.
            prefix = f"{route['label']}|{date}|"
            cancelled = []
            for identity in list(seen.keys()):
                if identity.startswith(prefix) and identity not in seen_this_run:
                    cancelled.append((identity, seen[identity]))
                    updated_seen.pop(identity, None)

            msg = build_summary_message(route["label"], date, entries, cancelled)
            log(msg)
            send_telegram(msg)

    save_seen(updated_seen)


if __name__ == "__main__":
    main()

# ─────────────────────────────────────────────────────────────────────────
# SAMPLE CRON LINE (runs every hour, on Linux/Mac):
#
#   0 * * * * /usr/bin/python3 /full/path/to/najaf_flight_bot.py
#
# On Windows, use Task Scheduler to run:
#   python C:\path\to\najaf_flight_bot.py
# every hour.
# ─────────────────────────────────────────────────────────────────────────
