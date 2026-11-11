"""Watch Google Flights for cheap Victoria (YYJ) <-> Calgary (YYC) weekend trips.

For every weekend until END_DATE and both directions (either person visiting
the other) it checks:
  * round-trip fares for every Thu/Fri/Sat -> Sun/Mon combination
  * one-way fares for every day Thu..Mon, combined into two-one-way trips
    (often cheaper, and lets you mix airlines)
and emails when the cheapest trip is under MAX_PRICE (CAD).
"""

import json
import os
import random
import smtplib
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html
from fast_flights.parser import parse_js
from selectolax.lexbor import LexborHTMLParser

# ---- config -----------------------------------------------------------------
A, B = "YYJ", "YYC"
CITY = {"YYJ": "Victoria", "YYC": "Calgary"}
MAX_PRICE = int(os.environ.get("MAX_PRICE") or 200)  # CAD, whole trip, 1 adult
NEAR_PRICE = int(os.environ.get("NEAR_PRICE") or 250)  # quieter "almost" alert below this
END_DATE = date.fromisoformat(os.environ.get("END_DATE") or "2026-12-13")  # last Sunday
OUT_DAYS = (3, 4, 5)  # leave Thu, Fri, Sat   (Mon=0 ... Sun=6)
RET_DAYS = (6, 0)  # come back Sun, Mon
REALERT_DROP = 10  # re-alert an already-emailed trip if it gets this much cheaper
DIGEST_HOUR = 8  # daily "still working" summary email on first run after this hour (PT)
DIGEST = (os.environ.get("DIGEST") or "1") != "0"
WORKERS = 4
MAX_FAIL_RATE = 0.2  # more failed searches than this = broken
MAX_EMPTY_RATE = 0.5  # more "no flights" pages than this = Google changed something
STATE_FILE = Path(__file__).with_name("alerted.json")
TZ = ZoneInfo("America/Vancouver")
# -----------------------------------------------------------------------------


def weekends(today: date):
    """Yield (list of (out, ret) pairs) per weekend from today until END_DATE."""
    sunday = today + timedelta(days=(6 - today.weekday()) % 7)
    while sunday <= END_DATE:
        pairs = []
        for o in OUT_DAYS:
            out = sunday - timedelta(days=6 - o)
            for r in RET_DAYS:
                ret = sunday + timedelta(days=1) if r == 0 else sunday
                if out >= today:
                    pairs.append((out, ret))
        if pairs:
            yield pairs
        sunday += timedelta(days=7)


def make_query(legs, trip):
    return create_query(
        flights=[FlightQuery(date=d.isoformat(), from_airport=o, to_airport=t) for o, t, d in legs],
        trip=trip,
        currency="CAD",
        language="en",
        passengers=Passengers(adults=1),
    )


def parse_all(html: str):
    """Parse BOTH Google result lists ("top flights" [2] and "other flights" [3]).

    fast-flights 3.1.0 only reads list [3] (so it misses the top/cheapest
    flights) and crashes on listings without a price; this handles both.
    """
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise RuntimeError("no flight data in page (blocked or layout changed)")
    data = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        return []  # Google: no flights for this search
    payload = json.loads(data)
    entries = []
    for idx in (2, 3):
        section = payload[idx] if len(payload) > idx else None
        if isinstance(section, list) and section and isinstance(section[0], list):
            entries += section[0]
    priced = [
        k for k in entries
        if isinstance(k, list) and len(k) > 1 and isinstance(k[1], list) and k[1]
        and isinstance(k[1][0], list) and len(k[1][0]) > 1 and isinstance(k[1][0][1], (int, float))
    ]
    payload[3] = [priced] + list(payload[3][1:] if isinstance(payload[3], list) else [])
    return list(parse_js("data:" + json.dumps(payload) + ", sideChannel: {}"))


def fetch(job):
    """job = (kind, legs). Returns (job, url, cheapest flight | None)."""
    kind, legs = job
    q = make_query(legs, kind)
    err = None
    for attempt in range(4):
        time.sleep(random.uniform(0.5, 2.0))
        try:
            flights = parse_all(fetch_flights_html(q))
            return job, q.url(), (min(flights, key=lambda f: f.price) if flights else None)
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(4 * (attempt + 1) + random.uniform(0, 3))
    raise RuntimeError(f"{kind} {legs}: {err!r}")


