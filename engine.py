import confidence
from strategy import MIN_EDGE
from decimal import Decimal, ROUND_CEILING
import base64
import forecast_test
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request, urlopen

from strategy import BLOCK, MIN_TRAIN, assess, chart_forecast, chart_side, chart_signal, cost, entry, gains, probability, update

ENTRY_RULE = "Opening five minutes, 50-cent floor"

API = "https://external-api.kalshi.com/trade-api/v2"
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
PATH = os.getenv("OPENING5_DB_PATH", "opening5.sqlite3")
DAILY = float(os.getenv("DAILY_LOSS_LIMIT", "250"))
MAX_POSITIONS = 5
INDEX_URL = "https://www.cfbenchmarks.com/api/v1/latest_values?id=BRTI"
def price_source():
    return "CF BRTI" if os.getenv("CFB_USERNAME") and os.getenv("CFB_API_KEY") else "Coinbase proxy"


def latest_tick():
    if price_source() == "CF BRTI":
        item = get(INDEX_URL)["payload"]["latest_values"]["BRTI"]
        return float(item["value"]), int(item["time"]) / 1000
    item = get(COINBASE)
    return float(item["price"]), stamp(item["time"])


if not math.isfinite(DAILY) or DAILY <= 0:
    raise ValueError("DAILY_LOSS_LIMIT must be positive and finite")


@contextmanager
def db(write=False):
    c = sqlite3.connect(PATH, timeout=20)
    c.row_factory = sqlite3.Row
    try:
        if write:
            c.execute("BEGIN IMMEDIATE")
        yield c
        c.commit()
    finally:
        c.close()


def initial():
    return dict(seen=0, online=[0.0]*5, active=None, frozen=None, epoch=0, cap=20,
                family="candles_v1", active_name="Opening Five chart forecast", last_review=None)


def init():
    Path(PATH).parent.mkdir(parents=True, exist_ok=True)
    with db(True) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and "opening_accounts" not in tables:
            raise ValueError("Choose a separate database for the opening-five bot using OPENING5_DB_PATH.")
        c.execute("""CREATE TABLE IF NOT EXISTS opening_accounts (
            id INTEGER PRIMARY KEY, session INTEGER NOT NULL, rule TEXT NOT NULL,
            cash REAL DEFAULT 500, enabled INTEGER DEFAULT 1,
            model TEXT, reason TEXT DEFAULT 'Starting worker', heartbeat REAL, btc REAL, UNIQUE(session, rule))""")
        c.execute("""CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY, account INTEGER, ticker TEXT, opened REAL, close REAL,
            snap TEXT, actual TEXT, result TEXT, settled REAL, pnl REAL, epoch INTEGER,
            UNIQUE(account,ticker))""")
        c.execute("CREATE TABLE IF NOT EXISTS chart_cache(id INTEGER PRIMARY KEY, end INTEGER, forecast TEXT)")
        confidence.init(c)
        forecast_test.init(c)
        if account(c) is None:
            new_session(c)
        # Keep existing ledgers and model weights when upgrading a running service.
        c.execute("UPDATE opening_accounts SET enabled=0 WHERE rule<>? AND enabled<>0", (ENTRY_RULE,))
        c.execute("""UPDATE opening_accounts SET model=json_set(model,'$.active_name',?)
                     WHERE rule=? AND json_extract(model,'$.active_name')=?""",
                  ("Opening Five chart forecast", ENTRY_RULE, "V5 chart forecast"))
        c.execute("""UPDATE opening_accounts SET model=json_remove(model,
                     '$.last_review.active_vs_baseline','$.last_review.rival_vs_baseline')
                     WHERE rule=? AND json_type(model,'$.last_review.active_vs_baseline') IS NOT NULL""",
                  (ENTRY_RULE,))


def new_session(c):
    session = c.execute("SELECT COALESCE(MAX(session), 0) + 1 FROM opening_accounts").fetchone()[0]
    c.execute("INSERT INTO opening_accounts(session,rule,model) VALUES(?,?,?)",
              (session, ENTRY_RULE, json.dumps(initial())))


