"""
Multi-Chain Engine — free public API only:
  - GeckoTerminal API (no key required, ~30 req/min rate limit): trending
    pools and newly created pools, per chain.

Covers 7 chains. Each chain's trending pools are also used to compute a
simple "chain momentum ranking" — average 24h volume and price change of
that chain's currently-trending pools. This is descriptive market context
(which chains are hot right now), not a trading signal.
"""
import asyncio
import httpx

GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"

# display name -> GeckoTerminal network id
CHAINS = {
    "ethereum": "eth",
    "bnb_chain": "bsc",
    "base": "base",
    "arbitrum": "arbitrum",
    "avalanche": "avax",
    "polygon": "polygon_pos",
    "solana": "solana",
}

CONCURRENCY = 3  # stay well under GeckoTerminal's free rate limit


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_pool(p, chain_name, gt_network_id):
    attr = p.get("attributes", {}) or {}
    price_change = attr.get("price_change_percentage") or {}
    volume = attr.get("volume_usd") or {}

    base_token_address = None
    try:
        base_id = (p.get("relationships", {}).get("base_token", {}).get("data", {}) or {}).get("id")
        prefix = f"{gt_network_id}_"
        if base_id and base_id.startswith(prefix):
            base_token_address = base_id[len(prefix):]
    except Exception:
        pass

    return {
        "chain": chain_name,
        "pair": attr.get("name"),
        "pool_address": attr.get("address"),
        "base_token_address": base_token_address,
        "price_usd": _to_float(attr.get("base_token_price_usd")),
        "price_change_1h_pct": _to_float(price_change.get("h1")),
        "price_change_24h_pct": _to_float(price_change.get("h24")),
        "volume_24h_usd": _to_float(volume.get("h24")),
        "liquidity_usd": _to_float(attr.get("reserve_in_usd")),
        "fdv_usd": _to_float(attr.get("fdv_usd")),
        "pool_created_at": attr.get("pool_created_at"),
    }


async def _get_pools(client: httpx.AsyncClient, endpoint: str, gt_network_id: str, chain_name: str, limit: int = 10):
    try:
        resp = await client.get(
            f"{GECKOTERMINAL_BASE}/networks/{gt_network_id}/{endpoint}",
            params={"page": 1},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [_parse_pool(p, chain_name, gt_network_id) for p in data[:limit]]
    except Exception:
        return []


async def get_trending_pools(client, gt_network_id, chain_name, limit=10):
    return await _get_pools(client, "trending_pools", gt_network_id, chain_name, limit)


async def get_new_pools(client, gt_network_id, chain_name, limit=10):
    return await _get_pools(client, "new_pools", gt_network_id, chain_name, limit)


async def scan_multichain(max_per_chain: int = 10):
    sem = asyncio.Semaphore(CONCURRENCY)

    async def bounded(chain_name, gt_id, client):
        async with sem:
            trending = await get_trending_pools(client, gt_id, chain_name, max_per_chain)
            new_pools = await get_new_pools(client, gt_id, chain_name, max_per_chain)
            return chain_name, trending, new_pools

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[bounded(name, gt_id, client) for name, gt_id in CHAINS.items()],
            return_exceptions=True,
        )

    chains_out = {}
    chain_momentum = []
    for r in results:
        if isinstance(r, Exception):
            continue
        chain_name, trending, new_pools = r
        chains_out[chain_name] = {"trending": trending, "new_pools": new_pools}

        vols = [t["volume_24h_usd"] for t in trending if t["volume_24h_usd"] is not None]
        changes = [t["price_change_24h_pct"] for t in trending if t["price_change_24h_pct"] is not None]
        avg_vol = round(sum(vols) / len(vols), 2) if vols else 0
        avg_change = round(sum(changes) / len(changes), 2) if changes else 0
        chain_momentum.append({
            "chain": chain_name,
            "avg_trending_volume_24h_usd": avg_vol,
            "avg_trending_price_change_24h_pct": avg_change,
            "trending_pool_count": len(trending),
        })

    chain_momentum.sort(key=lambda x: x["avg_trending_price_change_24h_pct"], reverse=True)

    return {
        "chains": chains_out,
        "chain_momentum_ranking": chain_momentum,
        "note": "Trending/new pools from GeckoTerminal's free public API. Momentum ranking is descriptive market context, not a trading signal — always verify a token's contract/security before acting.",
  }
