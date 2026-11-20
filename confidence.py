"""Per-account, forward-tested confidence adjustment for otherwise eligible paper entries."""
import math


def init(c):
    c.execute("""CREATE TABLE IF NOT EXISTS confidence_policy (
        book TEXT PRIMARY KEY, round INTEGER DEFAULT 0, active REAL DEFAULT 1,
        candidate REAL, last_decision TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS confidence_samples (
        book TEXT, ticker TEXT, round INTEGER, close_ts REAL, side TEXT, raw REAL,
        threshold REAL, cost REAL, entry_raw REAL, entry_prob REAL, entry_side TEXT,
        result TEXT, settled REAL, PRIMARY KEY(book,ticker))""")
    c.execute("CREATE INDEX IF NOT EXISTS confidence_pending ON confidence_samples(result,close_ts)")
    c.execute("CREATE INDEX IF NOT EXISTS confidence_round ON confidence_samples(book,round,result)")


def adjusted(p, factor):
    # Only reduce confidence on a favored side. Preserve all original decisions at 1.
    return p if factor == 1 or p <= .5 else .5 + factor * (p - .5)


def screen(c, book, ticker, close_ts, order, threshold):
    """Record the first eligible quote before resolution; reassess later entry attempts."""
    book = str(book)
    raw = order["confidence"]
    cost = order["paid"] / order["n"]
    if not (math.isfinite(raw) and 0 <= raw <= 1 and cost > 0):
        return None
    c.execute("INSERT OR IGNORE INTO confidence_policy(book) VALUES(?)", (book,))
    epoch, factor = c.execute("SELECT round,active FROM confidence_policy WHERE book=?", (book,)).fetchone()
    c.execute("""INSERT OR IGNORE INTO confidence_samples
        (book,ticker,round,close_ts,side,raw,threshold,cost) VALUES(?,?,?,?,?,?,?,?)""",
        (book,ticker,epoch,close_ts,order["side"].upper(),raw,threshold,cost))
    p = adjusted(raw, factor)
    if round(p - threshold, 12) < 0:
        return None
    return dict(order, raw_confidence=raw, confidence=p)


def filled(c, book, ticker, order):
    c.execute("""UPDATE confidence_samples SET entry_raw=?,entry_prob=?,entry_side=?
        WHERE book=? AND ticker=? AND entry_prob IS NULL AND result IS NULL""",
        (order["raw_confidence"],order["confidence"],order["side"].upper(),str(book),ticker))


def finish(c, book, ticker, result, now):
    book = str(book)
    result = result.upper()
    changed = c.execute("""UPDATE confidence_samples SET result=?,settled=?
        WHERE book=? AND ticker=? AND result IS NULL""",(result,now,book,ticker))
    if not changed.rowcount or result not in ("YES","NO"):
        return
    epoch,active,candidate = c.execute(
        "SELECT round,active,candidate FROM confidence_policy WHERE book=?", (book,)).fetchone()
    needed = 40 if epoch else 30
    rows = c.execute("""SELECT raw,side=result AS won,threshold,cost FROM confidence_samples
        WHERE book=? AND round=? AND result IN ('YES','NO') ORDER BY settled,rowid LIMIT ?""",
        (book,epoch,needed)).fetchall()
    if len(rows) < needed:
        return
    decision = None
    if epoch:
        improvement = total_active = total_candidate = 0.
        kept = 0
        for raw,won,threshold,cost in rows:
            old = adjusted(raw,active)
            new = adjusted(raw,candidate)
            improvement += (old-won)**2 - (new-won)**2
            unit_pnl = 20 * (won / cost - 1)  # Equal simulated risk for both policies.
            if round(old-threshold,12) >= 0:
                total_active += unit_pnl
            if round(new-threshold,12) >= 0:
                kept += 1
                total_candidate += unit_pnl
        promote = (improvement / len(rows) >= .005 and kept >= 12 and
                   total_candidate > 0 and total_candidate >= total_active - 1e-8)
        decision = "Adjustment promoted" if promote else "Current adjustment retained"
        if promote:
            active = candidate
    # Least-squares shrinkage toward 50%, with a small prior favoring the raw model.
    history = c.execute("""SELECT raw,side=result FROM confidence_samples
        WHERE book=? AND result IN ('YES','NO') AND raw>.5 ORDER BY settled DESC,rowid DESC LIMIT 200""",
        (book,)).fetchall()
    prior = .3
    numerator = prior + sum((p-.5)*(won-.5) for p,won in history)
    denominator = prior + sum((p-.5)**2 for p,_ in history)
    candidate = max(0., min(1., numerator / denominator))
    c.execute("""UPDATE confidence_policy SET round=?,active=?,candidate=?,last_decision=?
        WHERE book=?""", (epoch+1,active,candidate,decision,book))


def report(c, book):
    book = str(book)
    state = c.execute("SELECT round,active,candidate,last_decision FROM confidence_policy WHERE book=?", (book,)).fetchone()
    epoch,active,candidate,decision = state or (0,1.,None,None)
    progress = c.execute("""SELECT COUNT(*) FROM confidence_samples WHERE book=? AND round=?
                            AND result IN ('YES','NO')""", (book,epoch)).fetchone()[0]
    bins = []
    for bucket,n,mean_raw,mean_used,wins in c.execute("""SELECT
        CASE WHEN entry_prob < .60 THEN 0 WHEN entry_prob < .70 THEN 1
             WHEN entry_prob < .80 THEN 2 WHEN entry_prob < .90 THEN 3 ELSE 4 END AS bucket,
        COUNT(*),AVG(entry_raw),AVG(entry_prob),SUM(entry_side=result)
        FROM confidence_samples WHERE book=? AND entry_prob IS NOT NULL AND result IN ('YES','NO')
        GROUP BY bucket ORDER BY bucket""", (book,)):
        bins.append(dict(band=("Under 60%","60–<70%","70–<80%","80–<90%","90–100%")[bucket],
                         trades=n, raw=round(mean_raw*100,1), used=round(mean_used*100,1),
                         won=round(100*wins/n,1)))
    return dict(phase="Forward test" if epoch else "Gathering results", progress=progress,
                target=40 if epoch else 30, active=active, candidate=candidate,
                decision=decision,bins=bins)
