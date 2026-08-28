"""
Security checks + whale trade tracking — all free, no API key/signup required.

SECURITY (rugpull/honeypot/liquidity-lock):
  1. CoinGecko free API resolves a Binance base asset (e.g. "PEPE") to its
     on-chain contract address + platform/chain.
  2. GoPlus Security API (free, keyless) checks that contract for: honeypot,
     buy/sell tax, open-source status, LP-lock %, top-holder concentration.
  Native L1 coins (BTC, ETH, SOL, ...) have no "contract" to check — they are
  skipped and marked security_status = "native" (neutral, not penalized).

WHALE TRACKING:
  Uses Binance's own free/public aggTrades endpoint — recent trades above a
  USD threshold are treated as "whale" orders. We compute a whale buy/sell
  pressure ratio from them. No on-chain wallet API or registration needed.
"""
import time
import httpx

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
GOPLUS_BASE = "https://api.gopluslabs.io/api/v1"

# GoPlus chain IDs for the CoinGecko platform keys we care about
CHAIN_ID_MAP = {
    "ethereum": "1",
    "binance-smart-chain": "56",
    "polygon-pos": "137",
    "arbitrum-one": "42161",
    "optimistic-ethereum": "10",
    "avalanche": "43114",
    "base": "8453",
}

WHALE_TRADE_USD = 50_000   # a single trade above this is "whale-sized"
WHALE_WEIGHT = 15
SECURITY_WEIGHT = 10

_cg_cache = {"data": None, "fetched_at": 0}
_CG_CACHE_TTL = 900  # 15 min — one map covers a whole scan run comfortably


async def _get_coingecko_map(client: httpx.AsyncClient):
    """One CoinGecko call per cache window: symbol -> list of {id, platforms}."""
    now = time.time()
    if _cg_cache["data"] is not None and now - _cg_cache["fetched_at"] < _CG_CACHE_TTL:
        return _cg_cache["data"]
    try:
        resp = await client.get(f"{COINGECKO_BASE}/coins/list", params={"include_platform": "true"}, timeout=20)
        resp.raise_for_status()
        coins = resp.json()
    except Exception:
        return _cg_cache["data"] or {}

    mapping = {}
    for c in coins:
        sym = c.get("symbol", "").lower()
        mapping.setdefault(sym, []).append(c)
    _cg_cache["data"] = mapping
    _cg_cache["fetched_at"] = now
    return mapping


async def find_contract(client: httpx.AsyncClient, base_asset: str):
    """Best-effort: map a Binance base asset (e.g. 'PEPE') to (chain_id, contract).
    Returns (None, None) for native L1 coins or when nothing is found."""
    cg_map = await _get_coingecko_map(client)
    candidates = cg_map.get(base_asset.lower(), [])
    for c in candidates:
        platforms = c.get("platforms") or {}
        for platform_key, chain_id in CHAIN_ID_MAP.items():
            addr = platforms.get(platform_key)
            if addr:
                return chain_id, addr
    return None, None