# ---- wording -------------------------------------------------------------------
PLACES = {**CITY, "YVR": "Vancouver", "YLW": "Kelowna", "YEG": "Edmonton", "YXX": "Abbotsford",
          "YXS": "Prince George", "YKA": "Kamloops", "SEA": "Seattle", "PDX": "Portland"}


def clock(t) -> str:
    h, m = t
    return f"{h % 12 or 12}:{m:02d} {'am' if h < 12 else 'pm'}"


def day(d: date) -> str:
    return f"{d:%a %b} {d.day}"  # Fri Dec 4


def span(o: date, r: date) -> str:
    return f"{o:%b} {o.day}–{r.day}" if o.month == r.month else f"{o:%b} {o.day} – {r:%b} {r.day}"


def flight(f) -> str:
    """WestJet, nonstop, 7:05 pm  /  Air Canada, via Vancouver, 6:45 am"""
    stops = [PLACES.get(s.to_airport.code, s.to_airport.code) for s in f.flights[:-1]]
    route = "nonstop" if not stops else "via " + " and ".join(stops)
    return f"{' and '.join(f.airlines)}, {route}, {clock(f.flights[0].departure.time)}"


def short(opt) -> str:
    """One line for logs and the Actions summary."""
    if opt["kind"] == "rt":
        return f"round trip; out {flight(opt['f'])}"
    return f"two one-ways; out {flight(opt['out'][1])}; back {flight(opt['back'][1])}"


def deal_message(deal):
    """(title, body, links) for one trip."""
    h, a, o, r, p, opt = deal
    title = f"${p} round trip, {CITY[h]} to {CITY[a]}, {span(o, r)}"
    if opt["kind"] == "rt":
        body = [f"Leave {day(o)}: {flight(opt['f'])}.",
                f"Return {day(r)}: pick a flight in Google Flights."]
        links = [("Open in Google Flights", opt["url"])]
    else:
        (u1, f1), (u2, f2) = opt["out"], opt["back"]
        body = [f"Leave {day(o)}: {flight(f1)}. ${f1.price}.",
                f"Return {day(r)}: {flight(f2)}. ${f2.price}.",
                "Two one-way tickets, booked separately."]
        links = [("Outbound flight", u1), ("Return flight", u2)]
    return title, body, links


def notify(subject: str, body: str, priority: int = 3, links=()) -> bool:
    """Push via ntfy (if NTFY_TOPIC set) and email (if SMTP set). True if anything was sent."""
    sent = False
    mail = body + ("\n\n" + "\n".join(f"{label}: {url}" for label, url in links) if links else "")
    for fn in (lambda: send_ntfy(subject, body, priority, links), lambda: send_email(subject, mail)):
        try:
            sent = fn() or sent
        except Exception as e:  # noqa: BLE001
            print(f"!! notification failed: {e!r}")
    if not sent:
        print(f"!! no notification channel worked — would have sent: {subject}")
    return sent


def send_ntfy(title: str, body: str, priority: int, links) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    msg = {"topic": topic, "title": title, "message": body[:3900], "priority": priority}
    if links:
        msg["click"] = links[0][1]
        msg["actions"] = [{"action": "view", "label": label, "url": url} for label, url in links[:3]]
    req = urllib.request.Request(os.environ.get("NTFY_SERVER") or "https://ntfy.sh",
                                 data=json.dumps(msg).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            print(f"ntfy sent: {title}")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"ntfy attempt {attempt + 1} failed: {e!r}")
            time.sleep(5)
    return False


def send_email(subject: str, body: str) -> bool:
    user, pw, to = (os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "EMAIL_TO"))
    if not (user and pw and to):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    for attempt in range(3):
        try:
            with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST") or "smtp.gmail.com", 465, timeout=30) as s:
                s.login(user, pw)
                s.send_message(msg)
            print(f"Email sent to {to}: {subject}")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"email attempt {attempt + 1} failed: {e!r}")
            time.sleep(10)
    raise RuntimeError("could not send email")


sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    now = datetime.now(TZ)
    today = now.date()
    wknds = list(weekends(today))
    if not wknds:
        print(f"No weekends left before {END_DATE}. Nothing to do — disable the workflow.")
        return 0

    # ---- build every search ----
    trips = []  # (traveller_home, away, out, ret)
    jobs = set()
    for pairs in wknds:
        for home, away in ((A, B), (B, A)):
            for out, ret in pairs:
                trips.append((home, away, out, ret))
                jobs.add(("round-trip", ((home, away, out), (away, home, ret))))
                jobs.add(("one-way", ((home, away, out),)))
                jobs.add(("one-way", ((away, home, ret),)))
    jobs = sorted(jobs)
    print(f"{len(trips)} trips, {len(jobs)} searches")

    results, failed = {}, []
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(fetch, j): j for j in jobs}
        for fut, j in futs.items():
            try:
                _, url, best = fut.result()
                results[j] = (url, best)
            except Exception as e:  # noqa: BLE001
                failed.append(j)
                print(f"ERR {e}")
    if failed:  # one more slow serial pass for anything that failed
        print(f"Retrying {len(failed)} failed searches serially...")
        still = []
        for j in failed:
            try:
                _, url, best = fetch(j)
                results[j] = (url, best)
            except Exception as e:  # noqa: BLE001
                still.append(j)
                print(f"ERR (final) {e}")
        failed = still

    # ---- best option per trip ----
    table = []  # (home, away, out, ret, price | None, opt | None, complete)
    for home, away, out, ret in trips:
        options = []
        rt = results.get(("round-trip", ((home, away, out), (away, home, ret))))
        o1 = results.get(("one-way", ((home, away, out),)))
        o2 = results.get(("one-way", ((away, home, ret),)))
        complete = None not in (rt, o1, o2)
        if rt and rt[1]:
            options.append((rt[1].price, {"kind": "rt", "url": rt[0], "f": rt[1]}))
        if o1 and o1[1] and o2 and o2[1]:
            options.append((o1[1].price + o2[1].price, {"kind": "ow", "out": o1, "back": o2}))
        price, opt = min(options, key=lambda x: x[0]) if options else (None, None)
        table.append((home, away, out, ret, price, opt, complete))

    for home, away, out, ret, price, opt, complete in table:
        p = f"${price:>4}" if price is not None else "  n/a"
        print(f"{p}  {home}-{away} {out} to {ret}  {short(opt) if opt else ''}"
              f"{'' if complete else '  (partial data)'}")

    # ---- health ----
    total, nfail = len(jobs), len(failed)
    nempty = sum(1 for url, best in results.values() if best is None)
    broken = nfail / total > MAX_FAIL_RATE or nempty / total > MAX_EMPTY_RATE

    # ---- alerts ----
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    alerted = state.setdefault("alerted", {})  # trips we sent a deal alert for
    near_sent = state.setdefault("near", {})  # trips we sent an "almost" alert for
    deals, nears, gone = [], [], []
    for home, away, out, ret, price, opt, complete in table:
        key = f"{home}-{away}_{out}_{ret}"
        trip = (home, away, out, ret, price, opt)
        if price is not None and price < MAX_PRICE:
            prev = alerted.get(key)
            if prev is None or price <= prev - REALERT_DROP:
                deals.append(trip)  # recorded once sent
            continue
        if key in alerted and complete and not broken:
            # deal is over: price went back up, or no seats left
            gone.append((home, away, out, ret, alerted.pop(key), price))
            if price is not None and price < NEAR_PRICE:
                near_sent[key] = price  # the "gone" notice already says the new price
            continue
        if price is not None and price < NEAR_PRICE:
            prev = near_sent.get(key)
            if prev is None or price <= prev - REALERT_DROP:
                nears.append(trip)
        elif key in near_sent and complete and not broken:
            del near_sent[key]  # back above: alert again if it drops again
    for d in (alerted, near_sent):
        for key in [k for k in d if k.split("_")[2] < today.isoformat()]:
            del d[key]

    if deals:
        deals.sort(key=lambda d: d[4])
        title, body, links = deal_message(deals[0])
        def weekend(d):  # (traveller, that weekend's Sunday)
            return d[0], d[3] - timedelta(days=(d[3].weekday() + 1) % 7)
        seen, others = {weekend(deals[0])}, []
        for d in deals[1:]:  # cheapest first, so keep the first per weekend
            if weekend(d) not in seen:
                seen.add(weekend(d))
                others.append(d)
        if others:
            body += ["", f"Also under ${MAX_PRICE}:"]
            body += [f"${p}  {CITY[h]} to {CITY[a]}, {span(o, r)}" for h, a, o, r, p, _ in others[:8]]
            if len(others) > 8:
                body.append(f"and {len(others) - 8} more.")
        if notify(title, "\n".join(body), priority=5, links=links):
            for h, a, o, r, p, _ in deals:
                alerted[f"{h}-{a}_{o}_{r}"] = p

    if nears:
        nears.sort(key=lambda d: d[4])
        title, body, links = deal_message(nears[0])
        body = [f"Just over your ${MAX_PRICE} limit."] + body
        if len(nears) > 1:
            body += ["", f"Also under ${NEAR_PRICE}:"]
            body += [f"${p}  {CITY[h]} to {CITY[a]}, {span(o, r)}" for h, a, o, r, p, _ in nears[1:6]]
        if notify("Almost: " + title, "\n".join(body), priority=3, links=links):
            for h, a, o, r, p, _ in nears:
                near_sent[f"{h}-{a}_{o}_{r}"] = p

    if gone:
        def was_now(was, now_p):
            return f"It was ${was}. " + (f"It is now ${now_p}." if now_p is not None else "No seats are left.")
        if len(gone) == 1:
            h, a, o, r, was, now_p = gone[0]
            title, body = f"Deal gone: {CITY[h]} to {CITY[a]}, {span(o, r)}", was_now(was, now_p)
        else:
            title = f"{len(gone)} deals are gone"
            body = "\n".join(f"{CITY[h]} to {CITY[a]}, {span(o, r)}: {was_now(was, now_p)}"
                             for h, a, o, r, was, now_p in gone)
        notify(title, body, priority=3)

    print(f"\n{total - nfail}/{total} searches OK ({nempty} empty), {len(deals)} new deal(s), "
          f"{len(nears)} almost, {len(gone)} gone")
    write_summary(table, now, nfail, total)

    if broken and state.get("last_fail_email") != today.isoformat():
        run = os.environ.get("GITHUB_RUN_ID")
        links = ([("Open the logs", f"{os.environ.get('GITHUB_SERVER_URL')}/"
                   f"{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{run}")] if run else [])
        why = (f"{nfail} of {total} searches failed" if nfail / total > MAX_FAIL_RATE
               else f"{nempty} of {total} searches came back empty")
        if notify("Flight checker is failing",
                  f"At {clock((now.hour, now.minute))}, {why}. Google may be blocking the checker, "
                  "or its page has changed. You will get this once a day until it is fixed.",
                  priority=4, links=links):
            state["last_fail_email"] = today.isoformat()

    if DIGEST and now.hour >= DIGEST_HOUR and state.get("last_digest") != today.isoformat():
        title, body = digest(table, nfail, total)
        if notify(title, body, priority=2):
            state["last_digest"] = today.isoformat()

    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    # Exit 0 when broken: we already pushed a notice. A non-zero exit means a crash,
    # which the workflow's failure step reports.
    return 0


