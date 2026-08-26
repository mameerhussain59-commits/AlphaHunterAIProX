"""
Alpha Hunter Pro — scanning strategy.
"""
import asyncio
import httpx

import binance_client as bc
import indicators as ind

TIMEFRAMES = ["1w", "1d", "4h", "1h", "15m"]
WEIGHTS = {"1w": 20, "1d": 25, "4h": 20, "1h": 20, "15m": 15}
MONTHLY_RSI_GATE = 30
CONCURRENCY = 8


def _score_timeframe(df, tf: str) -> tuple[float, dict]:
    max_pts = WEIGHTS[tf]
    closes = df["close"]
    r = ind.rsi(closes).iloc[-1]
    macd_line, signal_line, hist = ind.macd(closes)
    hist_last = hist.iloc[-1]
    hist_prev = hist.iloc[-2] if len(hist) > 1 else hist_last
    vol_avg = df["volume"].tail(20).mean()
    vol_last = df["volume"].iloc[-1]

    pts = 0.0
    detail = {}

    rsi_pts = max_pts * 0.5
    if r < 30:
        rsi_score = 1.0
    elif r < 45:
        rsi_score = 0.8
    elif r < 55:
        rsi_score = 0.4
    else:
        rsi_score = 0.1
    pts += rsi_pts * rsi_score
    detail["rsi"] = round(float(r), 2)

    macd_pts = max_pts * 0.3
    if hist_last > 0 and hist_prev <= 0:
        macd_score = 1.0
    elif hist_last > hist_prev:
        macd_score = 0.6
    else:
        macd_score = 0.2
    pts += macd_pts * macd_score

    vol_pts = max_pts * 0.2
    vol_ratio = (vol_last / vol_avg) if vol_avg else 1.0
    vol_score = min(vol_ratio / 1.5, 1.0)
    pts += vol_pts * vol_score
    detail["vol_ratio"] = round(float(vol_ratio), 2)

    return round(pts, 2), detail


def _build_trade_setup(df_1h, df_4h):
    entry = float(df_1h["close"].iloc[-1])
    atr_1h = float(ind.atr(df_1h).iloc[-1])
    low = ind.swing_low(df_4h, lookback=20)
    stop_loss = min(low, entry - atr_1h * 1.5)
    risk = entry - stop_loss
    if risk <= 0:
        risk = entry * 0.02
        stop_loss = entry - risk
    tp1 = entry + risk * 1.5
    tp2 = entry + risk * 3
    tp3 = entry + risk * 5
    return {
        "entry": round(entry, 8),
        "stop_loss": round(stop_loss, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "tp3": round(tp3, 8),
    }


async def analyze_symbol(client: httpx.AsyncClient, symbol: str, volume_24h: float):
    try:
        monthly = await bc.get_klines(client, symbol, "1M", limit=60)
    except Exception:
        return None
    if not monthly or len(monthly) < 15:
        return None

    df_monthly = ind.to_df(monthly)
    m_rsi = float(ind.rsi(df_monthly["close"]).iloc[-1])
    if m_rsi >= MONTHLY_RSI_GATE:
        return {"symbol": symbol, "passed_gate": False, "monthly_rsi": round(m_rsi, 2)}

    frames = await bc.get_klines_multi(client, symbol, TIMEFRAMES, limit=100)
    if any(frames[tf] is None or len(frames[tf]) < 30 for tf in TIMEFRAMES):
        return None

    dfs = {tf: ind.to_df(frames[tf]) for tf in TIMEFRAMES}

    total_score = 0.0
    breakdown = {"monthly_rsi": round(m_rsi, 2)}
    for tf in TIMEFRAMES:
        pts, detail = _score_timeframe(dfs[tf], tf)
        total_score += pts
        breakdown[tf] = {"points": pts, "max": WEIGHTS[tf], **detail}

    setup = _build_trade_setup(dfs["1h"], dfs["4h"])

    return {
        "symbol": symbol,
        "passed_gate": True,
        "monthly_rsi": round(m_rsi, 2),
        "score": round(total_score, 2),
        "volume_24h": round(volume_24h, 2),
        "breakdown": breakdown,
        **setup,
    }


async def run_scan(min_volume_usdt: float = 500_000, max_symbols: int = 150):
    async with httpx.AsyncClient() as client:
        symbols = await bc.get_usdt_symbols(client, min_volume_usdt)
        symbols = symbols[:max_symbols]

        sem = asyncio.Semaphore(CONCURRENCY)

        async def bounded(s):
            async with sem:
                return await analyze_symbol(client, s["symbol"], s["volume_24h"])

        results = await asyncio.gather(*[bounded(s) for s in symbols])

    passed = [r for r in results if r and r.get("passed_gate")]
    passed.sort(key=lambda r: r["score"], reverse=True)
    gate_failed_count = sum(1 for r in results if r and not r.get("passed_gate"))
    return {
        "scanned": len(symbols),
        "gate_failed": gate_failed_count,
        "qualified": len(passed),
        "results": passed,
  }
