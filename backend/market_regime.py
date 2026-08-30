"""
Global Market Regime Engine — free public APIs only:
  - CoinGecko /global: BTC/ETH dominance, total market cap, market breadth
  - Binance Futures (fapi): funding rate, open interest for BTCUSDT/ETHUSDT
  - Binance Spot klines: realized volatility (30-day BTC daily returns)

No paid liquidation feed exists for free via REST, so liquidations are
approximated using open-interest 24h change as a proxy signal (a sharp OI
drop often reflects liquidations flushing out leveraged positions) rather
than an exact liquidation count. This whole module is descriptive market
context, not a trading signal — treat it as background, not instruction.
"""
import numpy as np
import httpx

COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
BINANCE_FAPI = "https://fapi.binance.com"


async def get_dominance_and_breadth(client: httpx.AsyncClient):
    try:
        resp = await client.get(COINGECKO_GLOBAL, timeout=15)
        resp.raise_for_status()
        d = resp.json()["data"]
        return {
            "btc_dominance_pct": round(d["market_cap_percentage"].get("btc", 0), 2),
            "eth_dominance_pct": round(d["market_cap_percentage"].get("eth", 0), 2),
            "total_market_cap_usd": d["total_market_cap"].get("usd"),
            "total_market_cap_change_24h_pct": round(d.get("market_cap_change_percentage_24h_usd", 0), 2),
            "active_cryptocurrencies": d.get("active_cryptocurrencies"),
            "markets": d.get("markets"),
        }
    except Exception:
        return None


async def get_funding_and_oi(client: httpx.AsyncClient, symbol: str = "BTCUSDT"):
    out = {}
    try:
        resp = await client.get(f"{BINANCE_FAPI}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=15)
        resp.raise_for_status()
        d = resp.json()
        out["funding_rate_pct"] = round(float(d["lastFundingRate"]) * 100, 4)
        out["mark_price"] = float(d["markPrice"])
    except Exception:
        out["funding_rate_pct"] = None
        out["mark_price"] = None

    try:
        resp = await client.get(f"{BINANCE_FAPI}/fapi/v1/openInterest", params={"symbol": symbol}, timeout=15)
        resp.raise_for_status()
        out["open_interest"] = float(resp.json()["openInterest"])
    except Exception:
        out["open_interest"] = None

    try:
        resp = await client.get(
            f"{BINANCE_FAPI}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": "1h", "limit": 24},
            timeout=15,
        )
        resp.raise_for_status()
        hist = resp.json()
        if len(hist) >= 2:
            first = float(hist[0]["sumOpenInterest"])
            last = float(hist[-1]["sumOpenInterest"])
            out["oi_change_24h_pct"] = round((last - first) / first * 100, 2) if first else None
        else:
            out["oi_change_24h_pct"] = None
    except Exception:
        out["oi_change_24h_pct"] = None

    return out


async def get_realized_volatility(client: httpx.AsyncClient, symbol: str = "BTCUSDT", days: int = 30):
    """Annualized realized volatility from daily log returns."""
    try:
        resp = await client.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": days + 1},
            timeout=15,
        )
        resp.raise_for_status()
        closes = [float(k[4]) for k in resp.json()]
        if len(closes) < 5:
            return None
        returns = np.diff(np.log(closes))
        daily_std = float(np.std(returns))
        return round(daily_std * np.sqrt(365) * 100, 2)
    except Exception:
        return None


def classify_regime(dominance, btc_funding_oi, btc_vol):
    """Simple, transparent rules — not a black box:
      - BTC dominance high/low hints at BTC-favored vs alt-favored conditions
      - Elevated positive funding = crowded longs, pullback risk
      - Negative funding = shorts paying longs, possible short-squeeze setup
      - Sharp OI drop = proxy for a recent liquidation flush
      - Volatility level = how big price swings currently are
    This is descriptive context only, not a trade signal."""
    notes = []
    regime = "neutral"

    if dominance and dominance.get("btc_dominance_pct") is not None:
        btc_dom = dominance["btc_dominance_pct"]
        if btc_dom > 55:
            notes.append("BTC dominance high — capital concentrated in BTC, alts often underperform")
            regime = "btc-favored"
        elif btc_dom < 45:
            notes.append("BTC dominance low — historically associated with altseason conditions")
            regime = "alt-favored"

    if btc_funding_oi and btc_funding_oi.get("funding_rate_pct") is not None:
        fr = btc_funding_oi["funding_rate_pct"]
        if fr > 0.05:
            notes.append(f"BTC funding rate elevated ({fr}%) — longs crowded, pullback risk")
        elif fr < -0.02:
            notes.append(f"BTC funding rate negative ({fr}%) — shorts paying longs, squeeze setup possible")

    if btc_funding_oi and btc_funding_oi.get("oi_change_24h_pct") is not None:
        oi_chg = btc_funding_oi["oi_change_24h_pct"]
        if oi_chg < -5:
            notes.append(f"BTC open interest dropped {oi_chg}% recently — consistent with a liquidation flush")
        elif oi_chg > 10:
            notes.append(f"BTC open interest rose {oi_chg}% — fresh leverage entering, watch for volatility")

    if btc_vol is not None:
        if btc_vol > 60:
            notes.append(f"BTC realized volatility high ({btc_vol}% annualized) — expect bigger swings")
        elif btc_vol < 25:
            notes.append(f"BTC realized volatility low ({btc_vol}% annualized) — market is calm/coiled")

    return {"regime": regime, "notes": notes}


async def get_market_regime():
    async with httpx.AsyncClient() as client:
        dominance = await get_dominance_and_breadth(client)
        btc_futures = await get_funding_and_oi(client, "BTCUSDT")
        eth_futures = await get_funding_and_oi(client, "ETHUSDT")
        btc_vol = await get_realized_volatility(client, "BTCUSDT")

    classification = classify_regime(dominance, btc_futures, btc_vol)

    return {
        "dominance_and_breadth": dominance,
        "btc_futures": btc_futures,
        "eth_futures": eth_futures,
        "btc_realized_volatility_annualized_pct": btc_vol,
        "regime_classification": classification,
        "note": "Liquidations are approximated via 24h open-interest change (no free exact liquidation feed exists). Descriptive market context, not a trading signal.",
  }
