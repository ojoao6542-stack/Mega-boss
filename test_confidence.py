import sqlite3
import unittest
import confidence


class ConfidenceTest(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.addCleanup(self.c.close)
        confidence.init(self.c)

    def scan(self, i, threshold=.62, raw=.8, side="NO", book="one"):
        order = dict(confidence=raw,side=side,n=40,paid=20.)
        return confidence.screen(self.c,book,str(i),i+1,order,threshold)

    def complete(self, i, win, threshold=.62):
        order=self.scan(i, threshold)
        if order:
            confidence.filled(self.c,"one",str(i),order)
        confidence.finish(self.c,"one",str(i),"NO" if win else "YES",i+1)

    def test_freeze_then_adopt_on_later_outcomes(self):
        for i in range(30):
            self.complete(i, i<21)
        state = confidence.report(self.c,"one")
        self.assertEqual((state["phase"],state["progress"],state["active"]),("Forward test",0,1))
        self.assertAlmostEqual(state["candidate"], .7)
        for i in range(30,69):
            self.complete(i,i<57)
        self.assertEqual(confidence.report(self.c,"one")["active"],1)
        self.complete(69,False)
        self.assertAlmostEqual(confidence.report(self.c,"one")["active"],.7)
        self.assertIsNone(self.scan(70, threshold=.75))
        order=self.scan(70,threshold=.75,raw=.9,side="YES")
        self.assertAlmostEqual(order["confidence"],.78)
        confidence.filled(self.c,"one","70",order)
        for _ in range(2): confidence.finish(self.c,"one","70","YES",71)
        state=confidence.report(self.c,"one")
        self.assertEqual(state["progress"],1)
        band=next(b for b in state["bins"] if b["band"]=="70–<80%")
        self.assertEqual((band["trades"],band["raw"],band["used"],band["won"]),(1,90,78,100))
        self.assertEqual(confidence.report(self.c,"other")["active"],1)
        self.assertAlmostEqual(self.scan(70,book="other")["confidence"],.8)

    def test_retain_filter_if_candidate_would_skip_every_trade(self):
        for i in range(70):
            self.complete(i,i%10<7,threshold=.78)
        self.assertEqual(confidence.report(self.c,"one")["active"],1)
        self.assertEqual(confidence.report(self.c,"one")["decision"],"Current adjustment retained")

    def test_scalar_missing_and_duplicate_signals(self):
        self.scan(0);self.scan(0,raw=.99)
        confidence.finish(self.c,"one","0","SCALAR",1)
        self.assertEqual(confidence.report(self.c,"one")["progress"],0)
        self.assertEqual(self.c.execute("SELECT COUNT(*),raw FROM confidence_samples").fetchone(),(1,.8))
        self.assertEqual(confidence.adjusted(.4,.3),.4)