async def check_token_security(client: httpx.AsyncClient, chain_id: str, contract: str):
    """Query GoPlus for one contract. Returns a dict or None if the check
    couldn't be completed (treated as neutral/unknown upstream, never fatal)."""
    try:
        resp = await client.get(
            f"{GOPLUS_BASE}/token_security/{chain_id}",
            params={"contract_addresses": contract},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        result = (data.get("result") or {}).get(contract.lower())
        if not result:
            return None
        return result
    except Exception:
        return None


def score_security(sec: dict | None):
    """Returns (points 0..SECURITY_WEIGHT, reject: bool, notes: dict)."""
    if sec is None:
        # No contract (native asset) or check failed — neutral, don't punish
        return SECURITY_WEIGHT * 0.7, False, {"status": "native_or_unknown"}

    is_honeypot = sec.get("is_honeypot") == "1"
    buy_tax = float(sec.get("buy_tax") or 0) * 100
    sell_tax = float(sec.get("sell_tax") or 0) * 100
    is_open_source = sec.get("is_open_source") == "1"
    is_mintable = sec.get("is_mintable") == "1"

    # hard reject — matches "no rugpull, no honeypot" requirement
    if is_honeypot or buy_tax > 15 or sell_tax > 15:
        return 0, True, {
            "status": "REJECTED", "is_honeypot": is_honeypot,
            "buy_tax": round(buy_tax, 1), "sell_tax": round(sell_tax, 1),
        }

    pts = SECURITY_WEIGHT
    notes = {"status": "ok", "buy_tax": round(buy_tax, 1), "sell_tax": round(sell_tax, 1)}

    if not is_open_source:
        pts -= 3
        notes["open_source"] = False
    if is_mintable:
        pts -= 2
        notes["mintable"] = True
    if buy_tax > 5 or sell_tax > 5:
        pts -= 2
        notes["high_tax"] = True

    # liquidity lock / holder concentration, when GoPlus provides it
    try:
        lp_holders = sec.get("lp_holders") or []
        locked_pct = sum(
            float(h.get("percent", 0)) for h in lp_holders
            if h.get("is_locked") == 1 or h.get("tag") in ("Burn", "LP Locker")
        ) * 100
        notes["lp_locked_pct"] = round(locked_pct, 1)
        if lp_holders and locked_pct < 50:
            pts -= 2
            notes["low_liquidity_lock"] = True
    except Exception:
        pass

    try:
        holders = sec.get("holders") or []
        top10_pct = sum(float(h.get("percent", 0)) for h in holders[:10]) * 100
        notes["top10_holder_pct"] = round(top10_pct, 1)
        if top10_pct > 50:
            pts -= 2
            notes["whale_concentration_risk"] = True
    except Exception:
        pass

    return max(pts, 0), False, notes


async def get_whale_flow(client: httpx.AsyncClient, symbol: str, limit: int = 1000):
    """Recent trades on Binance above WHALE_TRADE_USD — buy vs sell pressure
    from large orders. Free, public, no key. Returns None on failure."""
    try:
        resp = await client.get(
            "https://api.binance.com/api/v3/trades",
            params={"symbol": symbol, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        trades = resp.json()
    except Exception:
        return None

    whale_buy_usd = 0.0
    whale_sell_usd = 0.0
    whale_count = 0
    for t in trades:
        try:
            usd = float(t["price"]) * float(t["qty"])
        except (KeyError, ValueError):
            continue
        if usd < WHALE_TRADE_USD:
            continue
        whale_count += 1
        # isBuyerMaker=True means the taker was a seller hitting a resting buy order
        if t.get("isBuyerMaker"):
            whale_sell_usd += usd
        else:
            whale_buy_usd += usd

    total = whale_buy_usd + whale_sell_usd
    ratio = (whale_buy_usd / total) if total > 0 else 0.5
    return {
        "whale_trade_count": whale_count,
        "whale_buy_usd": round(whale_buy_usd, 2),
        "whale_sell_usd": round(whale_sell_usd, 2),
        "buy_pressure_ratio": round(ratio, 3),
    }


def score_whale(whale: dict | None):
    """Returns (points 0..WHALE_WEIGHT, notes: dict)."""
    if whale is None or whale["whale_trade_count"] == 0:
        return WHALE_WEIGHT * 0.5, {"status": "no_whale_activity"}

    ratio = whale["buy_pressure_ratio"]
    # reward whale accumulation (ratio > 0.5), penalize distribution (ratio < 0.5)
    pts = WHALE_WEIGHT * min(max(ratio, 0.0), 1.0)
    notes = {
        "status": "accumulation" if ratio > 0.55 else ("distribution" if ratio < 0.45 else "neutral"),
        "buy_pressure_ratio": ratio,
        "whale_trade_count": whale["whale_trade_count"],
    }
    return round(pts, 2), notes
