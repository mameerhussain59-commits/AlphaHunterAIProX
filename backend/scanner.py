"""
Alpha Hunter Pro — scanning strategy.

Rules (from README):
  1. Monthly RSI < 30 is a MANDATORY gate — token is dropped if it fails this,
     no matter how good the lower-timeframe setup looks.
  2. Multi-timeframe analysis: Monthly -> Weekly -> Daily -> 4H -> 1H -> 15M.
  3. 100-point scoring system across the timeframes below the gate.
  4. Trade setup: Entry / Stop-Loss / 3 take-profit targets.

Everything here runs on Binance's free public klines endpoint — no paid
data provider, no API key.
"""
import asyncio
import httpx

import binance_client as bc
import indicators as ind
import security_whale as sw

TIMEFRAMES = ["1w", "1d", "4h", "1h", "15m"]

# Technical points per timeframe (sums to 75). Remaining 25 = whale (15) + security (10).
WEIGHTS = {"1w": 15, "1d": 20, "4h": 15, "1h": 15, "15m": 10}

MONTHLY_RSI_GATE = 30
CONCURRENCY = 8  # simultaneous symbols being analyzed, keeps us well under Binance rate limits


ENTRY_TIMING_TFS = {"1h", "15m"}  # fast timeframes get StochRSI + Bollinger blended in


def _score_timeframe(df, tf: str) -> tuple[float, dict]:
    """Score one timeframe 0..WEIGHTS[tf] based on RSI position, MACD momentum,
    and volume confirmation on the most recent closed candle. On the fast
    entry-timing timeframes (1h/15m), the RSI component is blended with
    StochRSI and Bollinger %B for a sharper reversal signal."""
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

    # RSI component (50% of this timeframe's points) — reward oversold-recovering zone
    rsi_pts = max_pts * 0.5

    def _rsi_band_score(val):
        if val < 30:
            return 1.0
        elif val < 45:
            return 0.8
        elif val < 55:
            return 0.4
        return 0.1

    rsi_score = _rsi_band_score(r)
    detail["rsi"] = round(float(r), 2)

    if tf in ENTRY_TIMING_TFS:
        k, d = ind.stoch_rsi(closes)
        stoch_k = float(k.iloc[-1])
        _, _, _, percent_b = ind.bollinger_bands(closes)
        pb = float(percent_b.iloc[-1])
        # StochRSI < 20 = oversold; percent_b near/under 0 = price at or below lower band
        stoch_score = 1.0 if stoch_k < 20 else (0.7 if stoch_k < 40 else 0.2)
        bb_score = 1.0 if pb < 0.1 else (0.7 if pb < 0.3 else (0.3 if pb < 0.6 else 0.1))
        rsi_score = (rsi_score + stoch_score + bb_score) / 3
        detail["stoch_rsi_k"] = round(stoch_k, 2)
        detail["bb_percent_b"] = round(pb, 3)

    pts += rsi_pts * rsi_score

    # MACD momentum component (30%) — reward bullish histogram cross / rising momentum
    macd_pts = max_pts * 0.3
    if hist_last > 0 and hist_prev <= 0:
        macd_score = 1.0  # fresh bullish cross
    elif hist_last > hist_prev:
        macd_score = 0.6  # momentum improving
    else:
        macd_score = 0.2
    pts += macd_pts * macd_score

    # Volume confirmation component (20%) — reward above-average volume on the latest candle
    vol_pts = max_pts * 0.2
    vol_ratio = (vol_last / vol_avg) if vol_avg else 1.0
    vol_score = min(vol_ratio / 1.5, 1.0)
    pts += vol_pts * vol_score
    detail["vol_ratio"] = round(float(vol_ratio), 2)

    return round(pts, 2), detail


def _build_trade_setup(df_1h, df_4h):
    """Entry at current price; SL below recent swing low (with ATR buffer);
    TPs at 1.5R / 3R / 5R."""
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


def _early_pump_signal(dfs: dict, whale_notes: dict = None) -> dict:
    """Heuristic, NOT a guarantee — combines a volatility squeeze (BB width
    near a 60-period low = coiled price), OBV rising while price is roughly
    flat (quiet accumulation), volume acceleration (recent 5-candle volume
    vs the prior 15), and whale buy-pressure (from the same-symbol whale
    trade-flow check) into one score.
    4h is weighted higher than 1d since it's more actionable for near-term
    moves. Pumps can also be driven by news/listings/insider activity that
    no OHLCV-based signal can see coming — treat this as one extra data
    point, not a prediction."""
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

    # Volume acceleration on 4h — recent 5 candles vs the prior 15
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

    # Whale buy pressure (reuses the same whale trade-flow check already run for scoring)
    whale_accumulating = bool(whale_notes and whale_notes.get("status") == "accumulation")
    signals["whale_accumulating"] = whale_accumulating
    if whale_accumulating:
        score += 0.5

    # max possible = (1.5+1.5) + (0.5+0.5) + 1.0 + 0.5 = 5.5
    if score >= 2.5:
        label = "high"
    elif score >= 1.2:
        label = "medium"
    else:
        label = "low"

    return {"early_pump_probability": label, "note": "heuristic only, not a prediction", "details": signals}


async def analyze_symbol(client: httpx.AsyncClient, symbol: str, volume_24h: float):
    """Run the full gate + scoring pipeline for one symbol. Returns None if the
    monthly RSI gate fails or data couldn't be fetched."""
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

    # --- Security check (rugpull/honeypot/liquidity-lock) — best effort, free ---
    base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol
    try:
        chain_id, contract = await sw.find_contract(client, base_asset)
        sec_data = await sw.check_token_security(client, chain_id, contract) if contract else None
    except Exception:
        sec_data = None
    sec_pts, sec_reject, sec_notes = sw.score_security(sec_data)
    if sec_reject:
        # hard reject — honeypot or extreme tax, matches "no rugpull/no honeypot" rule
        return None
    total_score += sec_pts
    breakdown["security"] = {"points": round(sec_pts, 2), "max": sw.SECURITY_WEIGHT, **sec_notes}

    # --- Whale trade flow (Binance large-order buy/sell pressure) — free, no key ---
    try:
        whale_data = await sw.get_whale_flow(client, symbol)
    except Exception:
        whale_data = None
    whale_pts, whale_notes = sw.score_whale(whale_data)
    total_score += whale_pts
    breakdown["whale"] = {"points": whale_pts, "max": sw.WHALE_WEIGHT, **whale_notes}

    # --- Early pump signal (informational only — never gates/rejects) ---
    breakdown["early_pump_signal"] = _early_pump_signal(dfs, whale_notes)
    breakdown["source"] = "binance"
    breakdown["binance_url"] = f"https://www.binance.com/en/trade/{symbol}"

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


TOP_N_RESULTS = 20


async def run_scan(min_volume_usdt: float = 500_000, max_symbols: int = 300):
    """Scan USDT pairs on Binance, apply the monthly RSI gate, score survivors,
    return the top 20 sorted best-first. Uses only free public endpoints."""
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
