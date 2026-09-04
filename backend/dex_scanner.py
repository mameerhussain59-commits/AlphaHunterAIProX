"""
DEX Token Scanner — turns GeckoTerminal's multichain trending/new pools into
scored candidates shaped like the Binance scanner's output, so both can sit
in the same "Hunted Tokens" table.

Pipeline per candidate:
  1. Liquidity/volume floor filter (cheap, no extra API calls — kills dead pools)
  2. Security check via GoPlus (reused from security_whale) — HARD REJECT on
     honeypot/extreme tax, same "no rugpull/no honeypot" rule as Binance side
  3. Score 0-100 from momentum + volume/liquidity health + security + newness
  4. Approximate trade setup (entry/SL/TP) — NOTE: unlike the Binance side,
     we don't pull OHLCV candles here (keeps API calls low across 7 chains),
     so the stop distance is sized off the 24h price-change magnitude rather
     than true ATR. Labeled clearly as an approximation. Same applies to the
     time-to-target estimate below.

No paid API used — GeckoTerminal (trending/new pools) + GoPlus (security),
both free and keyless.
"""
import asyncio
import httpx

import multichain
import security_whale as sw

MIN_LIQUIDITY_USD = 10_000
MIN_VOLUME_24H_USD = 5_000
MAX_DEX_CANDIDATES = 20
CONCURRENCY = 4

CHAIN_TO_GOPLUS = {
    "ethereum": "1",
    "bnb_chain": "56",
    "base": "8453",
    "arbitrum": "42161",
    "avalanche": "43114",
    "polygon": "137",
}
DEXSCREENER_CHAIN_SLUGS = {
    "ethereum": "ethereum",
    "bnb_chain": "bsc",
    "base": "base",
    "arbitrum": "arbitrum",
    "avalanche": "avalanche",
    "polygon": "polygon",
    "solana": "solana",
}


async def _gather_candidates(max_per_chain: int = 10):
    result = await multichain.scan_multichain(max_per_chain=max_per_chain)
    dedup = {}
    for chain_name, data in result["chains"].items():
        for pool in data["trending"]:
            key = (chain_name, pool["pool_address"])
            dedup[key] = {**pool, "is_new": dedup.get(key, {}).get("is_new", False)}
        for pool in data["new_pools"]:
            key = (chain_name, pool["pool_address"])
            existing = dedup.get(key, {})
            dedup[key] = {**existing, **pool, "is_new": True}

    filtered = [
        c for c in dedup.values()
        if (c.get("liquidity_usd") or 0) >= MIN_LIQUIDITY_USD
        and (c.get("volume_24h_usd") or 0) >= MIN_VOLUME_24H_USD
        and c.get("base_token_address")
    ]
    filtered.sort(key=lambda c: c.get("volume_24h_usd") or 0, reverse=True)
    return filtered[:MAX_DEX_CANDIDATES]


def _score_momentum(pool) -> float:
    h1 = pool.get("price_change_1h_pct") or 0
    h24 = pool.get("price_change_24h_pct") or 0
    score = 20.0
    if h24 > 0 and h1 > 0:
        score = 32.0 if h1 >= h24 / 24 * 3 else 26.0
    elif h24 > 0 >= h1:
        score = 18.0
    elif h24 <= 0 < h1:
        score = 24.0
    else:
        score = 8.0
    return min(score, 40.0)


def _score_volume_health(pool) -> float:
    vol = pool.get("volume_24h_usd") or 0
    liq = pool.get("liquidity_usd") or 1
    ratio = vol / liq
    if 0.3 <= ratio <= 5:
        return 30.0
    if 0.1 <= ratio < 0.3 or 5 < ratio <= 15:
        return 18.0
    if ratio > 15:
        return 6.0
    return 8.0


def _score_newness(pool) -> float:
    return 10.0 if pool.get("is_new") else 3.0


def _dex_pump_signal(pool) -> dict:
    h1 = pool.get("price_change_1h_pct") or 0
    h24 = pool.get("price_change_24h_pct") or 0
    vol = pool.get("volume_24h_usd") or 0
    liq = pool.get("liquidity_usd") or 1
    ratio = vol / liq

    accelerating = h24 > 0 and h1 > (h24 / 24) * 2
    healthy_turnover = 0.3 <= ratio <= 8
    fresh = pool.get("is_new", False)

    hits = sum([accelerating, healthy_turnover, fresh])
    label = "high" if hits >= 3 else ("medium" if hits == 2 else "low")
    return {
        "early_pump_probability": label,
        "note": "heuristic only, not a prediction — based on 1h/24h momentum + volume/liquidity ratio, no OHLCV history used",
        "details": {"accelerating_1h_vs_24h": accelerating, "healthy_turnover_ratio": healthy_turnover, "is_new_pool": fresh, "volume_liquidity_ratio": round(ratio, 2)},
    }


