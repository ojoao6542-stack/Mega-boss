"""Signal, sizing and online calibration. Prices and risk caps are in dollars."""
import math
from decimal import Decimal

MIN_EDGE = 0.12
MIN_TRAIN = 30
BLOCK = 40


def clamp(x, low, high):
    return max(low, min(high, x))


def probability(baseline, weights, features):
    if weights is None:
        return baseline
    logit = math.log(baseline / (1 - baseline))
    correction = sum(w * x for w, x in zip(weights, features))
    return 1 / (1 + math.exp(-clamp(logit + correction, -8, 8)))


def update(weights, seen, base, x, yes):
    # Each labeled observation is consumed once, in settlement order.
    error = yes - probability(base, weights, x)
    rate = 0.18 / math.sqrt(1 + seen / 50)
    return [clamp(w + rate * (error * v - 0.015 * w), -3, 3)
            for w, v in zip(weights, x)]


def chart_signal(chart, spot, strike, seconds):
    horizon = seconds / 300
    rms = chart["rms"]
    if rms <= 0:
        return None
    drift = chart["drift"]
    distance = (math.log(spot / strike) + drift * horizon) / (rms * math.sqrt(horizon))
    chance = clamp((1 + math.erf(distance / math.sqrt(2))) / 2, .01, .99)
    return chance, [1., clamp(distance, -3, 3), drift / rms,
                    clamp(chart["recent"] / rms, -3, 3), seconds / 900]


def chart_forecast(candles, end):
    """Three hours of consecutive finalized five-minute bars, as known at end."""
    bars = {}
    for row in candles:
        ts = int(row[0])
        if ts < end - 10800 or ts >= end:
            continue
        op, close = float(row[3]), float(row[4])
        if all(math.isfinite(v) and v > 0 for v in (op, close)):
            bars[ts] = (op, close)
    required = range(end - 10800, end, 300)
    if any(t not in bars for t in required):
        return None
    prices = [bars[end - 10800][0]] + [bars[t][1] for t in required]
    moves = [math.log(b/a) for a,b in zip(prices,prices[1:])]
    rms = math.sqrt(sum(v*v for v in moves)/36)
    returns = [math.log(prices[-1]/prices[-1-n]) for n in (3,12,36)]
    # Trend-continuation estimate per five minutes, capped for sudden moves.
    drift = sum(w*r/n for w,r,n in zip((0.5,0.3,0.2),returns,(3,12,36)))
    drift = clamp(drift, -0.5*rms, 0.5*rms)
    direction = "MIXED"
    if rms > 0 and abs(drift) >= 0.10*rms and drift*returns[0] > 0:
        direction = "UP" if drift > 0 else "DOWN"
    return dict(end=end, rms=rms, recent=returns[0]/3, direction=direction, drift=drift, prices=prices,
                changes=[round(100*math.expm1(v),3) for v in returns])


def chart_side(chart, spot, strike, seconds):
    projected = spot * math.exp(chart["drift"]*seconds/300)
    if chart["direction"] == "UP" and projected >= strike:
        return "YES"
    if chart["direction"] == "DOWN" and projected < strike:
        return "NO"
    return None


def cost(price, n):
    p = Decimal(str(price))
    return float(n * p), 0.0


def entry(p_yes, quotes, cap, allowed=("YES","NO"), minimum=0):
    choices = []
    for side, p in (("YES", p_yes), ("NO", 1 - p_yes)):
        if side not in allowed:
            continue
        price, depth = quotes[side]
        if price < .50:
            continue
        affordable = int(Decimal(str(cap)) / Decimal(str(price)))
        n = min(depth, affordable) if minimum else affordable
        while n and cost(price, n)[0] > cap:
            n -= 1
        if not n or depth < n:
            continue
        paid, fee = cost(price, n)
        edge = round(p - paid / n,12)
        if paid >= minimum and edge >= MIN_EDGE:
            choices.append((edge, dict(side=side, price=price, n=n, paid=paid, fee=fee, confidence=p)))
    return max(choices, key=lambda pair: pair[0])[1] if choices else None


def gains(order, yes):
    if not order:
        return 0
    won = (order["side"] == "YES") == bool(yes)
    return round((order["n"] if won else 0) - order["paid"], 6)


def assess(rows, rival, reference):
    # Identical markets and a $20 simulated budget for each policy.
    orders = [r[rival + "_order"] for r in rows]
    profits = [gains(o, r["yes"]) for o, r in zip(orders, rows)]
    net = sum(profits)
    reference_net = sum(gains(r[reference + "_order"], r["yes"]) for r in rows)
    improvement = sum((r[reference] - r["yes"])**2 - (r[rival] - r["yes"])**2 for r in rows) / len(rows)
    fills = sum(o is not None for o in orders)
    passes = (fills >= 12 and improvement >= 0.005 and net > max(0, reference_net)
              and net - max(profits) > 0)
    return dict(passed=passes, trades=fills, net=round(net, 2), reference_net=round(reference_net, 2),
                brier_improvement=round(improvement, 5))
