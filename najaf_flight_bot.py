"""
Najaf Flight Availability Bot
------------------------------
Checks soltantravel.net every run for one-way flights on the configured
routes/dates. Sends a Telegram message when:
  - a flight option appears for the first time (highlighted if its
    departure falls within HIGHLIGHT_WINDOW_START..HIGHLIGHT_WINDOW_END)
  - a previously-seen flight's departure/arrival time changes
  - a previously-seen flight disappears from results (cancelled/sold out)
Price is shown as one of the details but is not tracked or compared.

Run this on a schedule (cron, Task Scheduler, GitHub Actions, etc.) - see
the bottom of this file for a sample cron line.

API SHAPE (reverse-engineered from soltantravel.net's own JS bundle and
confirmed against the live backend):
  1. POST /v2/search/flight   -> either {"data": [], ...} directly (zero
     results, returned synchronously) or {"sessionId": "..."} (results are
     being assembled server-side and must be polled for).
  2. GET  /v1/search/progress?pid=<pid>&sessionId=<id> -> {"percent": 0-100}
     Poll this until percent reaches 100.
  3. GET  /v1/search/results?pid=<pid>&sessionId=<id>&page=<n> -> the same
     {"data": [...], "pages": {...}} shape as a synchronous response.

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
import random
import string
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

# Routes to check: each has its own list of dates to search.
ROUTES = [
    {"label": "Al Najaf → Mashhad", "origin": 1597, "destination": 7280, "dates": ["2026-07-23", "2026-07-24"]},
]

# Departures inside this window get flagged/highlighted in the alert.
# Spans midnight: from 19:00 on the 23rd through 13:00 on the 24th.
HIGHLIGHT_WINDOW_START = datetime(2026, 7, 23, 19, 0)
HIGHLIGHT_WINDOW_END = datetime(2026, 7, 24, 13, 0)

BASE_URL = "https://marketplace.soltantravel.net"
SEARCH_URL = f"{BASE_URL}/v2/search/flight"
PROGRESS_URL = f"{BASE_URL}/v1/search/progress"
RESULTS_URL = f"{BASE_URL}/v1/search/results"

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_flights.json")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_log.txt")

# How long to wait for a search session to finish assembling results.
PROGRESS_MAX_ATTEMPTS = 30
PROGRESS_POLL_SECONDS = 2

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://soltantravel.net",
    "Referer": "https://soltantravel.net/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
}


# ── HELPERS ───────────────────────────────────────────────────────────────

def random_id(length=11):
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


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
    Older schema versions (price-keyed dict, or a flat list) don't carry
    timing history, so their entries are dropped rather than migrated -
    those flights will just be re-reported as new on the next run."""
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