def account(c):
    return c.execute("SELECT * FROM opening_accounts WHERE rule=? ORDER BY session DESC LIMIT 1",
                     (ENTRY_RULE,)).fetchone()


def daily(c, aid, now):
    midnight = now - now % 86400
    return c.execute("SELECT COALESCE(SUM(pnl),0) FROM observations WHERE account=? AND settled>=?",
                     (aid, midnight)).fetchone()[0]


def note(c, aid, msg):
    c.execute("UPDATE opening_accounts SET reason=? WHERE id=?", (msg, aid))


class RateLimited(Exception):
    def __init__(self, url, retry_after):
        self.provider = urlsplit(url).hostname
        self.minimum_wait = 0
        if retry_after:
            try:
                self.minimum_wait = max(0, int(retry_after))
            except ValueError:
                try:
                    deadline = parsedate_to_datetime(retry_after).timestamp()
                    self.minimum_wait = max(0, math.ceil(deadline - time.time()))
                except (ValueError, TypeError, OverflowError):
                    pass
        super().__init__(self.provider)


def get(url):
    request = Request(url, headers={"User-Agent":"KalshiPaperBot/5.0","Accept":"application/json"})
    if url == INDEX_URL:
        credentials = os.environ["CFB_USERNAME"] + ":" + os.environ["CFB_API_KEY"]
        request.add_header("Authorization", "Basic " + base64.b64encode(credentials.encode()).decode())
    try:
        with urlopen(request, timeout=7) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code != 429:
            raise
        exc.close()
        raise RateLimited(url, exc.headers.get("Retry-After")) from exc


