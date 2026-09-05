"""
Simplified backtest — NOT an exact replica of the live multi-timeframe
scoring engine (that would need historical data across 5 timeframes at every
point in time, which Binance's free API doesn't make practical to fetch in
bulk). This tests the CORE IDEA behind the strategy instead:

  "Buy when daily RSI is oversold (<30), stop below the recent swing low,
   target 1.5R / 3R / 5R — how often would that actually have worked out
   on real historical data?"

Run manually (not part of the live API):
    cd ~/AlphaHunterAIProX/backend
    python3 backtest.py BTCUSDT
    python3 backtest.py ETHUSDT --days 730

This is a sanity check for the strategy's core assumption, not a guarantee
of future performance. Past results never guarantee future ones.
"""
import asyncio
import sys
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


def summarize(outcomes: list):
    total = len(outcomes)
    if total == 0:
        print("No qualifying setups found in this window (RSI never dropped below the gate).")
        return

    counts = {"tp1": 0, "tp2": 0, "tp3": 0, "stop": 0, "none": 0}
    for o in outcomes:
        counts[o["outcome"]] += 1

    wins = counts["tp1"] + counts["tp2"] + counts["tp3"]
    win_rate = wins / total * 100

    print(f"\nTotal qualifying setups: {total}")
    print(f"  Hit TP1 or better: {wins} ({win_rate:.1f}%)")
    print(f"    -> TP1 only: {counts['tp1']}")
    print(f"    -> TP2 reached: {counts['tp2']}")
    print(f"    -> TP3 reached: {counts['tp3']}")
    print(f"  Hit stop-loss: {counts['stop']} ({counts['stop']/total*100:.1f}%)")
    print(f"  Neither hit within {MAX_HOLD_CANDLES} days: {counts['none']} ({counts['none']/total*100:.1f}%)")
    print(
        "\nReminder: this is a simplified single-timeframe approximation of the "
        "live strategy (which also uses whale flow, security checks, and lower "
        "timeframes). Real results will differ. Past performance never guarantees "
        "future performance."
    )


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
