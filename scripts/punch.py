#!/usr/bin/env python3
"""
Auto check-in / check-out for the HR-Orbit attendance portal.

Flow:
  1. Decide whether we should run today (config gating).
  2. Pick a RANDOM time inside the action's window and sleep until then.
  3. Log in freshly (username+password from env) to get a JWT.
  4. Punch in or out with the current IST time.

Nothing sensitive is stored: the token is fetched fresh each run.

Usage:
  python scripts/punch.py --action in
  python scripts/punch.py --action out --dry-run
  python scripts/punch.py --action in --no-sleep      # punch immediately (testing)
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

GRACE = timedelta(minutes=15)  # if GitHub delays the run past window end, still punch within this grace


def log(msg):
    print(f"[{datetime.now(ZoneInfo('Asia/Kolkata')):%Y-%m-%d %H:%M:%S %Z}] {msg}", flush=True)


def mask(token):
    if not token:
        return "<none>"
    return f"{token[:8]}…{token[-6:]} (len={len(token)})"


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_hhmm(day, hhmm, tz):
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=tz)


def should_run_today(cfg, now):
    """Return (ok: bool, reason: str) based on config gating."""
    if not cfg.get("enabled", True):
        return False, "disabled via config.enabled=false"

    today = now.date().isoformat()
    only = cfg.get("only_dates") or []
    skip = cfg.get("skip_dates") or []

    # only_dates wins: if set, run ONLY on those dates (ignores weekends).
    if only:
        if today in only:
            return True, f"{today} is in only_dates"
        return False, f"{today} not in only_dates {only}"

    if today in skip:
        return False, f"{today} is in skip_dates"

    if cfg.get("skip_weekends", True) and now.weekday() >= 5:  # 5=Sat, 6=Sun
        return False, f"{today} is a weekend (skip_weekends=true)"

    return True, "no gating rule blocked it"


def compute_sleep(now, window_start, window_end):
    """
    Pick a random target inside [max(now, window_start), window_end] and
    return (target, seconds_to_sleep) or (None, None) if the window is missed.
    """
    earliest = max(now, window_start)
    if now > window_end + GRACE:
        return None, None  # too late, bail
    if earliest > window_end:
        earliest = window_end  # delayed but within grace -> punch ~now
    span = max(0, int((window_end - earliest).total_seconds()))
    target = earliest + timedelta(seconds=random.randint(0, span))
    return target, max(0, (target - now).total_seconds())


def login(session, cfg, username, password):
    url = cfg["base_url"] + cfg["endpoints"]["login"]["path"]
    origin = cfg["base_url"].split("/backend")[0]
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": origin,
        "Referer": origin + "/login",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    }
    # multipart/form-data fields (requests builds the boundary automatically)
    files = {"username": (None, username), "password": (None, password)}
    r = session.post(url, headers=headers, files=files, timeout=30)
    log(f"login -> HTTP {r.status_code}")
    if r.status_code >= 400:
        log(f"login response (first 300 chars): {r.text[:300]}")
        r.raise_for_status()

    # Find the JWT: try JSON body keys, then fall back to the 'jwt' cookie.
    token = None
    try:
        body = r.json()
    except Exception:
        body = {}
    candidates = [body] if isinstance(body, dict) else []
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        candidates.append(body["data"])
    for obj in candidates:
        for key in ("access_token", "token", "jwt", "accessToken", "accessToken"):
            if obj.get(key):
                token = obj[key]
                log(f"token source: response body['{key}']")
                break
        if token:
            break
    if not token:
        token = session.cookies.get("jwt")
        if token:
            log("token source: 'jwt' cookie")

    if not token:
        log(f"Could NOT find a token. Login body was: {json.dumps(body)[:400]}")
        raise SystemExit("Login succeeded but no token found — inspect the body above and "
                         "adjust the token-key list in login().")

    log(f"got token {mask(token)}")
    return token


def punch(session, cfg, action, token, now):
    ep = cfg["endpoints"]["checkin" if action == "in" else "checkout"]
    url = cfg["base_url"] + ep["path"]
    method = ep["method"].upper()
    origin = cfg["base_url"].split("/backend")[0]

    # Make the token available both ways the portal expects.
    session.cookies.set("jwt", token)
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": origin,
        "Referer": origin + "/dashboard",
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
    }

    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    if action == "in":
        payload = {
            "date": date_str,
            "checkInTime": time_str,
            "workLocation": cfg.get("work_location", "Office"),
            "workOffice": cfg.get("work_office", ""),
        }
    else:
        payload = {"date": date_str, "checkOutTime": time_str}

    log(f"{action.upper()} -> {method} {url}  payload={payload}")
    r = session.request(method, url, headers=headers, json=payload, timeout=30)
    log(f"{action.upper()} -> HTTP {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    log(f"✅ {action.upper()} done at {time_str} IST")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", required=True, choices=["in", "out"])
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true", help="Plan only; never call the API")
    ap.add_argument("--no-sleep", action="store_true", help="Skip the random wait and punch now")
    args = ap.parse_args()

    cfg = load_config(args.config)
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    log(f"action={args.action}  now={now:%Y-%m-%d %H:%M:%S %Z}  weekday={now.strftime('%A')}")

    ok, reason = should_run_today(cfg, now)
    log(f"gating: {'RUN' if ok else 'SKIP'} — {reason}")
    if not ok:
        return

    win = cfg["checkin_window"] if args.action == "in" else cfg["checkout_window"]
    start = parse_hhmm(now.date(), win["start"], tz)
    end = parse_hhmm(now.date(), win["end"], tz)

    if args.no_sleep:
        target, sleep_s = now, 0
    else:
        target, sleep_s = compute_sleep(now, start, end)
        if target is None:
            log(f"window {win['start']}-{win['end']} already missed (now {now:%H:%M}). Bailing.")
            return

    log(f"window {win['start']}-{win['end']} IST; target {target:%H:%M:%S}; sleeping {int(sleep_s)}s")

    if args.dry_run:
        log("DRY RUN — stopping before login/punch.")
        return

    username = os.environ.get("HRORBIT_USERNAME")
    password = os.environ.get("HRORBIT_PASSWORD")
    if not username or not password:
        raise SystemExit("Missing HRORBIT_USERNAME / HRORBIT_PASSWORD environment variables.")

    if sleep_s > 0:
        time.sleep(sleep_s)

    punch_now = datetime.now(tz)  # real time at the moment we punch
    session = requests.Session()
    token = login(session, cfg, username, password)
    punch(session, cfg, args.action, token, punch_now)


if __name__ == "__main__":
    main()
