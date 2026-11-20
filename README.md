# Bitcoin Opening Five

Opening Five runs a single $500 paper account on Kalshi’s 15-minute Bitcoin markets (KXBTC15M). It evaluates a candidate forecast against its own current model on new markets.

## Update an existing Opening Five service

Replace the project files in the same repository and redeploy the existing service with its persistent disk and database path. The bot resumes the current account, including its balance, trades, model weights and training progress. Older comparison-account positions already open at upgrade settle into their own archived ledger. You can upgrade without resetting the account.

## Start on Render

1. On iPhone, tap the ZIP in Files to extract it. Create a new GitHub repository, upload the extracted files, and commit them. Place `render.yaml`, `app.py`, `engine.py`, `strategy.py`, `forecast_test.py`, and `dashboard.html` at the repository root.
2. In Render choose **New → Blueprint**, select that new repository, and review the service before creating it. The included configuration requests one **paid Starter web service** and a **1 GB persistent disk**.
3. In the new service’s **Environment** settings, copy the generated `DASHBOARD_PASSWORD`. Open the service URL in Safari and enter it. You can use **Share → Add to Home Screen** for quick access. Once deployed, the service runs with the phone closed.
4. Look for **Worker online**. The status line shows why an entry is waiting. If you start after minute five, Opening Five waits for the next eligible market.

## Entry rules

Opening Five can enter once per market during its first five minutes, when more than 10 and at most 15 minutes remain. It requires a fresh BTC trend consistent with the contract target, a spread of 10¢ or less, enough quoted size, and an ask of at least 50¢. The forecast for the side bought must exceed its ask by at least 12 percentage points. For example, a 50¢ ask requires 62% and a 60¢ ask requires 72%. Paper fills use the displayed ask and exclude fees and slippage.

The default signal uses three hours of completed five-minute Coinbase BTC-USD candles and a fresh Coinbase spot price. Kalshi supplies the market, strike, quote, and final settlement. Coinbase spot is a proxy for the settlement index. If you have licensed CF Benchmarks access, the existing optional `CFB_USERNAME` and `CFB_API_KEY` settings select BRTI for spot. The application only reads market data. It has no Kalshi order-placement route.

## Model selection and sizing

After 30 training observations, the bot freezes a candidate model. It scores the candidate and its current model on the same next 40 eligible settled forecasts, simulating each model’s orders at a $20 target. The candidate must improve the Brier score by at least 0.005, take at least 12 trades, earn positive P/L that exceeds the current model’s P/L, and stay profitable after removing its biggest win.

When promoted, the candidate becomes the active model and the sizing target increases by $10, up to $50. An unsuccessful review returns the target to $20. Whole-contract rounding can take a $20 entry slightly above its target. The bot also checks cash, book depth, a maximum of five open positions, and a default $250 daily loss allowance (UTC) including open risk. Set `DAILY_LOSS_LIMIT` on Render to choose a different allowance.

## Dashboard and history

The dashboard shows Opening Five’s available cash, open positions, equity, realized P/L, win rate, average profit per settled trade, and largest peak-to-trough decline in settled P/L.

**Daily trades** groups Opening Five entries by New Orleans date and displays their settled P/L and any open positions. The separate forecast-accuracy panel samples the first valid forecast with 4:30–5:00 remaining for up to 100 markets per session. That measurement does not trigger entries.

**Pause entries** keeps settlement running. **Archive and reset** starts a fresh $500 account and restarts learning. Previous Opening Five trades remain available in the trade CSV. Pending markets for the current account must settle before resetting.

## Run locally

Use Python 3.11 or later with system timezone data. Run this command from the extracted directory.

```sh
python app.py
```

Open [the local dashboard](http://127.0.0.1:8501). Keep the process running. By default the app binds to localhost and stores the ledger in `opening5.sqlite3`. The Render Blueprint sets `OPENING5_DB_PATH` to `/var/data/opening5.sqlite3`. Run the verification suite with the following command.

```sh
python -m unittest -v
```

[Kalshi public market data](https://docs.kalshi.com/getting_started/quick_start_market_data) · [Coinbase candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles) · [Render persistent disks](https://render.com/docs/disks)
