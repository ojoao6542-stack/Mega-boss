"""Prospective five-minute forecasts for the first 100 watched contracts per account."""
import csv
import io
from datetime import datetime, timezone


def init(c):
    c.execute("""CREATE TABLE IF NOT EXISTS forecast_test (
        account INTEGER, ticker TEXT, close REAL, captured REAL,
        yes_bid REAL, yes_ask REAL, model_yes REAL, market_yes REAL, blend_yes REAL,
        PRIMARY KEY(account,ticker))""")


def due(c, account, ticker, close, now):
    row = c.execute("SELECT captured FROM forecast_test WHERE account=? AND ticker=?",
                    (account, ticker)).fetchone()
    if row is None:
        count = c.execute("SELECT COUNT(*) FROM forecast_test WHERE account=?", (account,)).fetchone()[0]
        if count >= 100:
            return False
        c.execute("INSERT INTO forecast_test(account,ticker,close) VALUES(?,?,?)", (account,ticker,close))
    return (row is None or row[0] is None) and 270 <= close-now <= 300


def capture(c, account, ticker, now, quotes, model_yes, blend):
    # Compare the purchase price of the model's favored side, expressed in YES units.
    ask = quotes["YES"][0]
    bid = round(1-quotes["NO"][0], 8)
    market = ask if model_yes >= .5 else bid
    c.execute("""UPDATE forecast_test SET captured=?,yes_bid=?,yes_ask=?,model_yes=?,
                 market_yes=?,blend_yes=? WHERE account=? AND ticker=? AND captured IS NULL""",
              (now,bid,ask,model_yes,market,(model_yes+market)/2 if blend else None,account,ticker))


def records(c, account):
    return c.execute("""SELECT f.*,o.result,o.actual IS NOT NULL AS traded
                        FROM forecast_test f LEFT JOIN observations o
                        ON o.account=f.account AND o.ticker=f.ticker
                        WHERE f.account=? ORDER BY f.close,f.ticker""",(account,)).fetchall()


def summary(c, account):
    rows = records(c, account)
    captured = [r for r in rows if r["captured"] is not None]
    settled = [r for r in captured if r["result"] in ("YES","NO")]
    scores = {}
    for key in ("model_yes","market_yes","blend_yes"):
        values = [(r[key] - int(r["result"] == "YES"))**2 for r in settled if r[key] is not None]
        scores[key] = sum(values)/len(values) if values else None
    return dict(watched=len(rows), captured=len(captured), scored=len(settled), scores=scores)


def export(c, account):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow("market snapshot_utc seconds_left yes_bid yes_ask no_bid no_ask model_yes market_yes blend_yes traded result".split())
    for r in records(c,account):
        ts, bid, ask = r["captured"],r["yes_bid"],r["yes_ask"]
        writer.writerow([r["ticker"],datetime.fromtimestamp(ts,timezone.utc).isoformat() if ts else None,
                         round(r["close"]-ts,2) if ts else None,bid,ask,
                         round(1-ask,8) if ts else None,round(1-bid,8) if ts else None,
                         r["model_yes"],r["market_yes"],r["blend_yes"],r["traded"],r["result"]])
    return out.getvalue().encode()
