"""
Najaf Flight Availability Bot
------------------------------
Checks soltantravel.net every run for newly-available one-way flights on:
  - Mashhad  -> Al Najaf
  - Tehran   -> Al Najaf
for a fixed list of dates. Sends a Telegram message the first time a new
flight option appears.

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

# Routes to check: (label, origin_id, destination_id)
ROUTES = [
    {"label": "Mashhad → Al Najaf", "origin": 7280, "destination": 1597},
    {"label": "Tehran → Al Najaf", "origin": 255, "destination": 1597},
]

# Dates to check, one search per date per route
DATES = ["2026-07-30", "2026-07-31", "2026-08-01"]

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
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_set):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_set), f, ensure_ascii=False, indent=2)


def send_telegram(text):
    if "PUT_YOUR" in TELEGRAM_BOT_TOKEN or "PUT_YOUR" in TELEGRAM_CHAT_ID:
        log("Telegram not configured yet - skipping send. Message would have been:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        if resp.status_code != 200:
            log(f"Telegram send failed: {resp.status_code} {resp.text}")
    except requests.RequestException as e:
        log(f"Telegram send error: {e}")


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


def flight_key(item):
    """Unique fingerprint for a flight result, used to detect 'new' entries.
    flightBufferReferenceId is scoped to the search session and changes on
    every run even for the exact same flight, so it can't be used for
    dedup. Key on the fields that actually identify a flight offer instead."""
    try:
        info = item["serviceInfo"]["legs"][0]["info"]
        return "|".join([
            info["airline"]["abb"],
            info["flight_number"],
            info["departure"]["raw_time"],
            info["arrival"]["raw_time"],
            str(item["priceInfo"]["payable"]),
        ])
    except (KeyError, IndexError, TypeError):
        return json.dumps(item, sort_keys=True, ensure_ascii=False)


def describe_flight(item, route_label, date):
    """Human-readable summary of a flight result item."""
    try:
        info = item["serviceInfo"]["legs"][0]["info"]
        price = item["priceInfo"]["payable"]
        currency = item["priceInfo"]["currency"]["symbol"]
        stops = info.get("connections", 0)
        stops_text = "Direct" if stops == 0 else f"{stops} stop(s)"
        parts = [
            f"Route: {route_label}",
            f"Date: {date}",
            f"Airline: {info['airline']['title']} ({info['airline']['abb']})",
            f"Flight: {info['flight_number']}",
            f"Departs: {info['departure']['date_time']}",
            f"Arrives: {info['arrival']['date_time']}",
            f"Duration: {info.get('duration', '?')} | {stops_text}",
            f"Price: {currency}{price}",
        ]
        return "\n".join(parts)
    except (KeyError, IndexError, TypeError):
        return f"Route: {route_label}\nDate: {date}\n{json.dumps(item, ensure_ascii=False)[:500]}"


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    seen = load_seen()
    new_seen = set(seen)
    any_new = False

    for route in ROUTES:
        for date in DATES:
            log(f"Checking {route['label']} on {date}...")
            data = search_flights(route["origin"], route["destination"], date)
            log(f"  -> items returned={len(data)}")

            for item in data:
                key = flight_key(item)
                if key not in seen:
                    any_new = True
                    new_seen.add(key)
                    msg = "✈️ New flight option found!\n\n" + describe_flight(item, route["label"], date)
                    log("New flight found:\n" + msg)
                    send_telegram(msg)

    save_seen(new_seen)

    if not any_new:
        log("No new flights this run.")


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
