"""
Binance PUBLIC market-data endpoints only — no API key/secret needed.
"""
import asyncio
import httpx

BASE_URL = "https://api.binance.com"

INTERVAL_MAP = {
    "1M": "1M",
    "1w": "1w",
    "1d": "1d",
    "4h": "4h",
    "1h": "1h",
    "15m": "15m",
}


async def get_usdt_symbols(client: httpx.AsyncClient, min_volume_usdt: float = 500_000):
    resp = await client.get(f"{BASE_URL}/api/v3/ticker/24hr", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    symbols = []
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if any(base.endswith(suf) for suf in ("UP", "DOWN", "BULL", "BEAR")):
            continue
        try:
            qvol = float(t.get("quoteVolume", 0))
        except (TypeError, ValueError):
            continue
        if qvol >= min_volume_usdt:
            symbols.append({"symbol": sym, "volume_24h": qvol})
    symbols.sort(key=lambda x: x["volume_24h"], reverse=True)
    return symbols


async def get_klines(client: httpx.AsyncClient, symbol: str, interval: str, limit: int = 100):
    params = {"symbol": symbol, "interval": INTERVAL_MAP[interval], "limit": limit}
    resp = await client.get(f"{BASE_URL}/api/v3/klines", params=params, timeout=30)
    resp.raise_for_status()
    raw = resp.json()
    candles = []
    for k in raw:
        candles.append({
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return candles


async def get_klines_multi(client: httpx.AsyncClient, symbol: str, intervals, limit: int = 100):
    tasks = {tf: get_klines(client, symbol, tf, limit) for tf in intervals}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    out = {}
    for tf, res in zip(tasks.keys(), results):
        out[tf] = res if not isinstance(res, Exception) else None
    return out
