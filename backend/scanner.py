"""
Alpha Hunter Pro — scanning strategy (Improved v2).

Rules:
  1. Monthly RSI < 30 is a MANDATORY gate — token is dropped if it fails this.
  2. Weekly RSI < 42 is a SECONDARY gate (quality filter) — reduces weak setups.
  3. Multi-timeframe analysis: Monthly -> Weekly -> Daily -> 4H -> 1H -> 15M.
  4. Improved 100-point scoring with higher weight on volume + momentum confirmation.
  5. Trade setup: Entry / Stop-Loss / 3 take-profit targets (1.2R / 2.5R / 4R).
  6. Confirmation candle: latest 15m candle should close green. Stronger penalty if not.
  7. Prefer rising volume + improving MACD histogram on lower timeframes.

Everything here runs on Binance's free public klines endpoint — no paid
data provider, no API key.
"""
import asyncio
import httpx

import binance_client as bc
import indicators as ind
import security_whale as sw

TIMEFRAMES = ["1w", "1d", "4h", "1h", "15m"]

WEIGHTS = {"1w": 15, "1d": 20, "4h": 15, "1h": 15, "15m": 10}

MONTHLY_RSI_GATE = 30
WEEKLY_RSI_GATE = 42          # new secondary quality gate
CONCURRENCY = 8

ENTRY_TIMING_TFS = {"1h", "15m"}

CHAIN_ID_TO_DEXSCREENER = {
    "1": "ethereum",
    "56": "bsc",
    "137": "polygon",
    "42161": "arbitrum",
    "10": "optimism",
    "43114": "avalanche",
    "8453": "base",
}

CONFIRMATION_CANDLE_PENALTY = 10.0  # stronger penalty (was 5.0)


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

    # RSI weight reduced slightly to give more room to volume + MACD confirmation
    rsi_pts = max_pts * 0.40

    def _rsi_band_score(val):
        if val < 30:
            return 1.0
        elif val < 40:
            return 0.85
        elif val < 50:
            return 0.45
        return 0.1

    rsi_score = _rsi_band_score(r)
    detail["rsi"] = round(float(r), 2)

    if tf in ENTRY_TIMING_TFS:
        k, d = ind.stoch_rsi(closes)
        stoch_k = float(k.iloc[-1])
        stoch_d = float(d.iloc[-1])
        _, _, _, percent_b = ind.bollinger_bands(closes)
        pb = float(percent_b.iloc[-1])
        # Prefer Stoch RSI turning up from oversold
        stoch_score = 1.0 if (stoch_k < 25 and stoch_k >= stoch_d) else (0.75 if stoch_k < 35 else 0.25)
        bb_score = 1.0 if pb < 0.15 else (0.7 if pb < 0.35 else (0.3 if pb < 0.6 else 0.1))
        rsi_score = (rsi_score + stoch_score + bb_score) / 3
        detail["stoch_rsi_k"] = round(stoch_k, 2)
        detail["stoch_rsi_d"] = round(stoch_d, 2)
        detail["bb_percent_b"] = round(pb, 3)

    pts += rsi_pts * rsi_score

    # MACD: stronger reward for actual bullish crossover / rising histogram
    macd_pts = max_pts * 0.30
    if hist_last > 0 and hist_prev <= 0:
        macd_score = 1.0          # fresh bullish cross
    elif hist_last > hist_prev and hist_last > 0:
        macd_score = 0.85         # already positive and rising
    elif hist_last > hist_prev:
        macd_score = 0.55         # rising but still negative
    else:
        macd_score = 0.15
    pts += macd_pts * macd_score
    detail["macd_hist"] = round(float(hist_last), 6)

    # Volume weight increased (was 0.2 → 0.30)
    vol_pts = max_pts * 0.30
    vol_ratio = (vol_last / vol_avg) if vol_avg else 1.0
    # Require clearer volume expansion for full points
    if vol_ratio >= 1.8:
        vol_score = 1.0
    elif vol_ratio >= 1.3:
        vol_score = 0.75
    elif vol_ratio >= 1.0:
        vol_score = 0.45
    else:
        vol_score = 0.15
    pts += vol_pts * vol_score
    detail["vol_ratio"] = round(float(vol_ratio), 2)

    return round(pts, 2), detail


