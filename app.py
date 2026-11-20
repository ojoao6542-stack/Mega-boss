"""One process serves the dashboard and runs the paper worker."""
import base64
import csv
import hmac
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from http.cookies import SimpleCookie, CookieError
from datetime import datetime, timezone

import engine
import forecast_test
from strategy import BLOCK, MIN_TRAIN

HTML = Path(__file__).with_name("dashboard.html").read_bytes()


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts is not None else None


def trade(r):
    o = json.loads(r["actual"])
    return dict(account=r["account"],market=r["ticker"],opened_utc=utc(r["opened"]),side=o["side"],
                entry_rule=o.get("entry_rule","Earlier version"),
                entry_type="30-minute" if o.get("timed") else "Strategy",
                contracts=o["n"],price=o["price"],fee=o["fee"],risk=o["paid"],
                probability=round(o["confidence"],4),result=r["result"] or "OPEN",pnl=r["pnl"])


def stats(rows):
    wins = sum(r["pnl"]>0 for r in rows)
    curve=peak=drawdown=0
    for r in rows:
        curve += r["pnl"]
        peak=max(peak,curve)
        drawdown=max(drawdown,peak-curve)
    return dict(count=len(rows),wins=wins,net=round(curve,2),drawdown=round(drawdown,2),
                rate=round(100*wins/len(rows),1) if rows else None)


from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")


def daily_trades(rows):
    days = {datetime.now(CENTRAL).date().isoformat(): []}
    for row in sorted(rows, key=lambda item: item["opened_utc"]):
        opened = datetime.fromisoformat(row["opened_utc"]).replace(tzinfo=timezone.utc).astimezone(CENTRAL)
        days.setdefault(opened.date().isoformat(), []).append(
            dict(row, local_time=opened.strftime("%I:%M %p %Z")))
    return days


def comparison(c, session):
    result = {}
    for portfolio in engine.accounts(c, session):
        aid = portfolio["id"]
        settled = c.execute("""SELECT pnl FROM observations WHERE account=? AND pnl IS NOT NULL
                               ORDER BY settled,id""", (aid,)).fetchall()
        performance = stats(settled)
        performance["per_trade"] = round(sum(row["pnl"] for row in settled) / len(settled), 2) if settled else None
        performance["open"], performance["reserved"] = engine.open_risk(c, aid)
        key = "opening" if portfolio["rule"] == engine.ENTRY_RULE else "v5"
        result[key] = performance
    return result


