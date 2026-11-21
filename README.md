# Topstep 50K Combine Day-Trading Bot (TopstepX / ProjectX API)

An automated intraday futures bot built to pass the **Topstep $50K Trading
Combine**. It trades through the official TopstepX API, opens trades with
server-side stop-loss and take-profit brackets, closes them on target, stop,
breakeven, time stop or the 3:00 PM CT flatten, and enforces Topstep's account
rules, plus stricter rules of its own, on every trade.

> **Read this first.** No strategy can guarantee a pass, and nothing here is
> financial advice. Futures trading carries substantial risk. Combines cost
> money, and most attempts fail. The defaults are a starting point: **backtest on
> real data, paper trade, then go live** (steps below). Topstep changes its rules
> from time to time, so confirm every number in `config.yaml` against your own
> account's rules page.

---

## How it tries to pass

Most Combines are failed by the trailing drawdown, not by bad entries. The bot
is built around risk first:

| Topstep 50K rule (verify on your account) | What the bot does |
|---|---|
| Profit target **$3,000** | Tracks progress. Sizes down near the target. **Stops trading once passed.** |
| Max Loss Limit **$2,000**, trailing the end-of-day balance high, locks at $50,000 | Replicates the trailing MLL. Sizes every trade so a full stop-out lands at least `mll_buffer` ($150) above it. Emergency flatten if equity gets within $50. |
| Daily Loss Limit (optional on TopstepX, $1,000) | Bot's own daily stop at **-$450**, about 4 bad days before the MLL. Honors Topstep's DLL too if your account has one. |
| Consistency: best day **< 50%** of total profit | Daily profit cap of **+$900**, so no single day can break the rule. |
| Max **5 minis / 50 micros** | Hard contract cap. Trades micros (MNQ) by default for precise sizing. |
| Flat by **3:10 PM CT** | Flattens at **3:00 PM CT** and blocks entries after 2:30. |
| No HFT | At most 3 trades a day on 5-minute bars. |

Other guards: max 2 consecutive losses per day, half-size while more than $500
below your best day's close, news blackouts (with optional flatten), and a
kill-switch if a day ever runs past 1.5x the daily stop.

### Strategies

Both strategies are well-known, rule-based index-futures setups with a clear
structural stop. They run together and share one position.

1. **Opening Range Breakout (`orb`)**
   * The range is the high and low of the first 15 minutes after the 8:30 CT open.
   * A 5-minute close beyond the range, in the direction of session VWAP and the 50-EMA trend, triggers the entry.
   * Stop at the range midpoint. Target 2R. Entries only until 10:30 CT, one trade a day.
   * Skips days whose opening range is too wide to risk.
2. **VWAP / EMA trend pullback (`vwap_pullback`)**
   * Trend filter: 9-EMA above 21-EMA, 21-EMA rising, price above VWAP but not stretched far from it.
   * Entry when a 5-minute bar pulls back into the 9-EMA and closes strong in the trend direction.
   * Stop beyond the pullback's swing low. Target 1.8R. Mirror rules for shorts.

**Trade management:** the stop moves to breakeven (+2 ticks) at +1R. A time stop
closes trades still open after 2 hours. Everything is flat by 3:00 PM CT.

Parameters live in `config.yaml`. The backtester shows how each change affects
the pass rate.

---

## Setup

Requires **Python 3.10+**.

```bash
git clone <this repo> && cd REAL-AI-Pump
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env              # then put your TopstepX username + API key in .env
```

**TopstepX API access:** get an API key from TopstepX (Settings → API). The
ProjectX API is a paid add-on, so check its current price there.

**Brackets:** the bot sends its stop and target as bracket orders attached to
the entry. If those are rejected, enable **Auto OCO Brackets** in TopstepX's
order settings. After every fill the bot checks that a stop order exists on the
correct side of the entry. If it can't confirm one, it flattens immediately and
switches to placing separate stop and limit orders (`bracket_mode: manual`).

**Run it on your own computer.** Topstep's terms prohibit trading from a VPS,
VPN or remote server. Bots and API trading are allowed, but high-frequency
trading is not.

---

## The workflow: backtest → paper → live

