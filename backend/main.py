import asyncio
import csv
import io
import json
import os
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, delete

from database import init_db, async_session, ScanResult
from scanner import run_scan
import market_regime
import multichain
import dex_scanner
import telegram_alerts
import backtest

app = FastAPI(title="Alpha Hunter Pro")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_last_scan_meta = {"scan_id": None, "scanned": 0, "gate_failed": 0, "qualified": 0, "finished_at": None}
_scan_lock_running = False
_previously_alerted_high_symbols = set()

HIGH_SCORE_THRESHOLD = float(os.getenv("HIGH_SCORE_THRESHOLD", "80"))
MEDIUM_SCORE_THRESHOLD = float(os.getenv("MEDIUM_SCORE_THRESHOLD", "50"))

SCAN_INTERVAL_MINUTES = float(os.getenv("SCAN_INTERVAL_MINUTES", "5"))

REGIME_SEVERE_DROP_PCT = -5.0
REGIME_MILD_DROP_PCT = -2.0
REGIME_SEVERE_PENALTY = 10.0
REGIME_MILD_PENALTY = 5.0

RISK_MANAGEMENT_NOTE = (
    "Suggested: risk no more than 1-2% of total trading capital on a single position. "
    "Avoid opening several altcoin positions at once — most alts move together with BTC, "
    "so multiple 'different' trades can really be one large correlated bet."
)


def get_tier(score: float) -> str:
    if score >= HIGH_SCORE_THRESHOLD:
        return "high"
    elif score >= MEDIUM_SCORE_THRESHOLD:
        return "medium"
    return "low"


async def _get_regime_adjustment():
    try:
        regime = await market_regime.get_market_regime()
    except Exception:
        return 0.0, None

    penalty = 0.0
    dominance = regime.get("dominance_and_breadth") or {}
    change_24h = dominance.get("total_market_cap_change_24h_pct")
    if change_24h is not None:
        if change_24h <= REGIME_SEVERE_DROP_PCT:
            penalty = REGIME_SEVERE_PENALTY
        elif change_24h <= REGIME_MILD_DROP_PCT:
            penalty = REGIME_MILD_PENALTY

    summary = {
        "total_market_cap_change_24h_pct": change_24h,
        "regime": (regime.get("regime_classification") or {}).get("regime"),
        "notes": (regime.get("regime_classification") or {}).get("notes"),
        "score_penalty_applied": penalty,
    }
    return penalty, summary


async def perform_scan(min_volume_usdt: float = 500_000, max_symbols: int = 300, max_per_chain: int = 10):
    global _scan_lock_running
    if _scan_lock_running:
        return {"skipped": True, "reason": "A scan is already running"}
    _scan_lock_running = True
    try:
        binance_result = await run_scan(min_volume_usdt=min_volume_usdt, max_symbols=max_symbols)
        dex_results = await dex_scanner.scan_dex(max_per_chain=max_per_chain)

        merged = binance_result["results"] + dex_results
        merged.sort(key=lambda r: r["score"], reverse=True)
        merged = merged[:20]

        regime_penalty, regime_summary = await _get_regime_adjustment()
        for r in merged:
            if regime_penalty:
                r["score"] = max(0, round(r["score"] - regime_penalty, 2))
            r["tier"] = get_tier(r["score"])
            r["breakdown"]["risk_management"] = {
                "note": RISK_MANAGEMENT_NOTE,
                "risk_reward": {"tp1": "1.5R", "tp2": "3R", "tp3": "5R"},
            }
            if regime_summary:
                r["breakdown"]["market_regime"] = regime_summary

        merged.sort(key=lambda r: r["score"], reverse=True)

        scan_id = str(uuid.uuid4())

        async with async_session() as session:
            await session.execute(delete(ScanResult))
            for r in merged:
                session.add(ScanResult(
                    scan_id=scan_id,
                    symbol=r["symbol"],
                    score=r["score"],
                    monthly_rsi=r["monthly_rsi"],
                    passed_gate=1,
                    entry=r["entry"],
                    stop_loss=r["stop_loss"],
                    tp1=r["tp1"],
                    tp2=r["tp2"],
                    tp3=r["tp3"],
                    volume_24h=r["volume_24h"],
                    breakdown=json.dumps(r["breakdown"]),
                ))
            await session.commit()

        current_high_symbols = set()
        for r in merged:
            if r["tier"] == "high":
                current_high_symbols.add(r["symbol"])
                if r["symbol"] not in _previously_alerted_high_symbols:
                    message = telegram_alerts.format_high_score_alert(r)
                    await telegram_alerts.send_telegram_message(message)
        _previously_alerted_high_symbols.clear()
        _previously_alerted_high_symbols.update(current_high_symbols)

        _last_scan_meta.update({
            "scan_id": scan_id,
            "scanned": binance_result["scanned"],
            "gate_failed": binance_result["gate_failed"],
            "qualified": len(merged),
            "binance_qualified": binance_result["qualified"],
            "dex_qualified": len(dex_results),
            "high_score_count": len(current_high_symbols),
            "market_regime": regime_summary,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })

        return {"scan_id": scan_id, **_last_scan_meta, "results": merged}
    finally:
        _scan_lock_running = False