def _confirmation_candle_check(df_15m) -> dict:
    """Is the most recent CLOSED 15m candle green (close > open)? A red
    candle here means price is still falling at the moment of scoring — the
    reversal hasn't visibly started yet. Not a hard reject (the bounce can
    start on the very next candle), just a small score penalty so a
    confirmed setup ranks above an unconfirmed one."""
    last = df_15m.iloc[-1]
    confirmed = bool(last["close"] > last["open"])
    return {
        "confirmed": confirmed,
        "note": "latest 15m candle closed green (bounce visibly underway)" if confirmed
        else "latest 15m candle still red — reversal not yet visibly confirmed",
    }


def _human_duration(hours: float) -> str:
    if hours < 1:
        return f"~{max(round(hours * 60), 5)}m"
    if hours < 24:
        return f"~{round(hours, 1)}h"
    return f"~{round(hours / 24, 1)}d"


def _estimate_time_to_targets(atr_1h: float, entry: float, tp1: float, tp2: float, tp3: float) -> dict:
    if atr_1h <= 0:
        return {"note": "insufficient volatility data for a time estimate"}

    def hours_for(target):
        distance = abs(target - entry)
        hours = distance / atr_1h
        return round(max(hours, 0.1), 1)

    h1, h2, h3 = hours_for(tp1), hours_for(tp2), hours_for(tp3)
    return {
        "tp1_hours": h1,
        "tp1_human": _human_duration(h1),
        "tp2_hours": h2,
        "tp2_human": _human_duration(h2),
        "tp3_hours": h3,
        "tp3_human": _human_duration(h3),
        "note": "heuristic based on recent 1H volatility (ATR) — not a prediction or guarantee of timing",
    }