def _human_duration(hours: float) -> str:
    if hours < 1:
        return f"~{max(round(hours * 60), 5)}m"
    if hours < 24:
        return f"~{round(hours, 1)}h"
    return f"~{round(hours / 24, 1)}d"


def _approx_time_to_targets(pool, stop_pct: float) -> dict:
    h24 = abs(pool.get("price_change_24h_pct") or 0)
    daily_move_pct = max(h24, 1.0)

    def days_for(multiple):
        target_pct_distance = stop_pct * 100 * multiple
        days = target_pct_distance / daily_move_pct
        return round(max(days, 0.05), 2)

    d1, d2, d3 = days_for(1.5), days_for(3), days_for(5)
    return {
        "tp1_hours": round(d1 * 24, 1),
        "tp1_human": _human_duration(d1 * 24),
        "tp2_hours": round(d2 * 24, 1),
        "tp2_human": _human_duration(d2 * 24),
        "tp3_hours": round(d3 * 24, 1),
        "tp3_human": _human_duration(d3 * 24),
        "note": "rough approximation from 24h volatility (no OHLCV history for DEX candidates) — not a prediction or guarantee of timing",
    }


def _approx_trade_setup(pool):
    entry = pool.get("price_usd") or 0
    h24 = abs(pool.get("price_change_24h_pct") or 10)
    stop_pct = min(max(h24 / 100, 0.08), 0.25)
    stop_loss = entry * (1 - stop_pct)
    risk = entry - stop_loss
    time_estimate = _approx_time_to_targets(pool, stop_pct)
    return {
        "entry": entry,
        "stop_loss": round(stop_loss, 10),
        "tp1": round(entry + risk * 1.5, 10),
        "tp2": round(entry + risk * 3, 10),
        "tp3": round(entry + risk * 5, 10),
        "trade_setup_note": "approximated from 24h volatility, not true ATR (no OHLCV pulled for DEX candidates)",
    }, time_estimate


async def _score_candidate(client: httpx.AsyncClient, pool: dict):
    goplus_chain_id = CHAIN_TO_GOPLUS.get(pool["chain"])
    sec_data = None
    if goplus_chain_id:
        try:
            sec_data = await sw.check_token_security(client, goplus_chain_id, pool["base_token_address"])
        except Exception:
            sec_data = None
    sec_pts, sec_reject, sec_notes = sw.score_security(sec_data)
    if sec_reject:
        return None

    momentum = _score_momentum(pool)
    vol_health = _score_volume_health(pool)
    newness = _score_newness(pool)
    security_pts = (sec_pts / sw.SECURITY_WEIGHT) * 20
    total_score = round(momentum + vol_health + security_pts + newness, 2)

    setup, time_estimate = _approx_trade_setup(pool)
    pump_signal = _dex_pump_signal(pool)
    dex_slug = DEXSCREENER_CHAIN_SLUGS.get(pool["chain"], pool["chain"])
    dexscreener_url = f"https://dexscreener.com/{dex_slug}/{pool['pool_address']}"

    return {
        "symbol": f"{pool['pair']} [{pool['chain']}]",
        "passed_gate": True,
        "monthly_rsi": None,
        "score": total_score,
        "volume_24h": round(pool.get("volume_24h_usd") or 0, 2),
        "breakdown": {
            "source": "dex",
            "chain": pool["chain"],
            "pool_address": pool["pool_address"],
            "contract_address": pool["base_token_address"],
            "dexscreener_url": dexscreener_url,
            "momentum": {"points": momentum, "max": 40, "price_change_1h_pct": pool.get("price_change_1h_pct"), "price_change_24h_pct": pool.get("price_change_24h_pct")},
            "volume_health": {"points": vol_health, "max": 30},
            "security": {"points": round(security_pts, 2), "max": 20, **sec_notes},
            "newness": {"points": newness, "max": 10, "is_new_pool": pool.get("is_new", False)},
            "early_pump_signal": pump_signal,
            "time_estimate": time_estimate,
        },
        **setup,
    }


async def scan_dex(max_per_chain: int = 10):
    candidates = await _gather_candidates(max_per_chain)
    sem = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient() as client:
        async def bounded(pool):
            async with sem:
                return await _score_candidate(client, pool)

        results = await asyncio.gather(*[bounded(c) for c in candidates])

    return [r for r in results if r is not None]