async def background_scan_loop():
    if SCAN_INTERVAL_MINUTES <= 0:
        return
    await asyncio.sleep(10)
    while True:
        try:
            await perform_scan()
        except Exception as e:
            print(f"[background_scan_loop] scan failed: {e}")
        await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)


@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/market-regime")
async def get_market_regime_endpoint():
    return await market_regime.get_market_regime()


@app.get("/api/backtest")
async def run_backtest_endpoint(symbol: str, days: int = 500):
    """Browser-friendly backtest — open http://<server>/api/backtest?symbol=BTCUSDT
    (add &days=730 for a longer window, max ~1000). No SSH needed."""
    try:
        return await backtest.backtest_symbol(symbol, days)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Backtest failed for {symbol.upper()}: {e}")


@app.get("/api/multichain/trending")
async def get_multichain_trending_endpoint(max_per_chain: int = 10):
    return await multichain.scan_multichain(max_per_chain=max_per_chain)


@app.post("/api/scan")
async def trigger_scan(min_volume_usdt: float = 500_000, max_symbols: int = 300, max_per_chain: int = 10):
    if _scan_lock_running:
        raise HTTPException(status_code=409, detail="A scan is already running — try again shortly.")
    result = await perform_scan(min_volume_usdt=min_volume_usdt, max_symbols=max_symbols, max_per_chain=max_per_chain)
    return result


@app.get("/api/scan/latest")
async def latest_scan(min_score: float = 0, tier: str | None = None):
    async with async_session() as session:
        query = select(ScanResult).where(ScanResult.score >= min_score).order_by(ScanResult.score.desc())
        res = await session.execute(query)
        rows = res.scalars().all()

    if tier:
        rows = [r for r in rows if get_tier(r.score) == tier]

    if not rows:
        return {"scan_id": None, "results": [], "message": "No scan yet — POST /api/scan to run one."}

    return {
        "scan_id": rows[0].scan_id,
        "meta": _last_scan_meta,
        "results": [
            {
                "symbol": r.symbol,
                "score": r.score,
                "tier": get_tier(r.score),
                "monthly_rsi": r.monthly_rsi,
                "entry": r.entry,
                "stop_loss": r.stop_loss,
                "tp1": r.tp1,
                "tp2": r.tp2,
                "tp3": r.tp3,
                "volume_24h": r.volume_24h,
                "breakdown": json.loads(r.breakdown) if r.breakdown else {},
            }
            for r in rows
        ],
    }


@app.get("/api/scan/high-score")
async def high_score_tokens():
    async with async_session() as session:
        query = select(ScanResult).where(ScanResult.score >= HIGH_SCORE_THRESHOLD).order_by(ScanResult.score.desc())
        res = await session.execute(query)
        rows = res.scalars().all()
    return {
        "threshold": HIGH_SCORE_THRESHOLD,
        "count": len(rows),
        "results": [
            {
                "symbol": r.symbol,
                "score": r.score,
                "tier": "high",
                "monthly_rsi": r.monthly_rsi,
                "entry": r.entry,
                "stop_loss": r.stop_loss,
                "tp1": r.tp1,
                "tp2": r.tp2,
                "tp3": r.tp3,
                "volume_24h": r.volume_24h,
            }
            for r in rows
        ],
    }


@app.get("/api/tokens/{symbol}")
async def token_detail(symbol: str):
    async with async_session() as session:
        res = await session.execute(select(ScanResult).where(ScanResult.symbol == symbol.upper()))
        row = res.scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"{symbol} not found in latest scan")
    return {
        "symbol": row.symbol,
        "score": row.score,
        "tier": get_tier(row.score),
        "monthly_rsi": row.monthly_rsi,
        "entry": row.entry,
        "stop_loss": row.stop_loss,
        "tp1": row.tp1,
        "tp2": row.tp2,
        "tp3": row.tp3,
        "volume_24h": row.volume_24h,
        "breakdown": json.loads(row.breakdown) if row.breakdown else {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@app.get("/api/export/csv")
async def export_csv():
    async with async_session() as session:
        res = await session.execute(select(ScanResult).order_by(ScanResult.score.desc()))
        rows = res.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["symbol", "score", "tier", "monthly_rsi", "entry", "stop_loss", "tp1", "tp2", "tp3", "volume_24h"])
    for r in rows:
        writer.writerow([r.symbol, r.score, get_tier(r.score), r.monthly_rsi, r.entry, r.stop_loss, r.tp1, r.tp2, r.tp3, r.volume_24h])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=alpha_hunter_scan.csv"},
    )


@app.on_event("startup")
async def on_startup():
    await init_db()
    asyncio.create_task(background_scan_loop())


if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