def snapshot():
    with engine.db() as c:
        c.execute("BEGIN")
        a=engine.account(c)
        aid=a["id"]
        m=json.loads(a["model"])
        rows=[trade(r) for r in c.execute("SELECT * FROM observations WHERE account=? AND actual IS NOT NULL ORDER BY id",(aid,))]
        closed=[r for r in rows if r["result"]!="OPEN"]
        open_count, reserved=engine.open_risk(c,aid)
        positions=[r for r in rows if r["result"]=="OPEN"]
        progress=c.execute("SELECT COUNT(*) FROM observations WHERE account=? AND epoch=? AND json_extract(snap,'$.entry_rule')=? AND json_extract(snap,'$.family')='candles_v1' AND json_extract(snap,'$.x') IS NOT NULL AND result IN ('YES','NO')",
                           (aid,m["epoch"],engine.ENTRY_RULE)).fetchone()[0]
        ages=None if a["heartbeat"] is None else int(time.time()-a["heartbeat"])
        cached=c.execute("SELECT forecast FROM chart_cache WHERE id=1 AND end=?", (int(time.time()//300)*300,)).fetchone()
        curve = [500.0]
        for settled in c.execute("""SELECT pnl FROM observations WHERE account=? AND actual IS NOT NULL
                                    AND result IS NOT NULL ORDER BY settled,id""", (aid,)):
            curve.append(round(curve[-1] + settled[0], 6))
        chart_results = c.execute("""SELECT pnl FROM observations WHERE account=? AND pnl IS NOT NULL
                                     AND json_extract(snap,'$.family')='candles_v1' ORDER BY settled,id""",(aid,)).fetchall()
        return dict(comparison=comparison(c,a["session"]), session=a["session"], current_rule=stats([r for r in closed if r["entry_rule"]==engine.ENTRY_RULE]),forecast_test=forecast_test.summary(c,aid),chart_results=stats(chart_results), source=engine.price_source(),equity=curve,chart=json.loads(cached[0]) if cached else None,account=aid,cash=a["cash"],enabled=bool(a["enabled"]),status=a["reason"],age=ages,
                    btc=a["btc"],today=engine.daily(c,aid,time.time()),daily_limit=engine.DAILY,
                    cap=50,target=m["cap"],active=m["active_name"],seen=m["seen"],review=m["last_review"],
                    learning=(f"Forward test {m['epoch']} · {progress} of {BLOCK} markets"
                              if m["epoch"] else f"Initial training · {m['seen']} of {MIN_TRAIN} markets"),
                    positions=positions, open_count=open_count, max_positions=engine.MAX_POSITIONS,
                    reserved=reserved, all=stats(closed),
                    recent=rows[-30:][::-1],daily_trades=daily_trades(rows),archives=a["session"]-1)


def control(action, expected):
    with engine.db(True) as c:
        a=engine.account(c)
        if a["id"]!=expected:
            return 409,"Account has changed. Refresh the dashboard."
        if action=="reset":
            if any(engine.pending(c, row["id"]) for row in engine.accounts(c, a["session"])):
                return 409,"Pause and wait for the tracked market to settle before resetting."
            engine.new_session(c)
        elif action in ("pause","resume"):
            c.execute("UPDATE opening_accounts SET enabled=?,reason=? WHERE session=?",
                      (action=="resume","Waiting for next scan" if action=="resume" else "Paused",a["session"]))
        else:
            return 404,"Unknown control"
    return 200,"Saved"


SESSION_SECONDS = 12 * 60 * 60


def signature(password, expiry):
    return hmac.digest(password.encode(), ("opening5-session:" + expiry).encode(), "sha256").hex()


def login_page(error=False):
    message = "<p role=alert>That password didn’t match. Try again.</p>" if error else ""
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · BTC Opening Five</title>
    <style>*{{box-sizing:border-box}}body{{background:#101a21;color:#f3f0e8;font:17px/1.5 system-ui;
    margin:12vh auto;padding:20px;max-width:460px}}label,input,button{{display:block;width:100%}}
    input,button{{padding:12px;margin:6px 0 18px;font:inherit;border-radius:8px;border:1px solid #789}}
    button{{background:#a6efd0;color:#14281f;font-weight:650}}small{{color:#b4c4c8}}</style>
    <main><small>Bitcoin Opening Five</small><h1>Sign in</h1>{message}
    <form method="post" action="/login"><label>Dashboard password
    <input type="password" name="password" autocomplete="current-password" required></label>
    <button type="submit">Open dashboard</button></form>
    <small>Use DASHBOARD_PASSWORD from your Opening Five service’s Environment settings in Render.</small></main></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply(self, code, data, kind="text/plain", extra=None):
        self.send_response(code)
        self.send_header("Content-Type",kind)
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("X-Frame-Options","DENY")
        for key,val in (extra or {}).items():
            self.send_header(key,val)
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        token=os.getenv("DASHBOARD_PASSWORD")
        if not token:
            return True
        wanted=b"Basic "+base64.b64encode(("joao:"+token).encode())
        if hmac.compare_digest(self.headers.get("Authorization","").encode(),wanted):
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            expiry, mac = cookie["opening5_session"].value.split(".", 1)
            if (expiry.isascii() and expiry.isdigit() and len(expiry) <= 12 and int(expiry) > time.time()
                    and hmac.compare_digest(mac.encode(), signature(token, expiry).encode())):
                return True
        except (CookieError, KeyError, ValueError):
            pass
        if self.command == "GET" and urlsplit(self.path).path in ("/", "/login"):
            self.reply(200, login_page(), "text/html; charset=utf-8")
        else:
            self.reply(401, b"Open the dashboard to sign in.")
        return False

    def do_GET(self):
        path=urlsplit(self.path).path
        if path=="/health":
            return self.reply(200,b"ok")
        if not self.allowed():
            return
        if path in ("/", "/login"):
            return self.reply(200,HTML,"text/html; charset=utf-8")
        if path=="/state":
            return self.reply(200,json.dumps(snapshot(),allow_nan=False).encode(),"application/json")
        if path=="/forecasts.csv":
            with engine.db() as c:
                payload = forecast_test.export(c,engine.account(c)["id"])
            return self.reply(200,payload,"text/csv",{"Content-Disposition":'attachment; filename="forecasts.csv"'})
        if path in ("/trades.csv", "/v5-reference.csv"):
            with engine.db() as c:
                rule = engine.ENTRY_RULE if path == "/trades.csv" else engine.REFERENCE_RULE
                rows = [trade(r) for r in c.execute("""SELECT o.* FROM observations o
                        JOIN opening_accounts a ON a.id=o.account
                        WHERE a.rule=? AND o.actual IS NOT NULL ORDER BY o.id""", (rule,))]
            out=io.StringIO()
            fields="account market opened_utc side entry_type contracts price fee risk probability result pnl entry_rule".split()
            writer=csv.DictWriter(out,fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            filename = "opening5_trades.csv" if path == "/trades.csv" else "v5_reference_trades.csv"
            return self.reply(200,out.getvalue().encode(),"text/csv",{"Content-Disposition":f'attachment; filename="{filename}"'})
        self.reply(404,b"Not found")

    def do_POST(self):
        if self.path == "/login":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise ValueError()
                fields = parse_qs(self.rfile.read(size).decode(), max_num_fields=4)
            except (ValueError, UnicodeError):
                return self.reply(400, b"Invalid form")
            password = os.getenv("DASHBOARD_PASSWORD", "")
            if not password or not hmac.compare_digest(fields.get("password", [""])[0].encode(), password.encode()):
                return self.reply(401, login_page(error=True), "text/html; charset=utf-8")
            expiry = str(int(time.time()) + SESSION_SECONDS)
            cookie = (f"opening5_session={expiry}.{signature(password, expiry)}; Path=/; "
                      f"Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Lax; Secure")
            return self.reply(303, b"", extra={"Location": "/", "Set-Cookie": cookie})
        if not self.allowed():
            return
        if self.path!="/control" or self.headers.get("X-Opening5-Control")!="1":
            return self.reply(403,b"Use dashboard controls")
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 0 < size <= 512:
                raise ValueError()
            body=json.loads(self.rfile.read(size))
            code,msg=control(body["action"],int(body["account"]))
        except (ValueError,TypeError,KeyError):
            return self.reply(400,b"Invalid request")
        self.reply(code,msg.encode())


def main():
    host=os.getenv("HOST","0.0.0.0" if os.getenv("RENDER") else "127.0.0.1")
    if host not in ("127.0.0.1","localhost","::1") and not os.getenv("DASHBOARD_PASSWORD"):
        raise SystemExit("Set DASHBOARD_PASSWORD to run on a public interface.")
    engine.init()
    server=ThreadingHTTPServer((host,int(os.getenv("PORT","8501"))),Handler)
    stop=threading.Event()
    threading.Thread(target=engine.worker,args=(stop,),daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__=="__main__":
    main()