def _build_trade_setup(df_1h, df_4h):
    entry = float(df_1h["close"].iloc[-1])
    atr_1h = float(ind.atr(df_1h).iloc[-1])
    low = ind.swing_low(df_4h, lookback=20)
    # Slightly tighter ATR stop (1.3x instead of 1.5x) for better R:R
    stop_loss = min(low, entry - atr_1h * 1.3)
    risk = entry - stop_loss
    if risk <= 0:
        risk = entry * 0.015
        stop_loss = entry - risk

    # More realistic targets based on backtest feedback
    tp1 = entry + risk * 1.2   # was 1.5 — more achievable
    tp2 = entry + risk * 2.5   # was 3.0
    tp3 = entry + risk * 4.0   # was 5.0

    time_estimate = _estimate_time_to_targets(atr_1h, entry, tp1, tp2, tp3)

    return {
        "entry": round(entry, 8),
        "stop_loss": round(stop_loss, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "tp3": round(tp3, 8),
        "risk_reward": {"tp1": 1.2, "tp2": 2.5, "tp3": 4.0},
    }, time_estimate


def _early_pump_signal(dfs: dict, whale_notes: dict = None) -> dict:
    signals = {}
    score = 0.0
    tf_weight = {"4h": 1.5, "1d": 0.5}
    for tf in ("4h", "1d"):
        df = dfs[tf]
        closes = df["close"]
        squeeze_pctl = ind.bb_width_percentile(closes)
        o = ind.obv(df)
        obv_slope = float(o.tail(10).iloc[-1] - o.tail(10).iloc[0])
        price_change_pct = float((closes.iloc[-1] - closes.iloc[-10]) / closes.iloc[-10] * 100) if len(closes) >= 10 else 0.0

        is_squeeze = squeeze_pctl < 0.25
        is_quiet_accumulation = obv_slope > 0 and abs(price_change_pct) < 5.0

        signals[tf] = {
            "squeeze_percentile": round(squeeze_pctl, 3),
            "is_squeeze": is_squeeze,
            "obv_rising": obv_slope > 0,
            "price_change_pct_10candle": round(price_change_pct, 2),
            "quiet_accumulation": is_quiet_accumulation,
        }
        w = tf_weight[tf]
        if is_squeeze:
            score += w
        if is_quiet_accumulation:
            score += w

    df_4h = dfs["4h"]
    vol = df_4h["volume"]
    if len(vol) >= 20:
        recent5 = float(vol.tail(5).mean())
        prior15 = float(vol.iloc[-20:-5].mean())
        vol_accel_ratio = (recent5 / prior15) if prior15 else 1.0
    else:
        vol_accel_ratio = 1.0
    volume_accelerating = vol_accel_ratio > 1.3
    signals["volume_acceleration_ratio"] = round(vol_accel_ratio, 2)
    if volume_accelerating:
        score += 1.0

    whale_accumulating = bool(whale_notes and whale_notes.get("status") == "accumulation")
    signals["whale_accumulating"] = whale_accumulating
    if whale_accumulating:
        score += 0.5

    if score >= 2.5:
        label = "high"
    elif score >= 1.2:
        label = "medium"
    else:
        label = "low"

    return {"early_pump_probability": label, "note": "heuristic only, not a prediction", "details": signals}


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

    # --- Secondary quality gate: Weekly RSI ---
    w_rsi = float(ind.rsi(dfs["1w"]["close"]).iloc[-1])
    if w_rsi >= WEEKLY_RSI_GATE:
        return {
            "symbol": symbol,
            "passed_gate": False,
            "monthly_rsi": round(m_rsi, 2),
            "weekly_rsi": round(w_rsi, 2),
            "reason": "weekly_rsi_too_high",
        }

    total_score = 0.0
    breakdown = {
        "monthly_rsi": round(m_rsi, 2),
        "weekly_rsi": round(w_rsi, 2),
    }
    for tf in TIMEFRAMES:
        pts, detail = _score_timeframe(dfs[tf], tf)
        total_score += pts
        breakdown[tf] = {"points": pts, "max": WEIGHTS[tf], **detail}

    # --- Confirmation candle check (soft penalty, not a reject) ---
    confirmation = _confirmation_candle_check(dfs["15m"])
    if not confirmation["confirmed"]:
        total_score = max(0, total_score - CONFIRMATION_CANDLE_PENALTY)
    breakdown["confirmation_candle"] = confirmation

    base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol
    try:
        chain_id, contract = await sw.find_contract(client, base_asset)
        sec_data = await sw.check_token_security(client, chain_id, contract) if contract else None
    except Exception:
        chain_id, contract = None, None
        sec_data = None
    sec_pts, sec_reject, sec_notes = sw.score_security(sec_data)
    if sec_reject:
        return None
    total_score += sec_pts
    breakdown["security"] = {"points": round(sec_pts, 2), "max": sw.SECURITY_WEIGHT, **sec_notes}

    dexscreener_url = None
    if contract and chain_id:
        slug = CHAIN_ID_TO_DEXSCREENER.get(chain_id)
        if slug:
            dexscreener_url = f"https://dexscreener.com/{slug}/{contract}"
    breakdown["contract_address"] = contract
    breakdown["chain_id"] = chain_id
    breakdown["dexscreener_url"] = dexscreener_url

    try:
        whale_data = await sw.get_whale_flow(client, symbol)
    except Exception:
        whale_data = None
    whale_pts, whale_notes = sw.score_whale(whale_data)
    total_score += whale_pts
    breakdown["whale"] = {"points": whale_pts, "max": sw.WHALE_WEIGHT, **whale_notes}

    breakdown["early_pump_signal"] = _early_pump_signal(dfs, whale_notes)
    breakdown["source"] = "binance"
    breakdown["binance_url"] = f"https://www.binance.com/en/trade/{symbol}"

    setup, time_estimate = _build_trade_setup(dfs["1h"], dfs["4h"])
    breakdown["time_estimate"] = time_estimate

    return {
        "symbol": symbol,
        "passed_gate": True,
        "monthly_rsi": round(m_rsi, 2),
        "score": round(total_score, 2),
        "volume_24h": round(volume_24h, 2),
        "breakdown": breakdown,
        **setup,
    }


TOP_N_RESULTS = 20


async def run_scan(min_volume_usdt: float = 500_000, max_symbols: int = 300):
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
    top20 = passed[:TOP_N_RESULTS]
    return {
        "scanned": len(symbols),
        "gate_failed": gate_failed_count,
        "qualified": len(passed),
        "results": top20,
      }