def wait_for_results(pid, session_id):
    """Poll /v1/search/progress until the session's results are ready, then
    fetch every page from /v1/search/results and return the combined list
    of flight items."""
    for attempt in range(PROGRESS_MAX_ATTEMPTS):
        try:
            resp = requests.get(
                PROGRESS_URL, params={"pid": pid, "sessionId": session_id},
                headers=HEADERS, timeout=15,
            )
            resp.raise_for_status()
            percent = resp.json().get("percent", 0)
        except (requests.RequestException, ValueError) as e:
            log(f"  progress poll failed (attempt {attempt}): {e}")
            percent = None

        if percent == 100:
            break
        time.sleep(PROGRESS_POLL_SECONDS)
    else:
        log("  gave up waiting for search results to finish assembling")
        return []

    all_items = []
    page = 1
    page_count = 1
    while page <= page_count:
        try:
            resp = requests.get(
                RESULTS_URL, params={"pid": pid, "sessionId": session_id, "page": page},
                headers=HEADERS, timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()
        except (requests.RequestException, ValueError) as e:
            log(f"  results fetch failed on page {page}: {e}")
            break

        all_items.extend(result.get("data", []))
        page_count = result.get("pages", {}).get("pageCount", 1) or 1
        page += 1

    return all_items


def search_flights(origin, destination, date):
    """Runs a full search (initial POST, then polling if needed) and
    returns the list of flight result items for this route/date."""
    pid = random_id()
    searcher_identity = random_id()
    params = {"pid": pid, "lang": "AR", "currency": 158}
    form = {
        "adults": "1",
        "children": "0",
        "infants": "0",
        "cabin": "economy",
        "tripType": "oneWay",
        "searcherIdentity": searcher_identity,
        "legs[0][origin]": str(origin),
        "legs[0][destination]": str(destination),
        "legs[0][departure]": date,
    }
    try:
        resp = requests.post(SEARCH_URL, params=params, data=form, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        result = resp.json()
    except requests.RequestException as e:
        log(f"Request failed for origin={origin} dest={destination} date={date}: {e}")
        return []
    except ValueError:
        log(f"Non-JSON response for origin={origin} dest={destination} date={date}")
        return []

    session_id = result.get("sessionId")
    if session_id:
        return wait_for_results(pid, session_id)

    return result.get("data", [])


def flight_identity_key(route_label, date, item):
    """Identifies a specific flight (route + date + airline + flight
    number), deliberately excluding departure/arrival time so a schedule
    change is detected as 'this flight changed time' rather than looking
    like an unrelated new flight. flightBufferReferenceId is scoped to
    the search session and changes on every run even for the exact same
    flight, so it can't be used here."""
    info = item["serviceInfo"]["legs"][0]["info"]
    return "|".join([route_label, date, info["airline"]["abb"], info["flight_number"]])


def flight_times(item):
    info = item["serviceInfo"]["legs"][0]["info"]
    return {
        "departure": info["departure"]["raw_time"],
        "arrival": info["arrival"]["raw_time"],
    }


def parse_raw_time(raw_time):
    """The API has been observed returning raw_time in two different
    formats ("2026-07-23T07:00:00" and "2026-07-23 07:00") across
    otherwise-identical searches - handle both."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw_time, fmt)
        except ValueError:
            continue
    return None


def is_in_highlight_window(item):
    info = item["serviceInfo"]["legs"][0]["info"]
    dt = parse_raw_time(info["departure"]["raw_time"])
    if dt is None:
        return False
    return HIGHLIGHT_WINDOW_START <= dt <= HIGHLIGHT_WINDOW_END


def late_departure_banner(item):
    if not is_in_highlight_window(item):
        return ""
    start = HIGHLIGHT_WINDOW_START.strftime("%b %d %H:%M")
    end = HIGHLIGHT_WINDOW_END.strftime("%b %d %H:%M")
    return f"🌙 IN YOUR PREFERRED WINDOW ({start} – {end}) 🌙\n\n"


def flight_detail_lines(item, route_label, date):
    """Human-readable detail lines shared by every alert type."""
    info = item["serviceInfo"]["legs"][0]["info"]
    price = item["priceInfo"]["payable"]
    currency = item["priceInfo"]["currency"]["symbol"]
    stops = info.get("connections", 0)
    stops_text = "Direct" if stops == 0 else f"{stops} stop(s)"
    return [
        f"Route: {route_label}",
        f"Date: {date}",
        f"Airline: {info['airline']['title']} ({info['airline']['abb']})",
        f"Flight: {info['flight_number']}",
        f"Departs: {info['departure']['date_time']}",
        f"Arrives: {info['arrival']['date_time']}",
        f"Duration: {info.get('duration', '?')} | {stops_text}",
        f"Price: {currency}{price}",
    ]


def describe_new_flight(item, route_label, date):
    try:
        lines = flight_detail_lines(item, route_label, date)
        return late_departure_banner(item) + "✈️ New flight option found!\n\n" + "\n".join(lines)
    except (KeyError, IndexError, TypeError):
        return f"✈️ New flight option found!\n\nRoute: {route_label}\nDate: {date}\n{json.dumps(item, ensure_ascii=False)[:500]}"


def describe_timing_change(item, route_label, date, old_times):
    try:
        lines = flight_detail_lines(item, route_label, date)
        header = (
            f"🔄 Flight timing changed!\n\n"
            f"Was: departs {old_times['departure']}, arrives {old_times['arrival']}\n"
            f"Now: departs {item['serviceInfo']['legs'][0]['info']['departure']['raw_time']}, "
            f"arrives {item['serviceInfo']['legs'][0]['info']['arrival']['raw_time']}\n\n"
        )
        return late_departure_banner(item) + header + "\n".join(lines)
    except (KeyError, IndexError, TypeError):
        return f"🔄 Flight timing changed!\n\nRoute: {route_label}\nDate: {date}\n{json.dumps(item, ensure_ascii=False)[:500]}"


def describe_cancelled(identity, old_times):
    _route_label, date, airline_abb, flight_number = identity.split("|", 3)
    return (
        f"❌ Flight no longer available (cancelled or sold out)!\n\n"
        f"Date: {date}\n"
        f"Airline: {airline_abb}\n"
        f"Flight: {flight_number}\n"
        f"Was: departs {old_times['departure']}, arrives {old_times['arrival']}"
    )


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    seen = load_seen()
    updated_seen = dict(seen)
    any_change = False

    for route in ROUTES:
        for date in route["dates"]:
            log(f"Checking {route['label']} on {date}...")
            data = search_flights(route["origin"], route["destination"], date)
            log(f"  -> items returned={len(data)}")

            seen_this_run = set()
            for item in data:
                try:
                    identity = flight_identity_key(route["label"], date, item)
                    times = flight_times(item)
                except (KeyError, IndexError, TypeError):
                    continue

                seen_this_run.add(identity)

                if identity not in seen:
                    any_change = True
                    updated_seen[identity] = times
                    msg = describe_new_flight(item, route["label"], date)
                    log("New flight found:\n" + msg)
                    send_telegram(msg)
                elif seen[identity] != times:
                    any_change = True
                    updated_seen[identity] = times
                    msg = describe_timing_change(item, route["label"], date, seen[identity])
                    log("Timing change found:\n" + msg)
                    send_telegram(msg)

            # Anything previously tracked for this exact route+date that
            # didn't show up in this run's results is gone - cancelled or
            # sold out. Flag it once, then drop it so it doesn't repeat.
            prefix = f"{route['label']}|{date}|"
            for identity in list(seen.keys()):
                if identity.startswith(prefix) and identity not in seen_this_run:
                    any_change = True
                    msg = describe_cancelled(identity, seen[identity])
                    log("Cancelled flight found:\n" + msg)
                    send_telegram(msg)
                    updated_seen.pop(identity, None)

    save_seen(updated_seen)

    if not any_change:
        msg = "No new flights, timing changes, or cancellations this run."
        log(msg)
        send_telegram(msg)


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
