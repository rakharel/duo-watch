#!/usr/bin/env python3
"""
iPhone Duo availability watcher (standard library only).

Reads the same store-pickup and delivery-estimate data that Apple's own website
shows, and alerts you (ntfy push, and optionally Twilio SMS) when something changes.

  python3 check_duo.py                 one normal check
  python3 check_duo.py --mode test     send a test alert and show what Apple returns
  python3 check_duo.py --mode discover list part numbers found on Apple's page
  python3 check_duo.py --loop 120      keep checking every 120 seconds (Mac / PC)
  python3 check_duo.py --selftest      offline test of the alert logic
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # very old Python
    ZoneInfo = None

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state.json")

BUY_URL = "https://www.apple.com/shop/buy-iphone/iphone-duo"
FULFILL_URL = "https://www.apple.com/shop/fulfillment-messages"
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)

DEFAULT_STATE = {
    "available": [],          # "part|store" keys that were available last time
    "delivery": {},           # last delivery estimate seen per part
    "errors": 0,              # consecutive failed runs
    "error_alerted": False,
    "last_alert_ts": 0,
    "last_heartbeat_day": "",
}


class CheckError(Exception):
    pass


# --------------------------------------------------------------------------- files

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


# --------------------------------------------------------------------------- Apple

def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def fetch_apple(zip_code, parts):
    params = [("pl", "true"), ("mts.0", "regular"), ("mts.1", "sticky"), ("location", zip_code)]
    params += [("parts.%d" % i, p) for i, p in enumerate(parts)]
    url = FULFILL_URL + "?" + urllib.parse.urlencode(params)
    try:
        _, text = http_get(url)
    except urllib.error.HTTPError as e:
        raise CheckError("Apple returned HTTP %s for ZIP %s" % (e.code, zip_code))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CheckError("Network problem for ZIP %s: %s" % (zip_code, e))
    try:
        data = json.loads(text)
    except ValueError:
        raise CheckError("Apple did not return JSON for ZIP %s (got %r)" % (zip_code, text[:100]))
    if not isinstance(data, dict) or "body" not in data:
        raise CheckError("Unexpected reply from Apple for ZIP %s (no 'body')" % zip_code)
    return data


def parse_pickup(data, parts, zip_code):
    """Return one dict per (store, part) with its pickup status."""
    out = []
    content = (data.get("body") or {}).get("content") or {}
    stores = (content.get("pickupMessage") or {}).get("stores") or []
    for s in stores:
        avail = s.get("partsAvailability") or {}
        for part in parts:
            info = avail.get(part)
            if not isinstance(info, dict):
                continue
            display = str(info.get("pickupDisplay") or "").lower()
            buyable = (info.get("buyability") or {}).get("isBuyable")
            is_available = display == "available" or (
                buyable is True and display not in ("unavailable", "ships-to-store", ""))
            out.append({
                "zip": zip_code,
                "store": s.get("storeName") or "Apple Store",
                "city": s.get("city") or "",
                "state": s.get("state") or "",
                "part": part,
                "display": display,
                "quote": info.get("storePickupQuote") or info.get("pickupSearchQuote") or "",
                "available": bool(is_available),
            })
    return out


def parse_delivery(data, parts):
    """Return {part: delivery estimate text or None}."""
    content = (data.get("body") or {}).get("content") or {}
    dm = content.get("deliveryMessage") or {}
    out = {}
    for part in parts:
        info = dm.get(part)
        if not isinstance(info, dict):
            out[part] = None
            continue
        quote = info.get("quote")
        if isinstance(quote, list) and quote:
            out[part] = str(quote[0])
        elif isinstance(quote, str) and quote:
            out[part] = quote
        else:
            opts = (info.get("regular") or {}).get("deliveryOptions") or []
            out[part] = str(opts[0].get("date")) if opts and opts[0].get("date") else None
    return out


# --------------------------------------------------------------------------- alerts

def send_ntfy(title, message, priority="default", tags="iphone", click=None):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(server + "/" + topic, data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True
    except (urllib.error.URLError, OSError) as e:
        print("  ntfy failed: %s" % e)
        return False


def send_sms(body):
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.environ.get("TWILIO_FROM", "").strip()
    to = os.environ.get("TWILIO_TO", "").strip()
    if not (sid and token and sender and to):
        return False
    url = "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % sid
    data = urllib.parse.urlencode({"From": sender, "To": to, "Body": body[:300]}).encode()
    auth = base64.b64encode(("%s:%s" % (sid, token)).encode()).decode()
    req = urllib.request.Request(url, data=data, headers={"Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True
    except (urllib.error.URLError, OSError) as e:
        print("  SMS failed: %s" % e)
        return False


def notify_real(title, message, priority="default", tags="iphone", click=None, sms=False):
    print("ALERT [%s] %s | %s" % (priority, title, message.replace("\n", " / ")))
    ok = send_ntfy(title, message, priority, tags, click)
    if sms:
        send_sms("%s: %s %s" % (title, message, click or ""))
    return ok


# --------------------------------------------------------------------------- main logic

def run_check(cfg, state, fetch, notify, now_ts, local_now):
    parts = list(cfg["parts"].keys())
    names = cfg["parts"]
    st = json.loads(json.dumps(dict(DEFAULT_STATE, **state)))

    results, delivery_now, failures, ok = [], {}, [], 0
    for z in cfg["zip_codes"]:
        try:
            data = fetch(z, parts)
        except CheckError as e:
            failures.append(str(e))
            continue
        ok += 1
        results += parse_pickup(data, parts, z)
        if not delivery_now:
            delivery_now = parse_delivery(data, parts)

    stamp = local_now.strftime("%Y-%m-%d %H:%M")

    # Everything failed: count it, and warn once if it keeps happening.
    if ok == 0:
        st["errors"] += 1
        print("[%s] FAILED (%d in a row): %s" % (stamp, st["errors"], "; ".join(failures)))
        if st["errors"] >= cfg.get("notify_on_error_after", 4) and not st["error_alerted"]:
            notify("Duo checker is failing", "; ".join(failures)[:300] + "\nIt will keep retrying.",
                   priority="default", tags="warning")
            st["error_alerted"] = True
        return st

    if st["error_alerted"]:
        notify("Duo checker recovered", "Checks are working again.", priority="low", tags="white_check_mark")
    st["errors"] = 0
    st["error_alerted"] = False

    # Pickup availability
    seen, avail = {}, {}
    for r in results:
        key = "%s|%s" % (r["part"], r["store"])
        seen[key] = r
        if r["available"]:
            avail[key] = r
    keys = sorted(avail.keys())
    new_keys = [k for k in keys if k not in st["available"]]
    renotify = cfg.get("renotify_minutes", 30) * 60
    if keys and (new_keys or now_ts - st["last_alert_ts"] >= renotify):
        lines = []
        for k in keys[:8]:
            r = avail[k]
            where = "%s%s" % (r["store"], (" (%s, %s)" % (r["city"], r["state"])) if r["city"] else "")
            lines.append("%s at %s - %s" % (names.get(r["part"], r["part"]), where, r["quote"] or "available"))
        notify("iPhone Duo AVAILABLE" if new_keys else "iPhone Duo still available",
               "\n".join(lines) + "\nTap to open Apple.",
               priority="urgent", tags="rotating_light,iphone", click=BUY_URL, sms=True)
        st["last_alert_ts"] = now_ts
    st["available"] = keys

    # Delivery estimate changes
    if cfg.get("watch_delivery", True):
        changes = []
        for part, quote in delivery_now.items():
            if not quote:
                continue
            old = st["delivery"].get(part)
            if old is not None and old != quote:
                changes.append("%s: %s -> %s" % (names.get(part, part), old, quote))
            st["delivery"][part] = quote
        if changes:
            notify("iPhone Duo delivery estimate changed", "\n".join(changes),
                   priority="high", tags="package", click=BUY_URL)

    # Once-a-day "still alive" message
    day = local_now.strftime("%Y-%m-%d")
    if cfg.get("heartbeat", True) and st["last_heartbeat_day"] != day \
            and local_now.hour >= cfg.get("heartbeat_hour", 8):
        deliv = "; ".join("%s: %s" % (names.get(p, p), q) for p, q in st["delivery"].items())
        msg = "Still watching. Stores seen: %d. Pickup available now: %d." % (
            len({r["store"] for r in results}), len(keys))
        if deliv:
            msg += "\nDelivery: " + deliv
        notify("Duo watch is running", msg, priority="min", tags="eyes")
        st["last_heartbeat_day"] = day

    print("[%s] zips ok %d/%d | stores seen %d | available %d | delivery %s" % (
        stamp, ok, len(cfg["zip_codes"]), len({r["store"] for r in results}), len(keys),
        json.dumps(st["delivery"])))
    return st


def local_now_for(cfg):
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(cfg.get("timezone", "America/New_York")))
        except Exception:
            pass
    return datetime.now()


def run_once(cfg):
    state = load_json(STATE_PATH, {})
    new_state = run_check(cfg, state, fetch_apple, notify_real, time.time(), local_now_for(cfg))
    if json.dumps(new_state, sort_keys=True) != json.dumps(state, sort_keys=True):
        save_json(STATE_PATH, new_state)


def mode_test(cfg):
    parts = list(cfg["parts"].keys())
    print("Sending a test notification...")
    sent = notify_real("Duo watch test", "If you can read this, alerts work.", priority="high", tags="tada")
    print("  notification sent: %s" % ("yes" if sent else "NO (is the NTFY_TOPIC secret set?)"))
    for z in cfg["zip_codes"]:
        print("\nAsking Apple about ZIP %s ..." % z)
        try:
            data = fetch_apple(z, parts)
        except CheckError as e:
            print("  PROBLEM: %s" % e)
            continue
        rows = parse_pickup(data, parts, z)
        print("  stores returned: %d" % len({r["store"] for r in rows}))
        for r in rows:
            print("   - %-28s %-34s %-12s %s" % (r["store"][:28], cfg["parts"].get(r["part"], r["part"])[:34],
                                                 r["display"] or "?", r["quote"]))
        if not rows:
            print("  (no store rows. Apple may hide pickup until its daily update, which is normal.)")
        print("  delivery estimates: %s" % json.dumps(parse_delivery(data, parts)))


def mode_discover():
    print("Fetching %s ..." % BUY_URL)
    try:
        _, text = http_get(BUY_URL)
    except Exception as e:
        print("Could not load the page: %s" % e)
        return
    found = sorted(set(re.findall(r"\b[A-Z0-9]{5}LL/A\b", text)))
    if found:
        print("Part numbers found on the page:")
        for p in found:
            print("  " + p)
    else:
        print("No part numbers found (the page may load them with JavaScript).")
        print("Add them by hand to config.json instead.")


# --------------------------------------------------------------------------- self test

def selftest():
    from datetime import timedelta
    cfg = {"zip_codes": ["00000"], "parts": {"P1": "Duo A", "P2": "Duo B"},
           "watch_delivery": True, "notify_on_error_after": 2, "renotify_minutes": 30,
           "heartbeat": True, "heartbeat_hour": 8}

    def payload(display, quote="Jan 25 - Jan 29"):
        return {"body": {"content": {
            "pickupMessage": {"stores": [{"storeName": "Test Store", "city": "Syracuse", "state": "NY",
                "partsAvailability": {"P1": {"pickupDisplay": display, "storePickupQuote": "Today"},
                                      "P2": {"pickupDisplay": "unavailable"}}}]},
            "deliveryMessage": {"P1": {"quote": [quote]}, "P2": {"quote": [quote]}}}}}

    sent = []
    notify = lambda title, msg, **kw: sent.append((title, kw.get("priority")))
    t0 = datetime(2026, 10, 27, 11, 0)

    def step(fetch, minutes, st):
        now = t0 + timedelta(minutes=minutes)
        return run_check(cfg, st, fetch, notify, now.timestamp(), now)

    def boom(z, parts):
        raise CheckError("boom")

    st = {}
    st = step(lambda z, p: payload("unavailable"), 0, st)
    assert [t for t, _ in sent] == ["Duo watch is running"], sent          # heartbeat only
    sent.clear()
    st = step(lambda z, p: payload("available"), 5, st)
    assert sent == [("iPhone Duo AVAILABLE", "urgent")], sent               # new availability
    sent.clear()
    st = step(lambda z, p: payload("available"), 10, st)
    assert sent == [], sent                                                  # no spam
    st = step(lambda z, p: payload("available"), 40, st)
    assert sent == [("iPhone Duo still available", "urgent")], sent          # reminder
    sent.clear()
    st = step(lambda z, p: payload("unavailable"), 45, st)
    st = step(lambda z, p: payload("unavailable", "Nov 3 - Nov 5"), 50, st)
    assert sent == [("iPhone Duo delivery estimate changed", "high")], sent  # delivery moved
    sent.clear()
    st = step(boom, 55, st)
    st = step(boom, 60, st)
    st = step(boom, 65, st)
    assert sent == [("Duo checker is failing", "default")], sent             # error once
    sent.clear()
    st = step(lambda z, p: payload("unavailable", "Nov 3 - Nov 5"), 70, st)
    assert sent == [("Duo checker recovered", "low")], sent
    print("selftest passed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="check", choices=["check", "test", "discover"])
    ap.add_argument("--loop", type=int, default=0, help="repeat every N seconds")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit("config.json is missing or invalid")
    if args.mode == "discover":
        mode_discover()
        return
    if args.mode == "test":
        mode_test(cfg)
        return
    if args.loop:
        while True:
            try:
                run_once(cfg)
            except Exception as e:  # keep the loop alive
                print("Unexpected error: %s" % e)
            time.sleep(max(30, args.loop))
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