def load_chart(now):
    end = int(now // 300) * 300
    with db() as c:
        row = c.execute("SELECT forecast FROM chart_cache WHERE id=1 AND end=?", (end,)).fetchone()
    if row:
        cached = json.loads(row[0])
        if cached.pop("prices", None) and "rms" in cached and "recent" in cached:
            return cached
    url = COINBASE.rsplit("/",1)[0] + "/candles?" + urlencode(dict(
        granularity=300, start=datetime.fromtimestamp(end-10800,timezone.utc).isoformat(),
        end=datetime.fromtimestamp(end,timezone.utc).isoformat()))
    chart = chart_forecast(get(url), end)
    if chart:
        with db(True) as c:
            c.execute("INSERT OR REPLACE INTO chart_cache VALUES(1,?,?)",(end,json.dumps(chart)))
        chart.pop("prices", None)
    return chart


def stamp(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def market_feed(now):
    markets = get(API + "/markets?" + urlencode(dict(series_ticker="KXBTC15M",status="open",limit=100)))["markets"]
    valid = []
    for m in markets:
        if not m["ticker"].startswith("KXBTC15M-") or m.get("strike_type") not in ("greater", "greater_or_equal", None):
            continue
        close = stamp(m["close_time"])
        if m.get("floor_strike") and 60 < close - now <= 900 and stamp(m["open_time"]) <= now:
            valid.append((close, m))
    return sorted(valid, key=lambda item: item[0])


def quotes(m):
    try:
        y, n = float(m["yes_ask_dollars"]), float(m["no_ask_dollars"])
        yd, nd = float(m["yes_ask_size_fp"]), float(m["yes_bid_size_fp"])
        if not (0 < y < 1 and 0 < n < 1 and 0.99999 <= y+n <= 1.10001):
            return None
        if not all(math.isfinite(d) and d >= 0 for d in (yd,nd)):
            return None
        return {"YES":(y,int(yd)), "NO":(n,int(nd))}
    except (KeyError, ValueError, TypeError):
        return None


def complete_quotes(m):
    if quotes(m) is not None:
        return m
    data = get(API + "/markets/" + quote(m["ticker"], safe="") + "/orderbook?depth=1")
    try:
        book = data["orderbook_fp"]
        yes_bid, yes_size = book["yes_dollars"][-1]
        no_bid, no_size = book["no_dollars"][-1]
        return dict(m, yes_ask_dollars=round(1-float(no_bid), 6),
                    no_ask_dollars=round(1-float(yes_bid), 6),
                    yes_bid_size_fp=yes_size, yes_ask_size_fp=no_size)
    except (IndexError, KeyError, TypeError, ValueError):
        return m


def pending(c, aid):
    return c.execute("SELECT * FROM observations WHERE account=? AND result IS NULL ORDER BY id LIMIT 1",(aid,)).fetchone()


def open_risk(c, aid):
    row = c.execute("""SELECT COUNT(*), COALESCE(SUM(json_extract(actual,'$.paid')),0)
                       FROM observations WHERE account=? AND result IS NULL AND actual IS NOT NULL""",
                    (aid,)).fetchone()
    return row[0], row[1]


def sized_order(prob, quotes, target, available, allowed):
    choices = []
    for side in allowed:
        price = Decimal(str(quotes[side][0]))
        minimum_contracts = (Decimal(20) / price).to_integral_value(rounding=ROUND_CEILING)
        budget = min(available, max(target, float(minimum_contracts * price)))
        order = entry(prob,quotes,budget,allowed=(side,),minimum=20)
        if order:
            choices.append(order)
    return max(choices,key=lambda o:o["confidence"]-o["price"],default=None)


def record(aid, m, close, spot, now, chart):
    with db(True) as c:
        current = account(c)
        if aid == current["id"] and current["enabled"]:
            record_account(c, current, m, close, spot, now, chart)


def record_account(c, a, m, close, spot, now, chart):
    aid = a["id"]
    remaining = close - now
    if not 60 < remaining <= 900 or not a["enabled"] or a["rule"] != ENTRY_RULE:
        return
    if forecast_test.due(c, aid, m["ticker"], close, now):
        q = quotes(m)
        strike = float(m["floor_strike"])
        pred = chart_signal(chart, spot, strike, remaining) if (
            q and spot and strike > 0 and chart and chart["end"] == int(now//300)*300) else None
        if pred:
            base, features = pred
            model_yes = probability(base, json.loads(a["model"])["active"], features)
            forecast_test.capture(c, aid, m["ticker"], now, q, model_yes, blend=True)
    existing = c.execute("""SELECT actual IS NOT NULL OR result IS NOT NULL OR
                            json_extract(snap,'$.strategy') IS NOT 'chart' AS locked
                            FROM observations WHERE account=? AND ticker=?""",
                         (aid,m["ticker"])).fetchone()
    locked = existing is not None and existing["locked"]
    if locked:
        return
    model = json.loads(a["model"])
    q = quotes(m)
    strike = float(m["floor_strike"])
    source = price_source()
    forecast = None
    if remaining <= 600:
        reason = "Opening five-minute window has closed for this market"
    elif q is None:
        reason = "Waiting for valid Kalshi quotes and depth with a spread of 10¢ or less"
    elif not math.isfinite(strike) or strike <= 0:
        reason = "Waiting for a valid Kalshi target price"
    elif spot is None:
        reason = "Waiting for a fresh BTC spot price"
    elif not chart or chart["end"] != int(now//300)*300:
        reason = "Waiting for a current three-hour BTC chart"
    else:
        forecast = chart_signal(chart, spot, strike, remaining)
        reason = "Waiting for BTC candle movement"
    side = chart_side(chart,spot,strike,remaining) if forecast else None
    snap = dict(strategy='chart', family=model['family'], source=source, spot=spot,
                strike=strike, quotes=q, seconds=remaining)
    actual = None
    if forecast:
        base, x = forecast
        snap.update(base=base, x=x)
        count, reserved = open_risk(c, aid)
        available = min(50, a["cash"], DAILY + daily(c,aid,now) - reserved)
        allowed = (side,) if side else ()
        for label, weights in (("active", model["active"]), ("rival", model["frozen"])):
            prob = probability(base, weights, x)
            snap[label] = prob
            snap[label+"_order"] = sized_order(prob,q,20,available,allowed) if count < MAX_POSITIONS else None
        if not allowed:
            reason = "Waiting for the chart direction to agree with the target"
        elif count >= MAX_POSITIONS or available < 20:
            reason = "Waiting for available paper risk budget or daily loss allowance"
        else:
            actual = sized_order(snap["active"],q,model["cap"],available,allowed)
            reason = "Opened paper position" if actual else "Entry thresholds or available depth insufficient"
        snap["chart_direction"] = chart["direction"]
    snap["entry_rule"] = a["rule"]
    snap["reason"] = reason
    if actual:
        actual["entry_rule"] = a["rule"]
    if actual:
        actual = confidence.screen(c, aid, m["ticker"], close, actual, actual["price"] + MIN_EDGE)
        if actual is None:
            reason = "Skipped after entry confidence adjustment"
            snap["reason"] = reason
        else:
            confidence.filled(c, aid, m["ticker"], actual)
    encoded = json.dumps(actual) if actual else None
    c.execute("""INSERT INTO observations(account,ticker,opened,close,snap,actual,epoch)
                 VALUES(?,?,?,?,?,?,?)
                 ON CONFLICT(account,ticker) DO UPDATE SET opened=excluded.opened,
                   snap=excluded.snap,actual=excluded.actual,epoch=excluded.epoch
                 WHERE excluded.actual IS NOT NULL OR
                   (json_extract(observations.snap,'$.x') IS NULL AND json_extract(excluded.snap,'$.x') IS NOT NULL)""",(aid,m["ticker"],now,close,json.dumps(snap),encoded,model["epoch"]))
    if actual:
        c.execute("UPDATE opening_accounts SET cash=cash-? WHERE id=?", (actual["paid"],aid))
    note(c, aid, reason)


def learn(c, aid, model, snap, yes):
    model["online"] = update(model["online"],model["seen"],snap["base"],snap["x"],yes)
    model["seen"] += 1
    if model["epoch"]:
        raw = c.execute("""SELECT snap,result FROM observations WHERE account=? AND epoch=? AND json_extract(snap,'$.entry_rule')=?
                           AND json_extract(snap,'$.family')='candles_v1' AND json_extract(snap,'$.x') IS NOT NULL AND result IN ('YES','NO') ORDER BY id""",(aid,model["epoch"],snap["entry_rule"])).fetchall()
        if len(raw) < BLOCK:
            return
        rows = [dict(json.loads(r["snap"]), yes=int(r["result"]=="YES")) for r in raw]
        rival = assess(rows,"rival","active")
        promoted = rival["passed"]
        if promoted:
            model["active"] = model["frozen"]
            model["active_name"] = f"Validated model {model['epoch']}"
        model["cap"] = min(50, model["cap"] + 10) if promoted else 20
        model["last_review"] = dict(round=model["epoch"], promoted=promoted, rival=rival)
    if model["seen"] >= MIN_TRAIN:
        model["frozen"] = model["online"][:]
        model["epoch"] += 1


def settle(aid, oid, result, value, now):
    with db(True) as c:
        a = c.execute("SELECT * FROM opening_accounts WHERE id=?", (aid,)).fetchone()
        r = c.execute("SELECT * FROM observations WHERE id=? AND account=? AND result IS NULL",(oid,aid)).fetchone()
        if a is None or not r:
            return
        order = json.loads(r["actual"]) if r["actual"] else None
        payout = 0
        if order:
            payout = order["n"] * (value if order["side"]=="YES" else 1-value)
        pnl = round(payout-order["paid"],6) if order else None
        confidence.finish(c, aid, r["ticker"], result, now)
        c.execute("UPDATE observations SET settled=?,result=?,pnl=? WHERE id=?",(now,result,pnl,oid))
        if a["rule"] != ENTRY_RULE:
            # Settle any pre-upgrade fill against its retired ledger only.
            c.execute("UPDATE opening_accounts SET cash=cash+? WHERE id=?", (payout, aid))
            return
        model = json.loads(a["model"])
        snap = json.loads(r["snap"])
        if result in ("YES","NO") and snap.get("family") == model["family"] and "x" in snap:
            learn(c,aid,model,snap,int(result=="YES"))
        c.execute("UPDATE opening_accounts SET cash=cash+?,model=? WHERE id=?", (payout,json.dumps(model),aid))


def cycle():
    now = time.time()
    with db(True) as c:
        a = account(c)
        aid = a["id"]
        c.execute("UPDATE opening_accounts SET heartbeat=? WHERE id=?",(now,aid))
        due = c.execute("""SELECT o.id,o.account,o.ticker FROM observations o
                           JOIN opening_accounts a ON a.id=o.account
                           WHERE o.result IS NULL AND o.close<=? AND
                                 (o.account=? OR (a.rule<>? AND o.actual IS NOT NULL))
                           ORDER BY o.close,o.id""", (now, aid, ENTRY_RULE)).fetchall()
    by_ticker = {}
    for row in due:
        by_ticker.setdefault(row["ticker"], []).append(row)
    for ticker, pending_rows in by_ticker.items():
        try:
            m = get(API+"/markets/"+quote(ticker,safe=""))["market"]
            if m.get("status") not in ("settled", "finalized") or m.get("is_provisional", False):
                continue
            result = str(m.get("result", "")).upper()
            if result in ("YES", "NO"):
                value = int(result == "YES")
            elif m.get("settlement_value_dollars") is not None:
                value = float(m["settlement_value_dollars"])
                if not 0 <= value <= 1:
                    continue
                result = "SCALAR"
            else:
                continue
            for row in pending_rows:
                settle(row["account"], row["id"], result, value, now)
        except (OSError, ValueError, KeyError) as exc:
            logging.warning("Settlement for %s will retry: %s", ticker, exc)
    try:
        spot, tick_time = latest_tick()
        now = time.time()
        if not math.isfinite(spot) or spot <= 0 or not -5 <= now-tick_time <= 45:
            spot = None
    except (OSError, ValueError, KeyError):
        spot = None
    with db(True) as c:
        if account(c)["id"] != aid:
            return
        c.execute("UPDATE opening_accounts SET btc=? WHERE id=?",(spot,aid))
        if not account(c)["enabled"]:
            return note(c,aid,"Paused")
    try:
        chart = load_chart(now)
    except (OSError, ValueError, KeyError):
        chart = None
    markets = market_feed(time.time())
    if markets:
        for close,m in markets:
            m = complete_quotes(m)
            sampled = time.time()
            fresh_spot = spot if spot is not None and -5 <= sampled - tick_time <= 45 else None
            record(aid, m, close, fresh_spot, sampled, chart)
    else:
        with db(True) as c:
            note(c,aid,"Waiting for an open BTC market with more than one minute remaining")


def worker(stop):
    backoff = 30
    while not stop.is_set():
        try:
            cycle()
            backoff = 30
        except RateLimited as exc:
            delay = max(backoff, exc.minimum_wait)
            backoff = min(backoff * 2, 300)
            deadline = time.monotonic() + delay
            while not stop.is_set():
                remaining = math.ceil(deadline - time.monotonic())
                if remaining <= 0:
                    break
                with db(True) as c:
                    c.execute("UPDATE opening_accounts SET heartbeat=?,reason=? WHERE id=?",
                              (time.time(), f"{exc.provider} rate limit. Next scan in {remaining}s.",
                               account(c)["id"]))
                stop.wait(min(10, remaining))
            continue
        except Exception as exc:
            logging.exception("Scan failed")
            with db(True) as c:
                note(c,account(c)["id"],f"Scan will retry: {type(exc).__name__}: {str(exc)[:120]}")
        stop.wait(10)
