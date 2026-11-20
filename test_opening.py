import base64
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app
import engine as e
import strategy as s


class PaperTest(unittest.TestCase):

    def test_idle_account_waits_for_strategy_after_restart(self):
        start = self.now
        self.market.update(yes_ask_dollars=".51", no_ask_dollars=".51",
                           yes_bid_size_fp="200", yes_ask_size_fp="200")

        def record(t, ticker):
            market = dict(self.market, ticker=ticker)
            self.record(market, t + 500, 100, t, None)

        record(start + 1799, "first")
        e.init()
        record(start + 1801, "second")
        record(start + 7200, "third")
        state = app.snapshot()
        self.assertEqual(state["open_count"], 0)
        self.assertEqual(state["cash"], 500)

    def test_refuses_an_existing_v5_database(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder, patch.object(e, "PATH", folder + "/prior.sqlite3"):
            with sqlite3.connect(e.PATH) as conn:
                conn.execute("CREATE TABLE floor50_accounts(cash REAL)")
                conn.execute("INSERT INTO floor50_accounts VALUES(490)")
            with self.assertRaisesRegex(ValueError, "separate database"):
                e.init()
            with sqlite3.connect(e.PATH) as conn:
                self.assertEqual(conn.execute("SELECT cash FROM floor50_accounts").fetchone()[0], 490)

    def test_new_ask_floor_and_zero_fees(self):
        for side,p in (("YES",.9),("NO",.1)):
            q = {"YES":(.50,100),"NO":(.50,100)}
            q[side] = (.499,100)
            self.assertIsNone(s.entry(p,q,30,allowed=(side,),minimum=20))
            q[side] = (.50,100)
            self.assertEqual(s.entry(p,q,30,allowed=(side,),minimum=20)["fee"],0)

    def test_side_specific_edge_without_a_69_percent_cutoff(self):
        quotes = {"YES": (.50, 100), "NO": (.50, 100)}
        for side, exact, below in (("YES", .62, .6199), ("NO", .38, .3801)):
            self.assertIsNotNone(s.entry(exact, quotes, 20, allowed=(side,)))
            self.assertIsNone(s.entry(below, quotes, 20, allowed=(side,)))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(patch.object(e,"PATH",tmp.name+"/v5.sqlite3"))
        self.enterContext(patch.object(e,"DAILY",60))
        e.init()
        self.now = time.time()
        self.market = dict(ticker="KXBTC15M-TEST",floor_strike=98,
                           yes_ask_dollars="0.5000", no_ask_dollars="0.5100",
                           yes_bid_size_fp="500",yes_ask_size_fp="500")

    def chart(self, now):
        end=int(now//300)*300
        bars=[[end-(36-i)*300,0,0,100+i,101+i,1] for i in range(36)]
        return s.chart_forecast(bars,end)

    def record(self, market, close, spot, now, chart):
        with e.db(True) as c:
            e.record_account(c, e.account(c), market, close, spot, now, chart)

    def book(self, market=None):
        self.record(market or self.market, self.now+750,100,self.now,self.chart(self.now))

    def test_lifecycle_and_restart(self):
        with ThreadPoolExecutor(3) as pool:
            list(pool.map(lambda _:self.book(),range(3)))
        opened = app.snapshot()
        self.assertEqual(len(opened["recent"]),1)
        self.assertNotIn("filtered",opened)
        self.assertNotIn(b"60% filter experiment",app.HTML)
        risk = opened["recent"][0]["risk"]
        self.assertGreaterEqual(risk,20)
        self.assertLess(risk,21)
        self.assertAlmostEqual(opened["cash"],500-risk)
        self.assertEqual(app.control("reset",1)[0],409)
        app.control("pause",1)
        with ThreadPoolExecutor(3) as pool:
            list(pool.map(lambda _:e.settle(1,1,"YES",1,self.now),range(3)))
        e.init()
        closed = app.snapshot()
        self.assertEqual(closed["seen"],1)
        self.assertEqual(closed["all"]["wins"],1)
        self.assertAlmostEqual(closed["cash"],500+closed["recent"][0]["pnl"])
        self.assertEqual(app.control("reset",1)[0],200)
        self.assertEqual(app.snapshot()["cash"],500)
        self.assertEqual(app.control("reset",1)[0],409)
        with e.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM observations WHERE account=1").fetchone()[0],1)

    def test_chart_edge_after_fees_and_final_minute(self):
        close = self.now + 700
        def scan(now, chance):
            with patch.object(e, "chart_signal", return_value=(chance, [1,0,0,0,0])):
                self.record(self.market, close, 100, now, self.chart(now))
        scan(self.now, .61)  # Below the 12-cent gross edge requirement.
        self.assertEqual(app.snapshot()["cash"], 500)
        scan(self.now + 10, .69)
        order = app.snapshot()["recent"][0]
        self.assertGreaterEqual(order["probability"] - order["risk"] / order["contracts"], .12)
        self.record(dict(self.market, ticker="KXBTC15M-LATE"), self.now+60,
                 100, self.now, self.chart(self.now))
        self.assertEqual(app.snapshot()["open_count"], 1)

    def test_daily_allowance_and_liquidity(self):
        with patch.object(e,"DAILY",25):
            self.book()
            e.settle(1,1,"NO",0,self.now)
            self.book(dict(self.market,ticker="KXBTC15M-B"))
            self.assertEqual(len(app.snapshot()["recent"]),1)
            self.assertIn("allowance",app.snapshot()["status"])
        self.assertIsNone(s.entry(.95,{"YES":(.50,1),"NO":(.51,100)},20))
        for cap in (20,30,40,50):
            for p in (.50,.515,.5999,.56,.8):
                o=s.entry(.99,{"YES":(p,10000),"NO":(1-p,10000)},cap)
                self.assertIsNotNone(o)
                self.assertLessEqual(o["paid"],cap)
                self.assertGreater(s.cost(p,o["n"]+1)[0],cap)

    def test_minimum_with_limited_depth_and_cash(self):
        with e.db(True) as c:
            model = e.initial()
            model["cap"] = 50
            c.execute("UPDATE opening_accounts SET model=?,cash=35", (json.dumps(model),))
        self.book(dict(self.market, yes_ask_size_fp="38"))
        self.assertEqual(app.snapshot()["cash"],35)
        self.book(dict(self.market, yes_ask_size_fp="45"))
        state = app.snapshot()
        self.assertEqual(state["recent"][0]["contracts"],45)
        self.assertTrue(20 <= state["reserved"] <= 35)
        self.book(dict(self.market,ticker="KXBTC15M-OTHER"))
        self.assertEqual(app.snapshot()["open_count"],1)

    def test_five_slots_and_independent_settlement(self):
        with patch.object(e,"DAILY",250):
            markets=[dict(self.market,ticker=f"KXBTC15M-{i}") for i in range(6)]
            with ThreadPoolExecutor(6) as pool:
                list(pool.map(self.book,markets))
            state=app.snapshot()
            self.assertEqual(state["open_count"],5)
            self.assertAlmostEqual(state["cash"]+state["reserved"],500)
            with e.db() as c:
                skipped=c.execute("SELECT id FROM observations WHERE actual IS NULL").fetchone()[0]
                first=c.execute("SELECT id FROM observations WHERE actual IS NOT NULL ORDER BY id DESC").fetchone()[0]
            e.settle(1,skipped,"YES",1,self.now)
            self.assertEqual(app.snapshot()["open_count"],5)
            e.settle(1,first,"NO",0,self.now)
            self.assertEqual(app.snapshot()["open_count"],4)
            self.book(dict(self.market,ticker="KXBTC15M-NEW"))
            self.assertEqual(app.snapshot()["open_count"],5)

    def test_daily_cap_counts_unsettled_positions(self):
        with patch.object(e,"DAILY",250), e.db(True) as c:
            model=e.initial()
            model["cap"]=50
            c.execute("UPDATE opening_accounts SET model=?,cash=420 WHERE id=1",(json.dumps(model),))
            # Prior losses today leave $170 of room before opening new positions.
            c.execute("INSERT INTO observations(account,ticker,result,settled,pnl) VALUES(1,'prior','NO',?,-80)",
                      (self.now,))
        with patch.object(e,"DAILY",250):
            for i in range(6):
                self.book(dict(self.market,ticker=f"KXBTC15M-{i}"))
            state=app.snapshot()
            self.assertIn(state["open_count"], (3,4))
            self.assertLessEqual(state["reserved"],170)
            self.assertGreater(state["reserved"],145)
            for order in state["recent"]:
                self.assertTrue(20 <= order["risk"] <= 50)

    def test_scan_visits_all_eligible_markets(self):
        iso=lambda t: datetime.fromtimestamp(t,timezone.utc).isoformat()
        markets=[dict(self.market,ticker=f"KXBTC15M-{i}",open_time=iso(self.now-150),
                      close_time=iso(self.now+740+i)) for i in range(5)]
        def feed(url):
            if url==e.COINBASE:
                return {"price":"100","time":iso(time.time())}
            if "/candles?" in url:
                end=int(time.time()//300)*300
                return [[end-(36-i)*300,0,0,100+i,101+i,1] for i in range(36)]
            if "/markets?" in url:
                return {"markets":markets}
            return {"market":{"result":"yes","status":"finalized"}}
        with patch.object(e,"DAILY",250), patch.object(e,"get",side_effect=feed):
            e.cycle()
            self.assertEqual(app.snapshot()["open_count"],5)
            with e.db(True) as c:
                c.execute("UPDATE observations SET close=?",(self.now-1,))
            app.control("pause",1)
            e.cycle()
            self.assertEqual(app.snapshot()["open_count"],0)
            self.assertEqual(app.snapshot()["all"]["count"],5)

    def test_later_entry_charged_and_learned_once(self):
        close = self.now + 700
        def scan(seconds, probability):
            with patch.object(e,"chart_signal",return_value=(probability,[1,0,0,0,0])):
                self.record(self.market,close,100,close-seconds,self.chart(close-seconds))
        scan(890,.5)
        scan(800,.5)
        self.assertEqual(app.snapshot()["open_count"],0)
        scan(700,.9)
        opened=app.snapshot()
        self.assertEqual(opened["open_count"],1)
        scan(110,.98)
        self.assertEqual(app.snapshot()["cash"],opened["cash"])
        with e.db() as c:
            row=c.execute("SELECT * FROM observations").fetchone()
            self.assertEqual(row["opened"],close-700)
            snap=json.loads(row["snap"])
            self.assertEqual(snap["base"],.9)
            self.assertEqual(snap["active_order"]["confidence"],.9)
        e.settle(1,row["id"],"YES",1,close)
        self.assertEqual(app.snapshot()["seen"],1)

    def test_chart_forecast_and_order_gate(self):
        self.enterContext(patch.object(e,"DAILY",250))
        end=int(self.now//300)*300
        bars=[[end-(36-i)*300,0,0,150-i,149-i,1] for i in range(36)]
        falling=s.chart_forecast(list(reversed(bars))+[[end,0,0,1,10000,1]], end)
        self.assertEqual(falling["direction"],"DOWN")
        self.assertIsNone(s.chart_forecast(bars[1:],end))
        self.assertEqual(s.chart_forecast([[b[0],0,0,100,100,1] for b in bars],end)["direction"],"MIXED")
        for i,chart in enumerate((None,dict(falling,end=end-300),falling)):
            self.record(dict(self.market,ticker=f"KXBTC15M-WAIT-{i}"),self.now+750,100,self.now,chart)
        self.assertEqual(app.snapshot()["open_count"],0)
        self.record(dict(self.market,ticker="KXBTC15M-DOWN",floor_strike=102),self.now+750,100,self.now,falling)
        self.assertEqual(app.snapshot()["recent"][0]["side"],"NO")
        for oid in range(1,5): e.settle(1,oid,"NO",0,self.now)
        self.assertEqual(app.snapshot()["chart_results"]["count"],1)

    def test_missing_chart_skips(self):
        close = self.now+300
        with patch.object(e,"chart_signal",return_value=(.5,[1,0,0,0,0])):
            self.record(self.market,close,100,self.now,None)
        self.record(self.market,close,100,self.now+10,None)
        self.assertEqual(app.snapshot()["open_count"],0)
        e.settle(1,1,"YES",1,close)
        self.assertEqual(app.snapshot()["chart_results"]["count"],0)

    def test_orderbook_recovery_and_warmup_status(self):
        market = dict(self.market)
        del market["yes_ask_size_fp"]
        book = {"orderbook_fp":{"yes_dollars":[["0.49","120"]],"no_dollars":[["0.50","80"]]}}
        with patch.object(e,"get",return_value=book):
            self.assertEqual(e.quotes(e.complete_quotes(market)), {"YES":(.5,80),"NO":(.51,120)})
        self.book()
        self.assertEqual(app.snapshot()["open_count"], 1)

    def test_index_tick(self):
        with patch.dict(os.environ,CFB_USERNAME="demo",CFB_API_KEY="demo"), patch.object(e,"get") as feed:
            feed.return_value={"payload":{"latest_values":{"BRTI":{"time":1000500,"value":"70000.25"}}}}
            self.assertEqual(e.latest_tick(),(70000.25,1000.5))
            feed.assert_called_once_with(e.INDEX_URL)

    def test_forward_windows(self):
        # A deliberately biased stream exercises retraining, promotion and risk
        # increases. It is not a market performance test.
        with patch.object(e,"chart_signal",return_value=(.5,[1,0,0,0,0])):
            for i in range(190):
                self.book(dict(self.market,ticker=f"KXBTC15M-{i}"))
                y=int(i<150)
                e.settle(1,i+1,"YES" if y else "NO",y,self.now)
                if i in (29,68,69,109,149,189):
                    state=app.snapshot()
                    self.assertEqual(state["cap"],50)
                    if i==68:
                        self.assertIsNone(state["review"])
                    if i==69:
                        self.assertTrue(state["review"]["promoted"])
                if i==29:
                    with e.db() as c:
                        frozen=json.loads(e.account(c)["model"])["frozen"]
                if i==68:
                    with e.db() as c:
                        model=json.loads(e.account(c)["model"])
                        self.assertEqual(frozen,model["frozen"])
                        self.assertNotEqual(frozen,model["online"])
        self.assertFalse(app.snapshot()["review"]["promoted"])

    def test_public_feed_cycle(self):
        def iso(t):
            return datetime.fromtimestamp(t,timezone.utc).isoformat()
        market=dict(self.market,open_time=iso(self.now-150),close_time=iso(self.now+750),strike_type="greater_or_equal")
        settlement_requests = []
        def fixture(url):
            if url==e.COINBASE:
                return {"price":"100","time":iso(time.time())}
            if "/candles?" in url:
                end=int(time.time()//300)*300
                return [[end-(36-i)*300,0,0,100+i,101+i,1] for i in range(36)]
            if "/markets?" in url:
                return {"markets":[market]}
            settlement_requests.append(url)
            return {"market":{"result":"no","status":"finalized"}}
        with patch.object(e,"get",side_effect=fixture):
            e.cycle()
            before=app.snapshot()
            self.assertEqual(before["recent"][0]["result"],"OPEN")
            with e.db(True) as c:
                c.execute("UPDATE observations SET close=?",(self.now-2,))
            app.control("pause",1)
            e.cycle()
        after=app.snapshot()
        self.assertEqual(after["all"]["count"],1)
        self.assertEqual(after["cash"],before["cash"])
        self.assertNotIn("comparison", after)
        self.assertEqual(settlement_requests, [e.API + "/markets/" + self.market["ticker"]])

    def test_single_entry_window_settlement_pause_and_restart(self):
        def scan(ticker, seconds):
            with patch.object(e,"chart_signal",return_value=(.65,[1,0,0,0,0])):
                e.record(1, dict(self.market,ticker=ticker), self.now+seconds, 100,
                         self.now, self.chart(self.now))
        scan("early", 600.01)
        scan("early", 601)
        scan("boundary", 600)
        scan("too-late", 60)
        self.assertEqual(app.snapshot()["open_count"], 1)
        self.assertEqual(app.control("pause",1), (200, "Saved"))
        scan("paused", 800)
        with e.db() as c:
            pending = list(c.execute("SELECT id,account,ticker FROM observations ORDER BY id"))
        self.assertEqual(len(pending), 2)
        for row in pending:
            won = int(row["ticker"] == "early")
            e.settle(row["account"], row["id"], "YES" if won else "NO", won, self.now)
            e.settle(row["account"], row["id"], "YES" if won else "NO", won, self.now)
        e.init()
        self.assertEqual(app.snapshot()["all"]["net"], 20)
        self.assertEqual(app.control("reset",1)[0], 200)
        new = app.snapshot()
        self.assertEqual(new["session"], 2)
        self.assertEqual(new["all"]["count"], 0)
        scan("stale session", 800)
        with e.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM opening_accounts").fetchone()[0], 2)

    def test_upgrade_keeps_opening_history_and_retires_old_account(self):
        self.book()
        e.settle(1, 1, "YES", 1, self.now)
        before = app.snapshot()
        with e.db(True) as c:
            c.execute("UPDATE opening_accounts SET model=json_set(model, '$.active_name', 'V5 chart forecast') WHERE id=1")
            legacy_model = json.dumps(e.initial())
            old_id = c.execute("INSERT INTO opening_accounts(session,rule,model,cash) VALUES(1,?,?,480)",
                               ("V5 full window, 50-cent floor", legacy_model)).lastrowid
            c.execute("""INSERT INTO observations(account,ticker,opened,close,snap,actual)
                         SELECT ?, 'OLD-FILL', opened, ?,snap,actual FROM observations WHERE id=1""",
                      (old_id, self.now-1))
        e.init()
        after = app.snapshot()
        for field in ("session", "seen", "cash", "recent", "equity", "target"):
            self.assertEqual(before[field], after[field])
        self.assertEqual(after["active"], "Opening Five chart forecast")
        self.assertEqual(app.control("pause", 1)[0], 200)
        self.assertEqual(app.control("resume", 1)[0], 200)
        e.record(1, dict(self.market,ticker="new-only"), self.now+750, 100, self.now, self.chart(self.now))
        with e.db(True) as c:
            self.assertEqual(c.execute("SELECT account FROM observations WHERE ticker='new-only'").fetchall()[0][0],1)
            orders = list(c.execute("SELECT id FROM observations WHERE account=?", (old_id,)))
            self.assertEqual(len(orders), 1)
            self.assertEqual(c.execute("SELECT enabled FROM opening_accounts WHERE id=?", (old_id,)).fetchone()[0],0)
            current_trade = c.execute("SELECT id FROM observations WHERE ticker='new-only'").fetchone()[0]
        e.settle(1, current_trade, "NO", 0, self.now)
        self.assertEqual(app.control("reset",1)[0], 200)
        fresh = app.snapshot()
        app.control("pause", fresh["account"])
        with patch.object(e, "get", return_value={"market":{"result":"yes","status":"finalized"}}), \
                patch.object(e,"latest_tick", return_value=(100, self.now)):
            e.cycle()
            e.cycle()
        with e.db() as c:
            retired = c.execute("SELECT * FROM opening_accounts WHERE id=?", (old_id,)).fetchone()
            self.assertEqual(retired["cash"], 520)
            self.assertEqual(retired["model"], legacy_model)
        self.assertEqual(app.snapshot()["cash"], 500)
        self.assertEqual(app.snapshot()["all"]["count"], 0)

    def test_dashboard_http(self):
        self.enterContext(patch.dict(os.environ,DASHBOARD_PASSWORD="unit-test-secret"))
        srv=ThreadingHTTPServer(("127.0.0.1",0),app.Handler)
        thread=threading.Thread(target=srv.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url="http://127.0.0.1:"+str(srv.server_port)
        with self.assertRaises(HTTPError) as exc:
            urlopen(url+"/state")
        self.assertEqual(exc.exception.code,401)
        import http.client
        client = http.client.HTTPConnection(*srv.server_address)
        self.addCleanup(client.close)
        client.request("GET","/")
        page=client.getresponse()
        self.assertEqual(page.status,200)
        self.assertIn(b'action="/login"',page.read())
        client.request("POST","/login","password=wrong")
        page=client.getresponse()
        self.assertEqual(page.status,401)
        self.assertIn(b'role=alert',page.read())
        client.request("POST","/login","password=unit-test-secret")
        page=client.getresponse()
        self.assertEqual(page.status,303)
        self.assertEqual(page.getheader("Location"),"/")
        cookie=page.getheader("Set-Cookie").split(";")[0]
        page.read()
        for endpoint,expected in (("/",b"Chart strategy"),("/state",b'"cash": 500')):
            client.request("GET",endpoint,headers={"Cookie":cookie})
            page=client.getresponse()
            self.assertEqual(page.status,200)
            self.assertIn(expected,page.read())
        client.request("GET","/state",headers={"Cookie":cookie+"0"})
        page=client.getresponse()
        self.assertEqual(page.status,401)
        page.read()
        headers={"Authorization":"Basic "+base64.b64encode(b"joao:unit-test-secret").decode()}
        for path,marker in (("/","Bitcoin Opening Five"),("/state",'"cash": 500'),("/trades.csv","contracts")):
            with urlopen(Request(url+path,headers=headers)) as response:
                self.assertIn(marker,response.read().decode())
        with self.assertRaises(HTTPError) as exc:
            urlopen(Request(url+"/v5-reference.csv", headers=headers))
        self.assertEqual(exc.exception.code, 404)
        h=dict(headers,**{"X-Opening5-Control":"1"})
        with urlopen(Request(url+"/control",headers=h,data=b'{"action":"pause","account":1}')) as response:
            self.assertEqual(response.status,200)
        self.assertFalse(app.snapshot()["enabled"])


if __name__=="__main__":
    unittest.main()
