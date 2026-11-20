# Bitcoin Opening Five

A standalone paper trader for Kalshi's 15-minute Bitcoin markets (KXBTC15M). It runs Opening Five alongside a fresh V5 50¢ reference account, in one service. Both start with $500 in simulated cash. The dashboard shows which has earned more on newly observed markets.

## Start on Render

1. On iPhone, tap the ZIP in Files to extract it. Create a new GitHub repository, upload the extracted files, and commit them. Place `render.yaml`, `app.py`, `engine.py`, `strategy.py`, `forecast_test.py`, and `dashboard.html` at the repository root.
2. In Render choose **New → Blueprint**, select that new repository, and review the service before creating it. The included configuration requests one **paid Starter web service** and a **1 GB persistent disk**. Both simulated accounts run inside this one service.
3. In the new service’s **Environment** settings, copy the generated `DASHBOARD_PASSWORD`. Open the service URL in Safari and enter it. You can use **Share → Add to Home Screen** for quick access. Once deployed, the service runs with the phone closed.
4. Look for **Worker online**. The status line shows why an entry is waiting. If you start after minute five, Opening Five waits for the next eligible market. The V5 reference can still trade in the current one.

## What the accounts trade

| Rule | Opening Five | V5 reference |
| --- | --- | --- |
| Entry window on a 15-minute contract | First five minutes, when more than 10 and at most 15 minutes remain | Up to 15 minutes remaining, stopping when 1 minute remains |
| Entry price | At least 50¢ on the side bought | Same |
| Minimum forecast advantage | 12 percentage points above that side's ask | Same |
| Forecast floor | Determined by the ask plus the required advantage | Same |
| Cash and sizing | $500 paper cash, $20 initial target, $50 maximum per entry | Same, separate balance |
| Pricing | Displayed ask with enough quoted size, simulated zero fees and zero slippage | Same |

A 50¢ ask can qualify at a 62% forecast. At a 60¢ ask, the same rule requires at least 72%. Both strategies wait for the BTC trend to agree with the direction relative to the contract target and require a spread of 10¢ or less. Neither strategy enters on a timer. A qualifying market can open at most one paper position per account.

The default signal uses three hours of completed five-minute Coinbase BTC-USD candles and a fresh Coinbase spot price. Kalshi supplies the market, strike, quote, and final settlement. Coinbase spot is a proxy for the settlement index. If you have licensed CF Benchmarks access, the existing optional `CFB_USERNAME` and `CFB_API_KEY` settings select BRTI for spot. The application only reads market data. It has no Kalshi order-placement route.

Each account trains its own V5 chart model on settled forecasts from its entry window. After 30 training observations, a frozen candidate is evaluated on blocks of 40 additional settled forecasts. A promotion requires forward improvement in both prediction error and simulated P/L, with at least 12 candidate fills. A successful validation can raise the sizing target by $10, up to $50. A failed review returns the target to $20. Whole-contract rounding can take a $20 target slightly above $20. Each account checks cash, available depth, a maximum of five open positions, and a $250 default daily loss allowance (UTC) including open risk. Set `DAILY_LOSS_LIMIT` on Render to choose a different allowance.

## Read the comparison

**Opening Five vs. V5** shows the total settled P/L, settled trade count, win rate, average profit per trade, largest peak-to-trough decline in settled P/L, and open exposure for both accounts. Each account has its own trade CSV. The two strategies receive the same market snapshots and settlements. Their entry times, training histories and risk budgets can diverge. The V5 reference starts fresh at deployment.

**Daily trades** groups Opening Five entries by New Orleans date and displays their settled P/L and any open positions. The separate forecast-accuracy panel samples the first valid forecast with 4:30–5:00 remaining for up to 100 markets per session. That measurement does not trigger entries.

V5's saved 50¢ sample contained 58 settled trades. The 52 first-five-minute entries earned $124.36 and the six later entries lost $38.20. These are historical V5 fills used to select the window. New results start at zero. The comparison assumes both accounts can take the displayed quote independently. It omits exchange fees, execution delays and slippage from P/L.

**Pause entries** affects both portfolios and keeps settlement running. **Archive and reset** starts a new pair with fresh models, while historic fills remain in each CSV. Pending markets must settle first.

## Run locally

Use Python 3.11 or later with system timezone data. Run this command from the extracted directory.

```sh
python app.py
```

Open [the local dashboard](http://127.0.0.1:8501). Keep the process running. By default the app binds to localhost and stores the ledger in `opening5.sqlite3`. The Render Blueprint sets `OPENING5_DB_PATH` to `/var/data/opening5.sqlite3`. Use a separate database from the original V5 service. Run the verification suite with the following command.

```sh
python -m unittest -v
```

[Kalshi public market data](https://docs.kalshi.com/getting_started/quick_start_market_data) · [Coinbase candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles) · [Render persistent disks](https://render.com/docs/disks)