```bash
# 1. Find your account id
python -m topstep_bot accounts             # put the id in config.yaml -> live.account_id

# 2. Download real 1-minute data from TopstepX (as much history as it serves)
python -m topstep_bot download --symbol MNQ --days 180 --out data/mnq_1m.csv

# 3. Backtest + simulate many 50K Combine attempts
python -m topstep_bot backtest --data data/mnq_1m.csv --trades-out results/trades.csv

# 4. Paper trade: live data, simulated fills, no orders sent. Run it for days, not minutes.
python -m topstep_bot paper

# 5. Live on your Combine account (real orders)
python -m topstep_bot live --confirm

# Check the bot's view of your Combine at any time
python -m topstep_bot status
```

The backtest prints two reports:

* **Continuous backtest:** win rate, profit factor, expectancy in $ and R, max
  drawdown, best and worst day, per-strategy and per-exit breakdowns.
* **50K Combine simulation:** starts a fresh Combine on every day in your data
  and runs each one until it **passes, fails (MLL hit) or runs out of days**
  (default 30). The pass rate and median days-to-pass are the numbers to
  optimize.

You can also backtest any CSV of 1-minute bars with
`timestamp,open,high,low,close,volume` columns. Use `--tz America/Chicago` if
its timestamps have no timezone, and `--bar-time close` if they mark bar close.

Tips for honest results:
* Tune on one period and confirm on a different one you didn't tune on.
* The fill model is pessimistic on purpose: next-bar-open entries plus slippage,
  stop assumed to fill first if a bar hits both, and targets that need price to
  trade through. On pure random-walk data the bot loses about 0.09R per trade
  to costs. Any edge your backtest shows has to come from the market.
* `python -m topstep_bot synthetic` writes random test data. It only proves the
  software runs. **Do not judge strategies on it.**

---

## Running live: what to expect

* The bot polls TopstepX every 5 seconds and evaluates strategies on completed
  bars.
* Every action is logged to the console and `logs/bot.log`. Trades go to
  `logs/trades.csv`.
* Combine state (balance, trailing MLL, daily P&L for the consistency rule) is
  saved to `state/combine_state.json`.
  * Keep this file between runs.
  * If you start the bot on an account that has already traded, set
    `combine.resume_mll` and `combine.resume_best_day` from your TopstepX
    dashboard.
* On restart it rebuilds indicators from recent history and reloads today's
  P&L and trade count from the API.
* It refuses to start if a position is already open on the contract. It won't
  trade while a position it didn't open exists. **Don't trade the same contract
  by hand while the bot runs.**
* **Ctrl+C** flattens and exits. Pass `--keep-position-on-exit` to leave the
  position open; its server-side brackets stay active.
* Add the dates of FOMC, CPI and NFP releases to `session.blackouts`, with
  `flatten: true` if you want to be flat through the release.
* The bot's balance and MLL figures are its own calculation. TopstepX's
  dashboard is the source of truth, so check it daily.

---

## Project layout

```
topstep_bot/
  strategies/orb.py, vwap_pullback.py   the two strategies
  risk.py         Combine rules (trailing MLL, DLL, consistency) + position sizing / risk gates
  engine.py       the trading loop shared by backtest, paper and live
  sessions.py     Chicago-time sessions, entry windows, flatten time, blackouts
  broker/sim.py   simulated fills (backtest + paper)
  broker/projectx.py  TopstepX / ProjectX REST client
  broker/live.py  live order execution with bracket verification
  live.py         paper/live runner (polling loop, state, journal)
  backtest.py     continuous backtest + rolling Combine simulation
tests/            unit + end-to-end tests against a fake TopstepX API
```

Run the tests with `python -m pytest`.

## References (verify, as these change)

* ProjectX Gateway API, Place an Order: https://gateway.docs.projectx.com/docs/api-reference/order/order-place
* Topstep, Trading Combine parameters: https://intercom.help/topstep-llc/en/articles/8284197-trading-combine-parameters
* Topstep, Daily Loss Limit: https://intercom.help/topstep-llc/en/articles/10490293-daily-loss-limit-in-the-trading-combine-and-express-funded-account
* Topstep, Consistency: https://intercom.help/topstep-llc/en/articles/8284208-consistency-at-topstep
* Topstep, TopstepX API access (bots allowed, no VPS/VPN): https://help.topstep.com/en/articles/11187768-topstepx-api-access
* Topstep, Auto OCO Brackets: https://intercom.help/topstep-llc/en/articles/12267698-topstepx-auto-oco-brackets
