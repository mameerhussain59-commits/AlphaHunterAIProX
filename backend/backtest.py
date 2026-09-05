"""
Simplified backtest — NOT an exact replica of the live multi-timeframe
scoring engine (that would need historical data across 5 timeframes at every
point in time, which Binance's free API doesn't make practical to fetch in
bulk). This tests the CORE IDEA behind the strategy instead:

  "Buy when daily RSI is oversold (<30), stop below the recent swing low,
   target 1.5R / 3R / 5R — how often would that actually have worked out
   on real historical data?"

Two ways to run it:
  1. CLI (manual, via SSH):
       cd ~/AlphaHunterAIProX/backend
       python3 backtest.py BTCUSDT
       python3 backtest.py ETHUSDT --days 730

  2. Browser, via the live app's API (no SSH needed):
       http://<your-server>:8080/api/backtest?symbol=BTCUSDT
       http://<your-server>:8080/api/backtest?symbol=SOLUSDT&days=730

This is a sanity check for the strategy's core assumption, not a guarantee
of future performance. Past results never guarantee future ones.
"""
import asyncio
import argparse

import httpx
import indicators as ind

RSI_GATE = 30
SWING_LOOKBACK = 20
MAX_HOLD_CANDLES = 30  # how many daily candles we wait for a target/stop before giving up
ATR_STOP_MULT = 1.5


async def fetch_daily_klines(client: httpx.AsyncClient, symbol: str, limit: int):
    resp = await client.get(
        "https://data-api.binance.vision/api/v3/klines",
        params={"symbol": symbol, "interval": "1d", "limit": min(limit, 1000)},
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()
    return [
        {"open_time": k[0], "open": float(k[1]), "high": float(k[2]), "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
        for k in raw
    ]


def run_backtest(candles: list):
    df = ind.to_df(candles)
    outcomes = []

    for i in range(60, len(df) - 1):
        window = df.iloc[: i + 1]
        r = float(ind.rsi(window["close"]).iloc[-1])
        if r >= RSI_GATE:
            continue

        entry = float(window["close"].iloc[-1])
        atr = float(ind.atr(window).iloc[-1])
        swing_low = float(window["low"].tail(SWING_LOOKBACK).min())
        stop = min(swing_low, entry - atr * ATR_STOP_MULT)
        risk = entry - stop
        if risk <= 0:
            continue

        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 3
        tp3 = entry + risk * 5

        outcome = "none"
        for j in range(i + 1, min(i + 1 + MAX_HOLD_CANDLES, len(df))):
            low = float(df["low"].iloc[j])
            high = float(df["high"].iloc[j])
            if low <= stop:
                outcome = "stop"
                break
            if high >= tp3:
                outcome = "tp3"
                break
            if high >= tp2:
                outcome = "tp2"
                break
            if high >= tp1:
                outcome = "tp1"
                break

        outcomes.append({"rsi": round(r, 1), "entry": entry, "outcome": outcome})

    return outcomes


def summarize_dict(outcomes: list) -> dict:
    """Same aggregation as the CLI's summarize(), but returned as JSON-friendly
    data for the /api/backtest endpoint instead of printed to a terminal."""
    total = len(outcomes)
    if total == 0:
        return {
            "total_setups": 0,
            "message": "No qualifying setups found in this window (RSI never dropped below the gate).",
        }

    counts = {"tp1": 0, "tp2": 0, "tp3": 0, "stop": 0, "none": 0}
    for o in outcomes:
        counts[o["outcome"]] += 1

    wins = counts["tp1"] + counts["tp2"] + counts["tp3"]
    win_rate = round(wins / total * 100, 1)

    return {
        "total_setups": total,
        "wins_tp1_or_better": wins,
        "win_rate_pct": win_rate,
        "tp1_only": counts["tp1"],
        "tp2_reached": counts["tp2"],
        "tp3_reached": counts["tp3"],
        "stop_loss_hit": counts["stop"],
        "stop_loss_hit_pct": round(counts["stop"] / total * 100, 1),
        "no_outcome_within_window": counts["none"],
        "no_outcome_within_window_pct": round(counts["none"] / total * 100, 1),
        "max_hold_days": MAX_HOLD_CANDLES,
        "disclaimer": (
            "This is a simplified single-timeframe approximation of the live strategy "
            "(which also uses whale flow, security checks, confirmation candle, market "
            "regime filter, and lower timeframes). Real results will differ. Past "
            "performance never guarantees future performance."
        ),
    }


def summarize(outcomes: list):
    """CLI version — prints a human-readable report."""
    result = summarize_dict(outcomes)
    if result["total_setups"] == 0:
        print(result["message"])
        return

    print(f"\nTotal qualifying setups: {result['total_setups']}")
    print(f"  Hit TP1 or better: {result['wins_tp1_or_better']} ({result['win_rate_pct']}%)")
    print(f"    -> TP1 only: {result['tp1_only']}")
    print(f"    -> TP2 reached: {result['tp2_reached']}")
    print(f"    -> TP3 reached: {result['tp3_reached']}")
    print(f"  Hit stop-loss: {result['stop_loss_hit']} ({result['stop_loss_hit_pct']}%)")
    print(f"  Neither hit within {result['max_hold_days']} days: {result['no_outcome_within_window']} ({result['no_outcome_within_window_pct']}%)")
    print(f"\n{result['disclaimer']}")


async def backtest_symbol(symbol: str, days: int = 500) -> dict:
    """Used by the /api/backtest endpoint — fetches candles and returns the JSON summary."""
    async with httpx.AsyncClient() as client:
        candles = await fetch_daily_klines(client, symbol.upper(), days)
    outcomes = run_backtest(candles)
    result = summarize_dict(outcomes)
    result["symbol"] = symbol.upper()
    result["candles_fetched"] = len(candles)
    return result


async def main():
    parser = argparse.ArgumentParser(description="Simplified historical backtest for the reversal strategy.")
    parser.add_argument("symbol", help="Binance symbol, e.g. BTCUSDT")
    parser.add_argument("--days", type=int, default=500, help="How many days of history to test (max ~1000)")
    args = parser.parse_args()

    async with httpx.AsyncClient() as client:
        print(f"Fetching {args.days} days of daily candles for {args.symbol}...")
        candles = await fetch_daily_klines(client, args.symbol.upper(), args.days)

    print(f"Got {len(candles)} candles. Running backtest...")
    outcomes = run_backtest(candles)
    summarize(outcomes)


if __name__ == "__main__":
    asyncio.run(main())