def digest(table, nfail, total):
    """Lowest trip per weekend, grouped by who travels."""
    best = {}
    for home, away, out, ret, price, _, _ in table:
        if price is None:
            continue
        sunday = ret - timedelta(days=(ret.weekday() + 1) % 7)
        if (home, sunday) not in best or price < best[(home, sunday)][0]:
            best[(home, sunday)] = (price, out, ret)
    low = min((v[0] for v in best.values()), default=None)
    if low is None:
        title = "Daily check: no prices found"
    elif low < MAX_PRICE:
        title = f"Daily check: a trip is under ${MAX_PRICE}"
    else:
        title = f"Daily check: nothing under ${MAX_PRICE} yet"
    body = []
    for home, away in ((A, B), (B, A)):
        rows = sorted((k[1], v) for k, v in best.items() if k[0] == home)
        if rows:
            body += ([""] if body else []) + [f"{CITY[home]} to {CITY[away]} and back"]
            body += [f"{span(o, r)}: ${p}" for _, (p, o, r) in rows]
    if nfail:
        body += ["", f"{nfail} of {total} searches failed."]
    return title, "\n".join(body)


def write_summary(table, now, nfail, total) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    md = [f"## Lowest prices, {now:%b} {now.day} at {clock((now.hour, now.minute))} PT\n",
          f"{total - nfail} of {total} searches worked. Prices in CAD.\n",
          "| Trip | Leave | Return | Price | Flights |", "|---|---|---|---|---|"]
    for home, away, out, ret, price, opt, _ in sorted(table, key=lambda t: (t[0], t[2], t[3])):
        p = "n/a" if price is None else (f"**${price}**" if price < MAX_PRICE else f"${price}")
        md.append(f"| {CITY[home]} to {CITY[away]} | {day(out)} | {day(ret)} | {p} | {short(opt) if opt else ''} |")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")


if __name__ == "__main__":
    sys.exit(main())
